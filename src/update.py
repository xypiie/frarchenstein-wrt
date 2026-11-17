#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Controlled full Arch transactions. Run inside the verified maintenance root.

The persistent journal is intentionally conservative: a failed/interrupted
transaction requires `repair`, never a retry that silently clears the failure.
This program never reboots and implements no Arch rollback facility.
"""
import argparse
import fcntl
import json
import os
import re
import urllib.parse
from pathlib import Path
import shutil
import subprocess
import sys
import time

from runtime import atomic_json, read_json, sha, checked_generation, check_protected_upper

STATE = Path('/boot/frarchenstein/update.json')
RUN = Path('/run/frarchenstein')
ROOT = Path('/proc/1/root')
LIB = Path('/usr/lib/frarchenstein')
PACMAN = '/usr/bin/pacman'
REVIEW = RUN / 'transaction-kernel.json'


def call(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def journal(state, **fields):
    value = read_json(STATE) if STATE.exists() else {}
    value.update(fields, state=state, changed_ns=time.time_ns())
    atomic_json(STATE, value)
    return value


def audit_files():
    manifest = read_json('/usr/share/frarchenstein/integration-files.json')
    for name, expected in manifest.items():
        path = Path('/') / name
        if 'link' in expected:
            if not path.is_symlink() or os.readlink(path) != expected['link']:
                raise ValueError('integration link changed: ' + name)
        elif path.is_symlink() or sha(path) != expected['sha256']:
            raise ValueError('integration file changed: ' + name)
    for path in ['/etc/mkinitcpio-frarchenstein.conf', '/etc/mkinitcpio.d/frarchenstein.preset']:
        if Path(path + '.pacnew').exists():
            raise ValueError('review configuration .pacnew before publishing: ' + path)
    # Detect a new upstream publisher hook rather than guessing that existing
    # overrides cover it. The two reviewed mkinitcpio hooks remain masked.
    masks = {'60-mkinitcpio-remove.hook', '90-mkinitcpio-install.hook'}
    for directory in ['/usr/share/libalpm/hooks', '/etc/pacman.d/hooks']:
        for hook in Path(directory).glob('*.hook'):
            if hook.name in masks or hook.name.startswith(('05-frarchenstein-', '99-frarchenstein-')):
                continue
            content = hook.read_text()
            if any(word in content for word in ['mkinitcpio', 'kernel-install', 'bootctl', 'dracut']):
                raise ValueError('unreviewed boot transaction hook: ' + str(hook))


def audit_generation():
    gen, _, selection = checked_generation(Path('/boot'))
    current = read_json(RUN / 'selection.json')
    if gen != current['generation']:
        raise ValueError('boot the selected OpenWrt generation before updating Arch')
    upper = ROOT / 'persistent/generations' / gen / 'upper'
    for name, expected in selection['protected_paths'].items():
        if sha(ROOT / name.lstrip('/')) != expected:
            raise ValueError('OpenWrt integration changed: ' + name)
    for name, target in selection['protected_links'].items():
        if os.readlink(ROOT / name.lstrip('/')) != target:
            raise ValueError('OpenWrt integration link changed: ' + name)
    check_protected_upper(upper, selection)
    return gen


def check_environment():
    if os.geteuid() != 0 or not (RUN / 'prepared').exists():
        raise ValueError('requires the running hybrid maintenance chroot')
    if not Path('/boot').is_mount() or not (ROOT / 'persistent').is_mount():
        raise ValueError('boot and persistent mounts must be available')
    if read_json('/etc/frarchenstein/install.json') != read_json('/boot/frarchenstein/install.json'):
        raise ValueError('installation identity mismatch')
    for api in ['dev', 'proc', 'sys', 'run', 'boot']:
        a, b = Path('/' + api).stat(), (ROOT / api).stat()
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise ValueError('maintenance API mismatch: ' + api)
    from apps import registry
    registry()  # Validate contracts even while applications are stopped.
    resolver = Path('/etc/resolv.conf')
    if resolver.is_symlink() and os.readlink(resolver) == '/proc/1/root/etc/resolv.conf':
        # M3's root-only proc link is inaccessible to pacman's alpm downloader.
        # /tmp is already shared by identity; keep live OpenWrt DNS updates.
        pending = resolver.with_name('resolv.conf.frarchenstein-new')
        pending.symlink_to('/tmp/resolv.conf')
        os.replace(pending, resolver)
    if not resolver.is_symlink() or os.readlink(resolver) != '/tmp/resolv.conf':
        raise ValueError('review custom Arch resolver configuration before updating')
    audit_files()
    return audit_generation()


def preserve_running_tree():
    release = os.uname().release
    marker = RUN / 'modules-root'
    if marker.exists():
        base = Path(marker.read_text().strip())
        if base != Path('/var/lib/frarchenstein/running-modules') / release:
            raise ValueError('invalid preserved module root')
        if not (base / 'lib').exists():
            (base / 'lib').symlink_to('usr/lib')
        if not (base / 'usr/lib/modules' / release / 'modules.dep').is_file():
            raise ValueError('preserved running modules are incomplete')
        return
    source = Path('/usr/lib/modules') / release
    base = Path('/var/lib/frarchenstein/running-modules') / release
    if base.exists():
        # A previous boot may have retained this exact release. Never trust it
        # without replacing it from the still installed, verified current tree.
        shutil.rmtree(base)
    destination = base / 'usr/lib/modules' / release
    destination.parent.mkdir(parents=True)
    shutil.copytree(source, destination, symlinks=True)
    (base / 'lib').symlink_to('usr/lib')
    policy = read_json(LIB / 'kernel-policy.json')
    policies = LIB / 'kernel-policies'
    policies.mkdir(exist_ok=True)
    atomic_json(policies / (release + '.json'), policy)
    os.sync()
    temp = marker.with_suffix('.new')
    temp.write_text(str(base) + '\n')
    temp.replace(marker)


def preserve_running():
    # Wait for earlier bridge requests before changing their module root. Later
    # requests acquire the shared lock and read the durable preserved root.
    with (RUN / 'modules.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        preserve_running_tree()


def authorized_hook():
    if not (RUN / 'update-owner.json').exists():
        raise ValueError('use pacman from the arch shell; no live authorized transaction')
    auth = read_json(RUN / 'update-owner.json')
    owner = auth['pid']
    # Only a child of the live lock-owning updater may authorize pacman. An
    # environment variable or a stale file alone cannot bypass the pre-hook.
    pid = os.getppid()
    while pid > 1 and pid != owner:
        pid = int(Path('/proc', str(pid), 'stat').read_text().split(') ', 1)[1].split()[1])
    if pid != owner or Path('/proc', str(owner), 'stat').read_text().split(') ', 1)[1].split()[19] != auth['starttime']:
        raise ValueError('use pacman from the maintenance shell; no live authorized transaction')
    lock = (RUN / 'update.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    finally:
        lock.close()
    raise ValueError('updater does not hold the transaction lock')


def hook_pre():
    authorized_hook()
    check_environment()
    auth = read_json(RUN / 'update-owner.json')
    targets = sys.stdin.read().splitlines()
    args = auth.get('pacman_args')
    if args is None:
        raise ValueError('transaction lacks its pacman request')
    operation, _, _ = pacman_request(args)
    if operation == 'R' and set(targets) & {
            'linux', 'frarchenstein-wrt-arch', 'python', 'kmod', 'mkinitcpio',
            'util-linux', 'e2fsprogs', 'bash', 'squashfs-tools', 'gnupg',
            'pacman', 'libarchive'}:
        raise ValueError('removing the maintenance/boot integration is unsupported')
    if 'linux' in targets:
        review_transaction_kernel(args)
    if operation == 'S' and not full_upgrade(args):
        pending = subprocess.run([PACMAN, '-Quq'], capture_output=True, text=True)
        if pending.returncode not in (0, 1):
            raise ValueError('cannot check pending system upgrades')
        if pending.stdout.strip():
            raise ValueError('pending system upgrades: use pacman -Syu with your package targets to avoid a partial upgrade')
    # All refusal checks precede application shutdown and installed-file changes.
    journal('transaction', targets=targets)
    from apps import quiesce
    quiesce()
    preserve_running()


def hook_post():
    if not Path('/boot/arch/current').exists():
        return  # initial signed package install in the fresh-image builder
    authorized_hook()
    preserve_running()
    # Post hooks cannot roll pacman back. They only mark completion; the parent
    # checks pacman's return value, integrity and the complete boot candidate.
    journal('packages-installed')


def candidate_policy():
    files = output(PACMAN, '-Qlq', 'linux').splitlines()
    kernels = [Path(p) for p in files if p.startswith('/usr/lib/modules/') and p.endswith('/vmlinuz')]
    if len(kernels) != 1:
        raise ValueError('expected one package-owned linux kernel')
    kernel = kernels[0]
    release = kernel.parent.name
    from kernel_reviews import load_reviews
    reviewed = load_reviews()
    evidence = reviewed.get(release)
    if REVIEW.exists():
        transaction = read_json(REVIEW)
        if transaction['release'] == release:
            evidence = transaction['evidence']
    if not evidence or sha(kernel) != evidence['vmlinuz_sha256']:
        raise ValueError('kernel requires source/config/module review before publication: ' + release)
    if output(PACMAN, '-Q', 'linux').split()[1] != evidence['package_version']:
        raise ValueError('kernel package version differs from review')
    call('depmod', release)
    for name in evidence['startup_modules']:
        key = name.replace('-', '_')
        if key == 'button_hotplug':
            raise ValueError('excluded module in review')
        if key not in evidence['policy']['builtin_equivalents']:
            call('modprobe', '-S', release, '-C', '/dev/null', '--show-depends',
                 evidence['policy']['module_aliases'].get(key, name), stdout=subprocess.DEVNULL)
    return kernel, dict(evidence['policy'], kernel_release=release)


def prepare():
    check_environment()
    kernel, policy = candidate_policy()
    stage = Path('/boot/frarchenstein-build')
    stage.mkdir(exist_ok=True)
    (stage / 'initramfs-linux.img').unlink(missing_ok=True)
    shutil.copyfile(kernel, stage / 'vmlinuz-linux')
    atomic_json(stage / 'kernel-policy.json', policy)
    call('mkinitcpio', '-p', 'frarchenstein')
    call('/usr/bin/python3', LIB / 'publish_boot.py', '--update')
    # Keep the old default policy until publication succeeds. Per-release
    # policies permit the still-running old kernel to continue loading modules.
    atomic_json(LIB / 'kernel-policies' / (policy['kernel_release'] + '.json'), policy)
    atomic_json(LIB / 'kernel-policy.json', policy)
    journal('ready', kernel_release=policy['kernel_release'], generation=audit_generation(),
            active_boot=os.readlink('/boot/arch/current'), error=None)


def pacman_request(args):
    """Classify supported native syntax without rewriting package targets."""
    operations = {'--sync': 'S', '--upgrade': 'U', '--remove': 'R',
                  '--query': 'Q', '--files': 'F', '--deptest': 'T',
                  '--version': 'V', '--help': 'h'}
    op, flags, long_flags = [], '', set()
    skip = False
    end = False
    for arg in args:
        if end:
            continue
        if skip:
            skip = False
            continue
        if arg == '--':
            end = True
            continue
        if arg.startswith('--'):
            name = arg.split('=', 1)[0]
            if name in operations:
                op.append(operations[name])
            else:
                long_flags.add(name)
            if name == '--color' and '=' not in arg:
                skip = True
        elif arg.startswith('-'):
            flags += arg[1:]
            op += [c for c in arg[1:] if c in 'SURQFTVhD']
    if not args or 'h' in op or 'V' in op:
        return 'Q', False, args
    if len(op) != 1 or op[0] not in 'SURQFT':
        raise ValueError('unsupported pacman operation for this installation')
    operation = op[0]
    if operation in 'QFT':
        return operation, False, args
    allowed_long = {'--refresh', '--sysupgrade', '--search', '--info', '--list',
                    '--groups', '--print', '--downloadonly', '--clean', '--needed',
                    '--noconfirm', '--confirm', '--verbose', '--debug', '--color',
                    '--noprogressbar', '--disable-download-timeout', '--asdeps',
                    '--asexplicit', '--quiet', '--recursive', '--cascade',
                    '--nosave', '--unneeded'}
    if long_flags - allowed_long or any(c not in 'SURyuqvislgpwcnh' for c in flags):
        raise ValueError('unsupported pacman option; alternate roots, hook bypasses and dependency bypasses are refused')
    refresh = 'y' in flags or '--refresh' in long_flags
    full = 'u' in flags or '--sysupgrade' in long_flags
    if refresh and not full:
        raise ValueError('use pacman -Syu; refreshing without a full upgrade risks a partial upgrade')
    readonly = operation == 'S' and (bool(set(flags) & set('silgpwc')) or bool(long_flags & {
        '--search', '--info', '--list', '--groups', '--print', '--downloadonly', '--clean'}))
    return operation, not readonly, args


def full_upgrade(args):
    for arg in args:
        if arg == '--':
            break
        if arg == '--sysupgrade' or (arg.startswith('-') and not arg.startswith('--') and 'u' in arg[1:]):
            return True
    return False


def plan_arguments(args):
    # Pacman has already refreshed/resolved/downloaded its transaction when
    # PreTransaction runs. Query that same database without refreshing again.
    result = []
    end = False
    for arg in args:
        if arg == '--':
            end = True
        if not end and arg == '--refresh':
            continue
        if not end and arg.startswith('-') and not arg.startswith('--'):
            arg = '-' + arg[1:].replace('y', '')
            if arg == '-':
                continue
        result.append(arg)
    separator = result.index('--') if '--' in result else len(result)
    result[separator:separator] = ['--print', '--print-format', '%n\t%v\t%l']
    return result


def review_transaction_kernel(args):
    # Pacman calls PreTransaction after downloading and authenticating packages,
    # before changing installed packages. Resolve against the now-locked DB.
    planned = output(PACMAN, *plan_arguments(args))
    kernels = [line.split('\t') for line in planned.splitlines()
               if line.startswith('linux\t')]
    if len(kernels) != 1 or len(kernels[0]) != 3:
        raise ValueError('cannot identify the exact kernel transaction')
    _, version, location = kernels[0]
    from kernel_reviews import for_version
    release, evidence = for_version(version, refresh=True)
    parts = urllib.parse.urlsplit(location)
    if parts.scheme in ('https', 'http', 'ftp'):
        name = Path(urllib.parse.unquote(parts.path)).name
        directories = output('/usr/bin/pacman-conf', 'CacheDir').splitlines()
        candidates = [Path(directory) / name for directory in directories]
    elif parts.scheme == 'file':
        candidates = [Path(urllib.parse.unquote(parts.path))]
    elif not parts.scheme:
        candidates = [Path(location)]
    else:
        raise ValueError('unsupported kernel package location')
    package = next((p for p in candidates if p.is_file()), None)
    if package is None:
        raise ValueError('authenticated kernel archive is unavailable for inspection')
    if output(PACMAN, '-Qp', package).split() != ['linux', version]:
        raise ValueError('kernel archive identity differs from transaction')
    # Read archive contents to stdout only; never unpack into the live root.
    members = output('/usr/bin/bsdtar', '-tf', package).splitlines()
    kernels = [m for m in members if re.fullmatch(r'(?:\./)?usr/lib/modules/[^/]+/vmlinuz', m)]
    if len(kernels) != 1 or kernels[0].removeprefix('./') != 'usr/lib/modules/' + release + '/vmlinuz':
        raise ValueError('kernel archive release differs from review')
    import hashlib
    payload = subprocess.check_output(['/usr/bin/bsdtar', '-xOf', str(package), kernels[0]])
    if hashlib.sha256(payload).hexdigest() != evidence['vmlinuz_sha256']:
        raise ValueError('kernel archive hash differs from review; transaction refused')
    atomic_json(REVIEW, {'release': release, 'evidence': evidence})


def maintenance_status():
    print(json.dumps(read_json(STATE) if STATE.exists() else {'state': 'initial'}, indent=2))


def transaction(args, repair=False):
    check_environment()
    with (RUN / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read_json(STATE) if STATE.exists() else {'state': 'initial'}
        if not repair and state['state'] not in ['ready', 'initial']:
            raise ValueError('previous transaction needs pacman --frarchenstein-repair')
        if not repair and state.get('kernel_release', os.uname().release) != os.uname().release:
            raise ValueError('boot the published kernel before another transaction')
        if shutil.disk_usage('/').free < 1024**3 or shutil.disk_usage('/boot').free < 256 * 1024**2:
            raise ValueError('insufficient staging/module preservation headroom')
        atomic_json(RUN / 'update-owner.json', {'pid': os.getpid(),
            'starttime': Path('/proc/self/stat').read_text().split(') ', 1)[1].split()[19],
            'pacman_args': args})
        REVIEW.unlink(missing_ok=True)
        # Delay all state/application changes until the pre-hook has approved
        # the candidate. Cancellation/refusal leaves the prior state untouched.
        previous = STATE.read_bytes() if STATE.exists() else None
        try:
            if not repair:
                result = subprocess.run([PACMAN, *args])
                changed = (STATE.read_bytes() if STATE.exists() else None) != previous
                if result.returncode:
                    if not changed:
                        return result.returncode
                    raise RuntimeError('pacman failed after transaction preparation')
                if not changed:
                    return 0  # Nothing to do, or the user declined.
            else:
                # Repair finalizes installed packages; it never starts another
                # upgrade implicitly. Fix the reported cause before retrying.
                from apps import quiesce
                quiesce()
                preserve_running()
                journal('packages-installed')
            call('/usr/bin/python3', LIB / 'apps.py', 'refresh')
            prepare()
            call('/usr/bin/python3', LIB / 'apps.py', 'resume')
            print('Arch transaction ready. Reboot when convenient; no automatic reboot.')
            return 0
        except BaseException as error:
            changed = (STATE.read_bytes() if STATE.exists() else None) != previous
            if changed or repair:
                if Path('/etc/frarchenstein/apps.json').exists():
                    subprocess.run(['/usr/bin/python3', str(LIB / 'apps.py'), 'quiesce'])
                journal('repair-required', error=str(error))
            raise
        finally:
            (RUN / 'update-owner.json').unlink(missing_ok=True)
            REVIEW.unlink(missing_ok=True)


def pacman_main(args):
    if args == ['--frarchenstein-status']:
        maintenance_status()
        return 0
    if args == ['--frarchenstein-repair']:
        return transaction([], repair=True)
    _, mutate, _ = pacman_request(args)
    if not mutate:
        return subprocess.call([PACMAN, *args])
    return transaction(args)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'pacman':
        return pacman_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['pre', 'post'])
    args = parser.parse_args()
    (hook_pre if args.action == 'pre' else hook_post)()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('frarchenstein pacman: ' + str(error), file=sys.stderr)
        sys.exit(1)
