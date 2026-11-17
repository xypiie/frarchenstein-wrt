#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euxo pipefail
umask 022
mkdir -p /work/sdk /work/artifacts/keys /work/artifacts/packages /work/apk-source
chmod 755 /work/sdk /work/artifacts
chown builder:builder /work/sdk
runuser -u builder -- tar --zstd -xf /downloads/sdk.tar.zst -C /work/sdk --strip-components=1
mkdir -p /work/sdk/dl
for source in /downloads/*.tar.zst; do
 [[ $source == /downloads/sdk.tar.zst ]] && continue
 install -m644 "$source" /work/sdk/dl/
done
chown -R builder:builder /work/sdk/dl
# SDK ships exact immutable feed revisions; verify before contacting upstream.
python3 - <<'PY'
import json
from pathlib import Path
lock=json.load(open('/inputs/project/configs/versions.json'))
s=Path('/work/sdk/feeds.conf.default').read_text()
if ('^'+lock['openwrt']['base_commit']) not in s:raise SystemExit('SDK base feed pin changed')
PY
runuser -u builder -- bash -c 'cd /work/sdk && ./scripts/feeds update base && ./scripts/feeds install -p base ubox fstools procd base-files'
expected=$(python3 -c "import json;print(json.load(open('/inputs/project/configs/versions.json'))['openwrt']['base_commit'])")
[[ $(runuser -u builder -- git -C /work/sdk/feeds/base rev-parse HEAD) == "$expected" ]]
runuser -u builder -- python3 /inputs/project/recipes/openwrt/prepare_core.py --sdk /work/sdk
runuser -u builder -- openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out /work/sdk/frarchenstein-signing.pem
chmod 600 /work/sdk/frarchenstein-signing.pem
cat > /work/sdk/.config <<'EOF'
CONFIG_ALL=n
CONFIG_ALL_KMODS=n
CONFIG_ALL_NONSHARED=n
CONFIG_PACKAGE_ubox=y
CONFIG_PACKAGE_fstools=y
CONFIG_PACKAGE_procd=y
CONFIG_PACKAGE_procd-ujail=y
CONFIG_PACKAGE_procd-seccomp=y
CONFIG_PACKAGE_base-files=y
CONFIG_SIGNED_PACKAGES=y
CONFIG_USE_APK=y
EOF
chown builder:builder /work/sdk/.config
runuser -u builder -- bash -c 'cd /work/sdk && make defconfig && make -j2 package/feeds/base/ubox/compile package/feeds/base/fstools/compile package/feeds/base/procd/compile package/feeds/base/base-files/compile V=0'
python3 - <<'PYTHON'
import re,shutil
from pathlib import Path
for owner in ('ubox','fstools','procd','procd-ujail','procd-seccomp','base-files'):
 matches=[p for p in Path('/work/sdk/bin').rglob(owner+'-*.apk') if re.match(re.escape(owner)+r'-[0-9]',p.name)]
 if len(matches)!=1:raise SystemExit('missing/ambiguous owner '+owner)
 shutil.copyfile(matches[0],Path('/work/artifacts/packages')/matches[0].name)
PYTHON
cp /work/sdk/frarchenstein-signing.pem /work/artifacts/signing.pem
openssl pkey -in /work/artifacts/signing.pem -pubout -out /work/artifacts/keys/frarchenstein-build.pem
compiler=$(find /work/sdk/staging_dir -path '*toolchain*/bin/x86_64-openwrt-linux-musl-gcc' -print -quit)
[[ -x $compiler ]]
"$compiler" -std=c11 -Os -Wall -Wextra -Werror /inputs/project/src/arch-run.c -o /work/artifacts/arch-run
cc -static -no-pie -std=c11 -Os -Wall -Wextra -Werror /inputs/project/src/shutdown-final.c -o /work/artifacts/shutdown-final
# Build native APK index/package tooling from pinned source, never import a binary.
tar --zstd -xf /downloads/apk-3.0.5.tar.zst -C /work/apk-source --strip-components=1
meson setup /work/apk-native /work/apk-source -Ddocs=disabled -Dhelp=disabled -Dlua=disabled -Dpython=disabled -Dtests=disabled -Durl_backend=wget -Dzstd=disabled
ninja -C /work/apk-native
cp /work/apk-native/src/apk /work/apk-native/src/libapk.so.3.0.0 /work/
/work/apk-build --version 2>/dev/null || ln -s apk /work/apk-build
/work/apk-build --version
for p in /work/artifacts/packages/*.apk; do /work/apk-build --keys-dir /work/artifacts/keys verify "$p"; done
cp /work/sdk/frarchenstein-core-recipes.json /exports/core-recipes.json
cp /work/sdk/.config /exports/sdk.config
# Preserve a private reconstruction cache and diagnostics before target mutation.
mkdir -p /exports/compiled
cp -R --preserve=mode /work/artifacts /exports/compiled/
cp /work/apk /work/libapk.so.3.0.0 /exports/compiled/
