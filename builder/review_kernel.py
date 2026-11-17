#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Guest-only kernel review stages; provisional data never approves a release."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse

sys.path.insert(0, '/usr/lib/frarchenstein')
from runtime import REVISION, atomic_json, read_json, sha
from update import check_environment
from kernel_reviews import records

ROOT = Path('/proc/1/root')
WORK = Path('/var/lib/frarchenstein/kernel-validation')
STATE = WORK / 'state.json'
REVIEW_URL = 'https://127.0.0.1:9/kernel-validation.json'
REQUIRED_CONFIG = {
    '64BIT': 'y', 'X86_64': 'y', 'BLK_DEV_INITRD': 'y', 'DEVTMPFS': 'y',
    'TMPFS': 'y', 'OVERLAY_FS': 'ym', 'SQUASHFS': 'ym', 'SQUASHFS_XZ': 'y',
    'EXT4_FS': 'ym', 'BLK_DEV_LOOP': 'ym', 'NAMESPACES': 'y', 'NET_NS': 'y',
    'CGROUPS': 'y', 'CGROUP_PIDS': 'y', 'NETFILTER': 'y', 'NF_TABLES': 'ym',
    'NF_CONNTRACK': 'ym', 'NF_NAT': 'ym', 'VETH': 'ym', 'BRIDGE': 'ym',
    'INET': 'y', 'IPV6': 'ym',
}


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def snapshot_network():
    return {name: sha(ROOT / 'etc/config' / name) for name in ['network', 'system', 'firewall', 'dhcp']}


def module_checks(root, release, record):
    run('depmod', '-b', root, release)
    checks = {}
    for name in record['startup_modules']:
        key = name.replace('-', '_')
        actual = record['policy']['module_aliases'].get(key, name)
        equivalent = record['policy']['builtin_equivalents'].get(key)
        if equivalent and equivalent.startswith('CONFIG_'):
            checks[name] = {'config': equivalent}  # checked against the booted kernel
            continue
        if equivalent:
            actual = equivalent
        checks[name] = output('modprobe', '-d', root, '-S', release, '-C', '/dev/null', '--show-depends', actual)
    return checks


