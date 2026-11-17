#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Prepare and select compatible OpenWrt generations; never share full uppers.

Input images are produced by the signed-package builder. The explicit expected
hash identifies the reviewed result. This ABI accepts the existing reviewed
core owners; changing them requires a refreshed integration review/package.
"""
import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from runtime import REVISION, atomic_json, read_json, sha, check_protected_upper
from update import RUN, ROOT, check_environment

BOOT = Path('/boot/openwrt')
DATA = ROOT / 'persistent/generations'
CORE = ['ubox', 'fstools', 'procd', 'procd-ujail', 'procd-seccomp', 'base-files',
        'frarchenstein-wrt', 'frarchenstein-arch-kmods']


def packages(root):
    result = {}
    for record in (root / 'lib/apk/db/installed').read_text().strip().split('\n\n'):
        item, directory = {'files': []}, ''
        for line in record.splitlines():
            if line.startswith('P:'): item['name'] = line[2:]
            if line.startswith('V:'): item['version'] = line[2:]
            if line.startswith('D:'): item['dependencies'] = line[2:]
            if line.startswith('F:'): directory = line[2:]
            if line.startswith('R:'): item['files'].append(str(Path(directory) / line[2:]))
        result[item['name']] = item
    return result


def validate_lower(root, selection):
    for name, digest in selection['protected_paths'].items():
        path = root / name.lstrip('/')
        if path.is_symlink() or sha(path) != digest:
            raise ValueError('unreviewed lower integration path: ' + name)
    for name, target in selection['protected_links'].items():
        if os.readlink(root / name.lstrip('/')) != target:
            raise ValueError('unreviewed lower integration link: ' + name)
    candidate, current = packages(root), packages(ROOT / 'rom')
    for name in CORE:
        if candidate[name]['version'] != current[name]['version']:
            raise ValueError('core version needs an integration review: ' + name)
    if candidate['frarchenstein-wrt']['dependencies'] != current['frarchenstein-wrt']['dependencies']:
        raise ValueError('core dependency protection changed')
    world_names = {re.split(r'[<>=~@]', entry, maxsplit=1)[0]
                   for entry in (root / 'etc/apk/world').read_text().splitlines()}
    if 'frarchenstein-wrt' not in world_names:
        raise ValueError('integration must remain explicitly installed')
    if 'kernel' in candidate or any(name.startswith('kmod-') for name in candidate):
        raise ValueError('native kernel/kmod payload is forbidden')
    for name in selection['protected_paths']:
        if sum(name.lstrip('/') in item['files'] for item in candidate.values()) != 1:
            raise ValueError('missing or ambiguous protected package ownership')


def check_upper(gen, selection):
    directory = DATA / gen
    if directory.is_symlink() or read_json(directory / 'generation.json') != {'generation': gen, 'integration': REVISION}:
        raise ValueError('data generation identity mismatch')
    for name in ['upper', 'work']:
        if not (directory / name).is_dir() or (directory / name).is_symlink():
            raise ValueError('invalid overlay directories')
    state = read_json(directory / 'initialization.json')
    if state.get('generation') != gen or state.get('state') not in ['prepared', 'complete']:
        raise ValueError('generation initialization is incomplete')
    check_protected_upper(directory / 'upper', selection)


def inspect(image, selection):
    with tempfile.TemporaryDirectory(prefix='frarchenstein-generation-', dir='/var/tmp') as temp:
        root = Path(temp) / 'root'
        subprocess.run(['unsquashfs', '-no-progress', '-d', str(root), str(image)], check=True,
                       stdout=subprocess.DEVNULL)
        validate_lower(root, selection)


def prepare(image, expected, copy):
    if not re.fullmatch('[0-9a-f]{64}', expected) or image.is_symlink() or sha(image) != expected:
        raise ValueError('reviewed generation checksum mismatch')
    gen = '25.12.6-' + expected
    slot, directory = BOOT / 'slots' / gen, DATA / gen
    if slot.exists() or directory.exists():
        raise ValueError('refusing existing generation')
    selection = dict(read_json(RUN / 'selection.json'), generation=gen, sha256=expected)
    inspect(image, selection)
    # Validate the entire explicit migration list before writing either side.
    migration = []
    for name in copy:
        if not re.fullmatch(r'etc/config/[A-Za-z0-9_-]+', name):
            raise ValueError('migration is limited to explicit regular UCI config files')
        source = ROOT / name
        if source.is_symlink() or not source.is_file() or source.parent.is_symlink():
            raise ValueError('migration source is not a regular configuration file')
        migration.append((source, name, sha(source)))
    if shutil.disk_usage('/boot').free < image.stat().st_size + 64 * 1024**2:
        raise ValueError('insufficient boot partition headroom')
    slot.mkdir()
    shutil.copyfile(image, slot / 'rootfs.squashfs')
    atomic_json(slot / 'manifest.json', selection)
    (directory / 'upper').mkdir(parents=True)
    (directory / 'upper').chmod(0o755)
    (directory / 'work').mkdir()
    for source, name, digest in migration:
        destination = directory / 'upper' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if sha(destination) != digest:
            raise ValueError('configuration changed during migration')
    if any(name == 'etc/config/network' for _, name, _ in migration):
        marker = directory / 'upper/etc/frarchenstein/network-initialized'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('explicitly migrated\n')
    atomic_json(directory / 'generation.json', {'generation': gen, 'integration': REVISION})
    atomic_json(directory / 'initialization.json', {'generation': gen, 'state': 'prepared'})
    atomic_json(directory / 'migration.json', {'from': read_json(RUN / 'selection.json')['generation'],
                'files': {name: digest for _, name, digest in migration}})
    check_upper(gen, selection)
    os.sync()
    print(gen)


def select(gen):
    if not re.fullmatch(r'25\.12\.6-[0-9a-f]{64}', gen):
        raise ValueError('invalid generation identity')
    slot = BOOT / 'slots' / gen
    if slot.is_symlink() or slot.resolve(strict=True) != slot:
        raise ValueError('invalid generation directory')
    selection = read_json(slot / 'manifest.json')
    if selection['generation'] != gen or selection['sha256'] != gen.split('-', 1)[1] or selection['integration'] != REVISION:
        raise ValueError('generation manifest mismatch')
    image = slot / 'rootfs.squashfs'
    if image.is_symlink() or sha(image) != selection['sha256']:
        raise ValueError('generation checksum changed')
    # Candidates must still match the currently supported integration contract.
    current = read_json(RUN / 'selection.json')
    for field in ['protected_paths', 'protected_links']:
        if selection[field] != current[field]:
            raise ValueError('generation needs an integration review')
    inspect(image, selection)
    check_upper(gen, selection)
    pending = BOOT / 'current.pending'
    pending.unlink(missing_ok=True)
    pending.symlink_to('slots/' + gen)
    os.replace(pending, BOOT / 'current')
    os.sync()
    print('Selected for next boot: ' + gen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--rootfs', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--copy', action='append', default=[])
    sub.add_parser('select').add_argument('generation')
    args = parser.parse_args()
    check_environment()
    with (RUN / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = Path('/boot/frarchenstein/update.json')
        if state.exists() and read_json(state)['state'] != 'ready':
            raise ValueError('repair the Arch transaction before changing generations')
        if args.action == 'prepare':
            prepare(args.rootfs.resolve(), args.sha256, args.copy)
        else:
            select(args.generation)


if __name__ == '__main__':
    main()
