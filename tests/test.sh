#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
ENCODER="$ROOT/AV1Encode.sh"
COMPARATOR="$ROOT/tools/AV1Compare.py"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/av1encode-tests.XXXXXX")
cleanup() { rm -rf -- "$TEST_ROOT"; }
trap cleanup EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

assert_contains() {
    local text=$1 expected=$2
    [[ $text == *"$expected"* ]] || fail "expected output to contain: $expected"
}

bash -n "$ENCODER"
PYTHONPYCACHEPREFIX="$TEST_ROOT/pycache" python3 -m py_compile "$COMPARATOR"

[[ $("$ENCODER" --version) == 'AV1Encode.sh 1.2' ]] || fail 'unexpected encoder version'
[[ $("$ENCODER" --interface-version) == '1' ]] || fail 'unexpected machine-interface version'
[[ $(python3 "$COMPARATOR" --version) == 'AV1Compare.py 2.0' ]] || fail 'unexpected comparator version'
help=$("$ENCODER" --help)
assert_contains "$help" 'AV1 encoding'
assert_contains "$help" '--skip-av1'
assert_contains "$help" 'libsvtav1 preset (0-13)'
assert_contains "$help" 'Automatically select a working hardware encoder'
assert_contains "$help" '--machine-probe'
assert_contains "$help" '--result-json'
assert_contains "$help" '--preserve-all'

if "$ENCODER" --software --crf 64 missing.mkv >"$TEST_ROOT/invalid-crf.log" 2>&1; then
    fail 'CRF 64 was accepted'
fi
assert_contains "$(<"$TEST_ROOT/invalid-crf.log")" '--crf must be an integer from 0 to 63'

if "$ENCODER" --software --preset slow missing.mkv >"$TEST_ROOT/invalid-preset.log" 2>&1; then
    fail 'a non-numeric SVT-AV1 preset was accepted'
fi
assert_contains "$(<"$TEST_ROOT/invalid-preset.log")" '--preset must be an integer from 0 to 13'

if rg -qi 'legacy.intel|hevc_qsv_legacy|intel.media.sdk' "$ENCODER" "$ROOT/.github" "$ROOT/tools"; then
    fail 'HEVC-only legacy Intel support leaked into AV1Encode'
fi

command -v ffmpeg >/dev/null || fail 'ffmpeg is required for the integration test'
command -v ffprobe >/dev/null || fail 'ffprobe is required for the integration test'
ffmpeg -hide_banner -encoders 2>/dev/null | awk '{print $2}' | grep -Fxq libsvtav1 || \
    fail 'the integration test requires FFmpeg with libsvtav1'

printf '1\n00:00:00,000 --> 00:00:00,800\nTest subtitle\n' > "$TEST_ROOT/subtitle.srt"
printf 'attachment test\n' > "$TEST_ROOT/attachment.txt"
printf ';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=800\ntitle=Opening\n' > "$TEST_ROOT/chapters.ffmeta"
ffmpeg -hide_banner -loglevel error \
    -f lavfi -i 'testsrc2=size=160x90:rate=12:duration=1' \
    -f lavfi -i 'sine=frequency=440:duration=1' \
    -f lavfi -i 'sine=frequency=880:duration=1' \
    -f srt -i "$TEST_ROOT/subtitle.srt" \
    -f ffmetadata -i "$TEST_ROOT/chapters.ffmeta" \
    -map 0:v:0 -map 1:a:0 -map 2:a:0 -map 3:s:0 -map_chapters 4 \
    -c:v ffv1 -c:a pcm_s16le -c:s srt \
    -attach "$TEST_ROOT/attachment.txt" -metadata:s:t mimetype=text/plain \
    "$TEST_ROOT/source.mkv"

"$ENCODER" --machine-probe > "$TEST_ROOT/capabilities.json"
python3 - "$TEST_ROOT/capabilities.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["schema"] == "av1encode.capabilities"
assert report["protocol_version"] == 1
assert report["codec"] == "av1"
assert report["auto_policy"] == "hardware_only"
assert report["features"]["exact_output"] is True
assert report["features"]["preserve_all"] is True
encoders = {item["name"]: item for item in report["encoders"]}
assert set(encoders) == {"av1_vaapi", "av1_nvenc", "av1_qsv", "libsvtav1"}
assert encoders["libsvtav1"]["class"] == "software"
assert encoders["libsvtav1"]["auto_eligible"] is False
assert all(encoders[name]["auto_eligible"] for name in ("av1_vaapi", "av1_nvenc", "av1_qsv"))
PY

if ENCODER="$ENCODER" bash -c '
    source "$ENCODER"
    MODE=auto
    AUDIO_MODE=aac
    detect_hw() { HW_TYPE=none; HW_DETAIL=""; }
    configure_encoder
' >"$TEST_ROOT/auto-without-hardware.log" 2>&1; then
    fail 'AUTO succeeded without working AV1 hardware'
fi
assert_contains "$(<"$TEST_ROOT/auto-without-hardware.log")" \
    'AUTO could not find a working hardware AV1 encoder.'
