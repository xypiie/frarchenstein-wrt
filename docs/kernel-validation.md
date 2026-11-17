# Validate and record an Arch kernel

Run the validator as your normal host user from the source checkout:

```sh
./review-kernel.sh
```

It builds a fresh generic router installation using the pinned baseline inputs,
then tests the current `linux` package from the Arch repositories. It does not
attach a physical device or use an existing private router installation. Host
requirements are the same as for `build.sh`; KVM is preferred and `--tcg` is
available. Allow space for another 16 GiB sparse router disk, builder/client
scratch disks and package downloads.

To reuse a previously built, source-matched core cache:

```sh
./review-kernel.sh --reuse-build .work/jobs/JOB_ID
```

For a reproducible candidate selection, choose an Arch archive date and the
expected **package version** (which differs from `uname -r` spelling):

```sh
./review-kernel.sh \
  --snapshot YYYY/MM/DD \
  --expected-version PACKAGE_VERSION \
  --reuse-build .work/jobs/JOB_ID
```

A different resolved package version is refused. The default uses the current
repositories; the report records the actual package set and candidate hashes.
The test applies a full Arch upgrade, preserving package signature checks. It
does not install a new kernel on top of a selectively upgraded userspace.

## Executed gates

1. Build and boot a fresh generic baseline. Run the normal router smoke checks.
2. Download the candidate and dependencies, verify the official kernel package
   signature, inspect its kernel release/hash and resolve the required modules
   against the extracted package inside the VM.
3. Add provisional review data only to that disposable VM's local cache. Run
   the normal guarded full package upgrade. Check that OpenWrt configuration
   and selection remain unchanged and the old running kernel can still load
   its own modules through the OpenWrt module bridge.
4. Reboot into the candidate kernel. Check its actual kernel configuration,
   module versions, overlay/shared-data persistence and the normal router
   networking, firewall, SSH and LuCI checks.
5. Deliberately fail initramfs generation. Require repair state and unchanged
   active boot selection, then restore the configuration and run explicit
   repair through the pacman interface.
6. Prepare an OpenWrt generation B fixture using the same reviewed core and a
   distinct SquashFS. Boot B, verify independent overlays and shared-data
   persistence, and run the router smoke checks again.
7. Select generation A again. Reboot, verify its original overlay/settings,
   confirm B did not leak into A, and repeat the router checks.
8. Require clean guest shutdown and unchanged partition geometry on all four
   boots. Only then atomically append the successful review to the local file.

The required kernel configuration includes storage/filesystem support,
namespaces, cgroups, netfilter/nftables, connection tracking, NAT, bridge/veth
and IPv6 availability. IPv6 availability in the configuration is not an IPv6
routing acceptance test. Optional firmware warnings for unrelated physical
hardware are not proof that those devices work.

## Output and failure handling

`configs/reviewed-kernels.json` is the tracked output. Existing entries are
preserved. Each new entry records the package version, exact kernel hash,
integration ABI, module mappings, startup requirements and validation scope.
No review signature, GitHub name, commit or push is needed. Once you commit and
push this file, installations configured to download it can use the new review.

Detailed reports, package/source evidence, logs and disposable disks remain in
ignored `.work/kernel-reviews/RUN_ID/`, `.work/jobs/` and `.work/runs/`.
The validator prints the report location. If any required check fails, the
tracked review file is unchanged and the evidence is retained. A changed source
file or concurrent edit of the review list also prevents publication. Resolve
the reported problem and start a fresh run; incomplete results cannot be
promoted by rerunning only the final step. Use `build.sh` for deployment;
validation disks retain test fixtures and provisional cache settings.

The test VM's provisional entry is not an approval. Only completion of the
whole host-run sequence writes an entry to the repository. The generated
review explicitly records QEMU x86-64/SATA/virtio-net coverage and
`hardware_verified: false`. Real NICs, Wi-Fi, firmware and router hardware
still require their own validation. Generation B tests the same OpenWrt release
with a fixture; it does not certify arbitrary OpenWrt release migrations.

## Changed module policies

The default candidate policy comes from the reviewed baseline kernel. Missing
or renamed modules cause a failure rather than an invented mapping. If kernel
changes require a different policy, provide a JSON file containing only
`policy` and `startup_modules`, using the structure in an existing review:

```sh
./review-kernel.sh --policy candidate-policy.json
```

Review the proposed mappings yourself, then rerun the complete validation.
The same module, configuration, boot and router gates apply. The policy file
alone never authorizes a kernel installation or changes the public review list.
