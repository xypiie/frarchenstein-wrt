# SPDX-License-Identifier: GPL-2.0-only
"""Visible serial automation for disposable builder VMs."""
import os,re,select,subprocess,sys,time,uuid
class Console:
 def __init__(self,command,log,pass_fds=()):
  self.log=open(log,'xb');self.buffer=b''
  self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,pass_fds=pass_fds)
 def send(self,line):self.process.stdin.write(line.encode()+b'\n');self.process.stdin.flush()
 def expect(self,pattern,timeout=300):
  pattern=re.compile(pattern);end=time.monotonic()+timeout
  while True:
   m=pattern.search(self.buffer)
   if m:
    data,self.buffer=self.buffer[:m.end()],self.buffer[m.end():];return data
   if time.monotonic()>end:raise TimeoutError('serial timeout: '+repr(pattern.pattern))
   if select.select([self.process.stdout],[],[],1)[0]:
    data=os.read(self.process.stdout.fileno(),65536)
    if not data:raise RuntimeError('QEMU exited before expected marker')
    self.log.write(data);self.log.flush();sys.stdout.buffer.write(data);sys.stdout.buffer.flush();self.buffer=(self.buffer+data.replace(b'\r',b''))[-262144:]
 def command(self,line,timeout=300):
  tag=uuid.uuid4().hex
  self.send("( set -e; "+line+" ); r=$?; printf '\\nFWR_%s:%s\\n' '"+tag+"' \"$r\"")
  out=self.expect(('\nFWR_'+tag+':(\\d+)\n').encode(),timeout)
  if not out.endswith(('FWR_'+tag+':0\n').encode()):raise RuntimeError('guest command failed; inspect log')
  return out
 def finish(self,timeout=180):
  self.expect(b'Power down|Powering off',timeout);self.process.wait(timeout=30)
  if self.process.returncode:raise RuntimeError('QEMU shutdown failed')
  self.log.close()
