# Validation status

The initial fixed-layout standalone installation passed its QEMU acceptance
checks using KVM and QEMU 11.1.3. The tested inputs are OpenWrt 25.12.6, Arch's
2026/11/10 package snapshot, and kernel 7.2.15-arch1-1. Integration ABI is
`frarchenstein-wrt-1`.

| Gate | Executed result |
| --- | --- |
| Unit and source checks | 48 tests pass; Bash syntax, shutdown-helper C syntax and staged whitespace checks pass. Refusal tests cover undersized/existing image outputs and mounted, swap, held, busy, non-removable or unsupported device fixtures. |
| Input trust and builder | Public input hashes and signatures verified; fresh Arch builder bootstrapped in QEMU. |
| Construction | The original default build rebuilt the patched owners and helpers. The current installer reuses that source-matched signed core cache, rebuilds its Arch integration, installs BIOS/EXTLINUX, and checks all filesystems offline. |
| Public cache-reuse path | A second target was installed using `--reuse-build`, with matching source hashes, verified cached files and package signatures. |
| Capacity independence | 16 GiB and 20 GiB regular-file targets each passed two boots. Partition 3 and ext4 already occupied all remaining space before first boot. The entire MBR stayed unchanged across every boot. |
| Boot and module delegation | Arch kernel, OpenWrt procd, persistent overlay, direct module loading and matching module version metadata pass. |
| Router networking | A separate LAN client receives OpenWrt DHCP and DNS and reaches the Internet through router NAT. Firewall configuration checks pass; unsolicited WAN SSH is blocked. |
| Management access | LAN SSH banner and LuCI HTTP/HTTPS pass. HTTPS uses the guest's freshly generated certificate. |
| Reinstallation and persistence | All eight core/integration owners were actually reinstalled on every boot test. An incompatible stock ubox replacement was refused before changing the installed database or world. Edited network configuration, hostname and an overlay marker survived the following boot. |
| Shutdown and disk consistency | QMP confirms guest poweroff and host metadata checks confirm clean filesystems. After the boot tests, all three filesystems on both targets passed `e2fsck -fn` with the virtual target device read-only; no repairs were performed. |

Installation manifest format 2 records the final disk geometry. Boot only
validates and mounts it; there is no partition-growth state or resizing helper.
Earlier format-1 seed images are not accepted by the current launcher. Normal
startup/shutdown reads the MBR and three filesystem superblocks, not the whole
card. Release/package authentication and protected integration-file hashes
remain mandatory. Build and runtime evidence stays under ignored `.work/jobs/`
and `.work/runs/`; logs and generated signing keys are private outputs.

Two integration defects were fixed during validation: restrictive overlay-root
permissions blocked unprivileged daemons, and replacing the running procd
executable prevented an ordinary readonly remount. The installer now checks
traversal permissions. Shutdown finishes through a static PID 1 helper in RAM,
releasing replaced executable mappings before readonly remounts and poweroff.

## Remaining gates

Physical SD passthrough, real router hardware, NIC/firmware mapping, Wi-Fi, IPv6
and TCG performance are not accepted by these KVM file tests. Physical
installation and boot remain user-run.

The public source includes guarded Arch updates, OpenWrt generation management
and generic application supervision. The maintenance refinement below validates
same-kernel transactions and failure repair. Upgrading to a different reviewed
kernel version, generation switching and application lifecycle trials still need
standalone acceptance. Earlier prototype results are design evidence. Private
backup import and generic cross-capacity restoration remain separate work.

The pinned SDK emits duplicate-symbol and recursive-dependency configuration
diagnostics while combining its stubs with the reviewed owner source feed.
Required owner builds complete, and their output identities, signatures and
runtime behavior are checked; this is not a warning-free SDK build. Initramfs
generation also reports missing optional firmware for some hardware outside the
tested QEMU setup. Hardware support requires its own driver/firmware review.

## Historical maintenance refinement — 18 November 2026

Arch integration 1.0-14 and OpenWrt integration/provider 1.0-r8 add the `arch`
shell entrypoint, guarded `pacman` interface and optional signed kernel-review
downloads. The GitHub endpoint and signing identity remain unconfigured.

All 64 public unit tests pass. Real-signature fixtures cover verified downloads,
offline fallback, expiration, changed payloads, wrong signers, and rollback or
changed-sequence refusal. Transaction tests cover command dispatch and rejection
before application shutdown or installed-package changes.

A fresh 16 GiB QEMU installation passed 13 maintenance checks: shell/PATH and
native queries; unchanged native pacman ownership; direct transaction bypass
refusal; signed unknown-kernel and wrong-hash refusal; refusal of the actual
unreviewed upstream kernel 7.3.4.arch1-2; prevention of a subsequent partial
upgrade; normal full-upgrade syntax against the reviewed snapshot; ordinary
package installation/removal; reviewed kernel/mkinitcpio reinstallation with an
unreachable review server; running-module availability; and failed-initramfs
publication followed by explicit repair. Refusal checks verified unchanged
installed-package records, installed kernel and active boot selection.

The trial then passed the router networking, management, module-delegation and
eight-owner APK reinstall checks, with clean shutdown and filesystem metadata. A
subsequent boot passed with the regenerated boot files and retained settings. No
physical device or private router configuration is involved. No live GitHub
publication is claimed; the download transport uses configurable HTTPS URLs,
with authentication and offline behavior covered by the tests above.

## Kernel-review automation — 18 November 2026

Arch integration 1.0-15 trusts review downloads through the configured
repository's HTTPS endpoint. Detached review signatures are no longer required;
distribution package and release signatures remain mandatory. The GitHub URL was
unset during this trial; repository configuration followed later. All 68 public
unit tests and four local publication checks pass.

The new `review-kernel.sh` completed a real full Arch upgrade from kernel
7.2.15-arch1-1 to 7.3.4-arch1-2 in a fresh generic QEMU installation. It
verified the official package signature and exact kernel hash, candidate module
resolution, preservation of the old running modules, and unchanged OpenWrt
configuration during the upgrade. After reboot it checked the actual kernel
configuration, matching modules and persistent data.

The runner then passed deliberate initramfs failure and explicit repair, OpenWrt
generation B isolation, and rollback to generation A. All four boots passed
DHCP/DNS/NAT, firewall, SSH/LuCI, module delegation, eight-owner APK
reinstallation and stock-owner refusal checks, followed by clean shutdown and
unchanged disk geometry. Only after completion did the script add the new entry
to `configs/reviewed-kernels.json`.

Two initial runner failures (the extracted package's missing `/lib` view and a
shell prompt preceding a report) left the review list unchanged. Their logs
remain private evidence; both were fixed before the successful full run.

The resulting review is explicitly limited to QEMU x86-64 with SATA storage and
virtio networking; physical hardware is not certified. Generation B uses a
fixture of the same OpenWrt release. Application lifecycle, arbitrary OpenWrt
release migration, IPv6 routing, Wi-Fi, physical hardware and private restore
remain separate gates. All project VMs are stopped.

## GitHub review endpoint

Arch integration 1.0-16 configures the default review URL to the tracked
`configs/reviewed-kernels.json` on `xypiie/frarchenstein-wrt`'s `main` branch.
A live fetch through the installed fetcher code passed HTTPS and schema checks
and returned reviews for 7.2.14-arch1-1, 7.2.15-arch1-1 and 7.3.4-arch1-2.
All 68 source tests and the local publication audit passed. This configuration
change did not require another QEMU kernel-validation run.
