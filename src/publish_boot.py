#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Validate an entire boot candidate, then atomically replace its selector."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from runtime import sha, read_json, atomic_json, REVISION, checked_generation


def publish(update=False):
    boot = Path('/boot')
    active = boot / 'arch/current'
    if not boot.is_mount() or (not update and (active.exists() or active.is_symlink())):
        raise ValueError('publisher requires mounted boot and an appropriate initial/update mode')
    if update:
        from update import authorized_hook, audit_files, audit_generation
        authorized_hook()
        audit_files()
        audit_generation()
        if read_json(boot / 'frarchenstein/update.json')['state'] not in ['prepared', 'packages-installed']:
            raise ValueError('transaction has not completed successfully')
    stage = boot / 'frarchenstein-build'
    kernel, initrd = stage / 'vmlinuz-linux', stage / 'initramfs-linux.img'
    policy = read_json(stage / 'kernel-policy.json')
    release = policy['kernel_release']
    if sha(kernel) != sha(Path('/usr/lib/modules', release, 'vmlinuz')):
        raise ValueError('kernel and module set do not match')
    with tempfile.TemporaryDirectory(prefix='frarchenstein-initrd-') as temp:
        root = Path(temp)
        subprocess.run(['lsinitcpio', '-x', str(initrd)], cwd=root, check=True, stdout=subprocess.DEVNULL)
        for name, source in [('usr/lib/frarchenstein/runtime.py', '/usr/lib/frarchenstein/runtime.py'),
                             ('etc/frarchenstein/install.json', '/etc/frarchenstein/install.json'),
                             ('hooks/frarchenstein', '/usr/lib/initcpio/hooks/frarchenstein'),
                             ('etc/frarchenstein/kernel-policy.json', str(stage / 'kernel-policy.json'))]:
            if not (root / name).is_file() or sha(root / name) != sha(source):
                raise ValueError('initramfs missing or changed: ' + name)
        if not (root / 'usr/bin/python3').exists():
            raise ValueError('initramfs Python missing')
        if not (root / 'usr/lib/modules' / release).is_dir():
            raise ValueError('initramfs lacks matching modules')
    gen, _, _ = checked_generation(boot)
    manifest = {'kernel_release': release, 'kernel_sha256': sha(kernel),
                'initramfs_sha256': sha(initrd), 'integration': REVISION,
                'validated_generation': gen, 'policy': policy}
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    destination = boot / 'arch/sets' / identity
    if destination.exists():
        if read_json(destination / 'manifest.json') != manifest or any(
                sha(destination / p.name) != sha(p) for p in [kernel, initrd]):
            raise ValueError('existing boot candidate differs from its identity')
    else:
        destination.mkdir(parents=True)
        for path in [kernel, initrd]:
            shutil.copyfile(path, destination / path.name)
        atomic_json(destination / 'manifest.json', manifest)
    policies = Path('/usr/lib/frarchenstein/kernel-policies')
    policies.mkdir(exist_ok=True)
    atomic_json(policies / (release + '.json'), policy)
    os.sync()
    pending = active.with_name('current.pending')
    pending.unlink(missing_ok=True)
    pending.symlink_to('sets/' + identity)
    os.replace(pending, active)
    os.sync()
    # No kernel rollback slots: remove obsolete sets only after durable selection.
    for old in (boot / 'arch/sets').iterdir():
        if old != destination and old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
    os.sync()
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update', action='store_true')
    publish(parser.parse_args().update)
