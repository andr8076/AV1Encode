#!/usr/bin/env bash

# AV1Encode 1.4.0, derived from the 265Encode workflow.
# Hardware AV1 encoding remains capability-proven and hardware-only in AUTO.
# Per-file hardware decoding is now an optional, independently proven acceleration:
# AV1Encode probes the exact source through the selected GPU decode/encode path and
# falls back only the decode side to CPU when that bounded probe fails.
# AV1 batch encoder with both interactive and command-line operation.

set -o pipefail

SCRIPT_NAME="${0##*/}"
SCRIPT_VERSION="1.4.0"
MACHINE_INTERFACE_VERSION="1"
LATEST_MACHINE_INTERFACE_VERSION="2"
COMMON_EXTENSIONS=(mp4 mkv mov avi webm m4v ts mts m2ts wmv flv)
HARDWARE_PROBE_SIZE="256x256"

# Values left empty here are either requested interactively or filled with
# command-line defaults after argument parsing.
INTERACTIVE_MODE=""
INPUT_PATH=""
MODE=""
RECURSIVE=""
USE_ALL_EXTENSIONS=""
SKIP_AV1=""
OVERWRITE_MODE=""
START_CONFIRM=""
DRY_RUN="no"
LIST_HARDWARE_ONLY="no"
DEBUG_HARDWARE="no"
MACHINE_MODE="no"
MACHINE_ACTION="encode"
FORCED_ENCODER="auto"
OUTPUT_PATH_OVERRIDE=""
RESULT_JSON_PATH=""
PRESERVE_ALL_STREAMS="no"
SOFTWARE_CRF="30"
SOFTWARE_PRESET="6"
HARDWARE_QP="24"
AUDIO_MODE="aac"
AUDIO_BITRATE="192k"
OUTPUT_EXTENSION="mp4"
VAAPI_DEVICE_OVERRIDE=""
ALLOWED_EXTENSIONS=()
FILES=()
ACTIVE_ENCODER_ID=""
LAST_OUTPUT_FILE=""
LAST_RESULT_STATUS="not_started"
MACHINE_RESULT_WRITTEN="no"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/AV1HardwareDecode.sh
source "$SCRIPT_DIR/tools/AV1HardwareDecode.sh"

usage() {
    cat <<EOF_USAGE
Usage:
  $SCRIPT_NAME
  $SCRIPT_NAME [options] FILE_OR_FOLDER
  $SCRIPT_NAME [options] --input FILE_OR_FOLDER

With no arguments, the script asks for the input and automatically selects a
proven hardware AV1 encoder with its recommended settings.
With command-line arguments, it runs non-interactively unless --interactive
or --confirm is supplied.

Input and traversal:
  -i, --input PATH          Video file or folder to process
  -r, --recursive          Search folders recursively
      --no-recursive       Search only the selected folder
  -e, --extensions LIST    Extensions separated by commas or spaces
      --common-extensions  Use the built-in common video extensions
      --skip-av1           Skip input already encoded as AV1
      --process-av1        Allow AV1 input to be re-encoded

Encoding:
  -m, --mode MODE          auto, software, or hardware
      --auto               Automatically select a working hardware encoder
      --software           Explicitly allow libsvtav1 CPU encoding
      --hardware           Require hardware AV1 encoding
      --crf NUMBER         libsvtav1 CRF (0-63), default: 30
      --preset LEVEL       libsvtav1 preset (0-13), default: 6
      --qp NUMBER          Hardware constant-quality QP, default: 24
                           NVENC/QSV: 0-51; VA-API: 0-255
      --vaapi-device PATH  Force a VA-API render node, for example
                           /dev/dri/renderD128

Audio and output:
      --audio-bitrate RATE AAC bitrate, default: 192k
      --copy-audio         Copy the first audio stream instead of AAC encoding
      --aac-audio          Encode the first audio stream as AAC
      --container TYPE     mp4 or mkv, default: mp4
      --overwrite          Replace an existing output file
      --skip-existing      Skip an existing output file; CLI default

Operation:
      --interactive        Use prompts while honoring supplied options
      --confirm            Ask before starting in command-line mode
  -y, --yes                Do not ask for start confirmation
      --dry-run            Show FFmpeg commands without running them
      --list-hardware      Detect and display the available AV1 path
      --debug-hardware     Show hardware probe commands and full errors

Dependency interface:
      --machine            Non-interactive single-file encoding for callers
      --machine-probe      Print the versioned encoder capability JSON and exit
      --interface-version  Print the machine-interface version and exit
      --machine-negotiate VERSIONS
                           Select the newest supported version from a comma-separated list
      --machine-evaluate REQUIREMENTS.json --plan-json PLAN.json
      --machine-plan REQUIREMENTS.json --plan-json PLAN.json
                           Evaluate semantic protocol-v2 requirements and write a sealed plan
      --execute-plan PLAN.json --result-json RESULT.json
                           Validate fingerprints and execute an unchanged protocol-v2 plan
      --encoder NAME       auto, av1_vaapi, av1_nvenc, av1_qsv, or libsvtav1
      --output PATH        Exact .mp4 or .mkv destination; requires --machine
      --result-json PATH   Write an atomic machine-readable result; requires --machine
      --preserve-all       Preserve all streams, chapters, and metadata in MKV;
                           requires --machine and --copy-audio is recommended
  -h, --help               Show this help
      --version            Show the script version

Command-line defaults:
  mode=auto (hardware only), recursive=no, common extensions, process AV1, AAC 192k,
  container=mp4, and skip existing output files.

Examples:
  $SCRIPT_NAME --hardware --skip-av1 "movie.mkv"

  $SCRIPT_NAME --hardware --recursive --skip-av1 --container mkv "/path/to/videos"

  $SCRIPT_NAME --software --crf 28 --preset 6 --copy-audio "movie.mkv"

  $SCRIPT_NAME --interactive --input "/path/to/videos" --hardware
EOF_USAGE
}

error() {
    echo "Error: $*" >&2
}

require_value() {
    local option="$1"
    local value="${2-}"

    if [[ -z "$value" ]]; then
        error "$option requires a value."
        exit 2
    fi
}

is_integer_in_range() {
    local value="$1"
    local minimum="$2"
    local maximum="$3"

    [[ "$value" =~ ^[0-9]+$ ]] &&
        (( value >= minimum && value <= maximum ))
}

