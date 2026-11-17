#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Guest-root image construction; all inputs read-only, target serial verified."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys

sys.path.insert(0, '/inputs/project/src')
from runtime import REVISION, atomic_json, sha

INPUT = Path('/inputs')
PROJECT = INPUT / 'project'
SOURCE = PROJECT / 'src'
ARTIFACT = Path('/work/artifacts')
EXPORT = Path('/exports')
ARCH = Path('/work/arch')
OW = Path('/work/openwrt')
DATA = Path('/work/data')
PROTECTED = ['usr/bin/arch', 'sbin/mount_root', 'sbin/kmodloader', 'sbin/init', 'sbin/procd',
             'sbin/upgraded', 'sbin/factoryreset', 'sbin/sysupgrade', 'sbin/firstboot',
             'lib/preinit/80_mount_root', 'lib/preinit/79_move_config',
             'etc/init.d/umount', 'usr/libexec/validate_firmware_image',
             'etc/rc.button/reset', 'lib/upgrade/platform.sh', 'etc/preinit',
             'lib/functions/preinit.sh', 'etc/init.d/boot', 'etc/init.d/done',
             'etc/apk/repositories.d/frarchenstein.list', 'etc/init.d/frarchenstein-apps',
             'lib/apk/packages/ubox.alternatives',
             'usr/libexec/frarchenstein/arch-run', 'usr/libexec/frarchenstein/module-bridge',
             'usr/libexec/frarchenstein/shutdown-final', 'usr/libexec/frarchenstein/mount-root', 'usr/libexec/frarchenstein/shutdown-mounts']


