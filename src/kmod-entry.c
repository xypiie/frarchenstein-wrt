/* SPDX-License-Identifier: GPL-2.0-only */
/* Rebuilt ubox entrypoint. The ubox package retains its alternatives. */
#define _GNU_SOURCE
#include <libgen.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    char **command = calloc((size_t)argc + 3, sizeof(*command));
    if (!command) { perror("kmodloader: calloc"); return 1; }
    command[0] = "/usr/libexec/frarchenstein/module-bridge";
    command[1] = basename(argv[0]);
    for (int i = 1; i < argc; i++) command[i + 1] = argv[i];
    execv(command[0], command);
    perror("kmodloader: module bridge unavailable");
    free(command);
    return 127;
}