parse_extension_list() {
    local extension_text="$1"
    local item

    extension_text="${extension_text//,/ }"
    read -r -a ALLOWED_EXTENSIONS <<< "$extension_text"

    if [[ ${#ALLOWED_EXTENSIONS[@]} -eq 0 ]]; then
        error "No extensions were provided."
        exit 2
    fi

    for item in "${!ALLOWED_EXTENSIONS[@]}"; do
        ALLOWED_EXTENSIONS[$item]="${ALLOWED_EXTENSIONS[$item]#.}"
        ALLOWED_EXTENSIONS[$item]="${ALLOWED_EXTENSIONS[$item],,}"
    done

    USE_ALL_EXTENSIONS="no"
}

parse_arguments() {
    local original_arg_count="$#"

    if (( original_arg_count == 0 )); then
        INTERACTIVE_MODE="yes"
        return
    fi

    INTERACTIVE_MODE="no"

    while (( $# > 0 )); do
        case "$1" in
            -h|--help)
                usage
                exit 0
                ;;
            --version)
                echo "$SCRIPT_NAME $SCRIPT_VERSION"
                exit 0
                ;;
            --interface-version)
                echo "$LATEST_MACHINE_INTERFACE_VERSION"
                exit 0
                ;;
            -i|--input)
                require_value "$1" "${2-}"
                INPUT_PATH="$2"
                shift 2
                ;;
            -r|--recursive)
                RECURSIVE="yes"
                shift
                ;;
            --no-recursive)
                RECURSIVE="no"
                shift
                ;;
            -e|--extensions)
                require_value "$1" "${2-}"
                parse_extension_list "$2"
                shift 2
                ;;
            --common-extensions)
                USE_ALL_EXTENSIONS="yes"
                ALLOWED_EXTENSIONS=()
                shift
                ;;
            --skip-av1)
                SKIP_AV1="yes"
                shift
                ;;
            --process-av1)
                SKIP_AV1="no"
                shift
                ;;
            -m|--mode)
                require_value "$1" "${2-}"
                MODE="${2,,}"
                shift 2
                ;;
            --auto)
                MODE="auto"
                shift
                ;;
            --software)
                MODE="software"
                shift
                ;;
            --hardware)
                MODE="hardware"
                shift
                ;;
            --crf)
                require_value "$1" "${2-}"
                SOFTWARE_CRF="$2"
                shift 2
                ;;
            --preset)
                require_value "$1" "${2-}"
                SOFTWARE_PRESET="$2"
                shift 2
                ;;
            --qp)
                require_value "$1" "${2-}"
                HARDWARE_QP="$2"
                shift 2
                ;;
            --vaapi-device)
                require_value "$1" "${2-}"
                VAAPI_DEVICE_OVERRIDE="$2"
                shift 2
                ;;
            --audio-bitrate)
                require_value "$1" "${2-}"
                AUDIO_BITRATE="$2"
                shift 2
                ;;
            --copy-audio)
                AUDIO_MODE="copy"
                shift
                ;;
            --aac-audio)
                AUDIO_MODE="aac"
                shift
                ;;
            --container)
                require_value "$1" "${2-}"
                OUTPUT_EXTENSION="${2,,}"
                shift 2
                ;;
            --overwrite)
                OVERWRITE_MODE="yes"
                shift
                ;;
            --skip-existing)
                OVERWRITE_MODE="no"
                shift
                ;;
            --interactive)
                INTERACTIVE_MODE="yes"
                shift
                ;;
            --confirm)
                START_CONFIRM="yes"
                shift
                ;;
            -y|--yes)
                START_CONFIRM="no"
                shift
                ;;
            --dry-run)
                DRY_RUN="yes"
                shift
                ;;
            --list-hardware)
                LIST_HARDWARE_ONLY="yes"
                shift
                ;;
            --debug-hardware)
                DEBUG_HARDWARE="yes"
                shift
                ;;
            --machine)
                MACHINE_MODE="yes"
                MACHINE_ACTION="encode"
                INTERACTIVE_MODE="no"
                START_CONFIRM="no"
                shift
                ;;
            --machine-probe)
                MACHINE_MODE="yes"
                MACHINE_ACTION="probe"
                INTERACTIVE_MODE="no"
                START_CONFIRM="no"
                shift
                ;;
            --encoder)
                require_value "$1" "${2-}"
                FORCED_ENCODER="${2,,}"
                shift 2
                ;;
            --output)
                require_value "$1" "${2-}"
                OUTPUT_PATH_OVERRIDE="$2"
                shift 2
                ;;
            --result-json)
                require_value "$1" "${2-}"
                RESULT_JSON_PATH="$2"
                shift 2
                ;;
            --preserve-all)
                PRESERVE_ALL_STREAMS="yes"
                shift
                ;;
            --)
                shift
                while (( $# > 0 )); do
                    if [[ -n "$INPUT_PATH" ]]; then
                        error "Only one input file or folder may be specified."
                        exit 2
                    fi
                    INPUT_PATH="$1"
                    shift
                done
                ;;
            -*)
                error "Unknown option: $1"
                echo "Run '$SCRIPT_NAME --help' for usage." >&2
                exit 2
                ;;
            *)
                if [[ -n "$INPUT_PATH" ]]; then
                    error "Only one input file or folder may be specified."
                    exit 2
                fi
                INPUT_PATH="$1"
                shift
                ;;
        esac
    done
}

validate_options() {
    case "$MODE" in
        ""|auto|software|hardware) ;;
        *)
            error "Invalid mode '$MODE'. Use auto, software, or hardware."
            exit 2
            ;;
    esac

    if ! is_integer_in_range "$SOFTWARE_CRF" 0 63; then
        error "--crf must be an integer from 0 to 63."
        exit 2
    fi

    if ! is_integer_in_range "$SOFTWARE_PRESET" 0 13; then
        error "--preset must be an integer from 0 to 13."
        exit 2
    fi

    # VA-API AV1 exposes FFmpeg's 0..255 global-quality scale. NVENC and
    # QSV retain their narrower 0..51 range and are checked after hardware
    # selection, once AUTO has resolved to a concrete backend.
    if ! is_integer_in_range "$HARDWARE_QP" 0 255; then
        error "--qp must be an integer from 0 to 255."
        exit 2
    fi

    case "$OUTPUT_EXTENSION" in
        mp4|mkv) ;;
        *)
            error "--container must be mp4 or mkv."
            exit 2
            ;;
    esac

    case "$FORCED_ENCODER" in
        auto|av1_vaapi|av1_nvenc|av1_qsv|libsvtav1) ;;
        *)
            error "Invalid --encoder '$FORCED_ENCODER'."
            exit 2
            ;;
    esac

    if [[ "$MACHINE_MODE" != "yes" && ( -n "$OUTPUT_PATH_OVERRIDE" || -n "$RESULT_JSON_PATH" ) ]]; then
        error "--output and --result-json require --machine."
        exit 2
    fi

    if [[ "$PRESERVE_ALL_STREAMS" == yes && "$MACHINE_MODE" != yes ]]; then
        error "--preserve-all requires --machine."
        exit 2
    fi

    if [[ "$MACHINE_MODE" == "yes" && "$INTERACTIVE_MODE" == "yes" ]]; then
        error "--machine cannot be combined with --interactive."
        exit 2
    fi

    if [[ "$MACHINE_MODE" == "yes" && "$DRY_RUN" == "yes" ]]; then
        error "--machine cannot be combined with --dry-run."
        exit 2
    fi

    if [[ "$MACHINE_ACTION" == probe && -n "$RESULT_JSON_PATH" ]]; then
        error "--result-json is used with --machine encoding, not --machine-probe."
        exit 2
    fi

    if [[ -n "$OUTPUT_PATH_OVERRIDE" ]]; then
        case "${OUTPUT_PATH_OVERRIDE##*.}" in
            mp4|MP4) OUTPUT_EXTENSION="mp4" ;;
            mkv|MKV) OUTPUT_EXTENSION="mkv" ;;
            *)
                error "--output must end in .mp4 or .mkv."
                exit 2
                ;;
        esac
        if [[ ! -d $(dirname -- "$OUTPUT_PATH_OVERRIDE") ]]; then
            error "Output directory does not exist: $(dirname -- "$OUTPUT_PATH_OVERRIDE")"
            exit 2
        fi
    fi

    if [[ "$PRESERVE_ALL_STREAMS" == yes && "$OUTPUT_EXTENSION" != mkv ]]; then
        error "--preserve-all requires an .mkv output."
        exit 2
    fi

    if [[ -n "$VAAPI_DEVICE_OVERRIDE" && ! -e "$VAAPI_DEVICE_OVERRIDE" ]]; then
        error "VA-API device does not exist: $VAAPI_DEVICE_OVERRIDE"
        exit 2
    fi
}

