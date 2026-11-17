#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Media validation, early-root setup and overlay handoff for the appliance."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import time
import uuid

REVISION = 'frarchenstein-wrt-1'


def run(*args, **kw):
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE, **kw).stdout.strip()


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.pending-write')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def device(identifier):
    if str(uuid.UUID(identifier)) != identifier:
        raise ValueError('noncanonical filesystem UUID')
    found = run('/usr/bin/blkid', '-c', '/dev/null', '-t', 'UUID=' + identifier, '-o', 'device').splitlines()
    if len(found) != 1:
        raise ValueError('missing or duplicate filesystem UUID')
    path = Path(found[0]).resolve(strict=True)
    if not stat.S_ISBLK(path.stat().st_mode):
        raise ValueError('filesystem is not on a block device')
    return path


def number(path):
    return int(Path(path).read_text().strip())


def validate_mbr(manifest, mbr, capacity, kernel_geometry):
    """Validate the installed partition layout without modifying it."""
    if len(manifest['partitions']) != 3:
        raise ValueError('expected exactly three partitions')
    if len(mbr) != 512 or mbr[510:] != b'\x55\xaa' or any(mbr[494:510]):
        raise ValueError('unsupported MBR signature or extra partition')
    if mbr[440:444].hex() != manifest['mbr_id']:
        raise ValueError('MBR identity mismatch')
    previous_end = 2048
    for i, expected in enumerate(manifest['partitions']):
        offset = 446 + 16 * i
        start, size = struct.unpack_from('<II', mbr, offset + 8)
        if mbr[offset + 4] != 0x83 or start != expected['start'] or start < previous_end or not size:
            raise ValueError('partition type/start/overlap mismatch')
        if kernel_geometry[i] != (start, size):
            raise ValueError('partition table disagrees with kernel; reboot required')
        if size != expected['sectors']:
            raise ValueError('installed partition size changed')
        previous_end = start + size
    if previous_end != capacity or capacity * 512 != manifest['target_bytes'] or capacity > 0xffffffff:
        raise ValueError('installed layout must fill the recorded target capacity')


def geometry(manifest):
    if manifest['format'] != 2 or manifest['integration'] != REVISION:
        raise ValueError('unsupported installation manifest')
    parts = [device(p['uuid']) for p in manifest['partitions']]
    sys = [Path('/sys/class/block', p.name).resolve(strict=True) for p in parts]
    parent = sys[0].parent
    if any(p.parent != parent or number(p / 'partition') != i + 1 for i, p in enumerate(sys)):
        raise ValueError('partition parent or number mismatch')
    if number(parent / 'queue/logical_block_size') != 512:
        raise ValueError('only 512-byte-sector media is supported')
    capacity = number(parent / 'size')
    disk = Path('/dev', parent.name)
    fd = os.open(disk, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISBLK(os.fstat(fd).st_mode):
            raise ValueError('parent is not a block device')
        mbr = os.pread(fd, 512, 0)
    finally:
        os.close(fd)
    validate_mbr(manifest, mbr, capacity,
                 [(number(p / 'start'), number(p / 'size')) for p in sys])
    return disk, parts, mbr, capacity


def checked_generation(boot):
    selector = boot / 'openwrt/current'
    target = os.readlink(selector)
    if not re.fullmatch(r'slots/25\.12\.6-[0-9a-f]{64}', target):
        raise ValueError('invalid generation selector')
    slot = boot / 'openwrt' / target
    if slot.resolve(strict=True) != slot or slot.is_symlink():
        raise ValueError('generation path contains symlinks')
    manifest = read_json(slot / 'manifest.json')
    gen = target.split('/')[1]
    digest = gen.split('-', 1)[1]
    rootfs = slot / 'rootfs.squashfs'
    if rootfs.is_symlink() or manifest['generation'] != gen or manifest['sha256'] != digest:
        raise ValueError('generation manifest identity mismatch')
    if manifest['integration'] != REVISION or sha(rootfs) != digest:
        raise ValueError('generation checksum or integration mismatch')
    return gen, rootfs, manifest


def name_interfaces(boot, sysnet=Path('/sys/class/net')):
    """Optional explicit MAC-to-name mapping, shared across OpenWrt generations."""
    config = Path(boot) / 'frarchenstein/interfaces.json'
    if not config.exists():
        return
    if config.is_symlink():
        raise ValueError('interface map must be a regular file')
    mapping = read_json(config)
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('invalid interface map')
    addresses, targets = set(), set()
    devices = {p.name: (p / 'address').read_text().strip() for p in sysnet.iterdir()}
    changes = []
    for name, mac in mapping.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,14}', name) or name == 'lo' or name.startswith('rpi_tmp'):
            raise ValueError('invalid interface name')
        if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac) or int(mac[:2], 16) & 1 or mac == '00:00:00:00:00:00':
            raise ValueError('invalid interface MAC address')
        if mac in addresses:
            raise ValueError('duplicate interface MAC address')
        addresses.add(mac); targets.add(name)
        matches = [dev for dev, address in devices.items() if address == mac]
        if len(matches) != 1:
            raise ValueError('missing or ambiguous interface identity: ' + name)
        changes.append((matches[0], name))
    sources = {old for old, new in changes}
    if any(new in devices and new not in sources for old, new in changes):
        raise ValueError('interface target name belongs to another device')
    if any('rpi_tmp' + str(i) in devices for i in range(len(changes))):
        raise ValueError('temporary interface name is occupied')
    # Two phases also support a deliberate swap of existing names.
    for i, (old, new) in enumerate(changes):
        run('/usr/bin/ip', 'link', 'set', 'dev', old, 'down')
        run('/usr/bin/ip', 'link', 'set', 'dev', old, 'name', 'rpi_tmp' + str(i))
    for i, (old, new) in enumerate(changes):
        run('/usr/bin/ip', 'link', 'set', 'dev', 'rpi_tmp' + str(i), 'name', new)


