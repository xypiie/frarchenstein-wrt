# Validation status

The fixed-layout standalone installation passed its QEMU acceptance checks using
KVM and QEMU 11.1.3. The tested inputs are OpenWrt 25.12.6, Arch's 2026/11/10
package snapshot, and kernel 7.2.15-arch1-1. Integration ABI is
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
and generic application supervision. Full kernel-update and failure-repair
drills, generation switching and application lifecycle trials have not been
rerun for this standalone release. Earlier prototype results are design
evidence, not acceptance of these remaining public-release gates. Private backup
import and generic cross-capacity backup restoration remain separate work.

The pinned SDK emits duplicate-symbol and recursive-dependency configuration
diagnostics while combining its stubs with the reviewed owner source feed.
Required owner builds complete, and their output identities, signatures and
runtime behavior are checked; this is not a warning-free SDK build. Initramfs
generation also reports missing optional firmware for some hardware outside the
tested QEMU setup. Hardware support requires its own driver/firmware review.
