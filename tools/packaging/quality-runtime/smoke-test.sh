#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

RUNTIME=${1:-}
[[ -n $RUNTIME ]] || { printf 'Usage: bash %s PATH/TO/runtime\n' "$0" >&2; exit 2; }
RUNTIME=$(cd -- "$RUNTIME" && pwd -P)
FFMPEG="$RUNTIME/bin/ffmpeg"
FFPROBE="$RUNTIME/bin/ffprobe"
[[ -x $FFMPEG && -x $FFPROBE ]] || { printf 'Runtime is missing executable ffmpeg/ffprobe.\n' >&2; exit 3; }
[[ -r $RUNTIME/runtime-manifest.txt ]] || { printf 'Runtime manifest is missing.\n' >&2; exit 3; }

case $(uname -s) in
    # macOS builds carry @loader_path rpaths. Avoid globally overriding Apple
    # libraries with unrelated same-named libraries in a combined runtime.
    Darwin) ;;
    *) LD_LIBRARY_PATH="$RUNTIME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; export LD_LIBRARY_PATH ;;
esac

"$FFMPEG" -hide_banner -version >/dev/null
"$FFPROBE" -hide_banner -version >/dev/null

if "$FFMPEG" -hide_banner -buildconf 2>&1 | grep -F -- '--enable-nonfree' >/dev/null; then
    printf 'FAIL: runtime contains a non-redistributable --enable-nonfree FFmpeg build.\n' >&2
    exit 4
fi

FILTERS=$($FFMPEG -hide_banner -filters 2>/dev/null)
grep -E '(^|[[:space:]])libvmaf([[:space:]]|$)' <<< "$FILTERS" >/dev/null || {
    printf 'FAIL: libvmaf filter is not exposed.\n' >&2
    exit 4
}

ERR=$(mktemp "${TMPDIR:-/tmp}/av1compare-runtime-smoke.XXXXXX.err")
cleanup() { rm -f -- "$ERR"; }
trap cleanup EXIT
if ! "$FFMPEG" -hide_banner -v error -nostdin \
    -f lavfi -i 'color=c=black:s=64x64:r=1:d=1' \
    -f lavfi -i 'color=c=black:s=64x64:r=1:d=1' \
    -filter_complex '[0:v][1:v]libvmaf' -frames:v 1 -f null - \
    >/dev/null 2>"$ERR"; then
    printf 'FAIL: real libvmaf execution failed.\n' >&2
    tail -n 20 "$ERR" >&2 || true
    exit 4
fi

printf 'Media runtime smoke test passed.\n'
printf 'FFmpeg: %s\n' "$($FFMPEG -hide_banner -version 2>&1 | sed -n '1p')"
printf 'Runtime manifest:\n'
sed 's/^/  /' "$RUNTIME/runtime-manifest.txt"
