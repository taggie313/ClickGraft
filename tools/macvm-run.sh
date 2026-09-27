#!/bin/sh
# Launch the wrapped app so background screen control can address it by bundle id.
exec "$(dirname "$0")/macvm.app/Contents/MacOS/macvm" run --bundle "${1:-$HOME/VMs/macos12.bundle}" --cpus 6 --ram 8
