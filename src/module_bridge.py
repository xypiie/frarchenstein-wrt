#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Translate OpenWrt requests inside Arch, using the real kernel's kmod tools.

The custom initramfs/preinit must bind the current OpenWrt /etc at the private
/run/frarchenstein/openwrt-etc before calls. This module is not a host utility.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

ETC = Path('/run/frarchenstein/openwrt-etc')


def safe_tokens(line):
    tokens = shlex.split(line, comments=True, posix=True)
    if any('\x00' in token or '\n' in token for token in tokens):
        raise ValueError('invalid module configuration token')
    return tokens


def normalized(name):
    if '/' in name or name.startswith('-') or not name:
        raise ValueError('expected a module name or modalias, not a path/option')
    return name.replace('-', '_')


def config_snapshot(directory):
    selected = {}
    for folder in ['/usr/lib/modprobe.d', '/usr/local/lib/modprobe.d', '/run/modprobe.d', '/etc/modprobe.d']:
        for path in sorted(Path(folder).glob('*.conf')):
            selected[path.name] = path
    for name, path in selected.items():
        text = path.read_text()
        for line in text.splitlines():
            words = safe_tokens(line)
            if words and words[0] not in {'options', 'blacklist', 'alias', 'softdep', 'weakdep'}:
                raise ValueError(f'unreviewed Arch modprobe directive in {path}: {words[0]}')
        (directory / name).write_text(text)
    legacy = ETC / 'modules.conf'
    translated = []
    blacklisted = set()
    if legacy.exists():
        for line in legacy.read_text().splitlines():
            words = safe_tokens(line)
            if not words:
                continue
            if len(words) < 2 or words[0] not in {'options', 'blacklist', 'alias'}:
                raise ValueError('unreviewed OpenWrt modules.conf directive')
            normalized(words[1])
            if words[0] == 'blacklist':
                if len(words) != 2:
                    raise ValueError('blacklist requires one module')
                blacklisted.add(normalized(words[1]))
            # Preserve original validated quoting; kmod parses its own format.
            translated.append(line)
    (directory / 'zz-frarchenstein-openwrt.conf').write_text('\n'.join(translated) + '\n')
    return blacklisted


def policy():
    release = os.uname().release
    selected = Path('/usr/lib/frarchenstein/kernel-policies', release + '.json')
    data = json.loads((selected if selected.exists() else Path('/usr/lib/frarchenstein/kernel-policy.json')).read_text())
    marker = Path('/run/frarchenstein/modules-root')
    base = Path(marker.read_text().strip()) if marker.exists() else Path('/')
    if base != Path('/') and base != Path('/var/lib/frarchenstein/running-modules', release):
        raise ValueError('invalid preserved module root')
    data['module_root'] = str(base)
    if data['kernel_release'] != release or not (base / 'usr/lib/modules' / release).is_dir():
        raise ValueError('kernel/module policy does not match the running kernel')
    if 'button_hotplug' not in data['excluded']:
        raise ValueError('kernel policy must retain the user button-hotplug exclusion')
    return data


def probe(name, parameters, directory, data, blacklisted, quiet=False, verbose=False):
    key = normalized(name)
    if key in data['excluded']:
        raise ValueError(f'module excluded by policy: {name}')
    if key in blacklisted:
        return 0
    builtin = data['builtin_equivalents'].get(key)
    if builtin:
        # The publisher generates this allowlist from verified config/source;
        # unknown names never become successful no-ops.
        if parameters:
            raise ValueError(f'cannot apply load-time options to built-in {name}')
        return 0
    cmd = ['/usr/bin/modprobe', '-d', data['module_root'], '--use-blacklist', '--config', str(directory)]
    if quiet:
        cmd.append('-q')
    if verbose:
        cmd.append('-v')
    actual = data['module_aliases'].get(key, name)
    return subprocess.run(cmd + ['--', actual, *parameters]).returncode


