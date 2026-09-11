# AV1Encode

AV1Encode is a straightforward batch AV1 encoder for Linux and macOS. It is the
AV1 counterpart to [265Encode](https://github.com/andr8076/265Encode), with the
same interactive and command-line workflow.

It preserves the source video resolution and first audio track, writes completed
files beside their sources with an `_av1` suffix, and never replaces a source
file. A `.part` file is used until FFmpeg finishes successfully.

## Encoders

AV1Encode tests hardware paths with a bounded real encode before selecting one.
FFmpeg merely listing an encoder is not enough.

| Platform | FFmpeg encoder | Use |
|---|---|---|
| AMD on Linux | `av1_vaapi` | Hardware |
| NVIDIA | `av1_nvenc` | Hardware |
| Intel | `av1_qsv` | Hardware |
| Any supported CPU | `libsvtav1` | Software |

Intel Skylake/P530 has no AV1 encoding hardware, so the HEVC-only legacy Intel
runtime from 265Encode is intentionally not included. Those machines can still
use `--software` or AUTO's software fallback when FFmpeg provides `libsvtav1`.

## Requirements

- Bash 4 or newer
- FFmpeg and FFprobe
- An FFmpeg build with `libsvtav1` for software mode, or a working AV1 hardware
  encoder for hardware mode

## Run it

```bash
chmod +x AV1Encode.sh
./AV1Encode.sh
```

With no arguments, the program opens the interactive menus. Command-line use is
also available:

```bash
./AV1Encode.sh --auto "/path/to/movie.mkv"
./AV1Encode.sh --hardware --recursive --skip-av1 "/path/to/videos"
./AV1Encode.sh --software --crf 28 --preset 6 --container mkv "movie.mkv"
./AV1Encode.sh --list-hardware --debug-hardware
```

AUTO prefers a proven hardware encoder and falls back to SVT-AV1 software
encoding when AV1 hardware is unavailable. Use `--hardware` when falling back to
the CPU would be undesirable.

SVT-AV1 CRF accepts 0-63; lower values preserve more quality and produce larger
files. Its preset accepts 0-13; lower values are slower. Defaults are CRF 30 and
preset 6.

Run `./AV1Encode.sh --help` for every option.

## Compare an output

`tools/AV1Compare.py` compares metadata, tracks, size, and optionally VMAF:

```bash
python3 tools/AV1Compare.py original.mkv original_av1.mkv
python3 tools/AV1Compare.py --no-quality original.mkv original_av1.mkv
```

If the system FFmpeg lacks `libvmaf`, the comparator can download the pinned,
checksum-verified quality runtime published by this repository.

## Test

```bash
bash tests/test.sh
```
