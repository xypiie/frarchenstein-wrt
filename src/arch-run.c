/* SPDX-License-Identifier: GPL-2.0-only */
/* OpenWrt/musl entry to the prepared Arch root. Never installed setuid. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/magic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static void fail(const char *message)
{
    fprintf(stderr, "arch-run: %s: %s\n", message, strerror(errno));
    exit(125);
}

static void same_view(const char *outside, const char *inside)
{
    struct stat a, b;
    if (stat(outside, &a) || stat(inside, &b)) fail("missing shared API view");
    if (a.st_dev != b.st_dev || a.st_ino != b.st_ino) {
        errno = EXDEV;
        fail("Arch API view does not match OpenWrt");
    }
}

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--help")) {
        puts("Usage: arch-run /absolute/arch/program [arguments...]\n"
             "Requires root and a verified, prepared /arch mount. Enters / before exec.");
        return 0;
    }
    if (argc < 2 || argv[1][0] != '/' || strstr(argv[1], "/../") ||
        strstr(argv[1], "//") || !strcmp(argv[1], "/")) {
        fprintf(stderr, "arch-run: expected an absolute program path inside Arch\n");
        return 2;
    }
    if (geteuid() != 0) {
        errno = EPERM;
        fail("root is required to enter the maintenance chroot");
    }
    int root = open("/arch", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) fail("cannot open Arch root");
    struct stat st;
    if (fstat(root, &st)) fail("cannot inspect Arch root");
    FILE *identity = fopen("/run/frarchenstein/arch-device", "re");
    unsigned int expected_major, expected_minor;
    char extra;
    if (!identity || fscanf(identity, "%u:%u %c", &expected_major, &expected_minor, &extra) != 2) {
        errno = EINVAL;
        fail("missing or malformed verified Arch device identity");
    }
    fclose(identity);
    if (major(st.st_dev) != expected_major || minor(st.st_dev) != expected_minor) {
        errno = EXDEV;
        fail("Arch root device identity mismatch");
    }
    same_view("/proc", "/arch/proc");
    same_view("/sys", "/arch/sys");
    same_view("/dev", "/arch/dev");
    same_view("/run", "/arch/run");
    same_view("/tmp", "/arch/tmp");
    same_view("/boot", "/arch/boot");
    struct statfs fs;
    if (statfs("/arch/proc", &fs) || fs.f_type != PROC_SUPER_MAGIC) {
        errno = ENODEV;
        fail("procfs is not ready");
    }
    if (fchdir(root) || chroot(".") || chdir("/")) fail("chroot failed");
    close(root);
    /* The launcher starts from a deterministic environment. Application-specific
       environment/cwd and privilege drop happen explicitly inside this chroot. */
    if (clearenv() || setenv("PATH", "/usr/bin:/usr/sbin", 1) ||
        setenv("HOME", "/root", 1) || setenv("LANG", "C.UTF-8", 1))
        fail("cannot initialize environment");
    execv(argv[1], argv + 1);
    fail("exec failed");
}
