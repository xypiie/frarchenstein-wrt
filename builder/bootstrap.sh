#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
[[ $(id -u) == 0 && -d /run/archiso ]]
disk=/dev/disk/by-id/virtio-fwr-builder
[[ -b $disk && $(blockdev --getsize64 "$disk") -ge 34359738368 ]]
[[ -z $(lsblk -nr -o MOUNTPOINTS "$disk" | tr -d '[:space:]') ]]
# The host creates this bootstrap disk exclusively; it never attaches an SD here.
python3 - "$disk" <<'PY'
import sys
with open(sys.argv[1],'rb') as f:
 if any(f.read(1048576)):raise SystemExit('builder bootstrap disk is not blank')
PY
printf 'label: dos\nstart=2048,type=83,bootable\n' | sfdisk "$disk"
udevadm settle
part=/dev/disk/by-id/virtio-fwr-builder-part1
mkfs.ext4 -F -O '^64bit,^metadata_csum,^orphan_file' "$part"
mount "$part" /mnt
snapshot=$(python3 -c "import json;print(json.load(open('/inputs/project/configs/versions.json'))['arch']['snapshot'])")
sed "s|Include = /etc/pacman.d/mirrorlist|Server = https://archive.archlinux.org/repos/$snapshot/\$repo/os/\$arch|" /etc/pacman.conf > /tmp/fwr-pacman.conf
pacstrap -K -C /tmp/fwr-pacman.conf /mnt base linux linux-firmware arch-install-scripts base-devel python git cmake ninja meson rsync unzip wget curl openssl zlib zstd squashfs-tools syslinux socat gnupg
mkdir -p /mnt/boot/syslinux /mnt/etc/systemd/network /mnt/etc/systemd/system /mnt/opt/frarchenstein /mnt/work /mnt/inputs /mnt/downloads /mnt/exports
uuid=$(blkid -s UUID -o value "$part")
printf 'UUID=%s / ext4 defaults 0 1\n' "$uuid" > /mnt/etc/fstab
printf 'frarchenstein-builder\n' > /mnt/etc/hostname
printf '[Match]\nName=en* eth*\n[Network]\nDHCP=yes\n' > /mnt/etc/systemd/network/30-builder.network
arch-chroot /mnt systemctl enable systemd-networkd systemd-resolved
ln -sf /run/systemd/resolve/stub-resolv.conf /mnt/etc/resolv.conf
# Generic guest dispatcher: executable code arrives via an explicit read-only share.
cat > /mnt/opt/frarchenstein/dispatch.sh <<'EOF'
#!/bin/bash
set -euo pipefail
modprobe qemu_fw_cfg || true
mode=/sys/firmware/qemu_fw_cfg/by_name/opt/fwr/mode/raw
if [[ -f $mode && $(cat "$mode") == client ]]; then
 exec /opt/frarchenstein/lab-client.sh
fi
mount -t 9p -o trans=virtio,version=9p2000.L,ro inputs /inputs
mount -t 9p -o trans=virtio,version=9p2000.L,ro downloads /downloads
mount -t 9p -o trans=virtio,version=9p2000.L exports /exports
exec bash /inputs/project/builder/job.sh
EOF
cp /inputs/project/builder/lab-client.sh /mnt/opt/frarchenstein/lab-client.sh
chmod 755 /mnt/opt/frarchenstein/*.sh
cat > /mnt/etc/systemd/system/frarchenstein-builder.service <<'EOF'
[Unit]
Description=Frarchenstein disposable guest build/install job
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/opt/frarchenstein/dispatch.sh
StandardOutput=tty
StandardError=tty
TTYPath=/dev/ttyS0
TimeoutStartSec=infinity
[Install]
WantedBy=multi-user.target
EOF
arch-chroot /mnt systemctl enable frarchenstein-builder.service
arch-chroot /mnt mkinitcpio -P
cp /mnt/usr/lib/syslinux/bios/{ldlinux.c32,libutil.c32} /mnt/boot/syslinux/
printf 'DEFAULT arch\nPROMPT 0\nTIMEOUT 10\nLABEL arch\n LINUX /boot/vmlinuz-linux\n INITRD /boot/initramfs-linux.img\n APPEND root=UUID=%s rw console=ttyS0,115200\n' "$uuid" > /mnt/boot/syslinux/extlinux.conf
extlinux --install /mnt/boot/syslinux
# Exactly the BIOS boot-code bytes, preserving the partition table.
dd if=/mnt/usr/lib/syslinux/bios/mbr.bin of="$disk" bs=440 count=1 conv=notrunc status=none
cp /mnt/boot/{vmlinuz-linux,initramfs-linux.img} /exports/
printf '%s\n' "$uuid" > /exports/builder-root-uuid
arch-chroot /mnt pacman -Q > /exports/builder-packages.txt
sync
umount -R /mnt
e2fsck -fn "$part"
printf 'FWR_BOOTSTRAP_SUCCESS\n'
