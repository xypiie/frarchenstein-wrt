#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Kernel reviews trusted through configured HTTPS; no default repository."""
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import sys

from runtime import REVISION, atomic_json, read_json

CONFIG = Path('/etc/frarchenstein/kernel-reviews.json')
BUNDLED = Path('/usr/share/frarchenstein/reviewed-kernels.json')
CACHE = Path('/var/lib/frarchenstein/kernel-reviews.json')
LIMIT = 2 * 1024 * 1024


def https_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('kernel review downloads require an HTTPS URL without credentials')
    return url


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download(url):
    opener = urllib.request.build_opener(HTTPSRedirect())
    with opener.open(https_url(url), timeout=30) as response:
        https_url(response.geturl())
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('kernel review download exceeds size limit')
    return data


def records(value):
    if not isinstance(value, dict) or not value:
        raise ValueError('empty/malformed kernel reviews')
    for release, record in value.items():
        if not re.fullmatch(r'[A-Za-z0-9._+-]+', release):
            raise ValueError('invalid reviewed kernel release')
        if not isinstance(record, dict) or record.get('integration') != REVISION:
            raise ValueError('kernel review is for another integration contract')
        if not isinstance(record.get('package_version'), str):
            raise ValueError('kernel review lacks package version')
        if not re.fullmatch(r'[a-f0-9]{64}', record.get('vmlinuz_sha256', '')):
            raise ValueError('kernel review lacks kernel digest')
        policy, modules = record.get('policy'), record.get('startup_modules')
        if not isinstance(policy, dict) or not isinstance(modules, list):
            raise ValueError('kernel review lacks module policy')
        for field in ('builtin_equivalents', 'module_aliases'):
            mapping = policy.get(field)
            if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
                raise ValueError('invalid reviewed module mappings')
        if not isinstance(policy.get('excluded'), list):
            raise ValueError('invalid reviewed exclusions')
        if not all(isinstance(m, str) and re.fullmatch(r'[A-Za-z0-9_+-]+', m) for m in modules):
            raise ValueError('invalid reviewed module names')
        if any(m.replace('-', '_') == 'button_hotplug' for m in modules):
            raise ValueError('excluded module in review')
    versions = [record['package_version'] for record in value.values()]
    if len(versions) != len(set(versions)):
        raise ValueError('ambiguous reviewed package version')
    return value


def load_reviews(refresh=False):
    known = records(read_json(BUNDLED))
    config = read_json(CONFIG)
    url = config.get('url')
    if not url:
        return known
    https_url(url)
    if CACHE.exists():
        cache = read_json(CACHE)
        # Never carry trust across a configured repository change. Old signed
        # cache envelopes are not the current cache format and are ignored.
        if cache.get('url') == url and 'reviews' in cache:
            known.update(records(cache['reviews']))
    if not refresh:
        return records(known)
    try:
        current = records(json.loads(download(url)))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print('Kernel review download unavailable; using local reviews: ' + str(error), file=sys.stderr)
        return records(known)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(CACHE, {'url': url, 'reviews': current})
    known = records(read_json(BUNDLED))
    known.update(current)
    return records(known)


def for_version(version, refresh=False):
    matches = [(release, record) for release, record in load_reviews(refresh).items()
               if record['package_version'] == version]
    if len(matches) != 1:
        raise ValueError('kernel ' + version + ' has no valid compatibility review; no packages will be changed')
    return matches[0]
