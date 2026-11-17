#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Apply reviewed core-owner recipes inside a disposable SDK checkout."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil

PINS = {
    'ubox': '04932a8c0add12bc020bd152834cbca5c845db09159e0ff20ec3e38b87a88b24',
    'fstools': '0323d0b0435a81024c1bd58c55eca12e72fe14c57d41b60a129993c334b1aeda',
    'procd': '633679cf59d55995a2a38e4cc7ec7dca1b3f279d273a84cce44c05d91d5d0f39',
    'base-files': '08103be9602a2a6b4b20d13e2fa5ee646100f03b1356efd3e6d5eaf2a02a9009',
}


def once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('unexpected source anchor: ' + before)
    return text.replace(before, after, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', required=True, type=Path)
    args = parser.parse_args()
    sdk = args.sdk.resolve(strict=True)
    here = Path(__file__).resolve().parent
    records = []
    pack = sdk / 'include/package-pack.mk'
    before = pack.read_text()
    after = once(before, '$(STAGING_DIR_HOST)/bin/apk mkpkg',
                 '$(STAGING_DIR_HOST)/bin/apk mkpkg --sign-key $(TOPDIR)/frarchenstein-signing.pem')
    pack.write_text(after)
    records.append({'build_interface': 'include/package-pack.mk',
                    'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
                    'after_sha256': hashlib.sha256(after.encode()).hexdigest()})
    for owner, expected in PINS.items():
        directory = (sdk / 'package/feeds/base' / owner).resolve(strict=True)
        if not directory.is_relative_to(sdk):
            raise ValueError('package recipe escapes SDK')
        recipe = directory / 'Makefile'
        original = recipe.read_bytes()
        if hashlib.sha256(original).hexdigest() != expected:
            raise ValueError('recipe differs from audited release: ' + owner)
        text = original.decode()
        if owner != 'base-files':
            text = once(text, 'PKG_RELEASE:=1\n', 'PKG_RELEASE:=' + ('1003' if owner == 'procd' else '1001') + '\n')
            patches = directory / 'patches'
            patches.mkdir(exist_ok=True)
            destination = patches / '900-frarchenstein-wrt.patch'
            if destination.exists():
                raise ValueError('refusing existing integration patch')
            shutil.copyfile(here.parents[1] / 'patches' / (owner + '.patch'), destination)
        else:
            text = once(text, 'PKG_RELEASE:=$(COMMITCOUNT)', 'PKG_RELEASE:=1718.1004')
            start = text.index('define Package/base-files/install\n')
            end = text.index('\nendef', start)
            text = text[:end] + '\n\t$(CP) ./frarchenstein-files/. $(1)/\n\trm -f $(1)/rom/note' + text[end:]
            files = directory / 'frarchenstein-files'
            files.mkdir()
            def write(name, content, mode=0o755):
                path = files / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                path.chmod(mode)
            denial = ('#!/bin/sh\n'
                      'echo "frarchenstein: native firmware/reset commands are disabled; use the reviewed offline generation workflow" >&2\n'
                      'exit 2\n')
            for path in ['sbin/sysupgrade', 'sbin/firstboot', 'usr/libexec/validate_firmware_image', 'etc/rc.button/reset']:
                write(path, denial)
            write('lib/upgrade/platform.sh', 'platform_check_image() { return 1; }\nplatform_do_upgrade() { return 1; }\nplatform_copy_config() { return 1; }\n', 0o644)
            original_preinit = (directory / 'files/lib/preinit/80_mount_root').read_text()
            # This dedicated hybrid preinit never replays native sysupgrade archives.
            write('lib/preinit/80_mount_root', '''# Hybrid root is prepared by OpenWrt, not the Arch initramfs.
do_mount_root() {
    mount_root start || exit 1
    boot_run_hook preinit_mount_root
}
[ "$INITRAMFS" = "1" ] && exit 1
boot_hook_add preinit_main do_mount_root
''', 0o644)
            if 'boot_hook_add preinit_main do_mount_root' not in original_preinit:
                raise ValueError('unrecognized root preinit hook')
            write('lib/preinit/79_move_config', '# Native firmware configuration-archive restoration is disabled in this layout.\n', 0o644)
            # Generic upstream hooks may return 1 for an absent optional NIC.
            # Mandatory mount failures exit explicitly; normal completion must
            # report the validated overlay handoff, not the last optional test.
            preinit = (directory / 'files/etc/preinit').read_text()
            # The upstream install recipe substitutes this template before our
            # overlay is copied. Resolve the same reviewed target PATH here.
            preinit, count = re.subn(r'^export PATH=.*$',
                                    'export PATH="/usr/sbin:/usr/bin:/sbin:/bin"',
                                    preinit, flags=re.MULTILINE)
            if count != 1:
                raise ValueError('unrecognized preinit PATH template')
            if not preinit.rstrip().endswith('boot_run_hook preinit_main'):
                raise ValueError('unrecognized preinit completion')
            write('etc/preinit', preinit + '\n[ -r /run/frarchenstein/overlay-ready ] || exit 1\nexit 0\n')
            write('etc/init.d/umount', '''#!/bin/sh /etc/rc.common
STOP=90
restart() { :; }
stop() { /usr/libexec/frarchenstein/shutdown-mounts; }
''')
        recipe.write_text(text)
        records.append({'owner': owner, 'upstream_recipe_sha256': expected,
                        'custom_recipe_sha256': hashlib.sha256(text.encode()).hexdigest()})
    (sdk / 'frarchenstein-core-recipes.json').write_text(json.dumps(records, indent=2) + '\n')


if __name__ == '__main__':
    main()
