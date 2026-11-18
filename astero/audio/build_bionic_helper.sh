#!/bin/sh
# Build a 32-bit Android helper on an ARM64 Fedora host using the connected Dot.
set -eu
output=${1:?usage: build_bionic_helper.sh OUTPUT}
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
adb shell 'test "$(id -u)" = 0 && test "$(getprop ro.product.device)" = biscuit_puffin'
adb pull /system/lib/libc.so "$tmp/libc.so" >/dev/null
adb pull /system/lib/libtinyalsa.so "$tmp/libtinyalsa.so" >/dev/null
arm-linux-gnu-gcc -marm -mfloat-abi=softfp -fPIE -fno-stack-protector \
  -nostdlib -pie -Wl,--allow-shlib-undefined -Wl,--dynamic-linker=/system/bin/linker \
  -Wl,-e,_start -o "$output" "$root/astero/audio/capture_bionic.c" \
  -L"$tmp" -ltinyalsa -lc
sha256sum "$output"