def run(*args, **kwargs):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def capture(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def installed(root):
    result = {}
    for record in (root / 'lib/apk/db/installed').read_text().strip().split('\n\n'):
        p = {'files': []}
        directory = ''
        for line in record.splitlines():
            if line.startswith('P:'): p['name'] = line[2:]
            if line.startswith('V:'): p['version'] = line[2:]
            if line.startswith('F:'): directory = line[2:]
            if line.startswith('R:'): p['files'].append(str(Path(directory) / line[2:]))
        result[p['name']] = p
    return result


def put(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def initial_disks(manifest):
    matches=[]
    for dev in Path('/sys/class/block').iterdir():
        if (dev/'partition').exists(): continue
        for serial in (dev/'serial',dev/'device/serial'):
            if serial.exists() and serial.read_text().strip() == 'fwr-target':
                matches.append(Path('/dev')/dev.name); break
    if len(matches)!=1: raise ValueError('missing/ambiguous dedicated virtual target')
    path=matches[0]; sysfs=Path('/sys/class/block')/path.name
    capacity=int(capture('blockdev','--getsize64',path))
    minimum=(manifest['partitions'][-1]['start']+manifest['partitions'][-1]['sectors'])*512
    if not stat.S_ISBLK(path.stat().st_mode) or capacity!=manifest['target_bytes'] or capacity!=minimum or capacity//512>0xffffffff:
        raise ValueError('target capacity/identity mismatch')
    if int(capture('blockdev','--getss',path))!=512 or not manifest.get('format_authorized'):
        raise ValueError('unsupported sector size or missing format authorization')
    if capture('lsblk','-nr','-o','MOUNTPOINTS',path) or list((sysfs/'holders').iterdir()):
        raise ValueError('target mounted or held')
    builder=Path('/dev/disk/by-id/virtio-fwr-builder').resolve(strict=True)
    if path.resolve()==builder: raise ValueError('target is builder disk')
    identity = struct.unpack('<I', bytes.fromhex(manifest['mbr_id']))[0]
    table = f'label: dos\nlabel-id: 0x{identity:08x}\nunit: sectors\n'
    for i, part in enumerate(manifest['partitions']):
        table += f'start={part["start"]}, size={part["sectors"]}, type=83' + (', bootable' if i == 0 else '') + '\n'
    run('sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', path, input=table, text=True)
    run('udevadm', 'settle')
    parts=[]
    for i in range(1,4):
        found=[Path('/dev')/p.name for p in sysfs.glob(path.name+'*') if (p/'partition').exists() and int((p/'partition').read_text())==i]
        if len(found)!=1: raise ValueError('target partition discovery failed')
        parts.append(found[0])
    features = 'none,has_journal,ext_attr,resize_inode,dir_index,filetype,extent,sparse_super,large_file,uninit_bg,dir_nlink,extra_isize'
    for i, part in enumerate(parts):
        options = ['-O', features, '-b', '4096', '-I', '256'] if i == 0 else []
        run('mkfs.ext4', '-F', '-U', manifest['partitions'][i]['uuid'], *options, part)
    ARCH.mkdir()
    DATA.mkdir()
    run('mount', parts[1], ARCH)
    (ARCH / 'boot').mkdir()
    run('mount', parts[0], ARCH / 'boot')
    run('mount', parts[2], DATA)
    return path, parts


def package_arch():
    folder = Path('/work/arch-package')
    folder.mkdir()
    shutil.copyfile(PROJECT / 'recipes/arch/PKGBUILD', folder / 'PKGBUILD')
    names = ['runtime.py', 'module_bridge.py', 'shutdown-arch', 'publish_boot.py', 'guard-update', 'update.py', 'generations.py', 'apps.py', 'kernel_reviews.py', 'pacman', 'kernel-reviews.json', 'reviewed-kernels.json']
    for name in names:
        shutil.copyfile((PROJECT / 'configs' if name in ('reviewed-kernels.json', 'kernel-reviews.json') else SOURCE) / name, folder / name)
    for name in ['install', 'hook', 'config', 'preset']:
        shutil.copyfile(PROJECT / 'recipes/arch/mkinitcpio' / name, folder / ('mkinitcpio-' + name))
    names += ['mkinitcpio-' + n for n in ['install', 'hook', 'config', 'preset']]
    recipe = (folder / 'PKGBUILD').read_text()
    hashes = 'sha256sums=(' + ' '.join("'" + sha(folder / n) + "'" for n in names) + ')'
    recipe = re.sub(r"sha256sums=\([^\n]+\)", hashes, recipe)
    (folder / 'PKGBUILD').write_text(recipe)
    run('chown', '-R', 'builder:builder', folder)
    run('runuser', '-u', 'builder', '--', 'bash', '-c', 'cd /work/arch-package && makepkg --nodeps --noconfirm')
    package, = folder.glob('frarchenstein-wrt-arch-*.pkg.tar.zst')
    home = Path('/work/arch-signing')
    home.mkdir(mode=0o700)
    gpg = ['gpg', '--homedir', home, '--batch', '--pinentry-mode', 'loopback', '--passphrase', '']
    run(*gpg, '--quick-generate-key', 'Frarchenstein Prototype Build <builder@frarchenstein.invalid>', 'ed25519', 'sign', '0')
    fingerprint = next(line.split(':')[9] for line in capture(*gpg, '--with-colons', '--list-keys').splitlines() if line.startswith('fpr:'))
    run(*gpg, '--detach-sign', package)
    staging = ARCH / 'var/cache/frarchenstein-build'
    staging.mkdir(parents=True)
    public = staging / 'frarchenstein-build.asc'
    run(*gpg, '--armor', '--export', fingerprint, stdout=public.open('w'))
    run('arch-chroot', ARCH, 'pacman-key', '--add', '/var/cache/frarchenstein-build/frarchenstein-build.asc')
    run('arch-chroot', ARCH, 'pacman-key', '--lsign-key', fingerprint)
    config = ARCH / 'etc/pacman.conf'
    config.write_text(config.read_text().replace('LocalFileSigLevel = Optional', 'LocalFileSigLevel = Required'))
    shutil.copyfile(package, staging / package.name)
    shutil.copyfile(str(package) + '.sig', staging / (package.name + '.sig'))
    run('arch-chroot', ARCH, 'pacman', '-U', '--noconfirm', '/var/cache/frarchenstein-build/' + package.name)
    shutil.copyfile(package, EXPORT / package.name)
    shutil.copyfile(str(package) + '.sig', EXPORT / (package.name + '.sig'))
    shutil.copyfile(public, EXPORT / 'arch-package-signing-key.asc')
    (EXPORT / 'private').mkdir(mode=0o700,exist_ok=True)
    private = EXPORT / 'private/arch-signing-key.asc'
    with private.open('w') as stream:
        private.chmod(0o600)
        run(*gpg, '--armor', '--export-secret-keys', fingerprint, stdout=stream)
    shutil.copyfile(folder / 'PKGBUILD', EXPORT / 'Arch-PKGBUILD')
    (staging / package.name).unlink()
    (staging / (package.name + '.sig')).unlink()
    public.unlink()
    staging.rmdir()
    run('arch-chroot', ARCH, 'gpgconf', '--homedir', '/etc/pacman.d/gnupg', '--kill', 'all')
    run('gpgconf', '--homedir', home, '--kill', 'all')


def mkapk(name, version, files, dependencies, provides=''):
    package = Path('/work', f'{name}-{version}.apk')
    cmd = ['/work/apk-build', 'mkpkg', '--sign-key', '/work/artifacts/signing.pem', '--files', files,
           '--output', package, '--info', 'name:' + name, '--info', 'version:' + version,
           '--info', 'arch:x86_64', '--info', 'license:GPL-2.0-only', '--info', 'description:frarchenstein reviewed hybrid integration',
           '--info', 'depends:' + dependencies]
    if provides: cmd += ['--info', 'provides:' + provides]
    run(*cmd)
    run('/work/apk-build', '--keys-dir', ARTIFACT / 'keys', 'verify', package)
    return package


def openwrt():
    with gzip.open(Path('/downloads/openwrt.img.gz'), 'rb') as source, open('/work/upstream.img', 'xb') as dest:
        shutil.copyfileobj(source, dest)
    with open('/work/upstream.img','rb') as upstream:
        mbr=upstream.read(512)
        if mbr[510:]!=b'\x55\xaa': raise ValueError('unsupported upstream MBR')
        start=struct.unpack_from('<I',mbr,470)[0]*512
        upstream.seek(start)
        if upstream.read(4)!=b'hsqs': raise ValueError('upstream root is not SquashFS')
    run('unsquashfs', '-o', str(start), '-d', OW, '/work/upstream.img')
    original = installed(OW)
    (EXPORT / 'original-apk-world').write_bytes((OW / 'etc/apk/world').read_bytes())
    integration = Path('/work/integration-files')
    for directory in ['arch', 'persistent', 'overlay', 'rom', 'mnt', 'boot', 'run', 'etc/modules.d', 'etc/modules-boot.d']:
        (integration / directory).mkdir(parents=True, exist_ok=True)
    for name in ['module-bridge', 'mount-root', 'shutdown-mounts']:
        dest = integration / 'usr/libexec/frarchenstein' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, dest)
        dest.chmod(0o755)
    shutil.copyfile(ARTIFACT / 'arch-run', integration / 'usr/libexec/frarchenstein/arch-run')
    (integration / 'usr/libexec/frarchenstein/arch-run').chmod(0o755)
    shutil.copyfile(ARTIFACT / 'shutdown-final', integration / 'usr/libexec/frarchenstein/shutdown-final')
    (integration / 'usr/libexec/frarchenstein/shutdown-final').chmod(0o755)
    put(integration / 'etc/frarchenstein/integration', REVISION + '\n')
    put(integration / 'etc/apk/repositories.d/frarchenstein.list', '/arch/var/lib/frarchenstein/openwrt-repository/packages.adb\n')
    put(integration / 'etc/init.d/frarchenstein-apps', (SOURCE / 'frarchenstein-apps.init').read_text(), 0o755)
    (integration / 'etc/rc.d').mkdir(exist_ok=True)
    (integration / 'etc/rc.d/S99frarchenstein-apps').symlink_to('../init.d/frarchenstein-apps')
    (integration / 'etc/rc.d/K01frarchenstein-apps').symlink_to('../init.d/frarchenstein-apps')
    put(integration / 'usr/bin/arch', (SOURCE / 'arch').read_text(), 0o755)
    dependencies = 'ubox=2025.10.30~6f78fa49-r1001 fstools=2026.05.23~16718b6e-r1001 procd=2026.03.13~58eb263d-r1003 procd-ujail=2026.03.13~58eb263d-r1003 procd-seccomp=2026.03.13~58eb263d-r1003 base-files=1718.1004~981c978eff !kernel !kmod-button-hotplug'
    shutil.copytree(ARTIFACT / 'keys', integration / 'etc/apk/keys')
    put(integration / 'etc/uci-defaults/99-frarchenstein-network', (PROJECT / 'configs/network-defaults.sh').read_text(),0o755)
    core = mkapk('frarchenstein-wrt', '1.0-r8', integration, dependencies)
    capabilities = Path('/work/capability-files')
    capabilities.mkdir()
    provides, modules = [], set()
    for name, package in original.items():
        if not name.startswith('kmod-') or name == 'kmod-button-hotplug': continue
        provides.append(name + '=' + package['version'])
        for path in package['files']:
            if path.startswith(('etc/modules.d/', 'etc/modules-boot.d/')):
                source, dest = OW / path, capabilities / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest, follow_symlinks=False)
                if not source.is_symlink():
                    for line in source.read_text().splitlines():
                        if line.strip() and not line.lstrip().startswith('#'):
                            modules.add(line.split()[0])
            if path.endswith('.ko'):
                modules.add(Path(path).name[:-3])
    module_package = mkapk('frarchenstein-arch-kmods', '1.0-r8', capabilities, 'frarchenstein-wrt=1.0-r8', ' '.join(provides))
    for path in (ARTIFACT / 'keys').iterdir():
        shutil.copyfile(path, OW / 'etc/apk/keys' / path.name)
    local = OW / 'tmp/hybrid-packages'
    local.mkdir()
    packages = []
    for owner in ['ubox', 'fstools', 'procd', 'procd-ujail', 'procd-seccomp', 'base-files']:
        matches = [p for p in (ARTIFACT / 'packages').glob(owner + '-*.apk')
                   if re.match(re.escape(owner) + r'-[0-9]', p.name)]
        if len(matches) != 1: raise ValueError('missing or ambiguous custom owner: ' + owner)
        packages.append(matches[0])
    packages += [core, module_package]
    for package in packages:
        shutil.copyfile(package, local / package.name)
    # Install through the same signed repository identity used for later fixes.
    # APK 3.0.5 local-file identities differ from mkndx identities for v3 files;
    # mixing them makes normal fixes silently skip the installed core packages.
    run('/work/apk-build', 'mkndx', '--hash', 'sha256-160', '--sign-key', '/work/artifacts/signing.pem',
        '--keys-dir', ARTIFACT / 'keys', '--output', local / 'packages.adb',
        *sorted(local.glob('*.apk')))

    # world is the package manager's declared user intent, not its installed DB.
    # Removing the native kernel/button intent lets the atomic APK transaction
    # replace kmod dependencies with the reviewed provider and remove old .ko's.
    world = (OW / 'etc/apk/world').read_text().splitlines()
    keep = [s for s in world if re.split('[=<>~@]', s)[0] not in ['kernel', 'kmod-button-hotplug', 'grub2', 'grub2-bios-setup']]
    (OW / 'etc/apk/world').write_text('\n'.join(keep) + '\n')
    for api in ['dev', 'proc', 'sys']:
        run('mount', '--rbind', '/' + api, OW / api)
        run('mount', '--make-rslave', OW / api)
    try:
        apk = ['chroot', OW, '/usr/bin/apk', '--repositories-file', '/dev/null', '--network=no',
               '--repository', '/tmp/hybrid-packages/packages.adb']
        for package in packages:
            run(*apk, 'verify', '/tmp/hybrid-packages/' + package.name)
        run(*apk, '--preserve-env', 'add', '--upgrade', *[re.match(r'(.+?)-[0-9]', p.name)[1] for p in packages],
            env={'PATH': '/usr/bin:/usr/sbin:/sbin:/bin', 'HOME': '/root', 'IPKG_INSTROOT': '/'})
        put(OW / 'tmp/resolv.conf', Path('/run/systemd/resolve/resolv.conf').read_text())
        repo_config = OW / 'etc/apk/repositories.d/frarchenstein.list'
        final_repo_config = repo_config.read_text()
        try:
            repo_config.write_text('/tmp/hybrid-packages/packages.adb\n')
            run('chroot', OW, '/usr/bin/apk', 'update')
            run('chroot', OW, '/usr/bin/apk', '--preserve-env', 'add', 'luci-ssl',
                env={'PATH':'/usr/bin:/usr/sbin:/sbin:/bin','HOME':'/root','IPKG_INSTROOT':'/'})
        finally:
            repo_config.write_text(final_repo_config)
        final = installed(OW)
        if 'kernel' in final or any(p.startswith('kmod-') for p in final):
            raise ValueError('native kernel/kmod package remains installed')
        if list((OW / 'lib/modules').glob('**/*.ko')):
            raise ValueError('native OpenWrt module payload remains')
        for owner in ['frarchenstein-wrt', 'frarchenstein-arch-kmods', 'ubox', 'fstools', 'procd', 'base-files']:
            if owner not in final:
                raise ValueError('missing required package ' + owner)
        preinit_path = re.findall(r'^export PATH="([^"]+)"$',
                                  (OW / 'etc/preinit').read_text(), re.MULTILINE)
        if len(preinit_path) != 1 or '%' in preinit_path[0]:
            raise ValueError('unresolved preinit command-path template')
        run('chroot', OW, '/bin/sh', '-c',
            'for cmd in mount_root cat sleep lock rm mount pivot_root; do command -v "$cmd" || exit 1; done',
            env={'PATH': preinit_path[0]})
        (EXPORT / 'openwrt-packages.json').write_text(json.dumps(final, indent=2) + '\n')
    finally:
        for api in ['sys', 'proc', 'dev']:
            run('umount', '--recursive', OW / api)
    shutil.rmtree(local)
    shutil.copyfile(core, EXPORT / core.name)
    shutil.copyfile(module_package, EXPORT / module_package.name)
    repository = EXPORT / 'openwrt-repository'
    repository.mkdir()
    for package in packages:
        shutil.copyfile(package, repository / package.name)
    run('/work/apk-build', 'mkndx', '--hash', 'sha256-160', '--sign-key', '/work/artifacts/signing.pem',
        '--keys-dir', ARTIFACT / 'keys', '--output', repository / 'packages.adb',
        *sorted(repository.glob('*.apk')))
    run('/work/apk-build', '--keys-dir', ARTIFACT / 'keys', 'verify', repository / 'packages.adb')
    atomic_json(EXPORT / 'openwrt-repository.json', {'identity_hash': 'sha256-160',
                'files': {p.name: sha(p) for p in repository.iterdir()}})

    return modules


