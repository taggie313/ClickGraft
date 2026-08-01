/*
 * ClickGraft.app launcher.
 *
 * Why a compiled launcher instead of a shell script: notarization requires the
 * bundle's main executable to be a Mach-O signed with the hardened runtime.
 * A .app whose CFBundleExecutable is a shell script cannot be notarized, which
 * is the whole point of shipping a bundle rather than a .command file.
 *
 * All this does is locate its own bundle, point PYTHONPATH at Contents/Resources,
 * and hand off to Apple's python3 -- the same interpreter (and the same single
 * dependency, Xcode Command Line Tools) the project targets everywhere else.
 *
 * Build:  clang -O2 -arch arm64 -arch x86_64 -o ClickGraft launcher.c
 */
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PYTHON "/usr/bin/python3"

int main(int argc, char *argv[]) {
    char exec_path[PATH_MAX], resolved[PATH_MAX];
    uint32_t size = sizeof(exec_path);

    if (_NSGetExecutablePath(exec_path, &size) != 0) {
        fprintf(stderr, "ClickGraft: could not determine executable path\n");
        return 1;
    }
    if (realpath(exec_path, resolved) == NULL) {
        fprintf(stderr, "ClickGraft: could not resolve executable path\n");
        return 1;
    }

    /* resolved = <bundle>/Contents/MacOS/ClickGraft
     * dirname twice -> <bundle>/Contents   (dirname may mutate its argument,
     * so work on copies rather than assuming otherwise). */
    char macos_dir[PATH_MAX], contents_dir[PATH_MAX], resources[PATH_MAX];
    snprintf(macos_dir, sizeof(macos_dir), "%s", resolved);
    snprintf(contents_dir, sizeof(contents_dir), "%s", dirname(macos_dir));
    char contents_copy[PATH_MAX];
    snprintf(contents_copy, sizeof(contents_copy), "%s", contents_dir);
    snprintf(resources, sizeof(resources), "%s/Resources", dirname(contents_copy));

    if (setenv("PYTHONPATH", resources, 1) != 0) {
        fprintf(stderr, "ClickGraft: could not set PYTHONPATH\n");
        return 1;
    }
    /* Python puts the working directory at the FRONT of sys.path, ahead of
     * anything in PYTHONPATH. Launched from Finder the cwd is "/", so the
     * bundle wins — but run from a directory that happens to contain a
     * `clickgraft` folder (a clone of this repo, say) and that copy would
     * silently shadow the packaged one. Anchor the cwd to Resources so the
     * code we ship is always the code that runs. */
    if (chdir(resources) != 0) {
        fprintf(stderr, "ClickGraft: could not enter %s\n", resources);
        return 1;
    }
    /* Without this, Python writes __pycache__ directories into Contents/
     * Resources on first run, which invalidates the bundle's code signature.
     * That is exactly how HP Click breaks its own signature -- JDFPrintProcessor
     * writes font caches into its bundle -- so do not repeat it here. */
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);

    /* argv: python3 -m clickgraft.cli gui [caller's args...] */
    char **args = calloc((size_t)argc + 5, sizeof(char *));
    if (args == NULL) return 1;
    int n = 0;
    args[n++] = (char *)PYTHON;
    args[n++] = "-m";
    args[n++] = "clickgraft.cli";
    args[n++] = "gui";
    for (int i = 1; i < argc; i++) args[n++] = argv[i];
    args[n] = NULL;

    execv(PYTHON, args);

    /* Only reached if execv failed. */
    perror("ClickGraft: could not launch " PYTHON);
    fprintf(stderr,
            "Install the Xcode Command Line Tools with:  xcode-select --install\n");
    return 1;
}
