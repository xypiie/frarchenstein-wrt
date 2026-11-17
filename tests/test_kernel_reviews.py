# SPDX-License-Identifier: GPL-2.0-only
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import kernel_reviews as reviews


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.url = 'https://example.invalid/configs/reviewed-kernels.json'
        self.record = {'integration': reviews.REVISION, 'package_version': '7.0-1',
                       'vmlinuz_sha256': 'a' * 64,
                       'policy': {'builtin_equivalents': {}, 'module_aliases': {}, 'excluded': ['button_hotplug']},
                       'startup_modules': ['ext4']}
        self.config, self.bundled, self.cache = root/'config', root/'bundled', root/'cache'
        self.config.write_text(json.dumps({'url': self.url}))
        self.bundled.write_text(json.dumps({'7.0': self.record}))
        for name, path in [('CONFIG', self.config), ('BUNDLED', self.bundled), ('CACHE', self.cache)]:
            context = patch.object(reviews, name, path)
            context.start(); self.addCleanup(context.stop)

    def test_downloads_repository_file_directly_and_uses_offline_cache(self):
        new = {'7.1': dict(self.record, package_version='7.1-1')}
        with patch.object(reviews, 'download', return_value=json.dumps(new).encode()) as download:
            self.assertEqual(reviews.for_version('7.1-1', refresh=True)[0], '7.1')
            download.assert_called_once_with(self.url)
        with patch.object(reviews, 'download', side_effect=urllib.error.URLError('offline')):
            self.assertEqual(reviews.for_version('7.1-1', refresh=True)[0], '7.1')
            self.assertEqual(reviews.for_version('7.0-1', refresh=True)[0], '7.0')

    def test_disabled_source_never_contacts_network(self):
        self.config.write_text('{"url":null}')
        with patch.object(reviews, 'download') as network:
            self.assertEqual(reviews.for_version('7.0-1', refresh=True)[0], '7.0')
            with self.assertRaisesRegex(ValueError, 'no valid compatibility review'):
                reviews.for_version('7.1-1', refresh=True)
            network.assert_not_called()

    def test_unavailable_source_falls_back_and_unknown_kernel_is_refused(self):
        with patch.object(reviews, 'download', side_effect=urllib.error.URLError('offline')):
            self.assertEqual(reviews.for_version('7.0-1', refresh=True)[0], '7.0')
            with self.assertRaisesRegex(ValueError, 'no valid compatibility review'):
                reviews.for_version('7.1-1', refresh=True)

    def test_bad_download_does_not_replace_cache(self):
        original=json.dumps({'url':self.url,'reviews':{'7.0':self.record}})
        self.cache.write_text(original)
        for payload in [b'not json', b'{}', json.dumps({'7.0':dict(self.record,integration='wrong')}).encode(),
                        json.dumps({'7.0':dict(self.record,vmlinuz_sha256='bad')}).encode()]:
            with patch.object(reviews, 'download', return_value=payload), self.assertRaises(ValueError):
                reviews.load_reviews(refresh=True)
            self.assertEqual(self.cache.read_text(),original)

    def test_repository_change_does_not_reuse_other_repository_cache(self):
        self.cache.write_text(json.dumps({'url':'https://old.invalid/reviews.json',
                                         'reviews':{'7.1':dict(self.record,package_version='7.1-1')}}))
        with self.assertRaisesRegex(ValueError, 'no valid compatibility review'):
            reviews.for_version('7.1-1')

    def test_https_and_redirect_certificate_policy(self):
        for url in ['http://example.invalid/review','file:///tmp/review','https://user@example.invalid/review']:
            with self.assertRaises(ValueError):reviews.https_url(url)
        with self.assertRaises(ValueError):
            reviews.HTTPSRedirect().redirect_request(None,None,302,'',{},'http://example.invalid/review')


if __name__=='__main__':unittest.main()
