#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Reinstallation must never reset administrator configuration.
[ -e /etc/frarchenstein/network-initialized ] && exit 0
set -e
cat > /etc/config/network.frarchenstein-new <<'EOF'
config interface 'loopback'
 option device 'lo'
 option proto 'static'
 option ipaddr '127.0.0.1'
 option netmask '255.0.0.0'
config device
 option name 'br-lan'
 option type 'bridge'
 list ports 'lan0'
config interface 'lan'
 option device 'br-lan'
 option proto 'static'
 option ipaddr '192.168.1.1'
 option netmask '255.255.255.0'
 option ip6assign '60'
config interface 'wan'
 option device 'wan0'
 option proto 'dhcp'
config interface 'wan6'
 option device 'wan0'
 option proto 'dhcpv6'
EOF
mv /etc/config/network.frarchenstein-new /etc/config/network
if [ "$(uci -q get system.@system[0].hostname)" = OpenWrt ]; then
 uci set system.@system[0].hostname='frarchenstein-wrt'
 uci commit system
fi
mkdir -p /etc/frarchenstein
printf 'initialized\n' > /etc/frarchenstein/network-initialized
exit 0