def main():
    os.umask(0o022)
    if os.geteuid() != 0 or not Path('/inputs/install.json').is_file():
        raise ValueError('guest worker requires its verified input export')
    manifest = json.loads((INPUT / 'install.json').read_text())
    disk, parts = initial_disks(manifest)
    print('PHASE: build and install signed OpenWrt integration', flush=True)
    modules = openwrt()
    (EXPORT / 'private').mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(ARTIFACT / 'signing.pem', EXPORT / 'private/openwrt-signing.pem')
    (EXPORT / 'private/openwrt-signing.pem').chmod(0o600)
    print('PHASE: bootstrap fresh signed Arch root', flush=True)
    snapshot = INPUT / 'repository-snapshot'
    config_args = []
    if snapshot.exists():
        config = Path('/work/pacman-snapshot.conf')
        config.write_text(Path('/etc/pacman.conf').read_text().replace('Include = /etc/pacman.d/mirrorlist', 'Server = https://archive.archlinux.org/repos/' + snapshot.read_text().strip() + '/$repo/os/$arch'))
        config_args = ['-C', config]
    run('pacstrap', '-K', '-c', *config_args, ARCH, 'base', 'linux', 'linux-firmware', 'python', 'kmod', 'mkinitcpio', 'syslinux', 'e2fsprogs', 'squashfs-tools')
    run('arch-chroot', ARCH, 'pacman', '-Q', stdout=(EXPORT / 'arch-packages.txt').open('w'))
    (ARCH / 'etc/frarchenstein').mkdir(exist_ok=True)
    atomic_json(ARCH / 'etc/frarchenstein/install.json', manifest)
    (ARCH / 'boot/frarchenstein').mkdir(exist_ok=True)
    atomic_json(ARCH / 'boot/frarchenstein/install.json', manifest)
    atomic_json(ARCH / 'boot/frarchenstein/interfaces.json', manifest['interfaces'])
    package_arch()
    shutil.copytree(EXPORT / 'openwrt-repository', ARCH / 'var/lib/frarchenstein/openwrt-repository')
    for path in ['srv/router-apps', 'run', 'proc', 'sys', 'dev', 'tmp']:
        (ARCH / path).mkdir(parents=True, exist_ok=True)
    resolver = ARCH / 'etc/resolv.conf'
    resolver.unlink(missing_ok=True)
    resolver.symlink_to('/tmp/resolv.conf')
    put(ARCH / 'etc/fstab', '# Mounts are managed by the frarchenstein initramfs and OpenWrt preinit.\n')
    releases = [p.name for p in (ARCH / 'usr/lib/modules').iterdir() if (p / 'vmlinuz').is_file()]
    expected = json.loads((INPUT / 'kernel-evidence.json').read_text())
    if releases != [expected['release']]:
        raise ValueError('unreviewed Arch kernel set: ' + str(releases))
    release = releases[0]
    kernel = ARCH / 'usr/lib/modules' / release / 'vmlinuz'
    if sha(kernel) != expected['vmlinuz_sha256']:
        raise ValueError('kernel differs from the audited module/config mapping')
    run('depmod', '-b', ARCH, release)
    module_checks = {}
    builtins = {'nfnetlink': 'CONFIG_NETFILTER_NETLINK=y', 'libcrc32c': 'crc32',
                'dma_shared_buffer': 'CONFIG_DMA_SHARED_BUFFER=y'}
    aliases = {'crc32c_generic': 'crc32c-cryptoapi', 'nf_flow_table_hw': 'nf_flow_table'}
    for name in sorted(modules):
        if name.replace('-', '_') == 'button_hotplug':
            raise ValueError('button-hotplug startup survived removal')
        key = name.replace('-', '_')
        if key in builtins:
            module_checks[name] = {'builtin': builtins[key]}
        else:
            actual = aliases.get(key, name)
            module_checks[name] = {'actual': actual, 'resolution': capture('modprobe', '-d', ARCH, '-S', release, '-C', '/dev/null', '--show-depends', actual)}
    policy = {'kernel_release': release, 'excluded': ['button_hotplug'],
              'builtin_equivalents': builtins, 'module_aliases': aliases}
    atomic_json(ARCH / 'usr/lib/frarchenstein/kernel-policy.json', policy)
    atomic_json(EXPORT / 'module-checks.json', module_checks)
    # Public system directories and the overlay root must be traversable by daemons.
    for root in (ARCH, OW):
        for name in ('', 'etc', 'usr', 'lib', 'bin', 'sbin', 'usr/bin', 'usr/lib'):
            if (root / name).stat().st_mode & 0o555 != 0o555:
                raise ValueError('system directory lacks public read/traverse permissions: ' + name)
    print('PHASE: seal OpenWrt generation', flush=True)
    lower = Path('/work/rootfs.squashfs')
    run('mksquashfs', OW, lower, '-noappend', '-comp', 'xz', '-b', '262144', '-processors', '2')
    generation = '25.12.6-' + sha(lower)
    slot = ARCH / 'boot/openwrt/slots' / generation
    slot.mkdir(parents=True)
    shutil.copyfile(lower, slot / 'rootfs.squashfs')
    record = {'generation': generation, 'sha256': sha(lower), 'integration': REVISION,
              'protected_paths': {'/' + p: sha(OW / p) for p in PROTECTED},
              'protected_links': {str('/' + p): os.readlink(OW / p)
                  for p in ['sbin/insmod', 'sbin/modprobe', 'sbin/rmmod', 'sbin/lsmod', 'sbin/modinfo', 'sbin/jffs2reset', 'sbin/jffs2mark']}}
    atomic_json(slot / 'manifest.json', record)
    (ARCH / 'boot/openwrt/current').symlink_to('slots/' + generation)
    gen = DATA / 'generations' / generation
    (gen / 'upper').mkdir(parents=True)
    (gen / 'upper').chmod(0o755)
    (gen / 'work').mkdir()
    (DATA / 'shared/apps').mkdir(parents=True)
    atomic_json(gen / 'generation.json', {'generation': generation, 'integration': REVISION})
    atomic_json(gen / 'initialization.json', {'generation': generation, 'state': 'prepared'})
    stage = ARCH / 'boot/frarchenstein-build'
    stage.mkdir()
    shutil.copyfile(kernel, stage / 'vmlinuz-linux')
    atomic_json(stage / 'kernel-policy.json', policy)
    print('PHASE: generate and publish checked initramfs', flush=True)
    run('arch-chroot', ARCH, 'mkinitcpio', '-p', 'frarchenstein')
    run('arch-chroot', ARCH, 'python3', '/usr/lib/frarchenstein/publish_boot.py')
    boot = ARCH / 'boot'
    extlinux = boot / 'syslinux'
    extlinux.mkdir()
    put(extlinux / 'extlinux.conf', 'DEFAULT frarchenstein\nPROMPT 0\nTIMEOUT 30\nLABEL frarchenstein\n  LINUX /arch/current/vmlinuz-linux\n  INITRD /arch/current/initramfs-linux.img\n  APPEND init=/sbin/init console=tty0 console=ttyS0,115200 net.ifnames=0 firmware_class.path=/arch/usr/lib/firmware\n')
    for name in ['ldlinux.c32', 'libutil.c32']:
        shutil.copyfile(ARCH / 'usr/lib/syslinux/bios' / name, extlinux / name)
    run('extlinux', '--install', extlinux)
    with open(ARCH / 'usr/lib/syslinux/bios/mbr.bin', 'rb') as stream:
        code = stream.read(440)
    fd = os.open(disk, os.O_RDWR | os.O_NOFOLLOW)
    try:
        if os.pwrite(fd, code, 0) != 440:
            raise OSError('short MBR code write')
        os.fsync(fd)
    finally:
        os.close(fd)
    # Validate files and package ownership before accepting this build artifact.
    run('arch-chroot', ARCH, 'pacman', '-Qk', 'frarchenstein-wrt-arch')
    for path in PROTECTED:
        owners = [name for name, p in installed(OW).items() if path in p['files']]
        if len(owners) != 1:
            raise ValueError('protected path ownership is missing/ambiguous: ' + path + ' ' + str(owners))
    if sha(slot / 'rootfs.squashfs') != record['sha256'] or any((gen / 'upper').iterdir()):
        raise ValueError('generation hash or clean upper check failed')
    run('sfdisk', '--json', disk, stdout=(EXPORT / 'partition-table.json').open('w'))
    run('df', '-B1', ARCH, boot, DATA, stdout=(EXPORT / 'filesystem-headroom.txt').open('w'))
    atomic_json(EXPORT / 'generation.json', record)
    atomic_json(EXPORT / 'installation.json', manifest)
    os.sync()
    run('umount', DATA)
    run('umount', boot)
    run('umount', ARCH)
    for part in parts:
        run('e2fsck', '-fn', part)
    atomic_json(EXPORT / 'acceptance.json', {'success': True, 'milestone': 2,
                'generation': generation, 'kernel_release': release,
                'signed_core_and_integration': True, 'protected_package_ownership': True,
                'data_fills_target': True, 'first_boot_resize': False, 'empty_upper_work': True, 'filesystems_checked_unmounted': True,
                'hybrid_boot_tested': False,
                'limitations': ['Generic installer construction result; runtime acceptance recorded separately']})


if __name__ == '__main__':
    main()