def early(root):
    manifest = read_json('/etc/frarchenstein/install.json')
    deadline = time.monotonic() + 60
    while True:
        try:
            disk, parts, _, _ = geometry(manifest)
            break
        except (OSError, ValueError, subprocess.CalledProcessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    root = Path(root)
    root.mkdir(exist_ok=True)
    Path('/frarchenstein-boot').mkdir()
    run('/usr/bin/mount', '-t', 'ext4', '-o', 'rw', parts[0], '/frarchenstein-boot')
    boot = Path('/frarchenstein-boot')
    if read_json(boot / 'frarchenstein/install.json') != manifest:
        raise ValueError('boot manifest differs from the initramfs identity')
    update_state = boot / 'frarchenstein/update.json'
    if update_state.exists() and read_json(update_state)['state'] != 'ready':
        raise ValueError('Arch update requires external repair before boot')
    gen, lower, selection = checked_generation(boot)
    name_interfaces(boot)
    loop = run('/usr/bin/losetup', '--find', '--show', '--read-only', lower)
    run('/usr/bin/mount', '-t', 'squashfs', '-o', 'ro', loop, root)
    for name in ['boot', 'arch', 'persistent', 'rom', 'overlay', 'mnt', 'proc', 'sys', 'dev', 'tmp', 'run']:
        if not (root / name).is_dir() or (root / name).is_symlink():
            raise ValueError('missing or symlinked root mountpoint: ' + name)
    run('/usr/bin/mount', '--move', boot, root / 'boot')
    run('/usr/bin/mount', '-t', 'ext4', '-o', 'rw', parts[1], root / 'arch')
    run('/usr/bin/mount', '-t', 'tmpfs', '-o', 'mode=1777', 'tmpfs', root / 'tmp')
    (root / 'tmp/run').mkdir()
    run('/usr/bin/mount', '--bind', '/run', root / 'tmp/run')
    if not Path('/sys/fs/cgroup/cgroup.controllers').exists():
        run('/usr/bin/mount', '-t', 'cgroup2', 'cgroup2', '/sys/fs/cgroup')
    # Coldplug must retain this devtmpfs and its shared API submounts. Prepare
    # them before copying the mount tree into Arch so both sides see the same
    # PTYs and shared-memory filesystem throughout normal procd startup.
    for path, kind, options in [('/dev/pts', 'devpts', 'mode=0620,ptmxmode=0666'),
                                ('/dev/shm', 'tmpfs', 'mode=1777,nosuid,nodev')]:
        Path(path).mkdir(exist_ok=True)
        if not Path(path).is_mount():
            run('/usr/bin/mount', '-t', kind, '-o', options, kind, path)
    state = Path('/run/frarchenstein')
    state.mkdir(mode=0o700, exist_ok=True)
    (state / 'openwrt-etc').mkdir(exist_ok=True)
    run('/usr/bin/mount', '--bind', root / 'etc', state / 'openwrt-etc')
    run('/usr/bin/mount', '-o', 'remount,bind,ro', state / 'openwrt-etc')
    for api in ['dev', 'proc', 'sys', 'run']:
        run('/usr/bin/mount', '--rbind', '/' + api, root / 'arch' / api)
        run('/usr/bin/mount', '--make-rslave', root / 'arch' / api)
    for api in ['tmp', 'boot']:
        run('/usr/bin/mount', '--bind', root / api, root / 'arch' / api)
    st = (root / 'arch').stat()
    (state / 'arch-device').write_text(f'{os.major(st.st_dev)}:{os.minor(st.st_dev)}\n')
    policy_file = root / 'arch/usr/lib/frarchenstein/kernel-policies' / (os.uname().release + '.json')
    policy = read_json(policy_file if policy_file.exists() else root / 'arch/usr/lib/frarchenstein/kernel-policy.json')
    if policy['kernel_release'] != os.uname().release:
        raise ValueError('Arch kernel policy does not match running kernel')
    embedded_policy = Path('/etc/frarchenstein/kernel-policy.json')
    if embedded_policy.exists() and read_json(embedded_policy) != policy:
        raise ValueError('boot and maintenance kernel policies differ')
    integration_root = root / 'arch'
    integration_files = read_json(integration_root / 'usr/share/frarchenstein/integration-files.json')
    for name, expected in integration_files.items():
        path = integration_root / name
        if 'link' in expected:
            if not path.is_symlink() or os.readlink(path) != expected['link']:
                raise ValueError('Arch integration link changed: ' + name)
        elif path.is_symlink() or sha(path) != expected['sha256']:
            raise ValueError('Arch integration path changed: ' + name)
    for path, digest in selection['protected_paths'].items():
        if sha(root / path.lstrip('/')) != digest:
            raise ValueError('lower integration path changed: ' + path)
    for path, target in selection['protected_links'].items():
        if os.readlink(root / path.lstrip('/')) != target:
            raise ValueError('lower integration link changed: ' + path)
    atomic_json(state / 'install.json', manifest)
    atomic_json(state / 'selection.json', selection)
    (state / 'prepared').write_text(REVISION + '\n')


def ext4_size(fd, expected_uuid):
    sb = os.pread(fd, 1024, 1024)
    if len(sb) != 1024 or sb[56:58] != b'\x53\xef' or str(uuid.UUID(bytes=sb[104:120])) != expected_uuid:
        raise ValueError('data ext4 superblock/UUID mismatch')
    blocksize = 1024 << struct.unpack_from('<I', sb, 24)[0]
    if blocksize not in [1024, 2048, 4096]:
        raise ValueError('unsupported ext4 block size')
    blocks = struct.unpack_from('<I', sb, 4)[0]
    if struct.unpack_from('<I', sb, 96)[0] & 0x80:
        blocks |= struct.unpack_from('<I', sb, 0x150)[0] << 32
    return blocks * blocksize, blocksize


def data_device(manifest):
    _, parts, _, _ = geometry(manifest)
    fd = os.open(parts[2], os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size, blocksize = ext4_size(fd, manifest['partitions'][2]['uuid'])
        expected = manifest['partitions'][2]['sectors'] * 512
        if size != expected // blocksize * blocksize:
            raise ValueError('data filesystem does not fill its installed partition')
    finally:
        os.close(fd)
    return parts[2]


def check_protected_upper(upper, selection):
    """Allow byte-identical package reinstalls, reject incompatible shadows."""
    upper = Path(upper)
    for name in set(selection['protected_paths']) | set(selection['protected_links']):
        path = upper / name.lstrip('/')
        if path.exists() or path.is_symlink():
            if name in selection['protected_links']:
                valid = path.is_symlink() and os.readlink(path) == selection['protected_links'][name]
            else:
                valid = path.is_file() and not path.is_symlink() and sha(path) == selection['protected_paths'][name]
            if not valid:
                raise ValueError('upper shadows protected integration: ' + name)
        for parent in path.parents:
            if parent == upper.parent:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError('upper ancestor shadows protected integration')
            if parent.exists() and 'trusted.overlay.opaque' in os.listxattr(parent):
                raise ValueError('opaque upper ancestor shadows protected integration')


def data():
    root = Path('/proc/1/root')
    manifest = read_json('/run/frarchenstein/install.json')
    selection = read_json('/run/frarchenstein/selection.json')
    gen, _, fresh = checked_generation(Path('/boot'))
    if fresh != selection:
        raise ValueError('selector changed after early handoff')
    device_path = data_device(manifest)
    run('/usr/bin/mount', '-t', 'ext4', '-o', 'rw', device_path, root / 'persistent')
    directory = root / 'persistent/generations' / gen
    # No automatic filesystem creation or cleanup of other generations.
    if directory.parent.is_symlink() or directory.is_symlink() or read_json(directory / 'generation.json') != {'generation': gen, 'integration': REVISION}:
        raise ValueError('data generation identity mismatch')
    for leaf in ['upper', 'work']:
        path = directory / leaf
        if not path.is_dir() or path.is_symlink():
            raise ValueError('invalid overlay directory')
    if (directory / 'upper').stat().st_mode & 0o555 != 0o555:
        raise ValueError('overlay root lacks public read/traverse permissions')
    check_protected_upper(directory / 'upper', selection)
    state = read_json(directory / 'initialization.json')
    if state.get('generation') != gen or state.get('state') not in ['prepared', 'complete']:
        raise ValueError('invalid generation initialization state')
    run('/usr/bin/mount', '--bind', directory, root / 'overlay')
    run('/usr/bin/mount', '--bind', root / 'persistent/shared/apps', '/srv/router-apps')


def complete():
    selection = read_json('/run/frarchenstein/selection.json')
    path = Path('/proc/1/root/overlay/initialization.json')
    state = read_json(path)
    if state.get('generation') != selection['generation'] or state.get('state') not in ['prepared', 'complete']:
        raise ValueError('generation completion identity mismatch')
    if state['state'] != 'complete':
        state['state'] = 'complete'
        atomic_json(path, state)


if __name__ == '__main__':
    import sys
    try:
        if sys.argv[1:] == ['data']:
            data()
        elif sys.argv[1:] == ['complete']:
            complete()
        elif len(sys.argv) == 3 and sys.argv[1] == 'early':
            early(sys.argv[2])
        else:
            raise ValueError('expected early ROOT or data')
    except Exception as error:
        print('frarchenstein: ' + str(error), file=sys.stderr)
        sys.exit(1)
