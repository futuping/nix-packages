#include <stdio.h>
#include <unistd.h>

/* Nix substitutes a dependency-tracked path, never a host PATH lookup. */
int main(int argc, char **argv) {
    (void)argc;
    argv[0] = "@neomacsProgram@";
    execv(argv[0], argv);
    perror("Unable to launch Neomacs");
    return 127;
}