check_dependencies() {
    local tool

    for tool in ffmpeg ffprobe; do
        if ! command -v "$tool" &>/dev/null; then
            error "$tool is not installed. Please install it to continue."
            exit 1
        fi
    done
}

debug_log() {
    if [[ "$DEBUG_HARDWARE" == "yes" ]]; then
        printf '[hardware debug] %s\n' "$*" >&2
    fi
}

debug_print_command() {
    local argument

    [[ "$DEBUG_HARDWARE" == "yes" ]] || return 0

    printf '[hardware debug] command:' >&2
    for argument in "$@"; do
        printf ' %q' "$argument" >&2
    done
    printf '\n' >&2
}

run_hardware_probe() {
    local label="$1"
    shift

    local output
    local status
    local command=("$@")
    local runner=()

    if command -v timeout >/dev/null 2>&1; then
        runner=(timeout --kill-after=3 30)
    fi

    debug_log "$label"
    debug_print_command "${runner[@]}" "${command[@]}"

    # Capture FFmpeg's diagnostics while keeping normal probe output quiet.
    output="$("${runner[@]}" "${command[@]}" </dev/null 2>&1 >/dev/null)"
    status=$?

    if [[ "$DEBUG_HARDWARE" == "yes" ]]; then
        if [[ -n "$output" ]]; then
            while IFS= read -r line; do
                printf '[hardware debug]   %s\n' "$line" >&2
            done <<< "$output"
        fi

        if (( status == 0 )); then
            debug_log "result: success"
        else
            debug_log "result: failed (exit code $status)"
        fi
    fi

    return "$status"
}

validate_hardware_probe_output() {
    local output_file="$1"
    local codec

    [[ -s "$output_file" ]] || {
        debug_log "Probe produced no output file."
        return 1
    }

    codec="$(ffprobe -v error -select_streams V:0 \
        -show_entries stream=codec_name -of csv=p=0 "$output_file" 2>/dev/null | head -n 1)"
    [[ "$codec" == "av1" ]] || {
        debug_log "Probe output codec was '${codec:-unreadable}', not AV1."
        return 1
    }

    run_hardware_probe "Decoding and validating the AV1 probe output" \
        ffmpeg -hide_banner -loglevel error -xerror -i "$output_file" \
        -map 0:V:0 -f null -
}

encoder_available() {
    local encoder="$1"

    if ffmpeg -hide_banner -encoders 2>/dev/null |
        awk '{print $2}' |
        grep -Fxq "$encoder"; then
        debug_log "FFmpeg lists encoder: $encoder"
        return 0
    fi

    debug_log "FFmpeg does not list encoder: $encoder"
    return 1
}

test_simple_encoder() {
    local encoder="$1"
    local pixel_format="$2"
    local probe_dir
    local probe_output
    local status=1
    local encoder_args=()

    case "$encoder" in
        av1_nvenc)
            encoder_args=(-rc vbr -cq "$HARDWARE_QP" -preset slow)
            ;;
        av1_qsv)
            encoder_args=(-global_quality "$HARDWARE_QP" -preset slow)
            ;;
        *)
            debug_log "No production probe settings are defined for $encoder."
            return 1
            ;;
    esac

    probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/av1encode-hardware-probe.XXXXXX")" || return 1
    probe_output="$probe_dir/output.mkv"
    local command=(
        ffmpeg -hide_banner -loglevel error
        -f lavfi -i "color=black:size=${HARDWARE_PROBE_SIZE}:rate=8"
        -frames:v 8
        -an -sn -dn
        -c:v "$encoder"
        "${encoder_args[@]}"
        -pix_fmt "$pixel_format"
        -f matroska "$probe_output"
    )

    if run_hardware_probe "Testing $encoder with $pixel_format" "${command[@]}" &&
       validate_hardware_probe_output "$probe_output"; then
        status=0
    fi
    rm -rf -- "$probe_dir"
    return "$status"
}

test_software_encoder() {
    local probe_dir probe_output status=1
    local command=()

    probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/av1encode-software-probe.XXXXXX")" || return 1
    probe_output="$probe_dir/output.mkv"
    command=(
        ffmpeg -hide_banner -loglevel error
        -f lavfi -i "color=black:size=${HARDWARE_PROBE_SIZE}:rate=8"
        -frames:v 8 -an -sn -dn
        -c:v libsvtav1 -crf 45 -preset 13 -pix_fmt yuv420p
        -f matroska "$probe_output"
    )

    if run_hardware_probe "Testing manual-only libsvtav1" "${command[@]}" &&
       validate_hardware_probe_output "$probe_output"; then
        status=0
    fi
    rm -rf -- "$probe_dir"
    return "$status"
}

test_vaapi_device() {
    local device="$1"
    local upload_format="$2"
    local probe_dir
    local probe_output
    local status=1

    probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/av1encode-vaapi-probe.XXXXXX")" || return 1
    probe_output="$probe_dir/output.mkv"
    local command=(
        ffmpeg -hide_banner -loglevel error
        -init_hw_device "vaapi=va:${device}"
        -filter_hw_device va
        -f lavfi -i "color=black:size=${HARDWARE_PROBE_SIZE}:rate=8"
        -frames:v 8
        -an -sn -dn
        -vf "format=${upload_format},hwupload,scale_vaapi=w=${HARDWARE_PROBE_SIZE%x*}:h=${HARDWARE_PROBE_SIZE#*x}:format=${upload_format}:mode=hq"
        -c:v av1_vaapi
        -rc_mode CQP
        -qp "$HARDWARE_QP"
        -f matroska "$probe_output"
    )

    # Do not force Main or Main10 here. The VA-API encoder chooses the profile
    # from nv12 or p010le. Forcing a profile can create false probe failures on
    # otherwise working Mesa/VA-API combinations.
    if run_hardware_probe \
        "Testing VA-API device $device with upload format $upload_format" \
        "${command[@]}" && validate_hardware_probe_output "$probe_output"; then
        status=0
    fi
    rm -rf -- "$probe_dir"
    return "$status"
}

