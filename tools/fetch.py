#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fetch locked public inputs; isolated GPG keyring and mandatory signatures."""
import argparse,hashlib,json,os,subprocess,tempfile,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(path):
 with open(path,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def fetch(url,path,expected=None):
 path=Path(path)
 if path.exists():
  if expected and sha(path)!=expected:raise ValueError('cached input hash mismatch: '+path.name)
  return path
 partial=path.with_name(path.name+'.partial')
 print('Download:',url,flush=True)
 with urllib.request.urlopen(url,timeout=120) as src,partial.open('xb') as out:
  while chunk:=src.read(1024**2):out.write(chunk)
 if expected and sha(partial)!=expected:raise ValueError('download hash mismatch: '+path.name)
 partial.replace(path);return path

def verify(key,signature,payload,fingerprint,home):
 args=['gpg','--homedir',str(home),'--batch','--no-auto-key-retrieve']
 subprocess.run(args+['--import',str(key)],check=True,stdout=subprocess.DEVNULL)
 r=subprocess.run(args+['--status-fd','1','--verify',str(signature),str(payload)],capture_output=True,text=True)
 valid=[line.split() for line in r.stdout.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
 if r.returncode or not any(fingerprint in (line[2],line[-1]) for line in valid):raise ValueError('signature does not match pinned signer: '+payload.name)

def inputs(cache):
 cache.mkdir(parents=True,exist_ok=True,mode=0o700)
 lock=json.loads((ROOT/'configs/versions.json').read_text());a=lock['arch'];o=lock['openwrt']
 iso=fetch(a['iso_url'],cache/'arch.iso',a['iso_sha256'])
 isig=fetch(a['iso_url']+'.sig',cache/'arch.iso.sig')
 sums=fetch(o['base_url']+'sha256sums',cache/'openwrt-sha256sums')
 sig=fetch(o['base_url']+'sha256sums.asc',cache/'openwrt-sha256sums.asc')
 with tempfile.TemporaryDirectory(prefix='fwr-gpg-',dir=cache) as temp:
  for label,fp,payload,signature in [('arch',a['signing_fingerprint'],iso,isig),('openwrt',o['signing_fingerprint'],sums,sig)]:
   key=fetch('https://keys.openpgp.org/vks/v1/by-fingerprint/'+fp,cache/(label+'-public.asc'))
   verify(key,signature,payload,fp,Path(temp))
 for field,dest in [('image','openwrt.img.gz'),('sdk','sdk.tar.zst')]:
  expected=o[field+'_sha256']
  entries={line.split()[-1].lstrip('*'):line.split()[0] for line in sums.read_text().splitlines() if len(line.split())==2}
  if entries.get(o[field])!=expected:raise ValueError('signed OpenWrt manifest differs from lock')
  fetch(o['base_url']+o[field],cache/dest,expected)
 for s in lock['sources']:fetch(s['url'],cache/s['name'],s['sha256'])
 (cache/'verification.json').write_text(json.dumps({'success':True,'lock_sha256':sha(ROOT/'configs/versions.json'),'signatures_verified':True},indent=2)+'\n')
 return lock
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);a=p.parse_args();inputs(a.cache)
