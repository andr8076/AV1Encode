#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
ENCODER="$ROOT/AV1Encode.sh"
COMPARATOR="$ROOT/tools/AV1Compare.py"
PLANNER="$ROOT/tools/AV1Plan.py"
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
bash -n "$ROOT/tools/AV1HardwareDecode.sh"
PYTHONPYCACHEPREFIX="$TEST_ROOT/pycache" python3 -m py_compile "$COMPARATOR"
PYTHONPYCACHEPREFIX="$TEST_ROOT/pycache" python3 -m py_compile "$PLANNER"

[[ $("$ENCODER" --version) == 'AV1Encode.sh 1.5.0' ]] || fail 'unexpected encoder version'
[[ $("$ENCODER" --interface-version) == '2' ]] || fail 'unexpected machine-interface version'
[[ $(python3 "$COMPARATOR" --version) == 'AV1Compare.py 2.0' ]] || fail 'unexpected comparator version'
python3 - "$COMPARATOR" "$TEST_ROOT" <<'PY'
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("av1compare_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
runtime = Path(sys.argv[2]) / "mode-repair-runtime"
(runtime / "bin").mkdir(parents=True)
for name in ("ffmpeg", "ffprobe"):
    path = runtime / "bin" / name
    path.write_bytes(b"runtime")
    path.chmod(0o600)
module._activate_runtime_executables(runtime)
assert all(os.access(runtime / "bin" / name, os.X_OK) for name in ("ffmpeg", "ffprobe"))
PY
help=$("$ENCODER" --help)
assert_contains "$help" 'AV1 encoding'
assert_contains "$help" '--skip-av1'
assert_contains "$help" 'libsvtav1 preset (0-13)'
assert_contains "$help" 'Automatically select a working hardware encoder'
assert_contains "$help" '--machine-probe'
assert_contains "$help" '--result-json'
assert_contains "$help" '--preserve-all'
assert_contains "$help" '--machine-negotiate'
assert_contains "$help" '--machine-evaluate'
assert_contains "$help" '--execute-plan'

negotiation=$("$ENCODER" --machine-negotiate 1,2,3)
python3 - "$negotiation" <<'PY'
import json
import sys
report = json.loads(sys.argv[1])
assert report["schema"] == "av1encode.negotiation"
assert report["supported_protocol_versions"] == [1, 2]
assert report["selected_protocol_version"] == 2
assert report["compatible"] is True
PY
no_common=$("$ENCODER" --machine-negotiate 7,8)
python3 - "$no_common" <<'PY'
import json
import sys
report = json.loads(sys.argv[1])
assert report["compatible"] is False
assert report["selected_protocol_version"] is None
PY

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
    -c:v ffv1 -chroma_sample_location left -c:a pcm_s16le -c:s srt \
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
assert report["supported_protocol_versions"] == [1, 2]
assert report["codec"] == "av1"
assert report["auto_policy"] == "hardware_only"
assert report["features"]["exact_output"] is True
assert report["features"]["preserve_all"] is True
assert report["features"]["semantic_planning"] is True
assert report["features"]["fingerprint_invalidation"] is True
assert report["features"]["capability_proven_hardware_decode"] is True
for feature in (
    "semantic_requested_encoder", "semantic_quality_off", "semantic_scaling",
    "semantic_denoise", "semantic_audio_optimize",
):
    assert report["features"][feature] is True
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

MODE=hardware
FORCED_ENCODER=av1_vaapi
detect_forced_hardware() {
    HW_TYPE=vaapi
    HW_DETAIL='test VA-API'
    VAAPI_DEVICE=/dev/dri/renderD128
    VAAPI_UPLOAD_FORMAT=p010le
}
configure_encoder
[[ ${VIDEO_ENCODER_ARGS[2]} == -rc_mode && ${VIDEO_ENCODER_ARGS[3]} == CQP && \
   ${VIDEO_ENCODER_ARGS[4]} == -global_quality && ${VIDEO_ENCODER_ARGS[5]} == 24 ]] || \
    fail 'VA-API quality options are not using FFmpeg-compatible unscoped names'
INPUT_VIDEO_WIDTH=160
INPUT_VIDEO_HEIGHT=90
INPUT_VIDEO_SAR=1:1
INPUT_VIDEO_CHROMA_LOCATION=''
INPUT_VIDEO_COLOR_RANGE=''
INPUT_VIDEO_COLOR_SPACE=''
INPUT_VIDEO_COLOR_TRANSFER=''
INPUT_VIDEO_COLOR_PRIMARIES=''
build_file_video_filter
[[ ${VIDEO_OUTPUT_ARGS[0]} == -enc_time_base:v:0 &&
   ${VIDEO_OUTPUT_ARGS[1]} == demux && ${VIDEO_OUTPUT_ARGS[2]} == -noautoscale ]] || \
    fail 'VA-API output does not preserve the source demuxer time base'


# Hardware decoding is a per-source optimization, not a prerequisite for the
# proven hardware AV1 encoder. Mock only the bounded probe result so these
# policy tests remain deterministic on CI machines without GPUs.
(
    ACTIVE_MODE=hardware
    ACTIVE_ENCODER='AMD/Linux VA-API'
    HW_TYPE=vaapi
    VAAPI_DEVICE=/dev/dri/renderD128
    VAAPI_UPLOAD_FORMAT=p010le
    HARDWARE_QP=24
    DRY_RUN=no
    AV1ENCODE_DISABLE_HWDECODE=0
    INPUT_VIDEO_WIDTH=160
    INPUT_VIDEO_HEIGHT=90
    INPUT_VIDEO_SAR=1:1
    INPUT_VIDEO_CHROMA_LOCATION=''
    INPUT_VIDEO_COLOR_RANGE=''
    INPUT_VIDEO_COLOR_SPACE=''
    INPUT_VIDEO_COLOR_TRANSFER=''
    INPUT_VIDEO_COLOR_PRIMARIES=''
    VIDEO_ENCODER_ARGS=(-c:v:0 av1_vaapi -rc_mode CQP -global_quality 24)
    build_file_video_filter
    probe_hardware_decode_pipeline() { return 0; }
    configure_input_decode "$TEST_ROOT/source.mkv"
    [[ $ACTIVE_DECODER == vaapi ]] || fail 'VA-API hardware decode was not selected after a successful source probe'
    [[ ${INPUT_DECODE_ARGS[0]} == -hwaccel && ${INPUT_DECODE_ARGS[1]} == vaapi ]] || fail 'VA-API decode arguments were not installed'
    [[ ${VIDEO_FILTER_ARGS[1]} == *scale_vaapi* && ${VIDEO_FILTER_ARGS[1]} != *hwupload* ]] || \
        fail 'VA-API hardware decode did not keep decoded frames on hardware surfaces'
)

(
    ACTIVE_MODE=hardware
    ACTIVE_ENCODER='AMD/Linux VA-API'
    HW_TYPE=vaapi
    VAAPI_DEVICE=/dev/dri/renderD128
    VAAPI_UPLOAD_FORMAT=p010le
    HARDWARE_QP=24
    DRY_RUN=no
    AV1ENCODE_DISABLE_HWDECODE=0
    INPUT_VIDEO_WIDTH=160
    INPUT_VIDEO_HEIGHT=90
    INPUT_VIDEO_SAR=1:1
    INPUT_VIDEO_CHROMA_LOCATION=''
    INPUT_VIDEO_COLOR_RANGE=''
    INPUT_VIDEO_COLOR_SPACE=''
    INPUT_VIDEO_COLOR_TRANSFER=''
    INPUT_VIDEO_COLOR_PRIMARIES=''
    VIDEO_ENCODER_ARGS=(-c:v:0 av1_vaapi -rc_mode CQP -global_quality 24)
    build_file_video_filter
    probe_hardware_decode_pipeline() { return 1; }
    configure_input_decode "$TEST_ROOT/source.mkv"
    [[ $ACTIVE_DECODER == software ]] || fail 'failed VA-API decode probe did not fall back to CPU decode'
    [[ ${#INPUT_DECODE_ARGS[@]} -eq 0 ]] || fail 'failed hardware decode probe left hwaccel arguments active'
    [[ ${VIDEO_FILTER_ARGS[1]} == *hwupload* ]] || fail 'CPU-decode fallback did not restore the software-to-VAAPI upload path'
    [[ ${VIDEO_ENCODER_ARGS[1]} == av1_vaapi ]] || fail 'decode fallback changed the proven hardware AV1 encoder'
)

(
    ACTIVE_MODE=hardware
    ACTIVE_ENCODER='NVIDIA NVENC'
    HW_TYPE=nvidia
    HARDWARE_QP=24
    DRY_RUN=no
    AV1ENCODE_DISABLE_HWDECODE=0
    INPUT_VIDEO_WIDTH=1920
    INPUT_VIDEO_HEIGHT=1080
    INPUT_VIDEO_SAR=1:1
    VIDEO_FILTER_ARGS=()
    VIDEO_OUTPUT_ARGS=()
    VIDEO_ENCODER_ARGS=(-c:v:0 av1_nvenc -rc:v:0 vbr -cq:v:0 24 -preset:v:0 slow -pix_fmt:v:0 yuv420p10le)
    probe_hardware_decode_pipeline() { return 0; }
    configure_input_decode "$TEST_ROOT/source.mkv"
    [[ $ACTIVE_DECODER == nvdec ]] || fail 'NVDEC was not selected after a successful source probe'
    [[ ${INPUT_DECODE_ARGS[0]} == -hwaccel && ${INPUT_DECODE_ARGS[1]} == cuda ]] || fail 'CUDA decode arguments were not installed'
    [[ ${VIDEO_FILTER_ARGS[1]} == *scale_cuda* ]] || fail 'NVDEC path is not keeping conversion on CUDA surfaces'
    ! printf '%s\n' "${VIDEO_ENCODER_ARGS[@]}" | grep -q pix_fmt || fail 'NVDEC path requests a software pixel format before NVENC'
)

(
    ACTIVE_MODE=hardware
    ACTIVE_ENCODER='Intel Quick Sync'
    HW_TYPE=intel
    HARDWARE_QP=24
    DRY_RUN=no
    AV1ENCODE_DISABLE_HWDECODE=0
    INPUT_VIDEO_WIDTH=1920
    INPUT_VIDEO_HEIGHT=1080
    INPUT_VIDEO_SAR=1:1
    VIDEO_FILTER_ARGS=()
    VIDEO_OUTPUT_ARGS=()
    VIDEO_ENCODER_ARGS=(-c:v:0 av1_qsv -global_quality:v:0 24 -preset:v:0 slow -pix_fmt:v:0 yuv420p10le)
    probe_hardware_decode_pipeline() { return 0; }
    configure_input_decode "$TEST_ROOT/source.mkv"
    [[ $ACTIVE_DECODER == qsv ]] || fail 'QSV decode was not selected after a successful source probe'
    [[ ${INPUT_DECODE_ARGS[0]} == -hwaccel && ${INPUT_DECODE_ARGS[1]} == qsv ]] || fail 'QSV decode arguments were not installed'
    [[ ${VIDEO_FILTER_ARGS[1]} == *vpp_qsv* ]] || fail 'QSV decode path is not keeping conversion on QSV surfaces'
    ! printf '%s\n' "${VIDEO_ENCODER_ARGS[@]}" | grep -q pix_fmt || fail 'QSV decode path requests a software pixel format before AV1 QSV'
)

(
    ACTIVE_MODE=hardware
    ACTIVE_ENCODER='NVIDIA NVENC'
    HW_TYPE=nvidia
    HARDWARE_QP=24
    DRY_RUN=yes
    AV1ENCODE_DISABLE_HWDECODE=0
    VIDEO_FILTER_ARGS=()
    VIDEO_OUTPUT_ARGS=()
    VIDEO_ENCODER_ARGS=(-c:v:0 av1_nvenc -rc:v:0 vbr -cq:v:0 24 -preset:v:0 slow -pix_fmt:v:0 yuv420p10le)
    probe_hardware_decode_pipeline() { fail 'dry-run executed a real hardware decode probe'; }
    configure_input_decode "$TEST_ROOT/source.mkv"
    [[ $ACTIVE_DECODER == software ]] || fail 'dry-run pretended hardware decoding was proven'
)

HARDWARE_QP=118
configure_encoder
[[ ${VIDEO_ENCODER_ARGS[5]} == 118 ]] || \
    fail 'VA-API did not accept a calibrated quality value above 51'

if (
    MODE=hardware
    FORCED_ENCODER=av1_qsv
    HARDWARE_QP=118
    detect_forced_hardware() { HW_TYPE=intel; HW_DETAIL='test QSV'; }
    configure_encoder
) >"$TEST_ROOT/qsv-wide-qp.log" 2>&1; then
    fail 'QSV accepted a VA-API-only quality value above 51'
fi
assert_contains "$(<"$TEST_ROOT/qsv-wide-qp.log")" 'valid only for the VA-API AV1 backend'
HARDWARE_QP=24
FORCED_ENCODER=av1_vaapi

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
[[ $(ffprobe -v error -select_streams V:0 -show_entries stream=chroma_location -of csv=p=0 "$machine_output") == left ]] || \
    fail 'machine output did not preserve primary video chroma location'
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

# Protocol v2 keeps encoder settings inside AV1Encode. The caller supplies a
# semantic contract, receives sampled predictions, and later executes the
# sealed plan without restating CRF/QP/preset choices.
plan_source="$TEST_ROOT/plan-source.mkv"
cp "$TEST_ROOT/source.mkv" "$plan_source"
plan_output="$TEST_ROOT/planned-output.mkv"
requirements="$TEST_ROOT/requirements.json"
plan="$TEST_ROOT/plan.json"
plan_result="$TEST_ROOT/plan-result.json"
python3 - "$requirements" "$plan_source" "$plan_output" <<'PY'
import json
import sys
requirements = {
    "schema": "av1encode.requirements",
    "protocol_version": 2,
    "input": sys.argv[2],
    "output": sys.argv[3],
    "hardware_policy": "manual_software",
    "quality": {
        "metric": "ssim_percent",
        "target": 90,
        "p10_minimum": 86,
        "sustained_floor": 84,
        "maximum_sustained_seconds": 1,
    },
    "optimization": {"primary": "smallest_output", "secondary": "fastest_encoding"},
    "video": {"maximum_height": None, "denoise": "auto"},
    "preservation": {"streams": "all", "chapters": True, "metadata": True},
    "audio": {"mode": "copy_all"},
    "evaluation": {"sample_seconds": 1},
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(requirements, handle)
PY
python3 - "$requirements" "$TEST_ROOT/forbidden-requirements.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    requirements = json.load(handle)
requirements["ffmpeg_args"] = ["-crf", "1"]
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(requirements, handle)
PY
if "$ENCODER" --machine-evaluate "$TEST_ROOT/forbidden-requirements.json" \
    --plan-json "$TEST_ROOT/forbidden-plan.json" >"$TEST_ROOT/forbidden.log" 2>&1; then
    fail 'protocol v2 accepted caller-supplied FFmpeg arguments'
fi
assert_contains "$(<"$TEST_ROOT/forbidden.log")" 'Unknown requirement field(s): ffmpeg_args'

"$ENCODER" --machine-evaluate "$requirements" --plan-json "$plan" >"$TEST_ROOT/plan-reference.json"
python3 - "$plan" "$TEST_ROOT/plan-reference.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    plan = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    reference = json.load(handle)
assert plan["schema"] == "av1encode.plan"
assert plan["protocol_version"] == 2
assert plan["plan_id"].startswith("av1p_")
assert reference["plan_id"] == plan["plan_id"]
assert plan["selection"]["encoder"] == "libsvtav1"
assert plan["selection"]["policy_owner"] == "AV1Encode"
quality = plan["recipe"]["quality"]
assert quality["kind"] == "crf"
assert quality["preset"] == 6
assert isinstance(quality["value"], int) and 0 <= quality["value"] <= 63
assert plan["prediction"]["quality"]["metric"] == "ssim_percent"
assert isinstance(plan["prediction"]["quality"]["predicted_score"], float)
calibration = plan["prediction"]["calibration"]
assert calibration["strategy"] == "bounded_representative_quality_search"
assert calibration["selected_quality"] == quality["value"]
assert calibration["quality_kind"] == "crf"
assert calibration["sample_count"] >= 1
assert calibration["sample_starts_seconds"]
assert calibration["candidates"]
assert any(item["target_met_on_sample"] for item in calibration["candidates"])
assert plan["prediction"]["size"]["predicted_video_bytes"] > 0
assert plan["prediction"]["size"]["predicted_output_bytes"] >= plan["prediction"]["size"]["predicted_video_bytes"]
assert plan["prediction"]["speed"]["predicted_encode_seconds"] > 0
assert set(plan["fingerprints"]) == {"implementation", "runtime", "source", "requirements"}
assert all(item["value"].startswith("sha256:") for item in plan["fingerprints"].values())
PY

"$ENCODER" --execute-plan "$plan" --result-json "$plan_result" >"$TEST_ROOT/plan-execute.log" 2>&1
[[ -s $plan_output ]] || fail 'protocol-v2 plan did not create its output'
python3 - "$plan_result" "$plan" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    result = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    plan = json.load(handle)
assert result["schema"] == "av1encode.plan-result"
assert result["protocol_version"] == 2
assert result["plan_id"] == plan["plan_id"]
assert result["status"] == "ok"
assert result["executor_result"]["protocol_version"] == 1
PY

python3 - "$plan" "$TEST_ROOT/tampered-plan.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    plan = json.load(handle)
plan["recipe"]["quality"]["value"] = 1
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(plan, handle)
PY
if "$ENCODER" --execute-plan "$TEST_ROOT/tampered-plan.json" \
    --result-json "$TEST_ROOT/tampered-result.json" >"$TEST_ROOT/tampered.log" 2>&1; then
    fail 'a modified protocol-v2 plan was executed'
fi
assert_contains "$(<"$TEST_ROOT/tampered.log")" 'Plan integrity check failed'

printf 'changed after evaluation\n' >> "$plan_source"
if "$ENCODER" --execute-plan "$plan" --result-json "$TEST_ROOT/stale-result.json" \
    >"$TEST_ROOT/stale.log" 2>&1; then
    fail 'a plan with a changed source fingerprint was executed'
fi
assert_contains "$(<"$TEST_ROOT/stale.log")" 'source fingerprint changed'

python3 - "$PLANNER" "$plan" <<'PY'
import copy
import importlib.util
import json
import sys
spec = importlib.util.spec_from_file_location("av1plan_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with open(sys.argv[2], encoding="utf-8") as handle:
    plan = json.load(handle)
changed = copy.deepcopy(plan)
changed["fingerprints"]["implementation"]["value"] = "sha256:" + "0" * 64
assert module.calculate_plan_id(changed) != plan["plan_id"]
assert module.planning_quality_target({"metric":"vmaf", "target":92.0}) == (92.5, 0.5)
assert module.planning_quality_target({"metric":"vmaf", "target":99.8}) == (100.0, 0.5)
assert module.planning_quality_target({"metric":"ssim_percent", "target":98.0}) == (98.0, 0.0)
PY

printf 'All AV1Encode tests passed.\n'
