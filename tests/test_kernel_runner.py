# SPDX-License-Identifier: GPL-2.0-only
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import review_kernel as runner


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'reviewed-kernels.json'
        self.record = {'integration': runner.REVISION, 'package_version': '7.0-1',
                       'vmlinuz_sha256': 'a'*64, 'policy': {'builtin_equivalents': {},
                       'module_aliases': {}, 'excluded': ['button_hotplug']}, 'startup_modules': ['ext4']}
        self.original = (json.dumps({'7.0': self.record}) + '\n').encode()
        self.path.write_bytes(self.original)
        self.candidate = {'release':'7.1', 'baseline':'7.0',
                          'record':dict(self.record,package_version='7.1-1',vmlinuz_sha256='b'*64),
                          'checks':dict.fromkeys(runner.REQUIRED_GUEST,True),
                          'package_sha256':'c'*64,'config_sha256':'d'*64,'required_config':{}}
        self.boots = [{'result':dict(dict.fromkeys(runner.REQUIRED_BOOT,True),persistence_verified=True)} for _ in range(4)]

    def publish(self):
        return runner.publish(self.path,self.original,self.candidate,self.boots,{'fixture':'hash'})

    def test_every_guest_gate_is_required_before_modification(self):
        with patch.object(runner,'sources',return_value={'fixture':'hash'}):
            for gate in runner.REQUIRED_GUEST:
                self.candidate['checks'][gate]=False
                with self.assertRaises(ValueError):self.publish()
                self.assertEqual(self.path.read_bytes(),self.original)
                self.candidate['checks'][gate]=True

    def test_boot_failure_and_missing_reboot_prevent_publication(self):
        with patch.object(runner,'sources',return_value={'fixture':'hash'}):
            for gate in runner.REQUIRED_BOOT:
                self.boots[2]['result'][gate]=False
                with self.assertRaises(ValueError):self.publish()
                self.assertEqual(self.path.read_bytes(),self.original)
                self.boots[2]['result'][gate]=True
            self.boots.pop()
            with self.assertRaises(ValueError):self.publish()
            self.assertEqual(self.path.read_bytes(),self.original)

    def test_source_or_review_change_refuses_overwrite(self):
        with patch.object(runner,'sources',return_value={'fixture':'changed'}),self.assertRaises(ValueError):self.publish()
        with patch.object(runner,'sources',return_value={'fixture':'hash'}):
            self.path.write_bytes(b'concurrent edit')
            with self.assertRaises(ValueError):self.publish()
            self.assertEqual(self.path.read_bytes(),b'concurrent edit')

    def test_serial_prompt_prefix_does_not_hide_guest_report(self):
        console=Mock()
        console.command.return_value=b'root@router:~# FWR_KERNEL_REVIEW_RESULT={"release":"7.1"}\nFWR_done:0\n'
        def smoke(guest_check,after_smoke):
            guest_check(console,{})
            return {},Path('/tmp/evidence')
        with patch.object(runner,'upload'),patch.object(runner.start,'main',side_effect=smoke):
            candidate,_=runner.trial(Path('/tmp/image'),Path('/tmp/manifest'),
                                     Path('/tmp/work'),{},'boot_a',None,False,(2222,8080,8443))
        self.assertEqual(candidate['release'],'7.1')

    def test_success_preserves_existing_entries_and_limits_scope(self):
        with patch.object(runner,'sources',return_value={'fixture':'hash'}):self.publish()
        value=json.loads(self.path.read_text())
        self.assertEqual(value['7.0'],self.record)
        self.assertEqual(value['7.1']['validation']['scope'],'qemu-x86_64-sata-virtio-net')
        self.assertFalse(value['7.1']['validation']['hardware_verified'])


if __name__=='__main__':unittest.main()
