# AV1Encode dependency interface

AV1Encode supports executor protocol 1 and semantic planning protocol 2. The
ordinary interactive and command-line workflows do not use either protocol and
remain ready-to-go standalone tools.

## Negotiate before depending

```bash
AV1Encode.sh --machine-negotiate 1,2
```

The response selects the highest version shared by caller and tool. A caller
must stop when `compatible` is false. `--interface-version` prints the newest
version, while `--machine-probe` advertises the complete
`supported_protocol_versions` list. This lets a newer AV1Encode retain protocol
1 while adding protocol 2.

## Protocol 2: semantic evaluate/execute

The caller describes the result it needs, not FFmpeg flags or AV1 settings.
The formal input contract is
[`requirements-v2.schema.json`](requirements-v2.schema.json). Unknown fields
are rejected instead of being silently ignored.

```json
{
  "schema": "av1encode.requirements",
  "protocol_version": 2,
  "input": "/archive/source.mkv",
  "output": "/archive/staging/source.av1.mkv",
  "hardware_policy": "auto_hardware_only",
  "quality": {
    "metric": "vmaf",
    "target": 92,
    "p10_minimum": 88,
    "sustained_floor": 86,
    "maximum_sustained_seconds": 1
  },
  "optimization": {
    "primary": "smallest_output",
    "secondary": "fastest_encoding"
  },
  "video": {"maximum_height": null, "denoise": "auto"},
  "preservation": {"streams": "all", "chapters": true, "metadata": true},
  "audio": {"mode": "copy_all"},
  "evaluation": {"sample_seconds": 3}
}
```

Evaluate the requirements without encoding the complete asset:

```bash
AV1Encode.sh --machine-evaluate requirements.json --plan-json plan.json
# --machine-plan is an equivalent spelling
```

AV1Encode selects its own encoder, quality value, preset, pixel format, and
filter behavior. It performs a real bounded sample encode and reports:

- measured sample quality (`vmaf` mean, p10, and sustained-low duration, or a
  mean-only `ssim_percent` development signal when explicitly requested);
- predicted completed output bytes;
- measured sample real-time factor and predicted completed encode time;
- the sampling basis and its explicitly limited confidence.

VMAF evaluation uses the same pinned, checksum-verified managed runtime as
`AV1Compare.py` when system FFmpeg lacks libvmaf. Predictions are estimates,
not completed-output acceptance evidence. Hardcore Archive should still apply
its final full or sampled acceptance policy to the completed file.

The returned `plan_id` is an opaque handle. Callers may compare predictions and
associate decisions with the ID, but must not construct it or depend on the
contents of `recipe`. Execute the saved, unchanged plan:

```bash
AV1Encode.sh --execute-plan plan.json --result-json result.json
```

Before encoding, AV1Encode verifies the plan ID and recomputes four SHA-256
fingerprints:

| Fingerprint | Covers | Invalidation effect |
|---|---|---|
| implementation | encoder, planner, comparison code, policy version | an AV1Encode improvement requires evaluation again |
| runtime | FFmpeg path/version/build, chosen encoder, available driver identity | runtime or driver changes require evaluation again |
| source | canonical path, byte length, complete content hash | changed or replaced input requires evaluation again |
| requirements | normalized semantic request | any requested outcome change requires evaluation again |

The plan ID is content-addressed from the normalized requirements, resolved
recipe, predictions, and fingerprints. Consequently, a cache keyed by plan ID
cannot reuse an old AV1 decision after AV1Encode improves. Execution also
rejects stale plans directly; cache discipline is not the only safeguard.

`auto_hardware_only` never selects CPU encoding. `manual_software` is the only
semantic request that permits `libsvtav1`. Protocol 2 currently requires MKV,
all-stream/chapter/metadata preservation, and copied audio. A requested height
reduction is recognized but rejected until the implementation can honor it;
the request is never silently discarded.

## Protocol 1: direct executor

AV1Encode protocol version 1 provides a stable process boundary for callers
without changing the normal interactive and command-line experience.

## Compatibility

Callers must read `--interface-version` and accept only protocol versions they
understand. Additive JSON fields may appear without changing the protocol
version. Removing or changing the meaning of a field requires a new version.
The tool version follows semantic versioning and changes whenever planner,
recipe, execution, or capability behavior changes. Callers may use it as a
human-readable runtime identity; sealed plans continue to rely on their full
implementation and runtime fingerprints.

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
