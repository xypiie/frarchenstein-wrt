# SPDX-License-Identifier: GPL-2.0-only
import copy
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('hybrid_runtime', Path(__file__).resolve().parents[1] / 'src/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class LayoutGuards(unittest.TestCase):
    def setUp(self):
        self.manifest = {'mbr_id': '12345678', 'target_bytes': 62333952 * 512, 'partitions': [
            {'start': 2048, 'sectors': 2097152},
            {'start': 2099200, 'sectors': 20971520},
            {'start': 23070720, 'sectors': 62333952 - 23070720}]}
        self.mbr = bytearray(512)
        self.mbr[440:444] = bytes.fromhex(self.manifest['mbr_id'])
        self.mbr[510:] = b'\x55\xaa'
        self.kernel = []
        for i, p in enumerate(self.manifest['partitions']):
            self.mbr[450 + i * 16] = 0x83
            struct.pack_into('<II', self.mbr, 454 + i * 16, p['start'], p['sectors'])
            self.kernel.append((p['start'], p['sectors']))

    def check(self):
        runtime.validate_mbr(self.manifest, bytes(self.mbr), 62333952, self.kernel)

    def test_installed_layout_and_resize_refusal(self):
        self.check()
        for difference in (-1, 1):
            size = self.manifest['partitions'][2]['sectors'] + difference
            struct.pack_into('<I', self.mbr, 490, size)
            self.kernel[2] = (self.kernel[2][0], size)
            with self.assertRaisesRegex(ValueError, 'size changed'):
                self.check()

    def test_stale_kernel_view_requires_reboot(self):
        struct.pack_into('<I', self.mbr, 490, self.kernel[2][1] - 1)
        with self.assertRaisesRegex(ValueError, 'reboot required'):
            self.check()

    def test_other_partitions_cannot_change(self):
        for i in [0, 1]:
            with self.subTest(partition=i + 1):
                mbr = self.mbr.copy()
                kernel = self.kernel.copy()
                struct.pack_into('<I', mbr, 458 + 16 * i, kernel[i][1] - 1)
                kernel[i] = (kernel[i][0], kernel[i][1] - 1)
                with self.assertRaisesRegex(ValueError, 'size changed'):
                    runtime.validate_mbr(self.manifest, bytes(mbr), 62333952, kernel)

    def test_extra_partition_or_wrong_disk_rejected(self):
        for offset in [440, 498, 510]:
            with self.subTest(offset=offset):
                mbr = self.mbr.copy()
                mbr[offset] ^= 1
                with self.assertRaises(ValueError):
                    runtime.validate_mbr(self.manifest, bytes(mbr), 62333952, self.kernel)

    def test_data_shrink_rejected(self):
        start, size = self.kernel[2]
        struct.pack_into('<I', self.mbr, 490, size - 1)
        self.kernel[2] = (start, size - 1)
        with self.assertRaisesRegex(ValueError, 'size changed'):
            self.check()

    def test_overlap_and_media_overrun_rejected(self):
        with self.assertRaises(ValueError):
            runtime.validate_mbr(self.manifest, bytes(self.mbr), 23070720, self.kernel)
        changed = copy.deepcopy(self.manifest)
        changed['partitions'][2]['start'] = self.kernel[1][0]
        struct.pack_into('<I', self.mbr, 486, self.kernel[1][0])
        self.kernel[2] = (self.kernel[1][0], self.kernel[2][1])
        with self.assertRaisesRegex(ValueError, 'overlap'):
            runtime.validate_mbr(changed, bytes(self.mbr), 62333952, self.kernel)


class SelectorGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.boot = Path(self.temp.name)
        blob = self.boot / 'image'
        blob.write_bytes(b'fixture lower image')
        self.digest = runtime.sha(blob)
        self.gen = '25.12.6-' + self.digest
        self.slot = self.boot / 'openwrt/slots' / self.gen
        self.slot.mkdir(parents=True)
        blob.rename(self.slot / 'rootfs.squashfs')
        self.manifest = {'generation': self.gen, 'sha256': self.digest, 'integration': runtime.REVISION}
        (self.slot / 'manifest.json').write_text(json.dumps(self.manifest))
        self.selector = self.boot / 'openwrt/current'
        self.selector.symlink_to('slots/' + self.gen)

    def test_exact_generation_accepted(self):
        self.assertEqual(runtime.checked_generation(self.boot)[0], self.gen)

    def test_traversal_and_absolute_selector_rejected(self):
        for target in ['../slots/' + self.gen, '/slots/' + self.gen, 'slots/../' + self.gen]:
            self.selector.unlink()
            self.selector.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'invalid generation selector'):
                runtime.checked_generation(self.boot)

    def test_corrupt_lower_and_manifest_rejected(self):
        (self.slot / 'rootfs.squashfs').write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            runtime.checked_generation(self.boot)
        self.manifest['generation'] = 'wrong'
        (self.slot / 'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'identity'):
            runtime.checked_generation(self.boot)

class ProtectedUpperGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.upper = Path(self.temp.name) / 'upper'
        (self.upper / 'sbin').mkdir(parents=True)
        self.file = self.upper / 'sbin/procd'
        self.file.write_bytes(b'reviewed package payload')
        self.selection = {'protected_paths': {'/sbin/procd': runtime.sha(self.file)},
                          'protected_links': {'/sbin/init': 'procd'}}
        (self.upper / 'sbin/init').symlink_to('procd')

    def test_exact_package_reinstall_is_accepted(self):
        runtime.check_protected_upper(self.upper, self.selection)

    def test_changed_package_or_symlink_is_rejected(self):
        self.file.write_bytes(b'incompatible upstream payload')
        with self.assertRaisesRegex(ValueError, 'shadows'):
            runtime.check_protected_upper(self.upper, self.selection)
        self.file.write_bytes(b'reviewed package payload')
        (self.upper / 'sbin/init').unlink()
        (self.upper / 'sbin/init').symlink_to('/bin/sh')
        with self.assertRaisesRegex(ValueError, 'shadows'):
            runtime.check_protected_upper(self.upper, self.selection)

    def test_symlinked_parent_cannot_hide_unreviewed_payload(self):
        (self.upper / 'sbin').rename(self.upper / 'other')
        (self.upper / 'sbin').symlink_to('other')
        with self.assertRaisesRegex(ValueError, 'ancestor'):
            runtime.check_protected_upper(self.upper, self.selection)
