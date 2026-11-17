#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Validate an Arch kernel in fresh QEMU guests, then update local reviews."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

from fetch import ROOT
import start

sys.path.insert(0, str(ROOT / 'src'))
from kernel_reviews import records
from runtime import REVISION

REQUIRED_GUEST = {
    'official_package_signature', 'candidate_module_resolution', 'full_upgrade',
    'running_module_preservation', 'openwrt_unchanged_during_upgrade',
    'candidate_boot', 'required_kernel_config', 'matching_running_modules',
    'configuration_persistence', 'failed_initramfs_and_repair',
    'generation_b_isolation', 'generation_a_rollback',
}
REQUIRED_BOOT = {
    'clean_shutdown', 'partition_table_unchanged', 'readonly_filesystems_verified',
    'router_dhcp_dns_verified', 'lan_internet_verified', 'lan_ssh_verified',
    'luci_http_verified', 'luci_https_verified', 'direct_module_delegation_verified',
    'wan_ssh_blocked', 'owner_reinstalls_verified', 'stock_owner_refused_before_mutation',
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sources():
    names = (ROOT / 'SOURCE_FILES').read_text().splitlines()
    names += ['review-kernel.sh', 'tools/review_kernel.py', 'tools/start.py',
              'tools/console.py', 'tools/devices.py', 'tools/build.py']
    return {name: digest(ROOT / name) for name in sorted(set(names))}


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def publish(path, original, candidate, boots, expected_sources):
    """The only repository mutation; all gates must pass before this call."""
    if sources() != expected_sources:
        raise ValueError('source changed during validation; review file remains unchanged')
    if path.read_bytes() != original:
        raise ValueError('review file changed during validation; refusing to overwrite it')
    if len(boots) != 4 or any(not all(b['result'].get(k) is True for k in REQUIRED_BOOT) for b in boots):
        raise ValueError('not all four QEMU boots passed their acceptance gates')
    if not boots[-1]['result'].get('persistence_verified'):
        raise ValueError('rollback persistence was not verified')
    if not all(candidate['checks'].get(k) is True for k in REQUIRED_GUEST):
        raise ValueError('kernel validation is incomplete')
    release, record = candidate['release'], dict(candidate['record'])
    records({release: record})
    record['validation'] = {
        'scope': 'qemu-x86_64-sata-virtio-net', 'hardware_verified': False,
        'baseline_kernel': candidate['baseline'],
        'package_sha256': candidate['package_sha256'],
        'config_sha256': candidate['config_sha256'],
        'required_config': candidate['required_config'],
        'checks': sorted(REQUIRED_GUEST | REQUIRED_BOOT | {'generation_a_persistence'}),
    }
    updated = records(json.loads(original))
    previous = updated.get(release)
    if previous and any(previous.get(k) != record.get(k) for k in ('package_version', 'vmlinuz_sha256', 'policy', 'startup_modules')):
        raise ValueError('existing review has different kernel identity or policy; investigate before replacing it')
    updated[release] = record
    records(updated)
    # Preserve existing entries and permissions; interruption cannot truncate
    # the tracked review file. No automatic commit, signing or publication.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.kernel-review-', delete=False) as stream:
        pending = Path(stream.name)
        try:
            json.dump(updated, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.chmod(pending, path.stat().st_mode & 0o777)
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    return record


def upload(console, destination, payload):
    encoded = base64.b64encode(payload).decode()
    temporary = destination + '.b64'
    console.command(': > ' + shlex.quote(temporary))
    for offset in range(0, len(encoded), 700):
        console.command('printf %s ' + shlex.quote(encoded[offset:offset + 700]) + ' >> ' + shlex.quote(temporary))
    console.command('arch -c ' + shlex.quote('base64 -d ' + shlex.quote(temporary) + ' > ' + shlex.quote(destination)))


def trial(image, manifest, work, request, before, after, tcg, ports):
    latest = None
    uploaded = False
    def stage(phase, console, state):
        nonlocal latest, uploaded
        if not uploaded:
            upload(console, '/tmp/fwr-review.py', (ROOT / 'builder/review_kernel.py').read_bytes())
            upload(console, '/tmp/fwr-review-request.json', json.dumps(request).encode())
            uploaded = True
        result = console.command('arch -c ' + shlex.quote('python3 /tmp/fwr-review.py ' + phase + ' /tmp/fwr-review-request.json'), timeout=3600)
        matches = re.findall(r'FWR_KERNEL_REVIEW_RESULT=(\{[^\n]*\})',
                             result.decode(errors='replace'))
        if len(matches) != 1:
            raise ValueError('guest validation stage did not return exactly one report')
        latest = json.loads(matches[0])
    original_args = sys.argv
    sys.argv = ['start.sh', '--image', str(image), '--manifest', str(manifest),
                '--work-dir', str(work), '--smoke-test', '--ssh-port', str(ports[0]),
                '--http-port', str(ports[1]), '--https-port', str(ports[2])]
    if tcg:sys.argv.append('--tcg')
    try:
        result, evidence = start.main(
            guest_check=(lambda c, s: stage(before, c, s)) if before else None,
            after_smoke=(lambda c, s: stage(after, c, s)) if after else None)
    finally:
        sys.argv = original_args
    return latest, {'path': str(evidence / 'report.json'), 'result': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.work')
    parser.add_argument('--reuse-build', type=Path, help='reuse a source-matched compiled core cache')
    parser.add_argument('--snapshot', help='optional Arch archive date, YYYY/MM/DD')
    parser.add_argument('--expected-version', help='refuse a candidate with another linux package version')
    parser.add_argument('--policy', type=Path, help='explicit replacement policy/startup_modules JSON for the candidate')
    parser.add_argument('--tcg', action='store_true')
    parser.add_argument('--ssh-port', type=int, default=2222)
    parser.add_argument('--http-port', type=int, default=8080)
    parser.add_argument('--https-port', type=int, default=8443)
    args = parser.parse_args()
    if os.geteuid() == 0:parser.error('run as a normal host user; construction runs inside QEMU')
    if args.snapshot and not re.fullmatch(r'\d{4}/\d{2}/\d{2}', args.snapshot):
        parser.error('snapshot must be YYYY/MM/DD')
    os.umask(0o077)
    work = args.work_dir.absolute(); work.mkdir(parents=True, exist_ok=True)
    with (work / 'kernel-review.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_id = str(uuid.uuid4())
        directory = work / 'kernel-reviews' / run_id
        directory.mkdir(parents=True)
        review_file = ROOT / 'configs/reviewed-kernels.json'
        original, fingerprint = review_file.read_bytes(), sources()
        request = {'disposable_validation': True, 'run_id': run_id,
                   'snapshot': args.snapshot, 'expected_version': args.expected_version,
                   'runtime_sha256': {name: digest(ROOT / 'src' / name) for name in ['update.py', 'kernel_reviews.py', 'runtime.py', 'generations.py']}}
        if args.policy:
            request['policy'] = json.loads(args.policy.read_text())
            if set(request['policy']) != {'policy', 'startup_modules'}:
                parser.error('policy file must contain only policy and startup_modules')
        save(directory / 'request.json', request)
        report = {'success': False, 'published_to_local_file': False, 'source_sha256': fingerprint,
                  'physical_device_access': False, 'boots': []}
        save(directory / 'report.json', report)
        image = directory / 'router.img'
        try:
            command = [str(ROOT / 'build.sh'), '--image', str(image), '--size-mib', '16384',
                       '--work-dir', str(work), '--install']
            if args.reuse_build:command += ['--reuse-build', str(args.reuse_build.absolute())]
            if args.tcg:command.append('--tcg')
            print('Building fresh disposable baseline. Log:', directory / 'build.log', flush=True)
            with (directory / 'build.log').open('xb') as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
            manifest = directory / 'installation.json'
            installed = json.loads((work / 'last-install.json').read_text())
            if Path(installed['target']) != image or not installed['success']:
                raise ValueError('builder returned another target')
            save(manifest, installed)
            for before, after in [(None, 'prepare'), ('boot_a', 'prepare_b'), ('boot_b', 'select_a'), ('rollback_a', None)]:
                print('Kernel review stage:', before or after, flush=True)
                candidate, boot = trial(image, manifest, work, request, before, after, args.tcg,
                                        (args.ssh_port, args.http_port, args.https_port))
                report['boots'].append(boot)
                report['candidate'] = candidate
                save(directory / 'report.json', report)
            record = publish(review_file, original, candidate, report['boots'], fingerprint)
            report.update(success=True, published_to_local_file=True, release=candidate['release'], review=record)
            print('KERNEL REVIEW PASSED:', candidate['release'], flush=True)
            print('Updated:', review_file, flush=True)
            print('QEMU validation only; physical hardware is not certified.', flush=True)
        except BaseException as error:
            report['error'] = str(error)
            raise
        finally:
            save(directory / 'report.json', report)
            print('Validation evidence:', directory / 'report.json', flush=True)


if __name__ == '__main__':
    try:main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print('review-kernel:', error, file=sys.stderr)
        sys.exit(1)