show_vaapi_environment() {
    local device
    local devices=()

    [[ "$DEBUG_HARDWARE" == "yes" ]] || return 0

    debug_log "FFmpeg: $(ffmpeg -version 2>/dev/null | head -n 1)"
    debug_log "User: $(id 2>/dev/null)"
    debug_log "LIBVA_DRIVER_NAME=${LIBVA_DRIVER_NAME-<unset>}"

    shopt -s nullglob
    devices=(/dev/dri/renderD*)
    shopt -u nullglob

    if [[ ${#devices[@]} -eq 0 ]]; then
        debug_log "No /dev/dri/renderD* devices were found."
        return 0
    fi

    for device in "${devices[@]}"; do
        debug_log "Render device: $(ls -l "$device" 2>/dev/null || printf '%s' "$device")"
        if [[ -r "$device" && -w "$device" ]]; then
            debug_log "Current user has read/write access to $device"
        else
            debug_log "Current user does not have read/write access to $device"
        fi
    done
}

configure_vaapi() {
    local device
    local devices=()

    VAAPI_DEVICE=""
    VAAPI_UPLOAD_FORMAT=""
    VAAPI_PROFILE=""
    VAAPI_BIT_DEPTH=""

    show_vaapi_environment

    if ! encoder_available "av1_vaapi"; then
        debug_log "VA-API cannot be used because av1_vaapi is absent from FFmpeg."
        return 1
    fi

    if [[ -n "$VAAPI_DEVICE_OVERRIDE" ]]; then
        devices=("$VAAPI_DEVICE_OVERRIDE")
        debug_log "Using forced VA-API device: $VAAPI_DEVICE_OVERRIDE"
    else
        shopt -s nullglob
        devices=(/dev/dri/renderD*)
        shopt -u nullglob
    fi

    if [[ ${#devices[@]} -eq 0 ]]; then
        debug_log "No VA-API render nodes are available."
        return 1
    fi

    # Prefer 10-bit Main10 and fall back to 8-bit Main.
    for device in "${devices[@]}"; do
        if test_vaapi_device "$device" "p010le"; then
            VAAPI_DEVICE="$device"
            VAAPI_UPLOAD_FORMAT="p010le"
            VAAPI_PROFILE="main10"
            VAAPI_BIT_DEPTH="10-bit"
            debug_log "Selected VA-API device $device in 10-bit mode."
            return 0
        fi
    done

    for device in "${devices[@]}"; do
        if test_vaapi_device "$device" "nv12"; then
            VAAPI_DEVICE="$device"
            VAAPI_UPLOAD_FORMAT="nv12"
            VAAPI_PROFILE="main"
            VAAPI_BIT_DEPTH="8-bit"
            debug_log "Selected VA-API device $device in 8-bit mode."
            return 0
        fi
    done

    debug_log "All VA-API AV1 probes failed."
    return 1
}

json_string() {
    local value="${1-}"
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    printf '"%s"' "$value"
}

portable_file_size() {
    local path="$1" size
    size=$(stat -c '%s' -- "$path" 2>/dev/null || true)
    [[ $size =~ ^[0-9]+$ ]] || size=$(stat -f '%z' -- "$path" 2>/dev/null || true)
    [[ $size =~ ^[0-9]+$ ]] || size=0
    printf '%s' "$size"
}

write_machine_result() {
    local status="$1" exit_code="$2" result_dir temporary bytes=0 encoder_class=""
    [[ "$MACHINE_MODE" == yes && -n "$RESULT_JSON_PATH" ]] || return 0
    [[ "$MACHINE_RESULT_WRITTEN" != yes ]] || return 0

    result_dir=$(dirname -- "$RESULT_JSON_PATH")
    [[ -d $result_dir ]] || {
        error "Result directory does not exist: $result_dir"
        return 1
    }
    if [[ -n "$LAST_OUTPUT_FILE" && -s "$LAST_OUTPUT_FILE" ]]; then
        bytes=$(portable_file_size "$LAST_OUTPUT_FILE")
    fi
    [[ "$ACTIVE_ENCODER_ID" == libsvtav1 ]] && encoder_class=software
    [[ -n "$ACTIVE_ENCODER_ID" && "$ACTIVE_ENCODER_ID" != libsvtav1 ]] && encoder_class=hardware

    temporary=$(mktemp "${RESULT_JSON_PATH}.XXXXXX") || return 1
    {
        printf '{"schema":"av1encode.result","protocol_version":%s,' "$MACHINE_INTERFACE_VERSION"
        printf '"tool":{"name":"AV1Encode","version":%s},' "$(json_string "$SCRIPT_VERSION")"
        printf '"codec":"av1","status":%s,"exit_code":%s,' "$(json_string "$status")" "$exit_code"
        printf '"input":%s,"output":%s,' "$(json_string "$INPUT_PATH")" "$(json_string "$LAST_OUTPUT_FILE")"
        if [[ -n "$ACTIVE_ENCODER_ID" ]]; then
            printf '"encoder":%s,"encoder_class":%s,' \
                "$(json_string "$ACTIVE_ENCODER_ID")" "$(json_string "$encoder_class")"
        else
            printf '"encoder":null,"encoder_class":null,'
        fi
        printf '"auto_policy":"hardware_only","preserve_all":%s,"output_bytes":%s}\n' \
            "$([[ $PRESERVE_ALL_STREAMS == yes ]] && printf true || printf false)" "$bytes"
    } > "$temporary" || { rm -f -- "$temporary"; return 1; }
    mv -f -- "$temporary" "$RESULT_JSON_PATH" || { rm -f -- "$temporary"; return 1; }
    MACHINE_RESULT_WRITTEN=yes
}

machine_exit_handler() {
    local exit_code=$?
    if [[ "$MACHINE_RESULT_WRITTEN" != yes ]]; then
        write_machine_result failed "$exit_code" || true
    fi
}

machine_probe_encoder() {
    local encoder="$1"
    MACHINE_PROBE_ADVERTISED=false
    MACHINE_PROBE_USABLE=false
    MACHINE_PROBE_DETAIL=""

    if encoder_available "$encoder"; then
        MACHINE_PROBE_ADVERTISED=true
    fi

    case "$encoder" in
        av1_vaapi)
            if configure_vaapi; then
                MACHINE_PROBE_USABLE=true
                MACHINE_PROBE_DETAIL="${VAAPI_DEVICE}, ${VAAPI_BIT_DEPTH}"
            fi
            ;;
        av1_nvenc)
            if [[ "$MACHINE_PROBE_ADVERTISED" == true ]] && test_simple_encoder av1_nvenc p010le; then
                MACHINE_PROBE_USABLE=true
                MACHINE_PROBE_DETAIL="NVIDIA NVENC"
            fi
            ;;
        av1_qsv)
            if [[ "$MACHINE_PROBE_ADVERTISED" == true ]] && test_simple_encoder av1_qsv p010le; then
                MACHINE_PROBE_USABLE=true
                MACHINE_PROBE_DETAIL="Intel Quick Sync"
            fi
            ;;
        libsvtav1)
            if [[ "$MACHINE_PROBE_ADVERTISED" == true ]] && test_software_encoder; then
                MACHINE_PROBE_USABLE=true
                MACHINE_PROBE_DETAIL="SVT-AV1 software encoder"
            fi
            ;;
    esac
}

machine_encoder_json() {
    local name="$1" class="$2" advertised="$3" usable="$4" detail="$5"
    printf '{"name":%s,"class":%s,"auto_eligible":%s,"advertised":%s,"usable":%s,"detail":%s}' \
        "$(json_string "$name")" "$(json_string "$class")" \
        "$([[ $class == hardware ]] && printf true || printf false)" \
        "$advertised" "$usable" "$(json_string "$detail")"
}

show_machine_capabilities() {
    local ffmpeg_version auto_encoder="" first=true encoder class record
    local -a records=()

    ffmpeg_version=$(ffmpeg -hide_banner -version 2>/dev/null | head -n 1)
    for encoder in av1_vaapi av1_nvenc av1_qsv libsvtav1; do
        machine_probe_encoder "$encoder"
        class=hardware
        [[ $encoder == libsvtav1 ]] && class=software
        records+=("$(machine_encoder_json "$encoder" "$class" "$MACHINE_PROBE_ADVERTISED" \
            "$MACHINE_PROBE_USABLE" "$MACHINE_PROBE_DETAIL")")
        if [[ -z $auto_encoder && $class == hardware && $MACHINE_PROBE_USABLE == true ]]; then
            auto_encoder=$encoder
        fi
    done

    printf '{"schema":"av1encode.capabilities","protocol_version":%s,' "$MACHINE_INTERFACE_VERSION"
    printf '"tool":{"name":"AV1Encode","version":%s},' "$(json_string "$SCRIPT_VERSION")"
    printf '"supported_protocol_versions":[1,2],'
    printf '"codec":"av1","auto_policy":"hardware_only","ffmpeg":%s,' "$(json_string "$ffmpeg_version")"
    printf '"features":{"exact_output":true,"atomic_result":true,"preserve_all":true,"full_decode_validation":true,"semantic_planning":true,"opaque_plan_id":true,"fingerprint_invalidation":true,"sampled_predictions":true,"capability_proven_hardware_decode":true},'
    if [[ -n $auto_encoder ]]; then
        printf '"auto_encoder":%s,' "$(json_string "$auto_encoder")"
    else
        printf '"auto_encoder":null,'
    fi
    printf '"encoders":['
    for record in "${records[@]}"; do
        [[ $first == true ]] || printf ','
        first=false
        printf '%s' "$record"
    done
    printf ']}\n'
}

detect_hw() {
    HW_TYPE="none"
    HW_DETAIL=""

    # VA-API is the normal AMD path on Linux. Test it first because FFmpeg can
    # list encoders for hardware that is not actually present or usable.
    if configure_vaapi; then
        HW_TYPE="vaapi"
        HW_DETAIL="${VAAPI_DEVICE}, ${VAAPI_BIT_DEPTH} AV1"
    elif encoder_available "av1_nvenc" && test_simple_encoder "av1_nvenc" "p010le"; then
        HW_TYPE="nvidia"
        HW_DETAIL="NVIDIA NVENC"
    elif encoder_available "av1_qsv" && test_simple_encoder "av1_qsv" "p010le"; then
        HW_TYPE="intel"
        HW_DETAIL="Intel Quick Sync"
    fi
}

detect_forced_hardware() {
    HW_TYPE="none"
    HW_DETAIL=""
    case "$FORCED_ENCODER" in
        av1_vaapi)
            if configure_vaapi; then
                HW_TYPE="vaapi"
                HW_DETAIL="${VAAPI_DEVICE}, ${VAAPI_BIT_DEPTH} AV1"
            fi
            ;;
        av1_nvenc)
            if encoder_available av1_nvenc && test_simple_encoder av1_nvenc p010le; then
                HW_TYPE="nvidia"
                HW_DETAIL="NVIDIA NVENC"
            fi
            ;;
        av1_qsv)
            if encoder_available av1_qsv && test_simple_encoder av1_qsv p010le; then
                HW_TYPE="intel"
                HW_DETAIL="Intel Quick Sync"
            fi
            ;;
    esac
}

