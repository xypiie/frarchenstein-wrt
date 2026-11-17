# SPDX-License-Identifier: GPL-2.0-only
import shutil,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from build import compiled_cache
class CacheTests(unittest.TestCase):
 def fixture(self,base):
  root=base/'source';job=base/'job'
  for name in ('configs/versions.json','recipes/openwrt/build.sh','recipes/openwrt/prepare_core.py','src/arch-run.c','src/kmod-entry.c','src/shutdown-final.c','patches/fstools.patch'):
   p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic fixture')
  shutil.copytree(root,job/'inputs/project')
  for name in ('apk','libapk.so.3.0.0','artifacts/arch-run','artifacts/shutdown-final','artifacts/signing.pem'):
   p=job/'exports/compiled'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic fixture')
  return root,job
 def test_changed_source_and_symlink_refused(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,job=self.fixture(Path(tmp))
   with patch('build.ROOT',root):
    _,record=compiled_cache(job);self.assertEqual(len(record['files']),5)
    p=root/'src/arch-run.c';p.write_text('changed')
    with self.assertRaises(ValueError):compiled_cache(job)
    p.write_text('synthetic fixture')
    (job/'exports/compiled/link').symlink_to(root)
    with self.assertRaises(ValueError):compiled_cache(job)
if __name__=='__main__':unittest.main()
