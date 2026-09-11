# AV1Encode

AV1Encode is a straightforward batch AV1 encoder for Linux and macOS. It is the
AV1 counterpart to [265Encode](https://github.com/andr8076/265Encode), with the
same interactive and command-line workflow.

It preserves the source video resolution and first audio track, writes completed
files beside their sources with an `_av1` suffix, and never replaces a source
file. A `.part` file is used until FFmpeg finishes successfully.

## Encoders

AV1Encode tests hardware paths with a bounded real encode, checks that the result
is AV1, and decodes it before selecting the encoder. FFmpeg merely listing an
encoder is not enough.

| Platform | FFmpeg encoder | Use |
|---|---|---|
| AMD on Linux | `av1_vaapi` | Hardware |
| NVIDIA | `av1_nvenc` | Hardware |
| Intel | `av1_qsv` | Hardware |
| Any supported CPU | `libsvtav1` | Software |

Intel Skylake/P530 has no AV1 encoding hardware, so the HEVC-only legacy Intel
runtime from 265Encode is intentionally not included. AUTO therefore refuses to
run on those machines. They can still encode on the CPU only when `--software`
is supplied explicitly and FFmpeg provides `libsvtav1`.

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

AUTO selects a proven hardware encoder and never falls back to the CPU. If no
hardware AV1 path passes the real encode, codec, and decode checks, AV1Encode
stops with an error. CPU encoding is available only through explicit
`--software` selection.

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

## Dependency interface

AV1Encode remains a standalone ready-to-go program, but it also exposes a
versioned interface for programs such as Hardcore Archive. Normal users do not
need these options.

Inspect proven capabilities as JSON:

```bash
./AV1Encode.sh --machine-probe
```

Protocol 2 lets an orchestrator submit semantic requirements and receive an
evaluated, fingerprinted plan with sampled quality, size, and speed predictions:

```bash
./AV1Encode.sh --machine-negotiate 1,2
./AV1Encode.sh --machine-evaluate requirements.json --plan-json plan.json
./AV1Encode.sh --execute-plan plan.json --result-json result.json
```

The caller does not choose CRF, QP, preset, pixel format, or FFmpeg filters.
Those remain AV1Encode policy, so a future encoder-policy improvement changes
the implementation fingerprint, invalidates old plans, and automatically
benefits newly evaluated Hardcore Archive jobs.

Run one explicitly addressed dependency job:

```bash
./AV1Encode.sh --machine \
    --input source.mkv \
    --output staging/result.mkv \
    --result-json staging/result.json \
    --preserve-all \
    --copy-audio
```

Protocol-1 machine mode requires one input file and an exact output path. It supports a
forced encoder through `--encoder`, performs a full video/audio decode before
committing the output, and can preserve all streams, chapters, attachments, and
copyable metadata in Matroska. AUTO remains hardware-only in both normal and
machine operation. See `docs/dependency-interface.md` for the protocol.

## Test

```bash
bash tests/test.sh
```