show_hardware() {
    detect_hw

    if [[ "$HW_TYPE" == "none" ]]; then
        echo "Hardware AV1 encoding: unavailable"
        if [[ "$DEBUG_HARDWARE" != "yes" ]]; then
            echo "Run with --debug-hardware to see the failed probe details."
        fi
        return 1
    fi

    echo "Hardware AV1 encoding: available"
    echo "Type:   $HW_TYPE"
    echo "Detail: $HW_DETAIL"
}

is_allowed_extension() {
    local file="$1"
    local ext="${file##*.}"
    local allowed

    ext="${ext,,}"

    if [[ "$USE_ALL_EXTENSIONS" == "yes" ]]; then
        for allowed in "${COMMON_EXTENSIONS[@]}"; do
            [[ "$ext" == "$allowed" ]] && return 0
        done
        return 1
    fi

    for allowed in "${ALLOWED_EXTENSIONS[@]}"; do
        [[ "$ext" == "$allowed" ]] && return 0
    done

    return 1
}

prompt_yes_no() {
    local prompt="$1"
    local default_answer="$2"
    local answer

    while true; do
        read -r -p "$prompt" answer
        answer="${answer,,}"

        if [[ -z "$answer" ]]; then
            answer="$default_answer"
        fi

        case "$answer" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            *) echo "Please enter y or n." ;;
        esac
    done
}

collect_interactive_options() {
    local ext_choice
    local ext_input

    echo "--- AV1 Batch Encoder ---"
    echo

    if [[ -z "$INPUT_PATH" ]]; then
        read -r -p "Enter path to a video file or folder: " INPUT_PATH
    fi

    INPUT_PATH="${INPUT_PATH/#\~/$HOME}"

    if [[ ! -e "$INPUT_PATH" ]]; then
        error "Path does not exist: $INPUT_PATH"
        exit 1
    fi

    if [[ -z "$USE_ALL_EXTENSIONS" ]]; then
        echo
        echo "--- Extension Filter ---"
        echo "1) Use common video extensions"
        echo "2) Enter specific extensions"
        read -r -p "Choose 1 or 2: " ext_choice

        if [[ "$ext_choice" == "2" ]]; then
            read -r -p "Enter extensions separated by spaces, example: mp4 mkv mov: " ext_input
            parse_extension_list "$ext_input"
        else
            USE_ALL_EXTENSIONS="yes"
        fi
    fi

    if [[ -z "$SKIP_AV1" ]]; then
        echo
        if prompt_yes_no "Skip files already encoded as AV1? (y/n): " "n"; then
            SKIP_AV1="yes"
        else
            SKIP_AV1="no"
        fi
    fi

    if [[ -z "$MODE" ]]; then
        MODE="auto"
        echo
        echo "Encoding mode: AUTO (hardware only; CPU fallback disabled)"
    fi

    if [[ -d "$INPUT_PATH" && -z "$RECURSIVE" ]]; then
        echo
        if prompt_yes_no "Search subfolders too? (y/n): " "n"; then
            RECURSIVE="yes"
        else
            RECURSIVE="no"
        fi
    fi

    [[ -n "$RECURSIVE" ]] || RECURSIVE="no"
    [[ -n "$OVERWRITE_MODE" ]] || OVERWRITE_MODE="ask"
    [[ -n "$START_CONFIRM" ]] || START_CONFIRM="yes"
}

apply_cli_defaults() {
    INPUT_PATH="${INPUT_PATH/#\~/$HOME}"
    [[ -n "$MODE" ]] || MODE="auto"
    [[ -n "$RECURSIVE" ]] || RECURSIVE="no"
    [[ -n "$USE_ALL_EXTENSIONS" ]] || USE_ALL_EXTENSIONS="yes"
    [[ -n "$SKIP_AV1" ]] || SKIP_AV1="no"
    [[ -n "$OVERWRITE_MODE" ]] || OVERWRITE_MODE="no"
    [[ -n "$START_CONFIRM" ]] || START_CONFIRM="no"
}

