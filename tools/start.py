#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Boot an installed router and an isolated LAN client; WAN uses outbound NAT."""
import argparse,http.client,json,os,shlex,socket,ssl,struct,subprocess,sys,tempfile,threading,time,uuid
from pathlib import Path
from fetch import ROOT
from devices import inspect,opened,drive,layout,roles as checked_roles
from console import Console
from build import builder_boot

def escaped(path):return str(path).replace(',',',,')
class QMP:
 def __init__(self,path,process):
  self.events=[];self.socket=socket.socket(socket.AF_UNIX);self.socket.settimeout(1)
  for _ in range(100):
   try:self.socket.connect(str(path));break
   except (FileNotFoundError,ConnectionRefusedError):
    if process.poll() is not None:
     detail=process.stdout.read().decode(errors='replace') if process.stdout else ''
     raise RuntimeError('QEMU exited before QMP connected: '+detail)
    time.sleep(.1)
  else:raise TimeoutError('QMP socket unavailable')
  self.socket.settimeout(None);self.reader=self.socket.makefile('rb');json.loads(self.reader.readline())
  self.send('qmp_capabilities');self.thread=threading.Thread(target=self.read,daemon=True);self.thread.start()
 def read(self):
  for line in self.reader:
   try:self.events.append(json.loads(line))
   except ValueError:pass
 def send(self,command):self.socket.sendall(json.dumps({'execute':command}).encode()+b'\n')
 def clean(self):
  self.thread.join(timeout=2)
  return any(e.get('event')=='SHUTDOWN' and e.get('data',{}).get('reason')=='guest-shutdown' for e in self.events)
 def close(self):self.socket.close()

