<p align="center"><img src="artwork/logo.svg" alt="frarchenstein-wrt" width="900"></p>

Happily vibe coded with [OpenAI Codex](https://openai.com/codex/).

**OpenWrt userspace on an Arch Linux kernel, with an Arch maintenance and
application chroot.** Source-only, GPL-2.0-only, initially targeting x86-64 BIOS
systems with 512-byte sectors.

This is an experimental distribution integration. See
[validation](docs/validation.md) for executed checks and remaining gates. It
installs distribution defaults; configure users, keys and applications after
first boot.

The Linux kernel comes from Arch. OpenWrt owns PID 1, networking, firewall,
DHCP/DNS, LuCI and its writable overlay. Arch is mounted at `/arch`; it does not
run a second systemd instance. [Architecture and recipes](docs/architecture.md)
explains the boundaries and update protections.

### Build a disposable installation first

Host requirements: Linux x86-64, Python 3.11+, Bash, QEMU system-x86 and
qemu-img (tested with 11.1.3; stream networking with `reconnect-ms` is
required), GnuPG, `bsdtar`, `lsblk`, `fuser`, network access and approximately
25 GiB of available disk space for a build. QEMU uses 6 GiB RAM and two virtual
CPUs while building. KVM is preferred; `--tcg` works more slowly. No host
compiler, loop devices, mounts or chroot are used.

On Arch the relevant host packages are
`qemu-system-x86 qemu-img python gnupg libarchive psmisc util-linux`. Install
host dependencies yourself if needed. Run these scripts as your normal user.

```sh
./build.sh --image .work/router-test.img --size-mib 16384 --install
./start.sh --image .work/router-test.img --manifest .work/last-install.json
```

The builder downloads and verifies the pinned Arch ISO, OpenWrt image and SDK.
It bootstraps a separate Arch builder disk, installs build dependencies,
rebuilds the patched upstream owners and compiles the wrappers. A disposable
copy of that builder installs the target disk and powers off. Guest serial
output is visible throughout. A fresh compilation creates fresh package signing
keys; keep the generated private output private and backed up.

An optional `--reuse-build .work/jobs/JOB_ID` reuses the compiled helpers from
that local job when all relevant core source hashes still match. Cached files
are hashed before transfer and rechecked in the guest; signed packages are
verified again. This retains that build's OpenWrt package-signing identity. It
never imports an installed router image. The default builds from source.

For an automated **regular-file-only** boot/network check:

```sh
./start.sh --image .work/router-test.img --manifest .work/last-install.json --smoke-test
```

### Install an SD card

**Installation erases the selected whole device.** Review `lsblk` and use the
current whole-device path. The card must already be accessible to your normal
host user; the scripts do not change permissions or invoke host privilege
escalation.

```sh
lsblk -o NAME,PATH,SIZE,MODEL,SERIAL,RM,MOUNTPOINTS
./build.sh --device /dev/sdX --install
./start.sh --device /dev/sdX --manifest .work/last-install.json
```

The installer displays the device identity and requires its serial to confirm
erasure. It rejects partitions, symlinks, non-removable media,
mounted/swap/held/busy devices and unsupported geometry. It rechecks identity,
holds an exclusive descriptor, and assigns the guest target a dedicated virtual
serial. Only the identified target is formatted inside QEMU.
`--confirm-serial SERIAL` supplies the same explicit confirmation for a scripted
run.

The default layout is aligned MBR with a 1 GiB ext4 boot partition, 10 GiB ext4
Arch partition, and an ext4 data partition using **all remaining target space**.
The installer creates the final partition and filesystem sizes before shutdown;
boot only validates and mounts them. Minimum media capacity is **11,777 MiB**,
including at least 512 MiB for data. `--boot-mib`, `--arch-mib` and
`--min-data-mib` customize these requirements. BIOS/MBR limits and supported
sector sizes remain explicit.

Run the installer against each target's actual capacity. Copying an installed
image onto larger media does not automatically expand it; create a new
installation for that media instead.

The installer writes files into the target filesystems; it does not first create
a full-card-sized image. Routine start/shutdown checks read only the MBR and
filesystem superblocks. Download signatures, package signatures and
integration-file hashes remain mandatory. Whole-card checksum readback is not
part of normal use.

### Configure your router

The launcher connects the router's `wan0` to QEMU outbound NAT. Its `lan0`
connects to a separate disposable Arch client. That client obtains its lease,
default route and DNS from OpenWrt; its second, restricted management adapter
forwards these host-local ports:

| Host address | Router service |
| --- | --- |
| `http://127.0.0.1:8080/` | LuCI HTTP |
| `https://127.0.0.1:8443/` | LuCI HTTPS |
| `ssh -p 2222 root@127.0.0.1` | SSH, after setting a password/key |

Press Enter at the serial console, run `passwd`, then configure through LuCI or
SSH. SSH host keys and LuCI certificates are generated for the new installation.
Add your own users, keys and applications afterward. Logs and installed disks
become private once you configure them.

Finish with `sync; poweroff` **inside the router**, and wait for the launcher's
clean-shutdown message before removing the card. An interrupted run requires
offline filesystem recovery. There are no host LAN bridges or production-network
DHCP announcements. WAN downloads can access the Internet; the isolated
laboratory LAN uses `192.168.1.0/24`.

Before actual router hardware use, provide the real NIC MAC mapping using
`--interfaces mapping.json` at installation, with `lan0` and `wan0` keys. Review
driver/firmware coverage and physical port roles separately. The default mapping
belongs to the QEMU lab. Wi-Fi and physical-router acceptance are separate
gates.

### Maintain the Arch side

From the OpenWrt root console, run `arch` to open the maintenance Bash shell.
Inside it, use `pacman -Syu` for a full upgrade and `pacman -S PACKAGE` for
package installation. The integration checks the candidate kernel before any
installed package changes, preserves running modules and regenerates boot files.
An unreviewed kernel refuses the whole transaction.

[Maintenance and kernel reviews](docs/maintenance.md) describes status/repair
commands and optional HTTPS review downloads. The download endpoint is
not yet configured; bundled reviews remain usable when GitHub is unavailable.

### Validate a new Arch kernel

```sh
./review-kernel.sh
```

The runner builds a fresh disposable installation, validates the candidate
kernel through upgrade, boot, networking, failure repair and OpenWrt generation
rollback, then updates `configs/reviewed-kernels.json` locally on success.
An optional `--reuse-build .work/jobs/JOB_ID` reuses a matching compiled core
cache. [Kernel validation](docs/kernel-validation.md) documents the checks and
options. Commit and push the review file when you are ready; no signing or
GitHub repository name is needed to run validation.

### Source and publication

Everything generated lives under ignored `.work/`; never publish that directory.
`SOURCE_FILES` lists the inputs packaged for the builder. The only binary asset
in Git is `artwork/logo.png`; its editable source is `artwork/logo.svg`.

```sh
python3 -m unittest discover -s tests
```

The scripts reproduce the build from public sources and locked distribution
inputs. They do **not** promise byte-identical disk images: filesystem UUIDs,
package signing identities and first-boot host identities are intentionally
fresh. Package/version manifests and build logs are retained privately for
diagnosis.

Original project code and artwork: [GPL-2.0-only](LICENSE). Upstream components
retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
This project is independent of Arch Linux and OpenWrt.