configure_encoder() {
    local selected_mode="$MODE"

    FFMPEG_COMMAND=(ffmpeg)
    FFMPEG_GLOBAL_ARGS=()
    VIDEO_FILTER_ARGS=()
    VIDEO_ENCODER_ARGS=()

    if [[ "$AUDIO_MODE" == "copy" ]]; then
        AUDIO_ARGS=(-c:a copy)
    else
        AUDIO_ARGS=(-c:a aac -b:a "$AUDIO_BITRATE" -ar 48000)
    fi

    if [[ "$FORCED_ENCODER" == libsvtav1 ]]; then
        selected_mode=software
    elif [[ "$FORCED_ENCODER" != auto ]]; then
        selected_mode=hardware
    fi

    # Software is available only through an explicit mode or encoder request.
    if [[ "$selected_mode" == "software" ]]; then
        if [[ "$FORCED_ENCODER" != auto && "$FORCED_ENCODER" != libsvtav1 ]]; then
            error "--software cannot be combined with hardware encoder '$FORCED_ENCODER'."
            exit 2
        fi
        if ! encoder_available "libsvtav1" || ! test_software_encoder; then
            error "Software mode was requested, but libsvtav1 failed its capability probe."
            exit 1
        fi
        ACTIVE_MODE="software"
        ACTIVE_ENCODER="libsvtav1"
        ACTIVE_ENCODER_ID="libsvtav1"
        VIDEO_ENCODER_ARGS=(
            -c:v:0 libsvtav1
            -crf:v:0 "$SOFTWARE_CRF"
            -preset:v:0 "$SOFTWARE_PRESET"
            -pix_fmt:v:0 yuv420p10le
        )
        return
    fi

    if [[ "$FORCED_ENCODER" == auto ]]; then
        detect_hw
    else
        detect_forced_hardware
    fi

    if [[ "$selected_mode" == "auto" ]]; then
        if [[ "$HW_TYPE" == "none" ]]; then
            error "AUTO could not find a working hardware AV1 encoder."
            echo "CPU fallback is disabled. Use --software only when CPU encoding is intentional." >&2
            exit 1
        fi
        selected_mode="hardware"
    fi

    if [[ "$selected_mode" == "hardware" && "$HW_TYPE" == "none" ]]; then
        if [[ "$FORCED_ENCODER" == auto ]]; then
            error "Hardware mode was requested, but no working AV1 hardware encoder was detected."
        else
            error "Requested encoder '$FORCED_ENCODER' failed its capability probe."
        fi
        echo "CPU fallback is disabled. Use --software only when CPU encoding is intentional." >&2
        exit 1
    fi

    ACTIVE_MODE="hardware"

    if [[ "$HW_TYPE" != vaapi ]] && ! is_integer_in_range "$HARDWARE_QP" 0 51; then
        error "--qp values above 51 are valid only for the VA-API AV1 backend."
        exit 2
    fi

    case "$HW_TYPE" in
        nvidia)
            ACTIVE_ENCODER="NVIDIA NVENC"
            ACTIVE_ENCODER_ID="av1_nvenc"
            VIDEO_ENCODER_ARGS=(
                -c:v:0 av1_nvenc
                -rc:v:0 vbr
                -cq:v:0 "$HARDWARE_QP"
                -preset:v:0 slow
                -pix_fmt:v:0 yuv420p10le
            )
            ;;
        intel)
            ACTIVE_ENCODER="Intel Quick Sync"
            ACTIVE_ENCODER_ID="av1_qsv"
            VIDEO_ENCODER_ARGS=(
                -c:v:0 av1_qsv
                -global_quality:v:0 "$HARDWARE_QP"
                -preset:v:0 slow
                -pix_fmt:v:0 yuv420p10le
            )
            ;;
        vaapi)
            ACTIVE_ENCODER="AMD/Linux VA-API"
            ACTIVE_ENCODER_ID="av1_vaapi"
            FFMPEG_GLOBAL_ARGS=(
                -init_hw_device "vaapi=va:${VAAPI_DEVICE}"
                -filter_hw_device va
            )
            # The final per-file filter is built after ffprobe has supplied the
            # stream's initial width, height, and sample aspect ratio.
            VIDEO_FILTER_ARGS=()
            VIDEO_ENCODER_ARGS=(
                -c:v:0 av1_vaapi
                # av1_vaapi takes its quality level through FFmpeg's generic
                # global_quality option; -qp is accepted but ignored by FFmpeg 9.
                -rc_mode CQP
                -global_quality "$HARDWARE_QP"
            )
            ;;
        *)
            error "Internal error: unsupported hardware type '$HW_TYPE'."
            exit 1
            ;;
    esac
}

analyze_video() {
    local input_file="$1"
    local video_info
    local current_codec
    local current_width
    local current_height
    local current_sar
    local current_chroma current_range current_space current_transfer current_primaries

    video_info="$(ffprobe -v error \
        -select_streams v:0 \
        -show_entries stream=codec_name,width,height,sample_aspect_ratio \
        -of csv=p=0 \
        "$input_file")"

    IFS=',' read -r current_codec current_width current_height current_sar <<< "$video_info"
    current_chroma=$(ffprobe -v error -select_streams v:0 -show_entries stream=chroma_location \
        -of default=nw=1:nk=1 "$input_file" | head -n 1)
    current_range=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_range \
        -of default=nw=1:nk=1 "$input_file" | head -n 1)
    current_space=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_space \
        -of default=nw=1:nk=1 "$input_file" | head -n 1)
    current_transfer=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_transfer \
        -of default=nw=1:nk=1 "$input_file" | head -n 1)
    current_primaries=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_primaries \
        -of default=nw=1:nk=1 "$input_file" | head -n 1)

    if [[ -z "$current_codec" || ! "$current_width" =~ ^[0-9]+$ ||
          ! "$current_height" =~ ^[0-9]+$ ]]; then
        echo "Skipping: could not read the first video stream."
        LAST_RESULT_STATUS="skipped"
        return 1
    fi

    case "$current_sar" in
        ""|N/A|0:1|0/1) current_sar="1:1" ;;
    esac

    INPUT_VIDEO_CODEC="$current_codec"
    INPUT_VIDEO_WIDTH="$current_width"
    INPUT_VIDEO_HEIGHT="$current_height"
    INPUT_VIDEO_SAR="$current_sar"
    INPUT_VIDEO_CHROMA_LOCATION="$current_chroma"
    INPUT_VIDEO_COLOR_RANGE="$current_range"
    INPUT_VIDEO_COLOR_SPACE="$current_space"
    INPUT_VIDEO_COLOR_TRANSFER="$current_transfer"
    INPUT_VIDEO_COLOR_PRIMARIES="$current_primaries"

    echo "Codec:      $current_codec"
    echo "Resolution: ${current_width}x${current_height}"
    echo "Pixel SAR:  $current_sar"

    if [[ "$current_codec" == "av1" ]]; then
        echo "Warning: already AV1."
        if [[ "$SKIP_AV1" == "yes" ]]; then
            echo "Skipping because AV1 skip is enabled."
            LAST_RESULT_STATUS="skipped"
            return 1
        fi
    fi

    if (( current_height > 1080 )); then
        echo "Warning: resolution is higher than 1080p. Original resolution will be kept."
    fi

    return 0
}

build_file_video_filter() {
    local sar_for_filter

    VIDEO_FILTER_ARGS=()
    VIDEO_OUTPUT_ARGS=()

    if [[ "$ACTIVE_MODE" == "hardware" && "$HW_TYPE" == "vaapi" ]]; then
        # A colon separates filters and their options, so use slash notation
        # for the sample-aspect ratio inside the setsar expression.
        sar_for_filter="${INPUT_VIDEO_SAR/:/\/}"

        VIDEO_FILTER_ARGS=(
            -filter:v:0
            "format=${VAAPI_UPLOAD_FORMAT},hwupload,scale_vaapi=w=${INPUT_VIDEO_WIDTH}:h=${INPUT_VIDEO_HEIGHT}:format=${VAAPI_UPLOAD_FORMAT}:mode=hq,setsar=sar=${sar_for_filter}"
        )

        # The graph above already guarantees a fixed output size. Disabling
        # FFmpeg's implicit end-of-graph scaler prevents it from trying to put
        # a software scaler after VA-API hardware frames during reinitialization.
        # Preserve the input demuxer time base so the completed AV1 stream keeps
        # the source presentation timestamps. This prevents downstream quality
        # validation from pairing neighbouring frames after half-frame rounding.
        VIDEO_OUTPUT_ARGS=(-enc_time_base:v:0 demux -noautoscale)
    fi

    case "$INPUT_VIDEO_CHROMA_LOCATION" in
        ""|unknown|unspecified|N/A) ;;
        *) VIDEO_OUTPUT_ARGS+=(-chroma_sample_location:v:0 "$INPUT_VIDEO_CHROMA_LOCATION") ;;
    esac
    case "$INPUT_VIDEO_COLOR_RANGE" in
        ""|unknown|unspecified|N/A) ;;
        *) VIDEO_OUTPUT_ARGS+=(-color_range:v:0 "$INPUT_VIDEO_COLOR_RANGE") ;;
    esac
    case "$INPUT_VIDEO_COLOR_SPACE" in
        ""|unknown|unspecified|N/A) ;;
        *) VIDEO_OUTPUT_ARGS+=(-colorspace:v:0 "$INPUT_VIDEO_COLOR_SPACE") ;;
    esac
    case "$INPUT_VIDEO_COLOR_TRANSFER" in
        ""|unknown|unspecified|N/A) ;;
        *) VIDEO_OUTPUT_ARGS+=(-color_trc:v:0 "$INPUT_VIDEO_COLOR_TRANSFER") ;;
    esac
    case "$INPUT_VIDEO_COLOR_PRIMARIES" in
        ""|unknown|unspecified|N/A) ;;
        *) VIDEO_OUTPUT_ARGS+=(-color_primaries:v:0 "$INPUT_VIDEO_COLOR_PRIMARIES") ;;
    esac
}

