# SPDX-License-Identifier: GPL-2.0-only
import subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from fetch import fetch,verify
class TrustTests(unittest.TestCase):
 def test_cached_download_mismatch_refused(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'download';p.write_bytes(b'changed')
   with patch('fetch.urllib.request.urlopen') as network:
    with self.assertRaises(ValueError):fetch('https://example.invalid/input',p,'0'*64)
    network.assert_not_called()
 def test_wrong_signer_and_failed_verification_refused(self):
  fp='A'*40
  for code,signer in ((0,'B'*40),(1,fp)):
   result=subprocess.CompletedProcess([],code,stdout='[GNUPG:] VALIDSIG '+signer+' details '+signer+'\n',stderr='')
   with patch('fetch.subprocess.run',side_effect=[None,result]),self.assertRaises(ValueError):verify(Path('key'),Path('signature'),Path('payload'),fp,Path('home'))
if __name__=='__main__':unittest.main()
