#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Normal-user orchestrator; all formatting/install operations occur in QEMU."""
import argparse,fcntl,hashlib,json,os,shlex,shutil,subprocess,sys,uuid
from pathlib import Path
from fetch import inputs,sha,ROOT
from console import Console
from devices import inspect,opened,drive,layout,roles

def qpath(path):return str(path).replace(',',',,')
def put(path,data):
 temporary=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
 with temporary.open('x') as stream:
  stream.write(json.dumps(data,indent=2)+'\n');stream.flush();os.fsync(stream.fileno())
 temporary.replace(path)
def share(tag,path,ro=False):return ['-virtfs',f'local,path={str(path).replace(",",",,")},mount_tag={tag},security_model=none'+(',readonly=on' if ro else '')]
def snapshot(path):
 path.mkdir(mode=0o755)
 for name in (ROOT/'SOURCE_FILES').read_text().splitlines():
  relative=Path(name)
  if name!='LICENSE' and relative.parts[0] not in ('src','recipes','patches','builder','configs'):continue
  source=ROOT/relative;destination=path/relative
  if relative.is_absolute() or '..' in relative.parts or source.resolve()!=source or not source.is_file() or not source.resolve().is_relative_to(ROOT):raise ValueError('invalid source snapshot path')
  destination.parent.mkdir(parents=True,exist_ok=True)
  shutil.copyfile(source,destination);shutil.copymode(source,destination)
 for directory in path.rglob('*'):
  if directory.is_dir():directory.chmod(0o755)
