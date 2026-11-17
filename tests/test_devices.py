# SPDX-License-Identifier: GPL-2.0-only
import json,os,stat,struct,sys,tempfile,unittest,uuid
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from devices import opened,layout,drive,roles,inspect
class DeviceTests(unittest.TestCase):
 def test_physical_usage_guards(self):
  for problem in ('none','mounted','swap','held','busy','usage-error','fixed','readonly','partition','sector','small'):
   with self.subTest(problem=problem):
    child={'path':'/dev/sdz1','type':'part','maj:min':'8:17','mountpoints':[]}
    disk={'path':'/dev/sdz','type':'disk','size':16000000000,'log-sec':512,'ro':False,'rm':True,'mountpoints':[],'model':'fixture','serial':'fixture','maj:min':'8:16','children':[child]}
    if problem=='mounted':child['mountpoints']=['/']
    if problem=='fixed':disk['rm']=False
    if problem=='readonly':disk['ro']=True
    if problem=='partition':disk['type']='part'
    if problem=='sector':disk['log-sec']=4096
    if problem=='small':disk['size']=1024
    swaps='Filename Type Size Used Priority\n'+('/dev/sdz1 partition 1 0 -2\n' if problem=='swap' else '')
    with patch('devices.Path.exists',return_value=True), patch('devices.Path.is_symlink',return_value=False), patch('devices.Path.stat',return_value=SimpleNamespace(st_mode=stat.S_IFBLK,st_rdev=os.makedev(8,17))), patch('devices.Path.iterdir',return_value=iter([Path('holder')]) if problem=='held' else iter([])), patch('devices.Path.read_text',return_value=swaps), patch('devices.subprocess.check_output',return_value=json.dumps({'blockdevices':[disk]})), patch('devices.subprocess.run',return_value=SimpleNamespace(returncode=0 if problem=='busy' else 2 if problem=='usage-error' else 1)):
     if problem=='none':self.assertEqual(inspect('/dev/sdz',minimum=4096)['serial'],'fixture')
     else:
      with self.assertRaises(ValueError):inspect('/dev/sdz',minimum=4096)
 def test_installer_refuses_small_and_existing_output(self):
  import build
  with tempfile.TemporaryDirectory() as tmp:
   work=Path(tmp);small=work/'small.img';existing=work/'existing.img'
   existing.write_bytes(b'preserve-existing')
   for target,size,reason in ((small,'11776','outside supported range'),(existing,'16384','must be NEW')):
    with self.subTest(size=size), patch('sys.argv',['build.sh','--image',str(target),'--size-mib',size,'--install','--work-dir',str(work)]), patch('build.inputs',return_value={}), patch('build.os.geteuid',return_value=1000), patch('build.shutil.which',return_value='/fixture/tool'):
     with self.assertRaisesRegex(ValueError,reason):build.main()
   self.assertFalse(small.exists());self.assertEqual(existing.read_bytes(),b'preserve-existing')
 def test_installer_sizes_data_to_actual_capacity(self):
  from build import partition_plan
  for capacity in (11777*1024**2,16*1024**3,20*1024**3,16*1024**3+512):
   with self.subTest(capacity=capacity):
    parts=partition_plan(capacity,1024,10240,512)
    self.assertEqual([(p['start'],p['sectors']) for p in parts[:2]],[(2048,2097152),(2099200,20971520)])
    self.assertEqual((parts[2]['start']+parts[2]['sectors'])*512,capacity)
  for capacity in (11777*1024**2-512,16*1024**3+1,2**42):
   with self.assertRaises(ValueError):partition_plan(capacity,1024,10240,512)
 def test_invalid_role_map_refused(self):
  for mapping in ({}, {'lan0':'52:54:00:00:00:01','wan0':'52:54:00:00:00:01'}, {'lan0':'ff:ff:ff:ff:ff:ff','wan0':'52:54:00:00:00:02'}, {'lan0':'52:54:00:00:00:01,foo=bar','wan0':'52:54:00:00:00:02'}):
   with self.subTest(mapping=mapping),self.assertRaises(ValueError):roles(mapping)
 def test_existing_image_never_overwritten(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'disk';p.write_bytes(b'preserve')
   with self.assertRaises(FileExistsError):
    with opened(p,create_bytes=999):pass
   self.assertEqual(p.read_bytes(),b'preserve')
 def test_symlink_refused(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'disk';p.write_bytes(b'preserve');link=Path(tmp)/'link';link.symlink_to(p)
   with self.assertRaises(OSError):
    with opened(link):pass
 def test_physical_backend_is_explicit(self):
  self.assertIn('file.driver=host_device', ' '.join(drive(4,5,True)))
  self.assertIn('file.driver=file,', ' '.join(drive(4,5,False)))
 def fixture(self):
  m={'format':2,'target_bytes':8192*512,'mbr_id':'12345678','partitions':[]};mbr=bytearray(512);mbr[440:444]=bytes.fromhex(m['mbr_id']);mbr[510:]=b'\x55\xaa';blocks={0:bytes(mbr)}
  for i in range(3):
   p={'uuid':str(uuid.uuid4()),'start':2048+i*2048,'sectors':2048};m['partitions'].append(p)
   mbr[450+i*16]=0x83;struct.pack_into('<II',mbr,454+i*16,p['start'],p['sectors'])
   sb=bytearray(1024);struct.pack_into('<I',sb,4,1024);sb[56:58]=b'\x53\xef';sb[58:60]=b'\x01\x00';sb[104:120]=uuid.UUID(p['uuid']).bytes;blocks[p['start']*512+1024]=bytes(sb)
  blocks[0]=bytes(mbr);return m,blocks
 def test_metadata_only_checks(self):
  m,blocks=self.fixture()
  with patch('devices.os.pread',side_effect=lambda fd,n,off:blocks[off]) as read:
   self.assertTrue(layout(99,m,8192*512));self.assertEqual(sum(c.args[1] for c in read.call_args_list),3584)
 def test_unclean_and_wrong_identity_refused(self):
  for mutation in ('dirty','wrong-uuid','journal','overlap','oversize','partial-filesystem','old-format','changed-capacity'):
   with self.subTest(mutation=mutation):
    m,b=self.fixture();off=m['partitions'][2]['start']*512+1024;s=bytearray(b[off]);table=bytearray(b[0])
    if mutation=='dirty':s[58]=0
    if mutation=='wrong-uuid':s[104]^=255
    if mutation=='journal':s[96]=4
    if mutation=='overlap':struct.pack_into('<I',table,454+32,2048)
    if mutation=='oversize':struct.pack_into('<I',table,458+32,100000)
    if mutation=='partial-filesystem':struct.pack_into('<I',s,4,512)
    if mutation=='old-format':m['format']=1
    if mutation=='changed-capacity':m['target_bytes']+=512
    b[off]=bytes(s);b[0]=bytes(table)
    with patch('devices.os.pread',side_effect=lambda fd,n,off:b[off]):
     with self.assertRaises(ValueError):layout(99,m,8192*512)
if __name__=='__main__':unittest.main()