def prepare(request):
    require(not STATE.exists(), 'review state already exists; use a fresh disposable installation')
    WORK.mkdir(parents=True, exist_ok=True)
    require(not read_json('/etc/frarchenstein/apps.json') if Path('/etc/frarchenstein/apps.json').exists() else True,
            'kernel review requires a clean base with no registered applications')
    for name, digest in request['runtime_sha256'].items():
        require(sha(Path('/usr/lib/frarchenstein') / name) == digest, 'baseline integration differs from runner source: ' + name)
    before = os.uname().release
    bundled = records(read_json('/usr/share/frarchenstein/reviewed-kernels.json'))
    require(before in bundled, 'baseline kernel has no review')
    policy = request.get('policy') or {key: bundled[before][key] for key in ['policy', 'startup_modules']}
    snapshot = request.get('snapshot')
    if snapshot:
        config = Path('/etc/pacman.conf')
        text = config.read_text()
        server = 'Server = https://archive.archlinux.org/repos/' + snapshot + '/$repo/os/$arch'
        text = re.sub(r'^Include\s*=\s*/etc/pacman.d/mirrorlist\s*$', server, text, flags=re.M)
        text = re.sub(r'^Server\s*=.*$', server, text, flags=re.M)
        config.write_text(text)
    # Downloads authenticate official packages but do not change installed files.
    run('pacman', '-Syyuw', '--noconfirm', 'linux')
    plan = output('/usr/bin/pacman', '-Sup', '--print-format', '%n\t%v\t%l', 'linux')
    lines = [line.split('\t') for line in plan.splitlines() if line.startswith('linux\t')]
    require(len(lines) == 1 and len(lines[0]) == 3, 'ambiguous candidate kernel')
    _, version, location = lines[0]
    if request.get('expected_version'):
        require(version == request['expected_version'], 'candidate differs from requested package version')
    filename = Path(urllib.parse.unquote(urllib.parse.urlsplit(location).path)).name
    packages = [Path(directory) / filename for directory in output('pacman-conf', 'CacheDir').splitlines()]
    archive = next((p for p in packages if p.is_file()), None)
    require(archive is not None, 'candidate package missing from authenticated download cache')
    signature = Path(str(archive) + '.sig')
    require(signature.is_file(), 'candidate lacks its official package signature')
    run('pacman-key', '--verify', signature, archive)
    require(output('/usr/bin/pacman', '-Qp', archive).split() == ['linux', version], 'candidate package identity changed')
    members = output('bsdtar', '-tf', archive).splitlines()
    require(all(not Path(m).is_absolute() and '..' not in Path(m).parts for m in members), 'unsafe package member')
    kernels = [m for m in members if re.fullmatch(r'(?:\./)?usr/lib/modules/[^/]+/vmlinuz', m)]
    require(len(kernels) == 1, 'candidate must contain one kernel')
    release = kernels[0].removeprefix('./').split('/')[3]
    candidate = WORK / 'candidate'
    candidate.mkdir()
    run('bsdtar', '-xf', archive, '-C', candidate)
    # The linux package contains usr/lib; Arch's filesystem package supplies
    # /lib -> usr/lib in an installed root. Recreate that view for depmod.
    require(not (candidate / 'lib').exists(), 'unexpected lib path in kernel archive')
    (candidate / 'lib').symlink_to('usr/lib')
    record = dict(policy, integration=REVISION, package_version=version,
                  vmlinuz_sha256=sha(candidate / kernels[0]),
                  review='Automated QEMU validation; physical hardware not validated.')
    records({release: record})
    modules = module_checks(candidate, release, record)
    gen = read_json('/run/frarchenstein/selection.json')['generation']
    marker = request['run_id']
    (ROOT / 'etc/fwr-kernel-review-id').write_text(marker)
    shared = ROOT / 'persistent/shared/kernel-review-id'
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(marker)
    state = {'baseline': before, 'release': release, 'record': record,
             'package_sha256': sha(archive), 'packages_planned': plan.splitlines(),
             'module_resolution': modules, 'generation_a': gen, 'marker': marker,
             'network': snapshot_network(), 'checks': {'official_package_signature': True,
                                                       'candidate_module_resolution': True}}
    atomic_json(STATE, state)
    # This is a provisional local review in THIS DISPOSABLE VM ONLY. No
    # production bypass flag or unreviewed entry is written to the repository.
    atomic_json('/etc/frarchenstein/kernel-reviews.json', {'url': REVIEW_URL})
    atomic_json('/var/lib/frarchenstein/kernel-reviews.json', {'url': REVIEW_URL, 'reviews': {release: record}})
    run('pacman', '-Su', '--noconfirm', 'linux')
    require(output('/usr/bin/pacman', '-Q', 'linux').split()[1] == version, 'repository changed during candidate installation')
    require(os.uname().release == before, 'unexpected reboot during upgrade')
    require(read_json('/boot/frarchenstein/update.json')['state'] == 'ready', 'upgrade did not finish')
    require(snapshot_network() == state['network'], 'OpenWrt configuration changed during Arch update')
    require(read_json('/run/frarchenstein/selection.json')['generation'] == gen, 'OpenWrt selection changed during kernel update')
    marker_path = Path('/run/frarchenstein/modules-root')
    require(marker_path.is_file(), 'running-module preservation missing')
    # Load a module that was not needed for boot, through the OpenWrt bridge.
    run('chroot', ROOT, '/sbin/modprobe', 'dummy')
    vermagic = output('chroot', ROOT, '/sbin/modinfo', '-F', 'vermagic', 'dummy')
    require(vermagic.split()[0] == before, 'bridge lost running kernel modules')
    run('chroot', ROOT, '/sbin/rmmod', 'dummy')
    state['checks'].update(full_upgrade=True, running_module_preservation=True,
                           openwrt_unchanged_during_upgrade=True)
    atomic_json(STATE, state)


