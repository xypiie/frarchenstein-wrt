# SPDX-License-Identifier: GPL-2.0-only
import io,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from console import Console
class ConsoleTests(unittest.TestCase):
 def test_failure_before_success_is_not_masked(self):
  with tempfile.TemporaryDirectory() as tmp,patch('console.sys.stdout',SimpleNamespace(buffer=io.BytesIO())):
   vm=Console(['/bin/sh'],Path(tmp)/'serial.log')
   try:
    with self.assertRaises(RuntimeError):vm.command('false; true',timeout=5)
    vm.command('true',timeout=5)
   finally:
    vm.send('exit');vm.process.wait(timeout=5);vm.log.close();vm.process.stdin.close();vm.process.stdout.close()
if __name__=='__main__':unittest.main()