assert_contains "$(<"$TEST_ROOT/auto-without-hardware.log")" 'CPU fallback is disabled.'

# Exercise selection independently of CI hardware by replacing only the probe result.
# This verifies that AUTO selects hardware while explicit software remains available.
source "$ENCODER"
MODE=auto
AUDIO_MODE=aac
detect_hw() { HW_TYPE=nvidia; HW_DETAIL='test NVENC'; }
configure_encoder
[[ $ACTIVE_MODE == hardware ]] || fail 'AUTO did not select hardware mode'
[[ $ACTIVE_ENCODER == 'NVIDIA NVENC' ]] || fail 'AUTO did not select the proven encoder'
[[ ${VIDEO_ENCODER_ARGS[0]} == '-c:v:0' && ${VIDEO_ENCODER_ARGS[1]} == av1_nvenc ]] || \
    fail 'AUTO command is not AV1 NVENC'

dry_run=$("$ENCODER" --software --crf 40 --preset 10 --container mkv --dry-run "$TEST_ROOT/source.mkv")
assert_contains "$dry_run" '-c:v:0 libsvtav1'
assert_contains "$dry_run" 'source_av1.part.mkv'

"$ENCODER" --software --crf 40 --preset 10 --container mkv --yes \
    "$TEST_ROOT/source.mkv" >"$TEST_ROOT/encode.log" 2>&1
output="$TEST_ROOT/source_av1.mkv"
[[ -s $output ]] || fail 'AV1 output was not created'
[[ $(ffprobe -v error -select_streams V:0 -show_entries stream=codec_name -of csv=p=0 "$output") == av1 ]] || \
    fail 'output video codec is not AV1'
[[ $(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$output") == aac ]] || \
    fail 'output audio codec is not AAC'
ffmpeg -hide_banner -loglevel error -xerror -i "$output" -map 0:V:0 -f null -
validate_hardware_probe_output "$output" || fail 'probe output validation rejected valid AV1'

machine_output="$TEST_ROOT/machine-output.mkv"
machine_result="$TEST_ROOT/machine-result.json"
"$ENCODER" --machine --encoder libsvtav1 --crf 40 --preset 10 --copy-audio --preserve-all \
    --input "$TEST_ROOT/source.mkv" --output "$machine_output" \
    --result-json "$machine_result" >"$TEST_ROOT/machine-encode.log" 2>&1
[[ -s $machine_output ]] || fail 'machine mode did not create its exact output path'
python3 - "$machine_result" "$machine_output" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["schema"] == "av1encode.result"
assert report["protocol_version"] == 1
assert report["status"] == "ok"
assert report["exit_code"] == 0
assert report["codec"] == "av1"
assert report["encoder"] == "libsvtav1"
assert report["encoder_class"] == "software"
assert report["auto_policy"] == "hardware_only"
assert report["preserve_all"] is True
assert report["output"] == sys.argv[2]
assert report["output_bytes"] == os.path.getsize(sys.argv[2])
PY
[[ $(ffprobe -v error -select_streams V:0 -show_entries stream=codec_name -of csv=p=0 "$machine_output") == av1 ]] || \
    fail 'machine output video codec is not AV1'
for selector in v a s t; do
    source_count=$(ffprobe -v error -select_streams "$selector" -show_entries stream=index -of csv=p=0 "$TEST_ROOT/source.mkv" | sed '/^$/d' | wc -l)
    output_count=$(ffprobe -v error -select_streams "$selector" -show_entries stream=index -of csv=p=0 "$machine_output" | sed '/^$/d' | wc -l)
    [[ $source_count == "$output_count" ]] || fail "machine mode did not preserve every $selector stream"
done
source_chapters=$(ffprobe -v error -show_chapters -of csv=p=0 "$TEST_ROOT/source.mkv" | sed '/^$/d' | wc -l)
output_chapters=$(ffprobe -v error -show_chapters -of csv=p=0 "$machine_output" | sed '/^$/d' | wc -l)
[[ $source_chapters == "$output_chapters" ]] || fail 'machine mode did not preserve chapters'

failed_result="$TEST_ROOT/failed-result.json"
if "$ENCODER" --machine --input "$TEST_ROOT/missing.mkv" \
    --output "$TEST_ROOT/never-created.mkv" --result-json "$failed_result" \
    >"$TEST_ROOT/machine-failure.log" 2>&1; then
    fail 'machine mode accepted a missing input'
fi
python3 - "$failed_result" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["schema"] == "av1encode.result"
assert report["status"] == "failed"
assert report["exit_code"] == 1
assert report["encoder"] is None
PY

skip_log=$("$ENCODER" --software --skip-av1 --container mkv "$output")
assert_contains "$skip_log" 'Skipping because AV1 skip is enabled.'
[[ ! -e $TEST_ROOT/source_av1_av1.mkv ]] || fail 'skip-av1 created another output'

python3 "$COMPARATOR" --no-quality "$TEST_ROOT/source.mkv" "$output" \
    >"$TEST_ROOT/compare.log"
assert_contains "$(<"$TEST_ROOT/compare.log")" 'CANDIDATE'

printf 'All AV1Encode tests passed.\n'
