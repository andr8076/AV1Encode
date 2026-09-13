#!/usr/bin/env bash

# Per-file hardware decode acceleration for AV1Encode.
# This helper is sourced by AV1Encode.sh and deliberately treats decode
# acceleration as an optional optimization. A failed probe falls back to CPU
# decoding without changing the already-proven hardware AV1 encoder.

INPUT_DECODE_ARGS=()
ACTIVE_DECODER="software"
ACTIVE_DECODER_DETAIL="CPU / software decode"
HARDWARE_DECODE_PROBE_FRAMES="${AV1ENCODE_HWDECODE_PROBE_FRAMES:-12}"

av1_hwdecode_debug() {
    [[ "${DEBUG_HARDWARE:-no}" == yes ]] || return 0
    printf 'Hardware-decode debug: %s\n' "$*" >&2
}

av1_hwdecode_restore_software_path() {
    INPUT_DECODE_ARGS=()
    ACTIVE_DECODER="software"
    ACTIVE_DECODER_DETAIL="CPU / software decode"
    VIDEO_FILTER_ARGS=("${AV1_HWDECODE_BASE_FILTER_ARGS[@]}")
    VIDEO_OUTPUT_ARGS=("${AV1_HWDECODE_BASE_OUTPUT_ARGS[@]}")
    VIDEO_ENCODER_ARGS=("${AV1_HWDECODE_BASE_ENCODER_ARGS[@]}")
}

av1_hwdecode_candidate_path() {
    local sar_for_filter="${INPUT_VIDEO_SAR/:/\/}"

    case "$HW_TYPE" in
        vaapi)
            INPUT_DECODE_ARGS=(
                -hwaccel vaapi
                -hwaccel_device va
                -hwaccel_output_format vaapi
            )
            ACTIVE_DECODER="vaapi"
            ACTIVE_DECODER_DETAIL="VA-API hardware decode"
            VIDEO_FILTER_ARGS=(
                -filter:v:0
                "scale_vaapi=w=${INPUT_VIDEO_WIDTH}:h=${INPUT_VIDEO_HEIGHT}:format=${VAAPI_UPLOAD_FORMAT}:mode=hq,setsar=sar=${sar_for_filter}"
            )
            ;;
        nvidia)
            INPUT_DECODE_ARGS=(
                -hwaccel cuda
                -hwaccel_output_format cuda
            )
            ACTIVE_DECODER="nvdec"
            ACTIVE_DECODER_DETAIL="NVIDIA NVDEC/CUDA hardware decode"
            VIDEO_FILTER_ARGS=(
                -filter:v:0
                "scale_cuda=w=${INPUT_VIDEO_WIDTH}:h=${INPUT_VIDEO_HEIGHT}:format=p010le"
            )
            # CUDA hardware frames should be handed directly to NVENC. The
            # scale_cuda filter above owns the 10-bit conversion, so asking
            # FFmpeg for a software yuv420p10le pixel format would force an
            # unwanted download/upload transition.
            VIDEO_ENCODER_ARGS=(
                -c:v:0 av1_nvenc
                -rc:v:0 vbr
                -cq:v:0 "$HARDWARE_QP"
                -preset:v:0 slow
            )
            ;;
        intel)
            INPUT_DECODE_ARGS=(
                -hwaccel qsv
                -hwaccel_output_format qsv
            )
            ACTIVE_DECODER="qsv"
            ACTIVE_DECODER_DETAIL="Intel Quick Sync hardware decode"
            VIDEO_FILTER_ARGS=(
                -filter:v:0
                "vpp_qsv=w=${INPUT_VIDEO_WIDTH}:h=${INPUT_VIDEO_HEIGHT}:format=p010le"
            )
            # vpp_qsv owns the 10-bit conversion while keeping frames in QSV
            # surfaces, so do not request a software pixel format here.
            VIDEO_ENCODER_ARGS=(
                -c:v:0 av1_qsv
                -global_quality:v:0 "$HARDWARE_QP"
                -preset:v:0 slow
            )
            ;;
        *)
            return 1
            ;;
    esac
}

