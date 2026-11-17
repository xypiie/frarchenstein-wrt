#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# This runs only in an ephemeral builder-disk overlay. No router/card mounts.
rm -f /etc/systemd/network/30-builder.network
cat > /etc/systemd/network/10-lab-lan.network <<'EOF'
[Match]
MACAddress=52:54:00:77:00:10
[Network]
DHCP=ipv4
[DHCPv4]
UseDNS=yes
UseRoutes=yes
EOF
cat > /etc/systemd/network/10-lab-management.network <<'EOF'
[Match]
MACAddress=52:54:00:77:00:11
[Network]
Address=10.0.3.15/24
DHCP=no
EOF
networkctl reload
for device in /sys/class/net/*; do
 case "$(cat "$device/address")" in
  52:54:00:77:00:10|52:54:00:77:00:11) networkctl reconfigure "${device##*/}" ;;
 esac
done
for n in {1..30}; do
 ip -4 address | grep -q '10.0.3.15/24' && break
 sleep 1
done
ip -4 address | grep -q '10.0.3.15/24'
# Management NIC has NO default route; Internet traffic must traverse OpenWrt.
for spec in '2222 22' '8080 80' '8443 443'; do
 read -r listen target <<< "$spec"
 socat "TCP4-LISTEN:$listen,bind=10.0.3.15,reuseaddr,fork" "TCP:192.168.1.1:$target" &
done
for n in {1..90}; do
 if ip -4 route | grep -q '^default via 192.168.1.1 '; then break; fi
 sleep 1
done
ip -4 address
ip -4 route
if curl --fail --max-time 30 --head https://downloads.openwrt.org/; then echo FWR_LAN_INTERNET_PASS; fi
wait
