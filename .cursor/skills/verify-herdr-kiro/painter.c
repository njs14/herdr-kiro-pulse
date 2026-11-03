#include <stdio.h>
#include <string.h>
#include <unistd.h>

static void show(const char *mode) {
    fputs("\033[2J\033[H", stdout);
    if (strcmp(mode, "working") == 0) {
        fputs("▶ two-step running 0/1 · ctrl+x expand · ctrl+g monitor\n", stdout);
    } else if (strcmp(mode, "approval") == 0) {
        fputs("Write File requires approval\n", stdout);
        fputs("❯ Allow once\n", stdout);
        fputs("  Deny once\n", stdout);
        fputs("esc to close · ↑↓ to navigate · ↵ to select · Tab to edit\n", stdout);
    } else if (strcmp(mode, "trust") == 0) {
        fputs("Trust this workspace?\n", stdout);
        fputs("❯ Trust, continue\n", stdout);
        fputs("  Don't trust, exit\n", stdout);
    } else {
        fputs("› ask a question or describe a task\n", stdout);
    }
    fflush(stdout);
}

int main(int argc, char **argv) {
    const char *fifo = argc > 1 ? argv[1] : NULL;
    char buf[64];

    show("idle");
    if (fifo == NULL) {
        for (;;) {
            pause();
        }
    }
    for (;;) {
        FILE *handle = fopen(fifo, "r");
        if (handle == NULL) {
            break;
        }
        while (fgets(buf, sizeof buf, handle) != NULL) {
            buf[strcspn(buf, "\r\n")] = '\0';
            if (buf[0] != '\0') {
                show(buf);
            }
        }
        fclose(handle);
    }
    for (;;) {
        pause();
    }
}