probe_hardware_decode_pipeline() {
    local input_file="$1"
    local probe_dir probe_output stderr_file status=1 codec
    local -a command=()

    [[ "$HARDWARE_DECODE_PROBE_FRAMES" =~ ^[0-9]+$ ]] || HARDWARE_DECODE_PROBE_FRAMES=12
    (( HARDWARE_DECODE_PROBE_FRAMES > 0 )) || HARDWARE_DECODE_PROBE_FRAMES=12

    probe_dir=$(mktemp -d "${TMPDIR:-/tmp}/av1encode-hwdecode.XXXXXX") || return 1
    probe_output="$probe_dir/probe.mkv"
    stderr_file="$probe_dir/stderr.log"

    command=(
        "${FFMPEG_COMMAND[@]}"
        -hide_banner
        -loglevel error
        -y
        "${FFMPEG_GLOBAL_ARGS[@]}"
        "${INPUT_DECODE_ARGS[@]}"
        -i "$input_file"
        -map 0:V:0
        "${VIDEO_FILTER_ARGS[@]}"
        "${VIDEO_OUTPUT_ARGS[@]}"
        "${VIDEO_ENCODER_ARGS[@]}"
        -frames:v "$HARDWARE_DECODE_PROBE_FRAMES"
        -an -sn -dn
        -f matroska
        "$probe_output"
    )

    av1_hwdecode_debug "probing exact source path with ${ACTIVE_DECODER}: $(printf '%q ' "${command[@]}")"
    if "${command[@]}" > /dev/null 2>"$stderr_file" && [[ -s "$probe_output" ]]; then
        codec=$(ffprobe -v error -select_streams V:0 -show_entries stream=codec_name \
            -of csv=p=0 "$probe_output" 2>/dev/null | head -n 1)
        [[ "$codec" == av1 ]] && status=0
    fi

    if (( status != 0 )) && [[ "${DEBUG_HARDWARE:-no}" == yes && -s "$stderr_file" ]]; then
        sed 's/^/Hardware-decode probe: /' "$stderr_file" >&2
    fi
    rm -rf -- "$probe_dir"
    return "$status"
}

configure_input_decode() {
    local input_file="$1"

    AV1_HWDECODE_BASE_FILTER_ARGS=("${VIDEO_FILTER_ARGS[@]}")
    AV1_HWDECODE_BASE_OUTPUT_ARGS=("${VIDEO_OUTPUT_ARGS[@]}")
    AV1_HWDECODE_BASE_ENCODER_ARGS=("${VIDEO_ENCODER_ARGS[@]}")
    av1_hwdecode_restore_software_path

    [[ "${ACTIVE_MODE:-software}" == hardware ]] || return 0

    if [[ "${AV1ENCODE_DISABLE_HWDECODE:-0}" == 1 ]]; then
        av1_hwdecode_debug "hardware decode disabled by AV1ENCODE_DISABLE_HWDECODE=1"
        return 0
    fi

    # --dry-run must remain side-effect free. The bounded capability probe is a
    # real encode, so command previews intentionally retain the safe CPU-decode
    # path rather than pretending hardware decode was proven.
    if [[ "${DRY_RUN:-no}" == yes ]]; then
        av1_hwdecode_debug "hardware decode not probed during --dry-run"
        return 0
    fi

    if ! av1_hwdecode_candidate_path; then
        av1_hwdecode_restore_software_path
        return 0
    fi

    if probe_hardware_decode_pipeline "$input_file"; then
        printf 'Hardware decode: proven for this source (%s).\n' "$ACTIVE_DECODER_DETAIL"
        return 0
    fi

    av1_hwdecode_debug "${ACTIVE_DECODER_DETAIL} failed the bounded source probe; reverting decode to CPU"
    av1_hwdecode_restore_software_path
    printf 'Hardware decode: unavailable for this source; using CPU decode with %s.\n' "${ACTIVE_ENCODER:-hardware AV1 encoding}"
    return 0
}
