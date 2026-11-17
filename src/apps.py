#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Root-owned app contracts; unprivileged workloads supervised by OpenWrt procd.

Entry is through arch-run, so chroot happens before any privilege drop.
The foreground supervisor remains root solely to clean up its private cgroup.
"""
import argparse
import ctypes
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

from runtime import atomic_json, read_json, sha

CONFIG = Path('/etc/frarchenstein/apps.json')
ROOT = Path('/proc/1/root')
RUN = Path('/run/frarchenstein')
CGROUP = Path('/sys/fs/cgroup/frarchenstein-apps')
STATE = Path('/var/lib/frarchenstein/apps')


def validate(data):
    if not isinstance(data, dict) or data.get('format') != 1 or not isinstance(data.get('apps'), list):
        raise ValueError('expected application registry format 1')
    names, ids, listeners = set(), set(), set()
    result = {}
    for app in data['apps']:
        name = app['name']
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,23}', name) or name in names:
            raise ValueError('invalid or duplicate application name')
        names.add(name)
        for key in ['uid', 'gid']:
            if type(app[key]) is not int or not 1000 <= app[key] <= 60000:
                raise ValueError('application identity must be a non-system numeric ID')
        if app['uid'] in ids:
            raise ValueError('applications must have distinct user identities')
        ids.add(app['uid'])
        if not isinstance(app.get('groups', []), list) or any(type(x) is not int or x <= 0 for x in app.get('groups', [])):
            raise ValueError('invalid supplementary groups')
        if not isinstance(app.get('packages', []), list) or any(not isinstance(x,str) or not re.fullmatch(r'[a-z0-9][a-z0-9@._+\-]*',x) for x in app.get('packages', [])):
            raise ValueError('invalid declared application package')
        for key in ['command', 'check']:
            args = app[key]
            if not isinstance(args, list) or not args or any(not isinstance(x, str) or '\0' in x for x in args):
                raise ValueError('expected explicit argv for ' + key)
            if not args[0].startswith('/') or '..' in Path(args[0]).parts:
                raise ValueError('executable must be absolute')
        if app.get('cwd') != '/srv/router-apps/' + name:
            raise ValueError('cwd must be the declared shared application directory')
        for key, value in app.get('environment', {}).items():
            if not re.fullmatch('[A-Z][A-Z0-9_]*', key) or not isinstance(value, str) or '\0' in value:
                raise ValueError('invalid application environment')
            if key in ['PATH', 'HOME', 'USER', 'LOGNAME', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME']:
                raise ValueError('reserved application environment key')
        for endpoint in app.get('listeners', []):
            address, port = endpoint['address'], endpoint['port']
            socket.inet_pton(socket.AF_INET, address)
            if address == '0.0.0.0' or type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError('declare a specific IPv4 listener and unprivileged port')
            if (address, port) in listeners:
                raise ValueError('duplicate application listener')
            listeners.add((address, port))
        if app.get('python') not in [None, {'requirements': 'requirements.lock'}]:
            raise ValueError('Python environment requires requirements.lock in the shared directory')
        result[name] = app
    return result


def registry():
    if not CONFIG.exists():
        return {}
    st = CONFIG.lstat()
    if CONFIG.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
        raise ValueError('application registry must be root-owned and not writable by apps')
    return validate(read_json(CONFIG))


def environment(app):
    return dict(PATH='/usr/bin:/usr/sbin', HOME=app['cwd'], USER=app['name'],
                LOGNAME=app['name'], LANG='C.UTF-8', PYTHONUNBUFFERED='1',
                **app.get('environment', {}))


def check_mounts():
    if os.geteuid() != 0 or not (RUN / 'prepared').exists():
        raise ValueError('requires verified Arch chroot')
    a, b = Path('/srv/router-apps').stat(), (ROOT / 'persistent/shared/apps').stat()
    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino) or not Path('/srv/router-apps').is_mount():
        raise ValueError('shared application data is not mounted by identity')
    if not Path('/sys/fs/cgroup/cgroup.controllers').exists():
        raise ValueError('cgroup v2 is required for complete child cleanup')
    if not Path('/etc/resolv.conf').is_symlink() or os.readlink('/etc/resolv.conf') != '/tmp/resolv.conf':
        raise ValueError('shared DNS resolver is unavailable')


def ready(app):
    check_mounts()
    for base in [Path('/'), ROOT]:
        rows = [(line.split(':')[0], int(line.split(':')[2]), int(line.split(':')[3]))
                for line in (base / 'etc/passwd').read_text().splitlines() if line and not line.startswith('#')]
        if (app['name'], app['uid'], app['gid']) not in rows:
            raise ValueError('application identity mapping missing on both sides')
        if any(uid == app['uid'] and name != app['name'] for name, uid, _ in rows):
            raise ValueError('conflicting application UID')
    cwd = Path(app['cwd'])
    if cwd.is_symlink() or cwd.resolve() != cwd or cwd.stat().st_uid != app['uid']:
        raise ValueError('application data ownership/path mismatch')
    for group in app.get('groups', []):
        grp.getgrgid(group)


def provision():
    # Refuse missing/replaced backing mounts before account or directory writes.
    check_mounts()
    for app in registry().values():
        name, uid, gid = app['name'], app['uid'], app['gid']
        # Validate both identity databases completely before adding either entry.
        changes = []
        for base in [Path('/'), ROOT]:
            for leaf, number, row in [
                ('group', gid, f'{name}:x:{gid}:\n'),
                ('passwd', uid, f'{name}:x:{uid}:{gid}:Router application:{app["cwd"]}:/bin/false\n')]:
                path = base / 'etc' / leaf
                lines = path.read_text().splitlines()
                matches = [x.split(':') for x in lines if x and (x.split(':')[0] == name or int(x.split(':')[2]) == number)]
                if matches and (len(matches) != 1 or matches[0][0] != name or int(matches[0][2]) != number or (leaf == 'passwd' and int(matches[0][3]) != gid)):
                    raise ValueError('conflicting user/group: ' + name)
                if not matches: changes.append((path, row))
        for path, row in changes:
            with path.open('a') as f: f.write(row)
        for path in [Path(app['cwd']), STATE / name]:
            if path.is_symlink(): raise ValueError('symlinked application directory')
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, uid, gid)
            path.chmod(0o750)
        ready(app)


def child_setup(app, cgroup):
    os.setsid()
    (cgroup / 'cgroup.procs').write_text(str(os.getpid()))
    os.setgroups(app.get('groups', []))
    os.setgid(app['gid'])
    os.setuid(app['uid'])
    os.umask(0o027)
    # No setuid/capability elevation after executing application-owned code.
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'PR_SET_NO_NEW_PRIVS')


def populated(cgroup):
    return cgroup.exists() and 'populated 1' in (cgroup / 'cgroup.events').read_text()


def cleanup(cgroup):
    if not cgroup.exists(): return
    for pid in (cgroup / 'cgroup.procs').read_text().split():
        try: os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError: pass
    deadline = time.monotonic() + 3
    while populated(cgroup) and time.monotonic() < deadline:
        time.sleep(.05)
    if populated(cgroup): (cgroup / 'cgroup.kill').write_text('1')
    deadline = time.monotonic() + 3
    while populated(cgroup) and time.monotonic() < deadline:
        time.sleep(.05)
    if populated(cgroup): raise RuntimeError('application descendants did not stop')
    try: cgroup.rmdir()
    except OSError: pass


def execute(app, argv, timeout=None, stopping=None):
    ready(app)
    CGROUP.mkdir(exist_ok=True)
    group = CGROUP / (app['name'] + ('-check' if timeout else ''))
    group.mkdir(exist_ok=True)
    if populated(group): raise ValueError('application cgroup is already occupied')
    process = None
    try:
        process = subprocess.Popen(argv, cwd=app['cwd'], env=environment(app),
                                   preexec_fn=lambda: child_setup(app, group))
        started = time.monotonic()
        while process.poll() is None:
            if (stopping and stopping()) or (timeout and time.monotonic() - started > timeout):
                cleanup(group)
                process.wait(timeout=5)
                if timeout: raise TimeoutError('application command timed out')
                return 0
            time.sleep(.1)
        return process.returncode
    finally:
        cleanup(group)
        if process and process.poll() is None: process.wait(timeout=5)


def rebuild(app, force=False):
    ready(app)
    if not app.get('python'): return
    base = STATE / app['name']
    lock = Path(app['cwd']) / 'requirements.lock'
    if not lock.is_file() or lock.is_symlink(): raise ValueError('missing locked Python requirements')
    evidence = {'python': sys.version, 'requirements_sha256': sha(lock)}
    # App-writable venv directories must never contain root-written metadata.
    records = Path('/var/lib/frarchenstein/app-environments')
    records.mkdir(mode=0o700, exist_ok=True)
    record = records / (app['name'] + '.json')
    if not force and record.exists() and read_json(record) == evidence and (base / 'venv/bin/python').exists(): return
    # venv/pip run as the app identity. No third-party setup code runs as root.
    venv = base / 'venv'
    if venv.is_symlink(): raise ValueError('unexpected environment symlink')
    # A failed rebuild must never leave old metadata blessing a partial venv.
    atomic_json(record, dict(evidence, state='rebuilding'))
    if venv.exists(): shutil.rmtree(venv)
    if execute(app, ['/usr/bin/python3', '-m', 'venv', str(venv)], timeout=60):
        raise RuntimeError('venv creation failed')
    if lock.read_text().strip():
        if execute(app, [str(venv / 'bin/python'), '-m', 'pip', 'install', '--no-cache-dir', '--require-hashes', '--only-binary=:all:', '-r', str(lock)], timeout=300):
            raise RuntimeError('locked Python dependency rebuild failed')
    if sha(lock) != evidence['requirements_sha256']:
        raise ValueError('Python requirements changed during environment rebuild')
    atomic_json(record, evidence)


def check(app):
    if execute(app, app['check'], timeout=20): raise RuntimeError('application validation failed: ' + app['name'])


def service(action, *args):
    subprocess.run(['chroot', str(ROOT), '/etc/init.d/frarchenstein-apps', action, *args],
                   env=dict(os.environ, PATH='/usr/sbin:/usr/bin:/sbin:/bin'), check=True)


def quiesce():
    apps = registry()
    if not apps: return
    output = subprocess.check_output(['chroot',str(ROOT),'/bin/ubus','call','service','list',
                                      '{"name":"frarchenstein-apps"}'], text=True)
    instances = json.loads(output).get('frarchenstein-apps', {}).get('instances', {})
    names = [n for n, instance in instances.items() if instance.get('running')]
    resume = RUN / 'apps-resume.json'
    # Keep the original intent across a failed transaction and explicit repair.
    if not resume.exists(): atomic_json(resume, names)
    service('stop')
    deadline = time.monotonic() + 15
    while any(populated(CGROUP / name) for name in apps) and time.monotonic() < deadline: time.sleep(.1)
    if any(populated(CGROUP / name) for name in apps): raise RuntimeError('application stop incomplete')
    atomic_json(STATE / 'update.json', {'state':'stopped','resume':read_json(resume)})


def refresh():
    for app in registry().values():
        rebuild(app)
        check(app)


def resume():
    apps = registry()
    if not apps: return
    service('start', 'restore')
    names = read_json(RUN / 'apps-resume.json')
    # Runtime checks are separate from offline pre-start validation.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if all(populated(CGROUP / name) for name in names): break
        time.sleep(.1)
    else: raise RuntimeError('applications did not restart')
    for name in names:
        app = apps[name]
        for endpoint in app.get('listeners', []):
            deadline = time.monotonic() + 15
            while True:
                try:
                    with socket.create_connection((endpoint['address'], endpoint['port']), timeout=1): pass
                    break
                except OSError:
                    if time.monotonic() >= deadline: raise
                    time.sleep(.2)
    atomic_json(STATE / 'update.json', {'state':'ready','resumed':names})
    (RUN / 'apps-resume.json').unlink()


def run(app):
    state = Path('/boot/frarchenstein/update.json')
    if state.exists() and read_json(state)['state'] != 'ready': raise ValueError('Arch update is unfinished')
    for i in range(30):
        try:
            ready(app)
            for host in app.get('requires_dns', []): socket.getaddrinfo(host, None)
            break
        except (ValueError, OSError):
            if i == 29: raise
            time.sleep(1)
    stop = False
    def request_stop(signum, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print('frarchenstein-app: starting ' + app['name'], flush=True)
    result = execute(app, app['command'], stopping=lambda:stop)
    print('frarchenstein-app: stopped ' + app['name'] + ' status=' + str(result), flush=True)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['list','list-resume','provision','refresh','run','quiesce','resume'])
    p.add_argument('name',nargs='?')
    args=p.parse_args();apps=registry()
    if args.action in ['list','list-resume']:
        allowed=read_json(RUN/'apps-resume.json') if args.action=='list-resume' else list(apps)
        for name,app in apps.items():
            if app.get('enabled',True) and name in allowed: print(name)
    elif args.action=='run': return run(apps[args.name])
    else: globals()[args.action]()
    return 0


if __name__=='__main__':
    try: sys.exit(main())
    except Exception as e:
        print('frarchenstein-app: '+str(e),file=sys.stderr)
        sys.exit(1)
