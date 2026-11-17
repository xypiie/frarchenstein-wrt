# Arch maintenance and kernel reviews

From the OpenWrt root console, enter the prepared Arch maintenance shell:

```sh
arch
```

Use familiar pacman commands inside it:

```sh
pacman -Q linux
pacman -Ss htop
pacman -S htop
pacman -Syu
```

`exit` returns to OpenWrt. The OpenWrt `arch` command opens Bash; the existing
Arch coreutils command named `arch` still reports the architecture inside the
chroot.

## Package transactions

The integration owns `/usr/local/bin/pacman`, ahead of `/usr/bin` in the
maintenance shell's PATH. It delegates to Arch's unchanged `/usr/bin/pacman`.
Queries and downloads pass through. Install, upgrade and removal transactions
use the integration lock and package hooks. Normal pacman confirmation prompts
remain; `--noconfirm` is used only when explicitly requested.

Before a kernel transaction changes any installed packages, its pre-transaction
hook checks the proposed package version and reads the kernel from the
downloaded archive. The exact kernel release and SHA-256 must match a valid
compatibility review. An unreviewed kernel or mismatching hash refuses the whole
transaction, including unrelated package upgrades. It does not silently hold
back the kernel and perform a partial system upgrade. Package databases and
downloaded caches may have been refreshed, but installed packages and active
boot files remain unchanged on this refusal.

Once the checks pass, applications are stopped and the running kernel's modules
are preserved. After the package transaction, application environments are
checked, the custom initramfs is regenerated, and coherent boot files are
published. Applications are then restarted. No automatic reboot occurs.

After a refused upgrade, refreshed package databases may advertise newer
packages. A subsequent `pacman -S PACKAGE` is also refused while system upgrades
are pending; use `pacman -Syu PACKAGE` so the kernel guard applies to the whole
upgrade. This avoids inadvertently creating a partial Arch upgrade.

Standalone `pacman -Sy`, alternate roots/databases/hook directories, dependency
bypasses and removal of essential integration packages are refused. Direct
`/usr/bin/pacman` queries work, but its mutating transactions require the live
wrapper and its lock. The wrapper supports common `-S`, `-U` and `-R`
operations; it does not promise every advanced pacman option.

Check completion and explicitly retry finalization after resolving a failure:

```sh
pacman --frarchenstein-status
pacman --frarchenstein-repair
```

Repair validates the installed packages and rebuilds boot files; it does not
start another package upgrade. Do not reboot after a failed transaction. Once
status is `ready`, return to OpenWrt and reboot when convenient. There are no
Arch/kernel rollback slots. Reviewed kernels still require regression testing;
review metadata cannot guarantee compatibility with arbitrary future userspace.

## Downloaded compatibility reviews

The review file is trusted through HTTPS certificate verification and the
administrator-configured repository URL. It does not need a separate signature,
signing key or publication envelope. Official Arch/OpenWrt package and release
signature verification remains mandatory.

The repository address has not been chosen, so the download URL defaults to
`null`. Bundled reviews remain available in
`/usr/share/frarchenstein/reviewed-kernels.json`. Once the repository exists,
configure `/etc/frarchenstein/kernel-reviews.json`:

```json
{
  "url": "https://raw.githubusercontent.com/OWNER/REPO/main/configs/reviewed-kernels.json"
}
```

Use your own repository and branch in place of these placeholders. This URL
points directly to the tracked file; a normal Git push publishes new entries.
HTTPS certificate verification is enabled and redirects must remain HTTPS.
Downloaded records must match the integration ABI and review schema before the
cache is replaced atomically. Changing the configured repository URL prevents
reuse of a cache from the previous source.

When a transaction includes `linux`, its pre-transaction hook attempts to
refresh the reviews. If GitHub or the network is unavailable, it falls back to
bundled reviews and the locally cached records from that URL. If neither covers
the candidate version and exact kernel hash, the whole transaction is refused
before installed packages change. Malformed downloaded data cannot replace a
valid cache or authorize installation. Reviews of an exact kernel do not expire
merely because the router has been offline.

## Reviewing a new kernel locally

Run `./review-kernel.sh` on the normal host, from the source checkout. It builds
a fresh generic installation, runs the candidate through disposable QEMU upgrade
and regression checks, then updates `configs/reviewed-kernels.json` only after
all required checks pass. No GitHub repository name is required.

See [kernel validation](kernel-validation.md) for commands, the test sequence,
policy changes, failure evidence and the scope of the resulting review.
