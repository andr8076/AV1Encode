#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


encoder = ROOT / "AV1Encode.sh"
planner = ROOT / "tools" / "AV1Plan.py"
tests = ROOT / "tests" / "test.sh"

replace_once(
    encoder,
    """# AV1Encode 1.3.5, derived from the 265Encode workflow.\n# The VA-API filter chain now normalizes every frame to the input stream's initial\n# dimensions before it reaches the encoder, preventing an incompatible software\n# auto-scaler from being inserted after hwupload.\n# AV1 batch encoder with both interactive and command-line operation.\n# On Linux/AMD, input decoding stays on the CPU and decoded frames are\n# uploaded to the GPU for AV1 encoding through VA-API.\n""",
    """# AV1Encode 1.4.0, derived from the 265Encode workflow.\n# Hardware AV1 encoding remains capability-proven and hardware-only in AUTO.\n# Per-file hardware decoding is now an optional, independently proven acceleration:\n# AV1Encode probes the exact source through the selected GPU decode/encode path and\n# falls back only the decode side to CPU when that bounded probe fails.\n# AV1 batch encoder with both interactive and command-line operation.\n""",
)
replace_once(encoder, 'SCRIPT_VERSION="1.3.5"', 'SCRIPT_VERSION="1.4.0"')
replace_once(
    encoder,
    'MACHINE_RESULT_WRITTEN="no"\n',
    'MACHINE_RESULT_WRITTEN="no"\n\nSCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)\n# shellcheck source=tools/AV1HardwareDecode.sh\nsource "$SCRIPT_DIR/tools/AV1HardwareDecode.sh"\n',
)
replace_once(
    encoder,
    '"fingerprint_invalidation":true,"sampled_predictions":true},',
    '"fingerprint_invalidation":true,"sampled_predictions":true,"capability_proven_hardware_decode":true},',
)
replace_once(
    encoder,
    '    analyze_video "$input_file" || return 0\n    build_file_video_filter\n',
    '    analyze_video "$input_file" || return 0\n    build_file_video_filter\n    configure_input_decode "$input_file"\n',
)
replace_once(
    encoder,
    '        "${FFMPEG_GLOBAL_ARGS[@]}"\n        -i "$input_file"\n',
    '        "${FFMPEG_GLOBAL_ARGS[@]}"\n        "${INPUT_DECODE_ARGS[@]}"\n        -i "$input_file"\n',
)

replace_once(
    planner,
    '    files = [root / "AV1Encode.sh", root / "tools" / "AV1Plan.py", root / "tools" / "AV1Compare.py"]\n',
    '    files = [\n        root / "AV1Encode.sh",\n        root / "tools" / "AV1Plan.py",\n        root / "tools" / "AV1Compare.py",\n        root / "tools" / "AV1HardwareDecode.sh",\n    ]\n',
)

replace_once(
    tests,
    'bash -n "$ENCODER"\n',
    'bash -n "$ENCODER"\nbash -n "$ROOT/tools/AV1HardwareDecode.sh"\n',
)
replace_once(
    tests,
    "[[ $(\"$ENCODER\" --version) == 'AV1Encode.sh 1.3.5' ]] || fail 'unexpected encoder version'",
    "[[ $(\"$ENCODER\" --version) == 'AV1Encode.sh 1.4.0' ]] || fail 'unexpected encoder version'",
)
replace_once(
    tests,
    'assert report["features"]["fingerprint_invalidation"] is True\n',
    'assert report["features"]["fingerprint_invalidation"] is True\nassert report["features"]["capability_proven_hardware_decode"] is True\n',
)

hardware_decode_tests = r'''
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
    [[ " ${INPUT_DECODE_ARGS[*]} " == *' -hwaccel vaapi '* ]] || fail 'VA-API decode arguments were not installed'
    [[ " ${VIDEO_FILTER_ARGS[*]} " == *scale_vaapi* && " ${VIDEO_FILTER_ARGS[*]} " != *hwupload* ]] || \
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
    [[ " ${VIDEO_FILTER_ARGS[*]} " == *hwupload* ]] || fail 'CPU-decode fallback did not restore the software-to-VAAPI upload path'
    [[ " ${VIDEO_ENCODER_ARGS[*]} " == *av1_vaapi* ]] || fail 'decode fallback changed the proven hardware AV1 encoder'
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
    [[ " ${INPUT_DECODE_ARGS[*]} " == *' -hwaccel cuda '* ]] || fail 'CUDA decode arguments were not installed'
    [[ " ${VIDEO_FILTER_ARGS[*]} " == *scale_cuda* ]] || fail 'NVDEC path is not keeping conversion on CUDA surfaces'
    [[ " ${VIDEO_ENCODER_ARGS[*]} " != *pix_fmt* ]] || fail 'NVDEC path requests a software pixel format before NVENC'
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
    [[ " ${INPUT_DECODE_ARGS[*]} " == *' -hwaccel qsv '* ]] || fail 'QSV decode arguments were not installed'
    [[ " ${VIDEO_FILTER_ARGS[*]} " == *vpp_qsv* ]] || fail 'QSV decode path is not keeping conversion on QSV surfaces'
    [[ " ${VIDEO_ENCODER_ARGS[*]} " != *pix_fmt* ]] || fail 'QSV decode path requests a software pixel format before AV1 QSV'
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
'''
replace_once(
    tests,
    '\nHARDWARE_QP=118\nconfigure_encoder\n',
    '\n' + hardware_decode_tests + '\nHARDWARE_QP=118\nconfigure_encoder\n',
)

print("Hardware decode integration patch applied.")
