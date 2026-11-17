# SPDX-License-Identifier: GPL-2.0-only
"""Host device guards. No mounting, privilege escalation or automatic selection."""
from contextlib import contextmanager
import fcntl,json,os,re,stat,subprocess,struct,uuid
from pathlib import Path

def roles(mapping):
 if not isinstance(mapping,dict) or set(mapping)!={'lan0','wan0'}:raise ValueError('interface map requires exactly lan0 and wan0')
 values=list(mapping.values())
 for mac in values:
  if not isinstance(mac,str) or not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac) or int(mac[:2],16)&1 or mac=='00:00:00:00:00:00':raise ValueError('invalid interface MAC')
 if len(set(values))!=2:raise ValueError('LAN and WAN must have distinct MACs')
 return mapping

def children(node):
 yield node
 for n in node.get('children',[]):yield from children(n)
def inspect(path,minimum=0):
 path=Path(path)
 if path.is_symlink() or not stat.S_ISBLK(path.stat().st_mode):raise ValueError('use a direct whole-device path')
 data=json.loads(subprocess.check_output(['lsblk','--tree','--json','--bytes','-o','PATH,TYPE,SIZE,LOG-SEC,RO,RM,MOUNTPOINTS,MODEL,SERIAL,MAJ:MIN',str(path)],text=True))['blockdevices']
 if len(data)!=1:raise ValueError('ambiguous target')
 n=data[0]
 if n['type']!='disk' or not n['rm'] or n['ro'] or n['log-sec']!=512:raise ValueError('requires writable removable whole disk with 512-byte sectors')
 if int(n['size'])<minimum or int(n['size'])//512>0xffffffff:raise ValueError('target capacity outside supported layout')
 ids=set()
 for c in children(n):
  ids.add(c['maj:min'])
  if any(c.get('mountpoints') or []) or (c is not n and c['type']!='part'):raise ValueError('mounted/system/mapped target refused')
  if list((Path('/sys/dev/block')/c['maj:min']/'holders').iterdir()):raise ValueError('target has active holders')
 for line in Path('/proc/swaps').read_text().splitlines()[1:]:
  p=Path(line.split()[0])
  if p.exists():
   d=p.stat().st_rdev
   if f'{os.major(d)}:{os.minor(d)}' in ids:raise ValueError('target contains active swap')
 for c in children(n):
  r=subprocess.run(['fuser','-s',c['path']])
  if r.returncode!=1:raise ValueError('target is busy or usage cannot be verified')
 return {k:n[k] for k in ('path','size','model','serial','maj:min')}

@contextmanager
def opened(path,physical=False,create_bytes=None):
 flags=os.O_RDWR|os.O_NOFOLLOW
 if physical or create_bytes is not None:flags|=os.O_EXCL
 if create_bytes is not None:flags|=os.O_CREAT
 fd=os.open(path,flags,0o600)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  s=os.fstat(fd)
  if physical:
   if not stat.S_ISBLK(s.st_mode) or s.st_rdev!=Path(path).stat().st_rdev:raise ValueError('device identity changed')
  elif not stat.S_ISREG(s.st_mode):raise ValueError('requires regular image')
  if create_bytes is not None:os.ftruncate(fd,create_bytes)
  yield fd
 finally:os.close(fd)

def drive(fd,readonly_fd,physical,serial='fwr-target',id='target'):
 backend='host_device' if physical else 'file'
 return ['-add-fd',f'fd={fd},set=1','-add-fd',f'fd={readonly_fd},set=1','-device','ich9-ahci,id=ahci','-drive',f'file.driver={backend},file.filename=/dev/fdset/1,format=raw,if=none,id={id},cache=writeback','-device',f'ide-hd,drive={id},bus=ahci.0,serial={serial}']

def layout(fd,manifest,size,clean=True):
 if manifest.get('format')!=2 or len(manifest.get('partitions',[]))!=3:raise ValueError('invalid installation manifest')
 mbr=os.pread(fd,512,0)
 if mbr[510:]!=b'\x55\xaa' or mbr[440:444].hex()!=manifest['mbr_id'] or any(mbr[494:510]):raise ValueError('installation MBR identity/layout mismatch')
 end=2048
 for i,p in enumerate(manifest['partitions']):
  start,count=struct.unpack_from('<II',mbr,454+i*16)
  if mbr[450+i*16]!=0x83 or start!=p['start'] or start<end or count!=p['sectors']:raise ValueError('partition geometry changed unexpectedly')
  end=start+count
  if end*512>size:raise ValueError('partition beyond media')
  sb=os.pread(fd,1024,start*512+1024)
  if sb[56:58]!=b'\x53\xef' or str(uuid.UUID(bytes=sb[104:120]))!=p['uuid']:raise ValueError('filesystem identity mismatch')
  if clean and (struct.unpack_from('<H',sb,58)[0]!=1 or struct.unpack_from('<I',sb,96)[0]&4):raise ValueError('unclean filesystem: offline recovery required')
  if i==2:
   blocksize=1024<<struct.unpack_from('<I',sb,24)[0];blocks=struct.unpack_from('<I',sb,4)[0]
   if struct.unpack_from('<I',sb,96)[0]&0x80:blocks+=struct.unpack_from('<I',sb,336)[0]<<32
   if blocks*blocksize!=count*512//blocksize*blocksize:raise ValueError('data filesystem does not fill installed partition')
 if end*512!=size or manifest['target_bytes']!=size:raise ValueError('installed layout does not fill recorded target')
 return True