print_command() {
    local argument
    local first="yes"

    for argument in "$@"; do
        if [[ "$first" == "yes" ]]; then
            first="no"
        else
            printf ' '
        fi
        printf '%q' "$argument"
    done
    printf '\n'
}

stream_count_for_file() {
    local selector="$1" path="$2"
    ffprobe -v error -select_streams "$selector" -show_entries stream=index -of csv=p=0 "$path" 2>/dev/null |
        awk 'NF {count++} END {print count+0}'
}

validate_machine_output() {
    local source="$1" candidate="$2" codec source_duration output_duration difference selector

    codec=$(ffprobe -v error -select_streams V:0 -show_entries stream=codec_name \
        -of csv=p=0 "$candidate" 2>/dev/null | head -n 1)
    [[ $codec == av1 ]] || {
        error "Machine output codec was '${codec:-unreadable}', not AV1."
        return 1
    }

    source_duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$source" 2>/dev/null | head -n 1)
    output_duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$candidate" 2>/dev/null | head -n 1)
    if [[ $source_duration =~ ^[0-9]+([.][0-9]+)?$ && $output_duration =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        difference=$(LC_NUMERIC=C awk -v a="$source_duration" -v b="$output_duration" \
            'BEGIN {d=a-b; if(d<0)d=-d; printf "%.6f",d}')
        LC_NUMERIC=C awk -v d="$difference" 'BEGIN {exit !(d<=2.0)}' || {
            error "Machine output duration differs from the input by more than two seconds."
            return 1
        }
    fi

    if ! ffmpeg -hide_banner -loglevel error -xerror -nostdin -i "$candidate" \
        -map 0:V:0 -map '0:a?' -f null -; then
        error "Machine output failed full video/audio decode validation."
        return 1
    fi

    if [[ $PRESERVE_ALL_STREAMS == yes ]]; then
        for selector in v a s d t; do
            if [[ $(stream_count_for_file "$selector" "$source") != \
                  $(stream_count_for_file "$selector" "$candidate") ]]; then
                error "Machine output did not preserve every '$selector' stream."
                return 1
            fi
        done
    fi
}

encode_file() {
    local input_file="$1"
    local output_file="${input_file%.*}_av1.${OUTPUT_EXTENSION}"
    local temporary_output="${input_file%.*}_av1.part.${OUTPUT_EXTENSION}"
    local ffmpeg_status
    local overwrite_answer
    local output_args=()
    local command=()
    local stream_map_args=(-map 0:v:0 -map '0:a:0?')
    local stream_copy_args=()
    local primary_rows all_video_rows stream_index
    local -A primary_indexes=()

    VIDEO_OUTPUT_ARGS=()
    LAST_RESULT_STATUS="running"

    if [[ -n "$OUTPUT_PATH_OVERRIDE" ]]; then
        output_file="$OUTPUT_PATH_OVERRIDE"
        temporary_output="$(dirname -- "$output_file")/.${output_file##*/}.part.$$.${OUTPUT_EXTENSION}"
    fi
    LAST_OUTPUT_FILE="$output_file"

    echo
    echo "=========================================="
    echo "Processing:"
    echo "$input_file"
    echo "=========================================="

    if [[ -e "$output_file" ]]; then
        case "$OVERWRITE_MODE" in
            yes)
                echo "Overwriting existing output: $output_file"
                ;;
            ask)
                if ! prompt_yes_no "Output exists. Overwrite? (y/n): " "n"; then
                    echo "Skipped."
                    return 0
                fi
                ;;
            *)
                echo "Output exists; skipped: $output_file"
                LAST_RESULT_STATUS="skipped"
                return 0
                ;;
        esac
    fi

    analyze_video "$input_file" || return 0
    build_file_video_filter
    configure_input_decode "$input_file"

    if [[ -e "$temporary_output" ]]; then
        echo "Removing stale partial output: $temporary_output"
        if ! rm -f -- "$temporary_output"; then
            error "Could not remove stale partial output: $temporary_output"
            LAST_RESULT_STATUS="failed"
            return 1
        fi
    fi

    if [[ "$OUTPUT_EXTENSION" == "mp4" ]]; then
        output_args=(-movflags +faststart)
    fi

    if [[ "$PRESERVE_ALL_STREAMS" == yes ]]; then
        stream_map_args=()
        primary_rows=$(ffprobe -v error -select_streams V -show_entries stream=index \
            -of csv=p=0 "$input_file") || return 1
        all_video_rows=$(ffprobe -v error -select_streams v -show_entries stream=index \
            -of csv=p=0 "$input_file") || return 1
        while IFS= read -r stream_index; do
            [[ $stream_index =~ ^[0-9]+$ ]] || continue
            primary_indexes[$stream_index]=1
            stream_map_args+=(-map "0:$stream_index")
        done <<< "$primary_rows"
        ((${#stream_map_args[@]} > 0)) || {
            error "No primary video stream is available for encoding."
            LAST_RESULT_STATUS="failed"
            return 1
        }
        while IFS= read -r stream_index; do
            [[ $stream_index =~ ^[0-9]+$ ]] || continue
            [[ -n ${primary_indexes[$stream_index]:-} ]] && continue
            stream_map_args+=(-map "0:$stream_index")
        done <<< "$all_video_rows"
        stream_map_args+=(
            -map '0:a?' -map '0:s?' -map '0:d?' -map '0:t?'
            -map_metadata 0 -map_chapters 0 -copy_unknown
        )
        stream_copy_args=(-c:v copy -c:s copy -c:d copy -c:t copy -max_muxing_queue_size 4096)
    fi

    command=(
        "${FFMPEG_COMMAND[@]}"
        -hide_banner
        -y
        "${FFMPEG_GLOBAL_ARGS[@]}"
        "${INPUT_DECODE_ARGS[@]}"
        -i "$input_file"
        "${stream_map_args[@]}"
        "${VIDEO_FILTER_ARGS[@]}"
        "${VIDEO_OUTPUT_ARGS[@]}"
        "${stream_copy_args[@]}"
        "${VIDEO_ENCODER_ARGS[@]}"
        "${AUDIO_ARGS[@]}"
        "${output_args[@]}"
        "$temporary_output"
    )

    echo
    echo "Encoding with: $ACTIVE_ENCODER"
    if [[ "$ACTIVE_MODE" == "hardware" && "$HW_TYPE" == "vaapi" ]]; then
        echo "VA-API frame lock: ${INPUT_VIDEO_WIDTH}x${INPUT_VIDEO_HEIGHT}, SAR ${INPUT_VIDEO_SAR}"
    fi

    if [[ "$DRY_RUN" == "yes" ]]; then
        print_command "${command[@]}"
        return 0
    fi

    "${command[@]}"
    ffmpeg_status=$?

    if [[ $ffmpeg_status -eq 0 ]]; then
        if [[ ! -s "$temporary_output" ]]; then
            error "FFmpeg reported success, but no output file was created."
            LAST_RESULT_STATUS="failed"
            return 1
        fi

        if [[ "$MACHINE_MODE" == yes ]] && ! validate_machine_output "$input_file" "$temporary_output"; then
            error "Encoded output failed dependency-interface validation."
            rm -f -- "$temporary_output"
            LAST_RESULT_STATUS="failed"
            return 1
        fi

        if ! mv -f -- "$temporary_output" "$output_file"; then
            error "Encoding succeeded, but the completed file could not be moved into place."
            echo "Completed temporary file: $temporary_output" >&2
            LAST_RESULT_STATUS="failed"
            return 1
        fi

        echo "Done: $output_file"
        LAST_RESULT_STATUS="ok"
    else
        echo "Error while encoding: $input_file"
        echo "FFmpeg exit code: $ffmpeg_status"
        if [[ -e "$temporary_output" ]]; then
            echo "Partial output kept as: $temporary_output"
        fi
        LAST_RESULT_STATUS="failed"
        return "$ffmpeg_status"
    fi
}

collect_files_from_folder() {
    local folder="$1"
    local recursive="$2"
    local file
    local find_args=()

    FILES=()

    if [[ "$recursive" == "yes" ]]; then
        find_args=("$folder" -type f -print0)
    else
        find_args=("$folder" -maxdepth 1 -type f -print0)
    fi

    while IFS= read -r -d '' file; do
        if is_allowed_extension "$file"; then
            FILES+=("$file")
        fi
    done < <(find "${find_args[@]}" | sort -z)
}

collect_input_files() {
    if [[ -z "$INPUT_PATH" ]]; then
        error "No input was specified."
        echo "Run '$SCRIPT_NAME --help' for usage." >&2
        exit 2
    fi

    if [[ ! -e "$INPUT_PATH" ]]; then
        error "Path does not exist: $INPUT_PATH"
        exit 1
    fi

    if [[ -f "$INPUT_PATH" ]]; then
        if is_allowed_extension "$INPUT_PATH"; then
            FILES=("$INPUT_PATH")
        else
            error "File extension does not match the selected extension filter."
            exit 1
        fi
    elif [[ -d "$INPUT_PATH" ]]; then
        collect_files_from_folder "$INPUT_PATH" "$RECURSIVE"
    else
        error "Input is neither a regular file nor a directory: $INPUT_PATH"
        exit 1
    fi
}

show_plan() {
    local file

    echo
    echo "Encoding plan"
    echo "=========================================="
    echo "Input:       $INPUT_PATH"
    echo "Files:       ${#FILES[@]}"
    echo "Mode:        $ACTIVE_MODE"
    echo "Encoder:     $ACTIVE_ENCODER"
    echo "Container:   $OUTPUT_EXTENSION"
    if [[ "$AUDIO_MODE" == "aac" ]]; then
        echo "Audio:       AAC ${AUDIO_BITRATE}"
    else
        echo "Audio:       Copy first audio stream"
    fi
    echo "Recursive:   $RECURSIVE"
    echo "Skip AV1:   $SKIP_AV1"
    echo "Overwrite:   $OVERWRITE_MODE"
    echo "Dry run:     $DRY_RUN"
    echo "=========================================="

    for file in "${FILES[@]}"; do
        echo " - $file"
    done
}

dispatch_protocol_v2() {
    local script_dir planner
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    planner="$script_dir/tools/AV1Plan.py"

    case "${1-}" in
        --machine-negotiate)
            if (( $# != 2 )); then
                error "Usage: $SCRIPT_NAME --machine-negotiate VERSION[,VERSION...]"
                exit 2
            fi
            command -v python3 >/dev/null 2>&1 || { error "python3 is required for protocol v2."; exit 1; }
            exec python3 "$planner" negotiate "$2"
            ;;
        --machine-evaluate|--machine-plan)
            if (( $# != 4 )) || [[ "$3" != --plan-json ]]; then
                error "Usage: $SCRIPT_NAME $1 REQUIREMENTS.json --plan-json PLAN.json"
                exit 2
            fi
            command -v python3 >/dev/null 2>&1 || { error "python3 is required for protocol v2."; exit 1; }
            exec python3 "$planner" evaluate "$2" "$4" "$script_dir/AV1Encode.sh"
            ;;
        --execute-plan)
            if (( $# != 4 )) || [[ "$3" != --result-json ]]; then
                error "Usage: $SCRIPT_NAME --execute-plan PLAN.json --result-json RESULT.json"
                exit 2
            fi
            command -v python3 >/dev/null 2>&1 || { error "python3 is required for protocol v2."; exit 1; }
            exec python3 "$planner" execute "$2" "$4" "$script_dir/AV1Encode.sh"
            ;;
    esac
}

main() {
    local file
    local failures=0

    dispatch_protocol_v2 "$@"
    parse_arguments "$@"
    if [[ "$MACHINE_MODE" == yes && -n "$RESULT_JSON_PATH" ]]; then
        trap machine_exit_handler EXIT
    fi
    validate_options
    check_dependencies

    if [[ "$MACHINE_ACTION" == probe ]]; then
        show_machine_capabilities
        exit 0
    fi

    if [[ "$LIST_HARDWARE_ONLY" == "yes" ]]; then
        show_hardware
        exit $?
    fi

    if [[ "$INTERACTIVE_MODE" == "yes" ]]; then
        collect_interactive_options
    else
        apply_cli_defaults
    fi

    validate_options
    collect_input_files

    if [[ "$MACHINE_MODE" == yes ]]; then
        [[ -f "$INPUT_PATH" ]] || {
            error "--machine requires one input file, not a directory."
            exit 2
        }
        [[ -n "$OUTPUT_PATH_OVERRIDE" ]] || {
            error "--machine requires --output."
            exit 2
        }
        if [[ $(cd -- "$(dirname -- "$INPUT_PATH")" && pwd -P)/$(basename -- "$INPUT_PATH") == \
              $(cd -- "$(dirname -- "$OUTPUT_PATH_OVERRIDE")" && pwd -P)/$(basename -- "$OUTPUT_PATH_OVERRIDE") ]]; then
            error "Machine output must not replace the input file."
            exit 2
        fi
    fi

    configure_encoder

    if [[ ${#FILES[@]} -eq 0 ]]; then
        echo "No matching video files found."
        exit 0
    fi

    show_plan

    if [[ "$START_CONFIRM" == "yes" ]]; then
        echo
        if ! prompt_yes_no "Start encoding these files? (y/n): " "n"; then
            echo "Cancelled."
            exit 0
        fi
    fi

    for file in "${FILES[@]}"; do
        if ! encode_file "$file"; then
            ((failures++))
        fi
    done

    echo
    if (( failures == 0 )); then
        echo "All done."
        write_machine_result "$LAST_RESULT_STATUS" 0 || {
            error "Could not write machine result: $RESULT_JSON_PATH"
            exit 1
        }
        exit 0
    fi

    echo "Finished with $failures failed file(s)."
    write_machine_result failed 1 || error "Could not write machine result: $RESULT_JSON_PATH"
    exit 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