def boot_a(request):
    state = read_json(STATE)
    release = state['release']
    require(os.uname().release == release, 'candidate kernel did not boot')
    require(sha(Path('/usr/lib/modules') / release / 'vmlinuz') == state['record']['vmlinuz_sha256'], 'booted kernel package differs from candidate')
    require(read_json('/run/frarchenstein/selection.json')['generation'] == state['generation_a'], 'wrong initial OpenWrt generation')
    require(snapshot_network() == state['network'], 'OpenWrt configuration did not persist')
    require((ROOT / 'etc/fwr-kernel-review-id').read_text() == state['marker'], 'overlay marker lost')
    require((ROOT / 'persistent/shared/kernel-review-id').read_text() == state['marker'], 'shared data marker lost')
    if not Path('/proc/config.gz').exists():
        run('modprobe', 'configs')
    text = gzip.open('/proc/config.gz', 'rt').read()
    config = dict(re.findall(r'^(CONFIG_[A-Za-z0-9_]+)=(.*)$', text, re.M))
    for name, allowed in REQUIRED_CONFIG.items():
        require(config.get('CONFIG_' + name) in set(allowed), 'required kernel configuration missing: CONFIG_' + name)
    for equivalent in state['record']['policy']['builtin_equivalents'].values():
        if equivalent.startswith('CONFIG_'):
            name, value = equivalent.split('=', 1)
            require(config.get(name) == value, 'builtin equivalence changed: ' + equivalent)
    state['config_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    state['required_config'] = {key: config['CONFIG_' + key] for key in REQUIRED_CONFIG}
    state['booted_module_resolution'] = module_checks(Path('/'), release, state['record'])
    for module in ['dummy', 'nf_conntrack', 'nf_tables']:
        run('chroot', ROOT, '/sbin/modprobe', module)
        require(output('chroot', ROOT, '/sbin/modinfo', '-F', 'vermagic', module).split()[0] == release,
                'module does not match booted candidate: ' + module)
    run('chroot', ROOT, '/sbin/rmmod', 'dummy')
    state['checks'].update(candidate_boot=True, required_kernel_config=True,
                           matching_running_modules=True, configuration_persistence=True)
    atomic_json(STATE, state)


def prepare_b(request):
    state = read_json(STATE)
    config = Path('/etc/mkinitcpio-frarchenstein.conf')
    original = config.read_bytes()
    active = os.readlink('/boot/arch/current')
    try:
        config.write_bytes(original + b'\nHOOKS+=(fwr_review_missing_hook)\n')
        failed = subprocess.run(['pacman', '-S', '--noconfirm', 'mkinitcpio'])
        require(failed.returncode != 0, 'failure fixture unexpectedly succeeded')
        require(read_json('/boot/frarchenstein/update.json')['state'] == 'repair-required', 'failed build did not require repair')
        require(os.readlink('/boot/arch/current') == active, 'failed build activated incomplete boot files')
    finally:
        config.write_bytes(original)
    run('pacman', '--frarchenstein-repair')
    require(read_json('/boot/frarchenstein/update.json')['state'] == 'ready', 'repair failed')
    unpacked = WORK / 'generation-b'
    run('unsquashfs', '-d', unpacked, '/boot/openwrt/current/rootfs.squashfs', stdout=subprocess.DEVNULL)
    (unpacked / 'etc/fwr-review-generation').write_text('B\n')
    image = WORK / 'generation-b.squashfs'
    run('mksquashfs', unpacked, image, '-noappend', '-comp', 'xz', '-b', '262144', '-processors', '2', stdout=subprocess.DEVNULL)
    command = ['router-openwrt-update', 'prepare', '--rootfs', str(image), '--sha256', sha(image)]
    for name in ['network', 'system', 'firewall', 'dhcp']:
        command += ['--copy', 'etc/config/' + name]
    result = output(*command)
    matches = re.findall(r'^25\.12\.6-[a-f0-9]{64}$', result, re.M)
    require(len(matches) == 1, 'generation preparation did not return an identity')
    state['generation_b'] = matches[0]
    state['checks']['failed_initramfs_and_repair'] = True
    atomic_json(STATE, state)
    run('router-openwrt-update', 'select', state['generation_b'])


def boot_b(request):
    state = read_json(STATE)
    require(os.uname().release == state['release'], 'kernel changed during generation switch')
    require(read_json('/run/frarchenstein/selection.json')['generation'] == state['generation_b'], 'generation B did not boot')
    require((ROOT / 'etc/fwr-review-generation').read_text().strip() == 'B', 'wrong SquashFS in generation B')
    require(not (ROOT / 'etc/fwr-kernel-review-id').exists(), 'generation B shared generation A overlay')
    require((ROOT / 'persistent/generations' / state['generation_a'] / 'upper/etc/fwr-kernel-review-id').read_text() == state['marker'], 'generation A overlay lost')
    require((ROOT / 'persistent/shared/kernel-review-id').read_text() == state['marker'], 'shared data lost in generation B')
    (ROOT / 'etc/fwr-kernel-review-b').write_text(state['marker'])
    state['checks']['generation_b_isolation'] = True
    atomic_json(STATE, state)


def select_a(request):
    run('router-openwrt-update', 'select', read_json(STATE)['generation_a'])


def rollback_a(request):
    boot_a(request)
    state = read_json(STATE)
    require(not (ROOT / 'etc/fwr-kernel-review-b').exists(), 'generation B overlay leaked into A')
    require(not (ROOT / 'etc/fwr-review-generation').exists(), 'generation B lower remained active')
    state['checks']['generation_a_rollback'] = True
    atomic_json(STATE, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'boot_a', 'prepare_b', 'boot_b', 'select_a', 'rollback_a'])
    parser.add_argument('request', type=Path)
    args = parser.parse_args()
    require(os.geteuid() == 0 and Path('/run/frarchenstein/prepared').exists(), 'run only inside the disposable hybrid guest')
    check_environment()
    request = read_json(args.request)
    require(request['disposable_validation'] is True, 'missing disposable-test request')
    globals()[args.phase](request)
    print('FWR_KERNEL_REVIEW_RESULT=' + json.dumps(read_json(STATE), sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