def main():
 ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group(required=True)
 g.add_argument('--device',type=Path);g.add_argument('--image',type=Path)
 ap.add_argument('--manifest',type=Path,required=True);ap.add_argument('--work-dir',type=Path,default=ROOT/'.work');ap.add_argument('--tcg',action='store_true')
 ap.add_argument('--ssh-port',type=int,default=2222);ap.add_argument('--http-port',type=int,default=8080);ap.add_argument('--https-port',type=int,default=8443)
 ap.add_argument('--smoke-test',action='store_true',help='automated disposable-file acceptance; unavailable for physical devices')
 a=ap.parse_args();os.umask(0o077)
 if os.geteuid()==0:ap.error('run QEMU as normal host user')
 if a.smoke_test and a.device:ap.error('automated tests use regular files only')
 if any(not 1024<=p<=65535 for p in (a.ssh_port,a.http_port,a.https_port)) or len({a.ssh_port,a.http_port,a.https_port})!=3:ap.error('choose three distinct unprivileged ports')
 report=json.loads(a.manifest.read_text())
 if not report.get('success'):raise ValueError('requires a successful installation manifest')
 manifest=report['manifest'];target=(a.device or a.image).absolute();work=a.work_dir.absolute()
 if not (work/'builder.json').is_file():raise ValueError('build the disposable Arch builder first')
 base=work/json.loads((work/'builder.json').read_text()).get('path','builder.img')
 if not base.is_file():raise ValueError('build the disposable Arch builder first')
 ident=inspect(target) if a.device else None
 if ident:print(json.dumps(ident,indent=2))
 run=work/'runs'/str(uuid.uuid4());run.mkdir(parents=True,mode=0o700)
 state={'clean_shutdown':False,'physical':bool(a.device),'whole_card_hashing':False,
        'target':str(target),'install_id':manifest['install_id'],'smoke_test':a.smoke_test}
 accel='tcg' if a.tcg or not os.access('/dev/kvm',os.R_OK|os.W_OK) else 'kvm'
 client=None;router=None;qmp=None;console=None;client_log=None
 # Short private socket path avoids UNIX path length limits and host LAN bridges.
 with tempfile.TemporaryDirectory(prefix='fwr-net-') as sockets, opened(target,physical=bool(a.device)) as fd:
  capacity=int(ident['size']) if ident else os.fstat(fd).st_size
  layout(fd,manifest,capacity)
  original_mbr=os.pread(fd,512,0)
  ro=os.open(f'/proc/self/fd/{fd}',os.O_RDONLY)
  try:
   scratch=run/'client.qcow2';subprocess.run(['qemu-img','create','-f','qcow2','-F','raw','-b',str(base),str(scratch)],check=True)
   lan=Path(sockets)/'lan.sock';control=Path(sockets)/'router.qmp';client_control=Path(sockets)/'client.qmp'
   common=['qemu-system-x86_64','-machine','pc','-accel',accel,'-cpu',('host' if accel=='kvm' else 'max'),'-display','none','-monitor','none','-no-reboot']
   client_boot=builder_boot(work);client_boot[-1]+=' systemd.mask=systemd-networkd-wait-online.service'
   client_cmd=common+client_boot+['-m','1024','-smp','2','-serial','stdio','-qmp',f'unix:{client_control},server=on,wait=off','-fw_cfg','name=opt/fwr/mode,string=client','-drive',f'file={escaped(scratch)},format=qcow2,if=virtio','-netdev',f'stream,id=lan,server=on,addr.type=unix,addr.path={lan}','-device','virtio-net-pci,netdev=lan,mac=52:54:00:77:00:10','-netdev',f'user,id=management,net=10.0.3.0/24,ipv6=off,restrict=on,hostfwd=tcp:127.0.0.1:{a.ssh_port}-10.0.3.15:2222,hostfwd=tcp:127.0.0.1:{a.http_port}-10.0.3.15:8080,hostfwd=tcp:127.0.0.1:{a.https_port}-10.0.3.15:8443','-device','virtio-net-pci,netdev=management,mac=52:54:00:77:00:11']
   client_log=(run/'client.log').open('xb');client=subprocess.Popen(client_cmd,stdin=subprocess.DEVNULL,stdout=client_log,stderr=subprocess.STDOUT)
   for _ in range(100):
    if lan.exists():break
    if client.poll() is not None:raise RuntimeError('LAN client failed; inspect client.log')
    time.sleep(.1)
   else:raise TimeoutError('LAN socket not ready')
   wan='user,id=wan,net=10.0.2.0/24,ipv4=on,ipv6=on'
   wan_port=None
   if a.smoke_test:
    with socket.socket() as probe:
     probe.bind(('127.0.0.1',0));wan_port=probe.getsockname()[1]
    wan+=f',hostfwd=tcp:127.0.0.1:{wan_port}-10.0.2.15:22'
   roles=checked_roles(manifest['interfaces'])
   cmd=common+['-m','2048','-smp','2','-qmp',f'unix:{control},server=on,wait=off','-chardev','stdio,id=console,signal=off','-serial','chardev:console']+drive(fd,ro,bool(a.device))+['-netdev',f'stream,id=lan,server=off,addr.type=unix,addr.path={lan},reconnect-ms=250','-device',f'virtio-net-pci,netdev=lan,mac={roles["lan0"]}','-netdev',wan,'-device',f'virtio-net-pci,netdev=wan,mac={roles["wan0"]}']
   print(f'LAN: http://127.0.0.1:{a.http_port}/  SSH: ssh -p {a.ssh_port} root@127.0.0.1',flush=True)
   print('WAN has Internet access. Press Enter for serial console. Finish with: sync; poweroff',flush=True)
   print('Private run evidence:',run,flush=True)
   if a.smoke_test:
    console=Console(cmd,run/'router.log',pass_fds=(fd,ro));router=console.process
   else:router=subprocess.Popen(cmd,pass_fds=(fd,ro))
   qmp=QMP(control,router)
   if a.smoke_test:
    console.expect(b'Please press Enter to activate this console',timeout=300);console.send('');console.expect(rb'root@[^\n]*# ')
    time.sleep(10)
    for attempt in range(3):
     console.send('')
     try:
      console.command('/usr/libexec/frarchenstein/arch-run /usr/bin/stty -echo',timeout=10);break
     except TimeoutError:
      if attempt==2:raise
    ready="import json,time\nfor i in range(90):\n if json.load(open('/proc/1/root/overlay/initialization.json'))['state']=='complete': break\n time.sleep(1)\nelse: raise RuntimeError('initialization incomplete')"
    console.command('/usr/libexec/frarchenstein/arch-run /usr/bin/python3 -c '+shlex.quote(ready),timeout=100)

    console.command("test $(cat /proc/1/comm) = procd && uname -r && ubus call system board && mount | grep ' /overlay ' && fw4 check",timeout=120)
    result=console.command("if test -f /etc/fwr-test; then test $(cat /etc/fwr-test) = fwr-persistence && printf '%s%s\\n' FWR_PERSISTENCE _PASS; else echo fwr-persistence > /etc/fwr-test; fi; sync")
    state['persistence_verified']=b'FWR_PERSISTENCE_PASS' in result
    if state['persistence_verified']:
     console.command('sha256sum -c /etc/fwr-network.sha256; test $(uci get system.@system[0].hostname) = frarchenstein-test')
     state['administrator_configuration_preserved']=True
    else:
     console.command("echo '# retained administrator change' >> /etc/config/network; sha256sum /etc/config/network > /etc/fwr-network.sha256; uci set system.@system[0].hostname=frarchenstein-test; uci commit system")
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
     if b'FWR_LAN_INTERNET_PASS' in (run/'client.log').read_bytes():break
     if client.poll() is not None:raise RuntimeError('LAN client exited')
     time.sleep(1)
    else:raise RuntimeError('LAN DHCP/DNS/NAT Internet check failed; inspect client.log')
    console.command("grep -q '52:54:00:77:00:10' /tmp/dhcp.leases; nslookup downloads.openwrt.org 192.168.1.1",timeout=60)
    state['router_dhcp_dns_verified']=True
    with socket.create_connection(('127.0.0.1',a.ssh_port),timeout=5) as connection:
     if not connection.recv(256).startswith(b'SSH-2.0-'):raise RuntimeError('LAN SSH banner missing')
    state['lan_ssh_verified']=True
    http_connection=http.client.HTTPConnection('127.0.0.1',a.http_port,timeout=5)
    try:
     http_connection.request('GET','/');response=http_connection.getresponse()
     if response.status not in (200,301,302,303):raise RuntimeError('LuCI HTTP unavailable')
    finally:http_connection.close()
    state['luci_http_verified']=True
    https_connection=http.client.HTTPSConnection('127.0.0.1',a.https_port,timeout=10,context=ssl._create_unverified_context())
    try:
     # The clean guest creates its own certificate; verify TLS/service availability.
     https_connection.request('GET','/');response=https_connection.getresponse()
     if response.status not in (200,301,302,303):raise RuntimeError('LuCI HTTPS unavailable')
    finally:https_connection.close()
    state['luci_https_verified']=True
    console.command('modprobe -v nf_conntrack; modinfo -F vermagic nf_conntrack | grep -F "$(uname -r)"',timeout=30)
    state['direct_module_delegation_verified']=True
    with socket.create_connection(('127.0.0.1',wan_port),timeout=5) as connection:
     connection.settimeout(3)
     try:banner=connection.recv(256)
     except (TimeoutError,ConnectionResetError):banner=b''
     if banner:raise RuntimeError('WAN SSH unexpectedly reachable')
    state['wan_ssh_blocked']=True
    owners='frarchenstein-wrt frarchenstein-arch-kmods ubox fstools procd procd-ujail procd-seccomp base-files'
    result=console.command('apk fix --reinstall '+owners,timeout=180)
    if result.count(b'Reinstalling ')<8:raise RuntimeError('APK did not execute all eight owner reinstalls')
    state['owner_reinstalls_verified']=True
    console.command("sha256sum /lib/apk/db/installed /etc/apk/world > /tmp/fwr-apk-before; if apk add 'ubox=2025.10.30~6f78fa49-r1'; then false; else sha256sum -c /tmp/fwr-apk-before; fi",timeout=120)
    state['stock_owner_refused_before_mutation']=True
    console.command("test $(cat /etc/fwr-test) = fwr-persistence; sync")
    console.send('sync; poweroff');console.finish()
   else:router.wait()
   if router.returncode!=0 or not qmp.clean():raise RuntimeError('clean guest poweroff was not observed; inspect/recover offline before retry')
   os.fsync(fd);layout(fd,manifest,capacity)
   if os.pread(fd,512,0)!=original_mbr:raise ValueError('boot changed the partition table')
   state['partition_table_unchanged']=True
   if a.smoke_test:
    state['readonly_filesystems_verified']=True
    table=os.pread(fd,512,0);start,count=struct.unpack_from('<II',table,486)
    sb=os.pread(fd,1024,start*512+1024);blocksize=1024<<struct.unpack_from('<I',sb,24)[0]
    blocks=struct.unpack_from('<I',sb,4)[0]
    if struct.unpack_from('<I',sb,96)[0]&0x80:blocks+=struct.unpack_from('<I',sb,336)[0]<<32
    if count!=capacity//512-start or blocks*blocksize!=(count*512//blocksize)*blocksize:raise ValueError('installed partition or ext4 does not fill media capacity')
    state.update(capacity_bytes=capacity,data_partition_sectors=count,data_filesystem_bytes=blocks*blocksize)
   state.update(clean_shutdown=True,lan_internet_verified=b'FWR_LAN_INTERNET_PASS' in (run/'client.log').read_bytes())
   print('Clean shutdown and filesystem metadata checks passed. Safe to disconnect.',flush=True)
  finally:
   # Only terminate processes created by this invocation. Never touch another VM.
   if router and router.poll() is None and console:
    try:
     console.send('sync; poweroff');console.finish(timeout=60)
    except Exception:pass
   if router and router.poll() is None:
    print('Stopping this failed run; disk requires offline filesystem recovery.',file=sys.stderr)
    router.terminate();router.wait(timeout=30)
   if qmp:qmp.close()
   if client and client.poll() is None:client.terminate();client.wait(timeout=30)
   if client_log:client_log.close()
   os.close(ro);(run/'report.json').write_text(json.dumps(state,indent=2)+'\n')
if __name__=='__main__':
 try:main()
 except (OSError,ValueError,RuntimeError,subprocess.SubprocessError,KeyboardInterrupt) as e:print('start:',e,file=sys.stderr);sys.exit(1)
