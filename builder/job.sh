#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
umask 022
[[ $(id -u) == 0 && -f /inputs/install.json ]]
[[ $(cat /sys/class/dmi/id/sys_vendor) == QEMU ]]
finish() {
 status=$?
 if ((status)); then printf 'FWR_JOB_FAILED exit=%s\n' "$status"; printf '{"success":false,"exit":%s}\n' "$status" > /exports/job.json; fi
 sync
 systemctl poweroff
}
trap finish EXIT
# Private diagnostics survive failures without exposing credentials in source.
(while true; do ps -eo pid,ppid,stat,wchan:24,args > /exports/processes.txt; sleep 10; done) &
cd /inputs
sha256sum -c SHA256SUMS
job=$(python3 -c "import json;print(json.load(open('/inputs/install.json'))['install_id'])")
mkdir -p /var/lib/frarchenstein/jobs
[[ ! -e /var/lib/frarchenstein/jobs/$job ]]
printf 'started\n' > "/var/lib/frarchenstein/jobs/$job"
# Never use an inherited account/environment or host compiler.
id builder >/dev/null 2>&1 || useradd -m -d /work/builder builder
chmod 755 /work
for name in arch openwrt data integration-files capability-files arch-package arch-signing; do
 [[ ! -e /work/$name ]] || { echo "Stale /work/$name: rebuild the disposable builder before a new install"; exit 1; }
done
export LD_LIBRARY_PATH=/work
if [[ -f /inputs/reuse.json ]]; then
 mkdir /compiled
 mount -t 9p -o trans=virtio,version=9p2000.L,ro compiled /compiled
 python3 /inputs/project/builder/reuse.py
else
 bash /inputs/project/recipes/openwrt/build.sh
fi
python3 /inputs/project/builder/install.py
printf 'complete\n' > "/var/lib/frarchenstein/jobs/$job"
printf '{"success":true}\n' > /exports/job.json
printf 'FWR_JOB_SUCCESS\n'
