# AV1Encode dependency interface

AV1Encode protocol version 1 provides a stable process boundary for callers
without changing the normal interactive and command-line experience.

## Compatibility

Callers must read `--interface-version` and accept only protocol versions they
understand. Additive JSON fields may appear without changing the protocol
version. Removing or changing the meaning of a field requires a new version.

## Capability discovery

```bash
AV1Encode.sh --machine-probe
```

The command performs bounded real encodes, verifies the AV1 codec, and decodes
the result. It exits successfully even when no hardware encoder works because a
valid capability report was still produced. `auto_encoder` is `null` in that
case. Software encoders are reported but always have `auto_eligible: false`.

The standard encoder identifiers are:

- `av1_vaapi`
- `av1_nvenc`
- `av1_qsv`
- `libsvtav1` (manual-only)

## Encoding

```bash
AV1Encode.sh --machine \
    --input INPUT.mkv \
    --output OUTPUT.mkv \
    --result-json RESULT.json \
    [--encoder ENCODER] \
    [--preserve-all] \
    [encoding options]
```

Machine encoding is deliberately restricted to one input file. `--output` is
mandatory and cannot name the input. `--encoder auto` is the default and never
selects software. Naming `libsvtav1` or passing `--software` is an explicit CPU
request.

`--preserve-all` requires Matroska output. It maps all video, audio, subtitle,
data, and attachment streams, copies chapters and global metadata, transcodes
only the first primary video stream, and copies auxiliary video streams. Use
`--copy-audio` when every original audio stream must remain encoded with its
original codec.

The output is written to a temporary file. Before it is moved to the exact
destination, machine mode verifies:

- the first primary video is AV1;
- output duration is within two seconds of the source when both durations are
  available;
- the complete primary video and all audio streams decode without errors;
- requested stream counts are preserved.

## Result report

When `--result-json` is supplied, AV1Encode replaces that path atomically with a
report using the `av1encode.result` schema. The report includes protocol and tool
versions, status, exit code, input, output, selected encoder and class, AUTO
policy, preservation mode, and output size.

Status values in protocol version 1 are:

- `ok`: a validated output was committed;
- `skipped`: no output was written because existing-output or skip policy won;
- `failed`: the job did not complete.

Exit status `0` means the invocation completed or was intentionally skipped,
`1` means an operational/probe/encode failure, and `2` means invalid usage.
