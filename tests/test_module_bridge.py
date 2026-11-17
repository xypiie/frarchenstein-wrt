# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
from pathlib import Path
import types
import unittest
import shutil
import subprocess
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('hybrid_module_bridge', Path(__file__).resolve().parents[1] / 'src/module_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ModuleMapping(unittest.TestCase):
    def setUp(self):
        self.policy = {'module_root': '/', 'excluded': ['button_hotplug'],
                       'builtin_equivalents': {'dma_shared_buffer': 'CONFIG_DMA_SHARED_BUFFER=y'},
                       'module_aliases': {'crc32c_generic': 'crc32c-cryptoapi', 'nf_flow_table_hw': 'nf_flow_table'}}

    def test_legacy_names_reach_real_arch_modules(self):
        for old, new in self.policy['module_aliases'].items():
            with self.subTest(module=old), patch.object(bridge.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as execute:
                self.assertEqual(bridge.probe(old, [], Path('/run/fixture'), self.policy, set()), 0)
                self.assertEqual(execute.call_args.args[0][-2:], ['--', new])

    def test_probe_uses_a_real_kmod_entrypoint(self):
        # Exercise argument dispatch in real kmod, without loading any module.
        if not shutil.which('modprobe'):
            self.skipTest('kmod is not available')
        with patch.object(bridge.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as execute:
            bridge.probe('dummy', [], Path('/run/fixture'), self.policy, set())
            executable = execute.call_args.args[0][0]
        result = subprocess.run([executable, '--version'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('kmod', result.stdout)

    def test_excluded_and_verified_builtin_do_not_load_modules(self):
        with patch.object(bridge.subprocess, 'run') as execute:
            with self.assertRaisesRegex(ValueError, 'excluded'):
                bridge.probe('button-hotplug', [], Path('/run/fixture'), self.policy, set())
            self.assertEqual(bridge.probe('dma-shared-buffer', [], Path('/run/fixture'), self.policy, set()), 0)
            with self.assertRaisesRegex(ValueError, 'options'):
                bridge.probe('dma-shared-buffer', ['unexpected=1'], Path('/run/fixture'), self.policy, set())
            execute.assert_not_called()

    def test_unknown_names_are_not_successful_noops(self):
        with patch.object(bridge.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)) as execute:
            self.assertEqual(bridge.probe('missing-module', [], Path('/run/fixture'), self.policy, set()), 1)
            self.assertEqual(execute.call_args.args[0][-1], 'missing-module')
