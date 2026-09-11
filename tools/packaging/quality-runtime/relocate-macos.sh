#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

RUNTIME=${1:-}
[[ -x $RUNTIME/bin/ffmpeg && -d $RUNTIME/lib ]] || {
    printf 'Usage: %s RUNTIME_DIRECTORY\n' "$0" >&2
    exit 2
}
command -v otool >/dev/null 2>&1 || { printf 'Missing macOS build tool: otool\n' >&2; exit 2; }
command -v install_name_tool >/dev/null 2>&1 || { printf 'Missing macOS build tool: install_name_tool\n' >&2; exit 2; }

dependency=$(otool -L "$RUNTIME/bin/ffmpeg" | awk '$1 ~ /libvmaf.*[.]dylib/ {print $1; exit}')
[[ -n $dependency ]] || { printf 'FFmpeg does not link a libvmaf dylib.\n' >&2; exit 3; }
library_name=${dependency##*/}
[[ -e $RUNTIME/lib/$library_name ]] || {
    printf 'Linked VMAF library is absent from runtime/lib: %s\n' "$library_name" >&2
    exit 3
}

install_name_tool -id "@rpath/$library_name" "$RUNTIME/lib/$library_name"
for binary in "$RUNTIME/bin/ffmpeg" "$RUNTIME/bin/ffprobe"; do
    linked=$(otool -L "$binary" | awk '$1 ~ /libvmaf.*[.]dylib/ {print $1; exit}')
    [[ -n $linked ]] || continue
    [[ $linked == "@rpath/$library_name" ]] || \
        install_name_tool -change "$linked" "@rpath/$library_name" "$binary"
done

linked=$(otool -L "$RUNTIME/bin/ffmpeg" | awk '$1 ~ /libvmaf.*[.]dylib/ {print $1; exit}')
[[ $linked == "@rpath/$library_name" ]] || {
    printf 'FFmpeg VMAF dependency is still not relocatable: %s\n' "$linked" >&2
    exit 3
}
