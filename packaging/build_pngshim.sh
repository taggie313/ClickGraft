#!/bin/bash
# Build the libpng NEON shim that ships prebuilt in clickgraft/shims/.
#
# It used to be compiled on the user's Mac, which made clang a hard requirement
# for everyone making a copy -- and clang is one of the xcrun shims this project
# is trying to stop needing. It is four lines of C that HP's own arm64 build
# leaves dangling; compiling it on 3,000 Macs to get the same 16 KB every time
# was never the point of requiring a compiler.
#
# The objection to shipping it is fair and stands: a binary in git that nobody
# can diff is worse than four lines of C. The answer is that nobody has to trust
# it -- packaging/check_release.py rebuilds this from pngshim.c on the
# maintainer's Mac and compares the code, so the .c stays the reviewable thing
# and the .dylib has to match it.
#
#   ./packaging/build_pngshim.sh          # rebuild in place, print the sha256
#   ./packaging/build_pngshim.sh /tmp/out # build somewhere else, for comparison
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$HERE/clickgraft/shims/pngshim.c"
NAME="libclickgraft-pngshim.dylib"
DST="${1:-$HERE/clickgraft/shims/$NAME}"

# -arch arm64: the copy is arm64-only, and this exists because grafting an arm64
#   runtime makes HP's dangling reference live.
# -mmacosx-version-min=11.0: clang's default deployment target is the macOS it
#   runs on, and the copy's floor is the highest minimum inside it -- built on
#   macOS 27 without this the shim declared 27.0 and took the whole copy with
#   it. 11.0 is the first macOS on Apple Silicon and this calls nothing.
#
# NOT -Wl,-no_uuid, though it is tempting: LC_UUID is random per link, so
# dropping it makes two builds of identical source byte-identical, which would
# let the release gate compare bytes. It also makes the dylib UNLOADABLE.
# Measured 28 September 2026 by the acceptance suite's smoke launch, which is
# the only check that catches it -- codesign, lipo, vtool and otool are all
# happy with the file:
#
#   dyld: tried: '.../libclickgraft-pngshim.dylib' (missing LC_UUID load command)
#   ... app killed by signal 6
#
# So the gate compares the CODE instead (__TEXT,__text, the exports, the install
# name, the architecture and the minimum), all of which are identical across
# MacOSX15, MacOSX26 and MacOSX27 while the whole file is not.
clang -arch arm64 -dynamiclib -O2 \
      -mmacosx-version-min=11.0 \
      -install_name "@rpath/$NAME" \
      -o "$DST" "$SRC"

chmod 755 "$DST"
echo "built $DST"
shasum -a 256 "$DST"