def sums(folder):
 (folder/'SHA256SUMS').write_text(''.join(sha(p)+'  '+str(p.relative_to(folder))+'\n' for p in sorted(folder.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
def machine(accel,memory=6144):return ['qemu-system-x86_64','-machine','pc','-accel',accel,'-cpu',('host' if accel=='kvm' else 'max'),'-m',str(memory),'-smp','2','-display','none','-serial','stdio','-monitor','none','-nic','user,model=virtio-net-pci']
def builder_boot(work):
 record=json.loads((work/'builder.json').read_text())
 exports=work/record.get('stage','bootstrap')/'exports'
 root=(exports/'builder-root-uuid').read_text().strip()
 uuid.UUID(root)
 return ['-kernel',str(exports/'vmlinuz-linux'),'-initrd',str(exports/'initramfs-linux.img'),'-append','root=UUID='+root+' rw console=ttyS0,115200 systemd.mask=serial-getty@ttyS0.service']
def core_sha(root,name):
 # Output-copy mechanics do not affect compiled artifacts; all build steps do.
 if name=='recipes/openwrt/build.sh':
  data=(root/name).read_bytes().split(b'# Preserve a private reconstruction cache')[0]
  return hashlib.sha256(data).hexdigest()
 return sha(root/name)

def compiled_cache(job):
 job=job.resolve(strict=True)
 source=job/'inputs/project';compiled=job/'exports/compiled'
 required=['configs/versions.json','recipes/openwrt/build.sh','recipes/openwrt/prepare_core.py','src/arch-run.c','src/kmod-entry.c','src/shutdown-final.c']
 required += [str(p.relative_to(ROOT)) for p in sorted((ROOT/'patches').glob('*.patch'))]
 for name in required:
  if core_sha(source,name)!=core_sha(ROOT,name):raise ValueError('compiled build source differs: '+name)
 files={}
 for p in sorted(compiled.rglob('*')):
  if p.is_symlink():raise ValueError('symlink in compiled cache')
  if p.is_file():files[str(p.relative_to(compiled))]=sha(p)
 if not all(name in files for name in ('apk','libapk.so.3.0.0','artifacts/arch-run','artifacts/shutdown-final','artifacts/signing.pem')):raise ValueError('incomplete compiled cache')
 return compiled,{'source_sha256':{name:core_sha(ROOT,name) for name in required},'files':files}

def bootstrap(work,cache,lock,accel):
 record=work/'builder.json'
 key=hashlib.sha256((sha(ROOT/'configs/versions.json')+sha(ROOT/'builder/bootstrap.sh')+sha(ROOT/'builder/lab-client.sh')).encode()).hexdigest()
 base=work/('builder-'+key[:16]+'.img');stage=work/('bootstrap-'+key[:16]);stamp=stage/'builder.json'
 if stamp.exists():
  value=json.loads(stamp.read_text())
  if value.get('key')!=key or not value.get('success') or not base.is_file():raise ValueError('invalid cached builder record')
  put(record,value);return base
 if base.exists() or stage.exists():raise ValueError('unfinished builder found: retain diagnostics and use a new --work-dir')
 stage.mkdir(mode=0o700);project=stage/'project';snapshot(project);exports=stage/'exports';exports.mkdir()
 with base.open('xb') as f:f.truncate(32*1024**3)
 for name in ('vmlinuz-linux','initramfs-linux.img'):
  with (stage/name).open('xb') as f:subprocess.run(['bsdtar','-xOf',str(cache/'arch.iso'),'arch/boot/x86_64/'+name],stdout=f,check=True)
 cmd=machine(accel)+['-kernel',str(stage/'vmlinuz-linux'),'-initrd',str(stage/'initramfs-linux.img'),'-append','archisobasedir=arch archisosearchuuid='+lock['arch']['iso_uuid']+' console=ttyS0,115200 systemd.unit=multi-user.target cow_spacesize=4G','-drive',f'file={qpath(cache / "arch.iso")},media=cdrom,readonly=on','-drive',f'file={qpath(base)},format=raw,if=none,id=builder','-device','virtio-blk-pci,drive=builder,serial=fwr-builder']+share('inputs',stage,True)+share('exports',exports)
 vm=Console(cmd,stage/'serial.log')
 try:
  vm.expect(b'archiso login:');vm.send('root');vm.expect(rb'@archiso[^\n]*# ')
  vm.command('mkdir -p /inputs /exports; mount -t 9p -o trans=virtio,version=9p2000.L,ro inputs /inputs; mount -t 9p -o trans=virtio,version=9p2000.L exports /exports')
  vm.command('bash /inputs/project/builder/bootstrap.sh',timeout=7200)
  vm.send('sync; systemctl poweroff');vm.finish()
 except BaseException:
  if vm.process.poll() is None:
   try:
    vm.send('sync; systemctl poweroff');vm.finish(timeout=60)
   except Exception:
    vm.process.terminate();vm.process.wait(timeout=30)
  raise
 value={'success':True,'key':key,'bytes':base.stat().st_size,'public_inputs_verified':True,'path':base.name,'stage':stage.name}
 put(stamp,value);put(record,value)
 return base

def partition_plan(capacity,boot_mib,arch_mib,min_data_mib):
 minimum=(1+boot_mib+arch_mib+min_data_mib)*1024**2
 if capacity%512 or capacity<minimum or capacity//512>0xffffffff:raise ValueError('target capacity outside supported layout')
 parts=[];start=2048
 for sectors in (boot_mib*2048,arch_mib*2048,capacity//512-2048-(boot_mib+arch_mib)*2048):
  parts.append({'uuid':str(uuid.uuid4()),'start':start,'sectors':sectors});start+=sectors
 return parts

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 g=ap.add_mutually_exclusive_group();g.add_argument('--device',type=Path);g.add_argument('--image',type=Path)
 ap.add_argument('--install',action='store_true');ap.add_argument('--confirm-serial');ap.add_argument('--fetch-only',action='store_true')
 ap.add_argument('--reuse-build',type=Path,help='explicitly reuse a previous local job with matching core sources');ap.add_argument('--work-dir',type=Path,default=ROOT/'.work');ap.add_argument('--boot-mib',type=int,default=1024);ap.add_argument('--arch-mib',type=int,default=10240);ap.add_argument('--min-data-mib',type=int,default=512,help='minimum required data space; partition uses all remaining capacity');ap.add_argument('--size-mib',type=int);ap.add_argument('--interfaces',type=Path);ap.add_argument('--tcg',action='store_true')
 a=ap.parse_args();os.umask(0o077)
 if os.geteuid()==0:ap.error('run as normal host user; guest performs privileged installation')
 if a.device and a.size_mib is not None:ap.error('--size-mib applies only to new regular images')
 if not a.fetch_only and (not a.install or not(a.device or a.image)):ap.error('select --image or --device and explicitly request --install')
 if not(768<=a.boot_mib<=2048 and 4096<=a.arch_mib<=65536 and 256<=a.min_data_mib<=4096):ap.error('unsupported partition sizes')
 for command in ['qemu-system-x86_64','qemu-img','bsdtar','gpg']+(['lsblk','fuser'] if a.device else []):
  if not shutil.which(command):raise ValueError('missing host dependency: '+command)
 work=a.work_dir.absolute();work.mkdir(parents=True,exist_ok=True,mode=0o700)
 with (work/'build.lock').open('a') as guard:
  fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
  cache=work/'cache';lock=inputs(cache)
  if a.fetch_only:return
  target=(a.device or a.image).absolute();minimum=(1+a.boot_mib+a.arch_mib+a.min_data_mib)*1024**2
  ident=inspect(target,minimum) if a.device else None
  if a.device:
   print(json.dumps(ident,indent=2));serial=str(ident['serial'] or '').strip()
   if not serial:raise ValueError('device has no serial; unsupported automatic identity guard')
   if a.confirm_serial!=serial:
    if not sys.stdin.isatty() or input('ERASE this whole device? Type its serial: ').strip()!=serial:raise ValueError('explicit target confirmation required')
   capacity=int(ident['size'])
  else:
   if target.exists() or target.is_symlink():raise ValueError('image output must be NEW')
   capacity=(a.size_mib*1024**2) if a.size_mib else minimum
   if capacity<minimum or capacity//512>0xffffffff:raise ValueError('image size outside supported range')
  accel='tcg' if a.tcg or not os.access('/dev/kvm',os.R_OK|os.W_OK) else 'kvm'
  interfaces=roles(json.loads(a.interfaces.read_text()) if a.interfaces else {'lan0':'52:54:00:72:00:01','wan0':'52:54:00:72:00:02'})
  base=bootstrap(work,cache,lock,accel)
  job=work/'jobs'/str(uuid.uuid4());job.mkdir(parents=True,mode=0o700);stage=job/'inputs';stage.mkdir(mode=0o755);snapshot(stage/'project');exports=job/'exports';exports.mkdir(mode=0o755)
  manifest={'format':2,'integration':'frarchenstein-wrt-1','install_id':str(uuid.uuid4()),'mbr_id':os.urandom(4).hex(),'target_bytes':capacity,'format_authorized':True,'interfaces':interfaces,'partitions':[]}
  manifest['partitions']=partition_plan(capacity,a.boot_mib,a.arch_mib,a.min_data_mib)
  put(stage/'install.json',manifest);(stage/'repository-snapshot').write_text(lock['arch']['snapshot']+'\n');put(stage/'kernel-evidence.json',{'release':lock['arch']['kernel'],'vmlinuz_sha256':lock['arch']['kernel_sha256'],'signature_verified':True})
  reused,cache_record=compiled_cache(a.reuse_build) if a.reuse_build else (None,None)
  if cache_record:put(stage/'reuse.json',cache_record)
  sums(stage)
  # Builder copy is disposable; target SD is never present during bootstrap.
  scratch=job/'builder.qcow2';subprocess.run(['qemu-img','create','-f','qcow2','-F','raw','-b',str(base),str(scratch)],check=True)
  report={'success':False,'physical':bool(a.device),'target':str(target),'manifest':manifest,'whole_card_hashing':False,'reused_local_build':bool(reused)}
  with opened(target,physical=bool(a.device),create_bytes=None if a.device else capacity) as fd:
   if a.device:
    # fuser would observe our own descriptor; compare stable sysfs/lsblk identity directly.
    now=json.loads(subprocess.check_output(['lsblk','-dnJb','-o','SIZE,SERIAL,MODEL,MAJ:MIN',str(target)],text=True))['blockdevices'][0]
    if any(now[k]!=ident[k] for k in ('size','serial','model','maj:min')):raise ValueError('target changed after confirmation')
   ro=os.open(f'/proc/self/fd/{fd}',os.O_RDONLY)
   cmd=machine(accel)+builder_boot(work)+['-drive',f'file={qpath(scratch)},format=qcow2,if=none,id=builder','-device','virtio-blk-pci,drive=builder,serial=fwr-builder']+drive(fd,ro,bool(a.device))+share('inputs',stage,True)+share('downloads',cache,True)+share('exports',exports)
   if reused:cmd+=share('compiled',reused,True)
   vm=None
   try:
    vm=Console(cmd,job/'serial.log',pass_fds=(fd,ro));vm.expect(b'FWR_JOB_SUCCESS|FWR_JOB_FAILED',timeout=14400);vm.finish()
    if not json.loads((exports/'job.json').read_text())['success']:raise ValueError('guest installation failed')
    os.fsync(fd);layout(fd,manifest,capacity)
    report.update(success=True,acceptance=json.loads((exports/'acceptance.json').read_text()))
    put(work/'last-install.json',report);put(job/'installation.json',report)
   finally:
    os.close(ro);put(job/'report.json',report)
    if vm and vm.process.poll() is None:
     vm.process.terminate();vm.process.wait(timeout=30)
     print('Failed builder stopped; target requires a new installation or offline recovery.',file=sys.stderr)
  print('INSTALLATION PASSED. Manifest:',job/'installation.json')
  print('Boot with',shlex.join(['./start.sh','--device' if a.device else '--image',str(target),'--manifest',str(job/'installation.json'),'--work-dir',str(work)]))
if __name__=='__main__':
 try:main()
 except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as e:print('build:',e,file=sys.stderr);sys.exit(1)