def dispatch(argv):
    if not argv:
        raise ValueError('missing module entrypoint')
    mode, args = argv[0], argv[1:]
    data = policy()
    with tempfile.TemporaryDirectory(prefix='module-config-', dir='/run/frarchenstein') as temp:
        directory = Path(temp)
        blacklisted = config_snapshot(directory)
        if mode == 'kmodloader':
            if len(args) > 1:
                raise ValueError('kmodloader accepts one startup directory')
            requested = args[0].rstrip('/') if args else '/etc/modules.d'
            if requested not in {'/etc/modules.d', '/etc/modules-boot.d'}:
                raise ValueError('unreviewed startup directory')
            folder = ETC / requested.removeprefix('/etc/')
            failed = False
            for path in sorted(folder.iterdir()):
                if not path.resolve().is_relative_to(ETC.resolve()) or not path.is_file():
                    raise ValueError('startup entry escapes the current OpenWrt configuration')
                for line in path.read_text().splitlines():
                    words = safe_tokens(line)
                    if words:
                        failed |= probe(words[0], words[1:], directory, data, blacklisted) != 0
            return int(failed)
        if mode == 'modprobe':
            many = quiet = verbose = False
            while args and args[0].startswith('-'):
                flag = args.pop(0)
                if flag == '--':
                    break
                if not re.fullmatch('-[aqv]+', flag):
                    raise ValueError(f'unsupported modprobe option: {flag}')
                many |= 'a' in flag
                quiet |= 'q' in flag
                verbose |= 'v' in flag
            if not args:
                raise ValueError('modprobe needs a module')
            if many:
                statuses = [probe(name, [], directory, data, blacklisted, quiet, verbose) for name in args]
                return int(any(statuses))
            return probe(args[0], args[1:], directory, data, blacklisted, quiet, verbose)
        if mode == 'insmod':
            if not args:
                raise ValueError('insmod needs a module')
            name = args[0]
            if '/' in name:
                # Old firmware module paths are deliberately rejected; package
                # startup requests are names and are resolved by Arch kmod.
                path = Path(name).resolve(strict=False)
                if not path.is_relative_to(Path('/usr/lib/modules', data['kernel_release']).resolve(strict=False)):
                    raise ValueError('insmod path is outside the running Arch module tree')
                actual = Path(data['module_root']) / path.relative_to('/')
                preserved = Path(data['module_root']) / 'usr/lib/modules' / data['kernel_release']
                if not actual.resolve(strict=True).is_relative_to(preserved.resolve(strict=True)):
                    raise ValueError('insmod target escapes the preserved running module tree')
                return subprocess.run(['/usr/bin/insmod', str(actual), *args[1:]]).returncode
            return probe(name.removesuffix('.ko'), args[1:], directory, data, set())
        if mode == 'lsmod':
            if args:
                raise ValueError('lsmod takes no arguments')
            return subprocess.run(['/usr/bin/lsmod']).returncode
        if mode == 'rmmod':
            if not args or any(name.startswith('-') or '/' in name for name in args):
                raise ValueError('rmmod requires names; forced removal is unsupported')
            return subprocess.run(['/usr/bin/rmmod', '--', *[data['module_aliases'].get(normalized(n), n) for n in args]]).returncode
        if mode == 'modinfo':
            # Only names and a narrow output-field option are forwarded.
            forwarded = []
            if args[:1] in [['-F'], ['--field']]:
                if len(args) != 3 or not re.fullmatch('[a-z_]+', args[1]):
                    raise ValueError('invalid modinfo field request')
                forwarded, args = args[:2], args[2:]
            if len(args) != 1:
                raise ValueError('modinfo needs one module')
            normalized(args[0])
            return subprocess.run(['/usr/bin/modinfo', '-b', data['module_root'], *forwarded, '--', data['module_aliases'].get(normalized(args[0]), args[0])]).returncode
        raise ValueError('unknown module entrypoint: ' + mode)


def main(argv):
    with Path('/run/frarchenstein/modules.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return dispatch(argv)


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, OSError, KeyError) as error:
        print(f'module-bridge: {error}', file=sys.stderr)
        sys.exit(1)
