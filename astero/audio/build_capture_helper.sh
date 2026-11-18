#!/bin/sh
set -eu

# Usage: build_capture_helper.sh /path/to/android-ndk /path/to/output
# Pull the link library from the connected Echo itself so the helper's tinyalsa
# ABI matches that device. No helper binary is committed or left on the Dot.
if [ "$#" -ne 2 ]; then
    echo "usage: $0 ANDROID_NDK OUTPUT" >&2
    exit 2
fi
ndk=$1
output=$2
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
toolchain=$(find "$ndk/toolchains/llvm/prebuilt" -mindepth 1 -maxdepth 1 -type d | head -n 1)
compiler="$toolchain/bin/armv7a-linux-androideabi25-clang"
if [ ! -x "$compiler" ]; then
    echo "Android NDK compiler not found under $ndk/toolchains/llvm/prebuilt" >&2
    exit 1
fi

adb shell 'test "$(id -u)" = 0 && test "$(getprop ro.product.device)" = biscuit_puffin'
adb shell 'getprop ro.product.cpu.abi' | tr -d '\r' | grep -qx 'armeabi-v7a'
adb pull /system/lib/libtinyalsa.so "$tmp/libtinyalsa.so" >/dev/null
sha256sum "$tmp/libtinyalsa.so"
"$compiler" \
    -Wall -Wextra -Werror -O2 -fPIE -pie \
    "$root/astero/audio/capture_helper.c" -L"$tmp" \
    -Wl,--no-as-needed -ltinyalsa -o "$output"
sha256sum "$output"
