#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Reuse only an explicitly selected local source-matched build, never a router image."""
import hashlib,json,shutil,subprocess
from pathlib import Path
record=json.loads(Path('/inputs/reuse.json').read_text())
root=Path('/compiled')
actual_paths=set()
for p in root.rglob('*'):
 if p.is_symlink():raise ValueError('symlink in compiled cache')
 if p.is_file():actual_paths.add(str(p.relative_to(root)))
if actual_paths!=set(record['files']):raise ValueError('compiled cache file set changed')
for name,expected in record['files'].items():
 p=root/name
 if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root):raise ValueError('invalid compiled-cache path')
 with p.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
 if actual!=expected:raise ValueError('compiled cache changed: '+name)
shutil.copytree(root/'artifacts',Path('/work/artifacts'))
for name in ('apk','libapk.so.3.0.0'):shutil.copyfile(root/name,Path('/work')/name)
Path('/work/apk').chmod(0o755);Path('/work/apk-build').symlink_to('apk')
for p in Path('/work/artifacts/packages').glob('*.apk'):
 subprocess.run(['/work/apk-build','--keys-dir','/work/artifacts/keys','verify',str(p)],check=True)
Path('/exports/reused-build.json').write_text(json.dumps(record,indent=2)+'\n')
print('Reused explicitly selected, source-matched local compiled build.',flush=True)
