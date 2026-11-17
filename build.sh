#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$root/tools/build.py" "$@"
