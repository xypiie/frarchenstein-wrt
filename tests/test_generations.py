# SPDX-License-Identifier: GPL-2.0-only
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(source))
spec = importlib.util.spec_from_file_location('hybrid_generations', source / 'generations.py')
generations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generations)


class GenerationPackageGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / 'candidate'
        self.current = self.root / 'running/rom'
        self.records = []
        for name in generations.CORE:
            record = f'P:{name}\nV:1.0-r1\nD:reviewed-core=1.0-r1\n'
            if name == 'procd':
                record += 'F:sbin\nR:procd\n'
            self.records.append(record)
        for root in [self.candidate, self.current]:
            (root / 'lib/apk/db').mkdir(parents=True)
            (root / 'lib/apk/db/installed').write_text('\n'.join(self.records))
            (root / 'etc/apk').mkdir(parents=True)
            (root / 'etc/apk/world').write_text('frarchenstein-wrt><Q1fixture-checksum\n')
            (root / 'sbin').mkdir()
            (root / 'sbin/procd').write_bytes(b'reviewed procd payload')
        self.selection = {'protected_paths': {'/sbin/procd': hashlib.sha256(b'reviewed procd payload').hexdigest()},
                          'protected_links': {}}
        self.scope = patch.object(generations, 'ROOT', self.root / 'running')
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def test_checksum_pinned_and_plain_explicit_world_are_supported(self):
        generations.validate_lower(self.candidate, self.selection)
        (self.candidate / 'etc/apk/world').write_text('frarchenstein-wrt\n')
        generations.validate_lower(self.candidate, self.selection)

    def test_missing_explicit_integration_is_rejected(self):
        (self.candidate / 'etc/apk/world').write_text('procd\n')
        with self.assertRaisesRegex(ValueError, 'explicitly installed'):
            generations.validate_lower(self.candidate, self.selection)

    def test_core_dependency_protection_cannot_change(self):
        database = self.candidate / 'lib/apk/db/installed'
        database.write_text(database.read_text().replace('P:frarchenstein-wrt\nV:1.0-r1\nD:reviewed-core=1.0-r1',
                                                       'P:frarchenstein-wrt\nV:1.0-r1\nD:reviewed-core'))
        with self.assertRaisesRegex(ValueError, 'dependency protection'):
            generations.validate_lower(self.candidate, self.selection)

    def test_incomplete_initialization_cannot_be_selected(self):
        gen = '25.12.6-' + '0' * 64
        data = self.root / 'generations'
        directory = data / gen
        (directory / 'upper').mkdir(parents=True)
        (directory / 'work').mkdir()
        (directory / 'generation.json').write_text(json.dumps({'generation': gen, 'integration': generations.REVISION}))
        with patch.object(generations, 'DATA', data):
            with self.assertRaises(FileNotFoundError):
                generations.check_upper(gen, self.selection)
            state = directory / 'initialization.json'
            state.write_text(json.dumps({'generation': gen, 'state': 'prepared'}))
            generations.check_upper(gen, self.selection)
            state.write_text(json.dumps({'generation': 'other', 'state': 'complete'}))
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                generations.check_upper(gen, self.selection)
