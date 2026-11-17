/* SPDX-License-Identifier: GPL-2.0-only */
/* Executed from tmpfs by PID 1 after procd has terminated other processes.
 * Replacing procd during APK transactions can leave its old executable
 * unlinked but mapped. Exec releases those mappings before remounting. */
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <unistd.h>

static void stop(const char *reason)
{
    perror(reason);
    fprintf(stderr, "frarchenstein: shutdown incomplete; disk must remain attached\n");
    for (;;) sleep(60);
}

static void readonly(const char *path)
{
    struct statvfs fs;
    for (int attempt = 0; attempt < 5; ++attempt) {
        sync();
        if (!mount(NULL, path, NULL, MS_REMOUNT | MS_RDONLY, NULL) &&
            !statvfs(path, &fs) && (fs.f_flag & ST_RDONLY)) return;
        sleep(1);
    }
    stop(path);
}

int main(int argc, char **argv)
{
    if (getpid() != 1 || argc != 2) {
        fputs("shutdown-final: only procd PID 1 may invoke this helper\n", stderr);
        return 2;
    }
    char *end;
    unsigned long event = strtoul(argv[1], &end, 16);
    if (*end || (event != (unsigned int)RB_POWER_OFF &&
                 event != (unsigned int)RB_AUTOBOOT && event != (unsigned int)RB_HALT_SYSTEM)) {
        errno = EINVAL;
        stop("unsupported shutdown event");
    }
    if (syscall(SYS_close_range, 3U, ~0U, 0U) || chdir("/")) stop("release process references");
    readonly("/");
    readonly("/arch");
    readonly("/persistent");
    readonly("/boot");
    sync();
    puts("frarchenstein: all persistent filesystems are read-only");
    fflush(NULL);
    /* Some reboot implementations terminate the caller; keep PID 1 alive. */
    pid_t child = fork();
    if (child < 0) stop("fork reboot");
    if (!child) {
        if (reboot((int)event)) perror("reboot");
        _exit(1);
    }
    for (;;) sleep(60);
}
