# SPDX-License-Identifier: GPL-2.0-only
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import update


class PacmanTests(unittest.TestCase):
    def test_native_commands_keep_arguments(self):
        for args, mutates in [(['-Syu'], True), (['-S', '--needed', 'htop'], True),
                              (['-U', '/tmp/integration.pkg.tar.zst'], True),
                              (['-Rns', 'htop'], True), (['-Qk'], False),
                              (['-Ss', 'python'], False), (['-Si', 'linux'], False),
                              (['-Syuw'], False), (['--help'], False)]:
            self.assertEqual(update.pacman_request(args)[1:], (mutates, args))

    def test_partial_updates_and_bypasses_refused(self):
        for args in [['-Sy'], ['-Sy', 'linux'], ['-Syu', '--root', '/tmp/root'],
                     ['-Syu', '--hookdir=/tmp/hooks'], ['-Sdd', 'linux'],
                     ['-Syu', '--ignore=linux'], ['-D', '--asdeps', 'linux'],
                     ['-U', '--dbonly', 'fixture.pkg.tar.zst']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                update.pacman_request(args)

    def test_install_after_refused_upgrade_cannot_partially_update_system(self):
        with patch.object(update, 'authorized_hook'), patch.object(update, 'check_environment'), \
                patch.object(update, 'read_json', return_value={'pacman_args': ['-S', 'htop']}), \
                patch.object(update.sys, 'stdin', Mock(read=lambda: 'htop\n')), \
                patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='linux\nglibc\n')), \
                patch('apps.quiesce') as stop, patch.object(update, 'journal') as journal:
            with self.assertRaisesRegex(ValueError, 'partial upgrade'):
                update.hook_pre()
            stop.assert_not_called()
            journal.assert_not_called()

    def test_plan_never_refreshes_locked_database(self):
        args = update.plan_arguments(['-Syyu', '--needed', '--', 'linux'])
        self.assertEqual(args, ['-Su', '--needed', '--print', '--print-format',
                                '%n\t%v\t%l', '--', 'linux'])

    def test_query_calls_native_pacman_without_update_work(self):
        with patch.object(update, 'transaction') as transaction, patch.object(update.subprocess, 'call', return_value=0) as call:
            self.assertEqual(update.pacman_main(['-Q', 'linux']), 0)
            call.assert_called_once_with(['/usr/bin/pacman', '-Q', 'linux'])
            transaction.assert_not_called()

    def test_unreviewed_kernel_stops_before_apps_modules_or_journal(self):
        with patch.object(update, 'authorized_hook'), patch.object(update, 'check_environment'), \
                patch.object(update, 'read_json', return_value={'pacman_args': ['-Syu']}), \
                patch.object(update.sys, 'stdin', Mock(read=lambda: 'linux\n')), \
                patch.object(update, 'output', return_value='linux\t99.0-1\thttps://example.invalid/linux.pkg.tar.zst'), \
                patch('kernel_reviews.for_version', side_effect=ValueError('no valid compatibility review')), \
                patch('apps.quiesce') as stop, patch.object(update, 'preserve_running') as modules, \
                patch.object(update, 'journal') as journal:
            with self.assertRaisesRegex(ValueError, 'no valid compatibility review'):
                update.hook_pre()
            stop.assert_not_called()
            modules.assert_not_called()
            journal.assert_not_called()

    def test_reviewed_version_with_wrong_archive_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'linux.pkg.tar.zst'
            archive.write_bytes(b'fixture')
            record = {'vmlinuz_sha256': hashlib.sha256(b'approved kernel').hexdigest()}
            answers = ['linux\t7.0-1\t' + str(archive), 'linux 7.0-1', 'usr/lib/modules/7.0/vmlinuz']
            with patch.object(update, 'output', side_effect=answers), \
                    patch('kernel_reviews.for_version', return_value=('7.0', record)), \
                    patch.object(update.subprocess, 'check_output', return_value=b'changed kernel'), \
                    patch.object(update, 'atomic_json') as save:
                with self.assertRaisesRegex(ValueError, 'hash differs'):
                    update.review_transaction_kernel(['-U', str(archive)])
                save.assert_not_called()

    def test_successful_archive_review_records_exact_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'linux.pkg.tar.zst'
            archive.write_bytes(b'fixture')
            record = {'vmlinuz_sha256': hashlib.sha256(b'approved kernel').hexdigest()}
            answers = ['linux\t7.0-1\t' + str(archive), 'linux 7.0-1', 'usr/lib/modules/7.0/vmlinuz']
            with patch.object(update, 'output', side_effect=answers), \
                    patch('kernel_reviews.for_version', return_value=('7.0', record)), \
                    patch.object(update.subprocess, 'check_output', return_value=b'approved kernel'), \
                    patch.object(update, 'atomic_json') as save:
                update.review_transaction_kernel(['-U', str(archive)])
                save.assert_called_once_with(update.REVIEW, {'release': '7.0', 'evidence': record})

    def test_native_refusal_leaves_state_and_apps_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state.json'
            state.write_text('{"state":"ready"}')
            previous = state.read_bytes()
            with patch.object(update, 'STATE', state), patch.object(update, 'RUN', root), \
                    patch.object(update, 'REVIEW', root / 'review'), patch.object(update, 'check_environment'), \
                    patch.object(update.shutil, 'disk_usage', return_value=Mock(free=2**40)), \
                    patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
                    patch.object(update, 'prepare') as prepare, patch('apps.quiesce') as stop:
                self.assertEqual(update.transaction(['-Syu']), 1)
                self.assertEqual(state.read_bytes(), previous)
                self.assertFalse((root / 'update-owner.json').exists())
                prepare.assert_not_called()
                stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
