#!/usr/bin/env python3
"""Protocol-v2 semantic planning for AV1Encode.

The plan document is intentionally an implementation detail.  Callers supply
requirements, compare the predictions, retain the opaque plan_id, and hand the
unchanged document back for execution.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOLS = (1, 2)
REQUIREMENTS_SCHEMA = "av1encode.requirements"
PLAN_SCHEMA = "av1encode.plan"
RESULT_SCHEMA = "av1encode.plan-result"
PLANNER_VERSION = "3"
VMAF_PLANNING_MARGIN = 0.5


class PlanError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"{path} must contain a JSON object.")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=False, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def exact_keys(value: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanError(f"Unknown {location} field(s): {', '.join(unknown)}")


def object_field(parent: dict[str, Any], name: str) -> dict[str, Any]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise PlanError(f"'{name}' must be an object.")
    return value


def finite_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise PlanError(f"'{name}' must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"'{name}' must be a number.") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise PlanError(f"'{name}' must be between {minimum:g} and {maximum:g}.")
    return number


def normalize_requirements(raw: dict[str, Any]) -> dict[str, Any]:
    exact_keys(raw, {
        "schema", "protocol_version", "input", "output", "hardware_policy",
        "quality", "optimization", "video", "preservation", "audio", "evaluation",
    }, "requirement")
    if raw.get("schema") != REQUIREMENTS_SCHEMA:
        raise PlanError(f"'schema' must be '{REQUIREMENTS_SCHEMA}'.")
    if raw.get("protocol_version") != PROTOCOL_VERSION:
        raise PlanError(f"'protocol_version' must be {PROTOCOL_VERSION}.")

    input_path = Path(str(raw.get("input", ""))).expanduser().resolve()
    output_path = Path(str(raw.get("output", ""))).expanduser().resolve()
    if not input_path.is_file():
        raise PlanError(f"Input is not a file: {input_path}")
    if input_path == output_path:
        raise PlanError("Output must not replace the input.")
    if not output_path.parent.is_dir():
        raise PlanError(f"Output directory does not exist: {output_path.parent}")

    hardware_policy = raw.get("hardware_policy", "auto_hardware_only")
    if hardware_policy not in {"auto_hardware_only", "manual_software"}:
        raise PlanError("'hardware_policy' must be auto_hardware_only or manual_software.")

    quality = object_field(raw, "quality")
    exact_keys(quality, {
        "metric", "target", "p10_minimum", "sustained_floor",
        "maximum_sustained_seconds",
    }, "quality")
    metric = quality.get("metric")
    if metric not in {"vmaf", "ssim_percent"}:
        raise PlanError("'quality.metric' must be vmaf or ssim_percent.")
    target = finite_number(quality.get("target"), "quality.target", 0, 100)
    p10 = finite_number(quality.get("p10_minimum", target - 4), "quality.p10_minimum", 0, 100)
    floor = finite_number(quality.get("sustained_floor", target - 6), "quality.sustained_floor", 0, 100)
    maximum_sustained = finite_number(
        quality.get("maximum_sustained_seconds", 1),
        "quality.maximum_sustained_seconds", 0, 60,
    )
    if not floor <= p10 <= target:
        raise PlanError("Quality thresholds must satisfy sustained_floor <= p10_minimum <= target.")

    optimization = object_field(raw, "optimization")
    exact_keys(optimization, {"primary", "secondary"}, "optimization")
    primary = optimization.get("primary")
    secondary = optimization.get("secondary")
    objectives = {"smallest_output", "fastest_encoding", "highest_quality"}
    if primary not in objectives or secondary not in objectives or primary == secondary:
        raise PlanError("Optimization objectives must be two different supported objectives.")

    video = object_field(raw, "video")
    exact_keys(video, {"maximum_height", "denoise"}, "video")
    maximum_height = video.get("maximum_height")
    if maximum_height is not None:
        if isinstance(maximum_height, bool) or not isinstance(maximum_height, int) or maximum_height < 144:
            raise PlanError("'video.maximum_height' must be null or an integer of at least 144.")
    denoise = video.get("denoise", "auto")
    if denoise not in {"auto", "never"}:
        raise PlanError("'video.denoise' must be auto or never.")

    preservation = object_field(raw, "preservation")
    exact_keys(preservation, {"streams", "chapters", "metadata"}, "preservation")
    if preservation.get("streams") != "all" or preservation.get("chapters") is not True or preservation.get("metadata") is not True:
        raise PlanError("Protocol v2 currently requires all streams, chapters, and metadata.")
    if output_path.suffix.lower() != ".mkv":
        raise PlanError("Full preservation requires an .mkv output.")

    audio = object_field(raw, "audio")
    exact_keys(audio, {"mode"}, "audio")
    if audio.get("mode") != "copy_all":
        raise PlanError("Protocol v2 currently supports only audio.mode=copy_all.")

    evaluation = raw.get("evaluation", {})
    if not isinstance(evaluation, dict):
        raise PlanError("'evaluation' must be an object.")
    exact_keys(evaluation, {"sample_seconds"}, "evaluation")
    sample_seconds = finite_number(evaluation.get("sample_seconds", 3), "evaluation.sample_seconds", 1, 10)

    return {
        "schema": REQUIREMENTS_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "input": str(input_path),
        "output": str(output_path),
        "hardware_policy": hardware_policy,
        "quality": {
            "metric": metric,
            "target": target,
            "p10_minimum": p10,
            "sustained_floor": floor,
            "maximum_sustained_seconds": maximum_sustained,
        },
        "optimization": {"primary": primary, "secondary": secondary},
        "video": {"maximum_height": maximum_height, "denoise": denoise},
        "preservation": {"streams": "all", "chapters": True, "metadata": True},
        "audio": {"mode": "copy_all"},
        "evaluation": {"sample_seconds": sample_seconds},
    }


def command_output(command: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
    if result.returncode != 0:
        raise PlanError(result.stderr.strip() or f"Command failed: {command[0]}")
    return result.stdout


def implementation_fingerprint(root: Path) -> dict[str, Any]:
    files = [root / "AV1Encode.sh", root / "tools" / "AV1Plan.py", root / "tools" / "AV1Compare.py"]
    record = {
        "planner_version": PLANNER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "files": {str(path.relative_to(root)): file_digest(path) for path in files},
    }
    return {"algorithm": "sha256", "value": digest(record), "components": record}


def runtime_fingerprint(encoder: str) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PlanError("ffmpeg and ffprobe are required.")
    components: dict[str, Any] = {
        "ffmpeg_path": str(Path(ffmpeg).resolve()),
        "ffprobe_path": str(Path(ffprobe).resolve()),
        "ffmpeg_version": command_output([ffmpeg, "-version"]).splitlines()[0],
        "ffmpeg_buildconf": command_output([ffmpeg, "-buildconf"]),
        "encoder": encoder,
    }
    for tool, args in (("nvidia-smi", ["--query-gpu=name,driver_version", "--format=csv,noheader"]), ("vainfo", [])):
        executable = shutil.which(tool)
        if executable:
            result = subprocess.run([executable, *args], text=True, capture_output=True, check=False)
            components[tool] = (result.stdout + result.stderr).strip()
    return {"algorithm": "sha256", "value": digest(components), "components": components}


def source_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    components = {"path": str(path), "bytes": stat.st_size, "content": file_digest(path)}
    return {"algorithm": "sha256", "value": digest(components), "components": components}


def probe_source(path: Path) -> dict[str, Any]:
    data = json.loads(command_output([
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate,bit_rate:format=duration,size,bit_rate",
        "-of", "json", str(path),
    ]))
    streams = data.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        raise PlanError("Input has no primary video stream.")
    stream = video_streams[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if duration <= 0 or width <= 0 or height <= 0:
        raise PlanError("Input duration or dimensions could not be determined.")
    copied_bitrate = 0
    for item in streams:
        if item is stream:
            continue
        try:
            copied_bitrate += max(0, int(item.get("bit_rate") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "copied_stream_bitrate": copied_bitrate,
    }


def capabilities(script: Path) -> dict[str, Any]:
    return json.loads(command_output([str(script), "--machine-probe"]))


def choose_encoder(requirements: dict[str, Any], report: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    encoders = {item["name"]: item for item in report.get("encoders", [])}
    if requirements["hardware_policy"] == "manual_software":
        chosen = encoders.get("libsvtav1")
        if not chosen or not chosen.get("usable"):
            raise PlanError("Manual software encoding was requested, but libsvtav1 is not usable.")
        return "libsvtav1", "software", chosen
    name = report.get("auto_encoder")
    if not name or not encoders.get(name, {}).get("usable"):
        raise PlanError("No proven hardware AV1 encoder satisfies auto_hardware_only.")
    return str(name), "hardware", encoders[name]


def recipe_for(encoder: str, requirements: dict[str, Any], source: dict[str, Any], capability: dict[str, Any]) -> dict[str, Any]:
    # These are AV1Encode-owned policy choices, not caller-controlled FFmpeg flags.
    if encoder == "libsvtav1":
        quality = {"kind": "crf", "value": 30, "preset": 6}
    elif encoder == "av1_vaapi":
        # AV1 VA-API uses the codec's 0..255 quantizer scale, unlike the
        # 0..51 quality scale used by NVENC and QSV.
        quality = {"kind": "qp", "value": 128, "preset": "slow"}
    else:
        quality = {"kind": "qp", "value": 24, "preset": "slow"}
    maximum = requirements["video"]["maximum_height"]
    if maximum is not None and source["height"] > maximum:
        raise PlanError("maximum_height scaling is negotiated but not implemented by this AV1Encode version.")
    recipe = {
        "encoder": encoder,
        "quality": quality,
        "resolution": "source",
        "denoise": "none",
        "container": "matroska",
        "audio": "copy_all",
        "preserve_all": True,
    }
    if encoder == "av1_vaapi":
        detail = str(capability.get("detail", ""))
        device = detail.split(",", 1)[0]
        if not device.startswith("/dev/"):
            raise PlanError("The selected VA-API capability did not report its render device.")
        recipe["vaapi_device"] = device
        recipe["vaapi_upload_format"] = "p010le" if "10-bit" in detail else "nv12"
        recipe["source_width"] = source["width"]
        recipe["source_height"] = source["height"]
    return recipe


def encode_sample(source: Path, destination: Path, encoder: str, recipe: dict[str, Any], start: float, length: float) -> float:
    quality = recipe["quality"]
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.6f}", "-t", f"{length:.6f}", "-i", str(source), "-map", "0:V:0", "-an", "-sn", "-dn"]
    if encoder == "libsvtav1":
        args += ["-c:v", "libsvtav1", "-crf", str(quality["value"]), "-preset", str(quality["preset"]), "-pix_fmt", "yuv420p10le"]
    elif encoder == "av1_nvenc":
        args += ["-c:v", encoder, "-rc", "vbr", "-cq", str(quality["value"]), "-preset", "slow", "-pix_fmt", "yuv420p10le"]
    elif encoder == "av1_qsv":
        args += ["-c:v", encoder, "-global_quality", str(quality["value"]), "-preset", "slow", "-pix_fmt", "yuv420p10le"]
    elif encoder == "av1_vaapi":
        device = str(recipe["vaapi_device"])
        upload = str(recipe["vaapi_upload_format"])
        args[1:1] = ["-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va"]
        frame_filter = (
            f"format={upload},hwupload,scale_vaapi=w={recipe['source_width']}:"
            f"h={recipe['source_height']}:format={upload}:mode=hq"
        )
        args += [
            "-vf", frame_filter, "-c:v", "av1_vaapi", "-rc_mode", "CQP",
            "-global_quality", str(quality["value"]), "-enc_time_base:v:0", "demux",
        ]
    else:
        raise PlanError(f"No sample recipe exists for encoder: {encoder}")
    args += ["-f", "matroska", str(destination)]
    before = time.monotonic()
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    elapsed = time.monotonic() - before
    if result.returncode != 0 or not destination.is_file():
        raise PlanError(result.stderr.strip() or "Sample encoding failed.")
    return max(elapsed, 0.001)


def quality_tools(root: Path, metric: str) -> tuple[str, dict[str, str], str]:
    if metric == "ssim_percent":
        return "ffmpeg", os.environ.copy(), metric
    module_path = root / "tools" / "AV1Compare.py"
    spec = importlib.util.spec_from_file_location("av1compare_runtime", module_path)
    if spec is None or spec.loader is None:
        raise PlanError("Could not load the VMAF runtime selector.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    selected = module.select_quality_tools()
    return selected.ffmpeg, selected.env, metric


def measure_quality(
    root: Path,
    reference: Path,
    candidate: Path,
    quality: dict[str, Any],
    sample_duration: float,
) -> dict[str, Any]:
    metric = quality["metric"]
    ffmpeg, env, selected_metric = quality_tools(root, metric)
    if selected_metric == "vmaf":
        with tempfile.TemporaryDirectory(prefix="av1plan-vmaf-") as raw:
            log = Path(raw) / "vmaf.json"
            decoded_candidate = Path(raw) / "candidate-decoded.mkv"
            # The small managed VMAF runtime intentionally focuses on scoring
            # and may not carry every codec decoder. Capability proof already
            # established that the host FFmpeg can completely decode this AV1
            # output, so normalize it losslessly to FFV1 before scoring.
            decode = subprocess.run([
                "ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(candidate),
                "-map", "0:V:0", "-an", "-sn", "-dn", "-c:v", "ffv1",
                str(decoded_candidate),
            ], text=True, capture_output=True, check=False)
            if decode.returncode != 0 or not decoded_candidate.is_file():
                raise PlanError(decode.stderr.strip() or "Could not normalize the AV1 sample for VMAF.")
            graph = f"[0:v]setpts=PTS-STARTPTS[dist];[1:v]setpts=PTS-STARTPTS[ref];[dist][ref]libvmaf=log_fmt=json:log_path={log}"
            result = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(decoded_candidate), "-i", str(reference), "-lavfi", graph, "-f", "null", "-"], env=env, text=True, capture_output=True, check=False)
            if result.returncode != 0:
                raise PlanError(result.stderr.strip() or "VMAF measurement failed.")
            data = json.loads(log.read_text(encoding="utf-8"))
            scores = [
                float(frame["metrics"]["vmaf"])
                for frame in data.get("frames", [])
                if isinstance(frame.get("metrics", {}).get("vmaf"), (int, float))
            ]
            if not scores:
                raise PlanError("VMAF produced no frame scores.")
            ordered = sorted(scores)
            p10 = ordered[max(0, math.ceil(len(ordered) * 0.10) - 1)]
            longest = current = 0
            for score in scores:
                if score < quality["sustained_floor"]:
                    current += sample_duration / len(scores)
                    longest = max(longest, current)
                else:
                    current = 0.0
            mean = float(data["pooled_metrics"]["vmaf"]["mean"])
            policy_met = (
                mean >= quality["target"]
                and p10 >= quality["p10_minimum"]
                and longest < quality["maximum_sustained_seconds"]
            )
            return {
                "metric": metric,
                "predicted_score": round(mean, 3),
                "predicted_p10": round(p10, 3),
                "predicted_longest_below_floor_seconds": round(longest, 3),
                "target": quality["target"],
                "target_met_on_sample": policy_met,
                "assessment": "mean_p10_and_sustained_sample_policy",
            }
    result = subprocess.run([ffmpeg, "-hide_banner", "-v", "info", "-i", str(candidate), "-i", str(reference), "-lavfi", "[0:v]setpts=PTS-STARTPTS[dist];[1:v]setpts=PTS-STARTPTS[ref];[dist][ref]ssim", "-f", "null", "-"], env=env, text=True, capture_output=True, check=False)
    match = re.search(r"All:([0-9.]+)", result.stderr)
    if result.returncode != 0 or not match:
        raise PlanError(result.stderr.strip() or "SSIM measurement failed.")
    score = float(match.group(1)) * 100.0
    return {
        "metric": metric,
        "predicted_score": round(score, 3),
        "target": quality["target"],
        "target_met_on_sample": score >= quality["target"],
        "assessment": "mean_only; p10 and sustained thresholds require vmaf",
    }


def planning_quality_target(quality: dict[str, Any]) -> tuple[float, float]:
    """Return the sampled planning target and margin without changing acceptance policy."""
    margin = VMAF_PLANNING_MARGIN if quality["metric"] == "vmaf" else 0.0
    return min(100.0, float(quality["target"]) + margin), margin


def predict(root: Path, requirements: dict[str, Any], encoder: str, recipe: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    duration = source["duration_seconds"]
    length = min(float(requirements["evaluation"]["sample_seconds"]), duration)
    # Calibration is sampled, while final acceptance can cover additional windows.
    # Require a small mean-VMAF cushion during planning so normal sample variance
    # does not produce plans that sit only a few hundredths above the hard target.
    planning_score_target, planning_vmaf_margin = planning_quality_target(requirements["quality"])

    # A single centre sample can miss difficult openings/endings and produce an
    # executable plan that the completed-output validator immediately rejects.
    # Keep evaluation bounded, but cover early/middle/late content whenever the
    # source is long enough to contain distinct windows.
    raw_starts = [max(0.0, duration * fraction - length / 2.0) for fraction in (0.10, 0.50, 0.90)]
    maximum_start = max(0.0, duration - length)
    starts: list[float] = []
    for value in raw_starts:
        value = round(min(value, maximum_start), 6)
        if not starts or abs(value - starts[-1]) > 0.001:
            starts.append(value)
    tested: dict[int, dict[str, Any]] = {}

    with tempfile.TemporaryDirectory(prefix="av1plan-sample-") as raw:
        temp = Path(raw)
        references: list[Path] = []
        for index, sample_start in enumerate(starts):
            reference = temp / f"reference-{index}.mkv"
            extract = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{sample_start:.6f}", "-t", f"{length:.6f}",
                "-i", requirements["input"], "-map", "0:V:0",
                "-an", "-sn", "-dn", "-c:v", "ffv1", str(reference),
            ], text=True, capture_output=True, check=False)
            if extract.returncode != 0:
                raise PlanError(extract.stderr.strip() or "Could not extract an evaluation sample.")
            references.append(reference)

        def evaluate_quality(value: int) -> dict[str, Any]:
            if value in tested:
                return tested[value]
            candidate_recipe = dict(recipe)
            candidate_recipe["quality"] = dict(recipe["quality"])
            candidate_recipe["quality"]["value"] = value
            samples: list[dict[str, Any]] = []
            for index, (sample_start, reference) in enumerate(zip(starts, references)):
                candidate = temp / f"candidate-{value}-{index}.mkv"
                elapsed = encode_sample(
                    Path(requirements["input"]), candidate, encoder,
                    candidate_recipe, sample_start, length,
                )
                quality = measure_quality(
                    root, reference, candidate, requirements["quality"], length
                )
                samples.append({
                    "start_seconds": sample_start,
                    "sample_bytes": candidate.stat().st_size,
                    "encode_seconds": elapsed,
                    "quality": quality,
                })

            worst = min(samples, key=lambda item: float(item["quality"]["predicted_score"]))
            aggregate_quality = dict(worst["quality"])
            aggregate_quality["predicted_score"] = round(
                min(float(item["quality"]["predicted_score"]) for item in samples), 3
            )
            if all("predicted_p10" in item["quality"] for item in samples):
                aggregate_quality["predicted_p10"] = round(
                    min(float(item["quality"]["predicted_p10"]) for item in samples), 3
                )
            if all("predicted_longest_below_floor_seconds" in item["quality"] for item in samples):
                aggregate_quality["predicted_longest_below_floor_seconds"] = round(
                    max(float(item["quality"]["predicted_longest_below_floor_seconds"]) for item in samples), 3
                )
            base_policy_met = all(
                bool(item["quality"]["target_met_on_sample"]) for item in samples
            )
            aggregate_quality["planning_target"] = round(planning_score_target, 3)
            aggregate_quality["planning_margin"] = round(planning_vmaf_margin, 3)
            aggregate_quality["target_met_on_sample"] = (
                base_policy_met
                and float(aggregate_quality["predicted_score"]) >= planning_score_target
            )
            aggregate_quality["assessment"] = "representative_windows_all_must_pass_with_planning_margin"
            tested[value] = {
                "quality_value": value,
                "sample_bytes": sum(int(item["sample_bytes"]) for item in samples),
                "encode_seconds": sum(float(item["encode_seconds"]) for item in samples),
                "quality": aggregate_quality,
                "samples": samples,
            }
            return tested[value]

        if encoder == "av1_vaapi":
            minimum, maximum, step = 0, 255, 32
        elif encoder == "libsvtav1":
            minimum, maximum, step = 0, 63, 8
        else:
            minimum, maximum, step = 0, 51, 8
        initial = int(recipe["quality"]["value"])
        first = evaluate_quality(initial)
        passing_value: int | None = initial if first["quality"]["target_met_on_sample"] else None
        failing_value: int | None = None if passing_value is not None else initial

        if passing_value is not None:
            probe = initial
            while probe < maximum:
                candidate_value = min(maximum, probe + step)
                candidate = evaluate_quality(candidate_value)
                if candidate["quality"]["target_met_on_sample"]:
                    passing_value = candidate_value
                    probe = candidate_value
                    if probe == maximum:
                        break
                else:
                    failing_value = candidate_value
                    break
        else:
            probe = initial
            while probe > minimum:
                candidate_value = max(minimum, probe - step)
                candidate = evaluate_quality(candidate_value)
                if candidate["quality"]["target_met_on_sample"]:
                    passing_value = candidate_value
                    break
                failing_value = candidate_value
                probe = candidate_value

        if passing_value is not None and failing_value is not None:
            while failing_value - passing_value > 1:
                candidate_value = (passing_value + failing_value) // 2
                candidate = evaluate_quality(candidate_value)
                if candidate["quality"]["target_met_on_sample"]:
                    passing_value = candidate_value
                else:
                    failing_value = candidate_value

        primary = requirements["optimization"]["primary"]
        secondary = requirements["optimization"]["secondary"]
        if primary == "highest_quality":
            evaluate_quality(minimum)

        def objective(item: dict[str, Any], name: str) -> float:
            if name == "smallest_output":
                return float(item["sample_bytes"])
            if name == "fastest_encoding":
                return float(item["encode_seconds"])
            return -float(item["quality"]["predicted_score"])

        passing = [item for item in tested.values() if item["quality"]["target_met_on_sample"]]
        candidates = passing or list(tested.values())
        selected = min(
            candidates,
            key=lambda item: (
                objective(item, primary),
                objective(item, secondary),
                int(item["quality_value"]),
            ),
        )
        selected_value = int(selected["quality_value"])
        recipe["quality"]["value"] = selected_value
        sampled_seconds = length * len(starts)
        predicted_video_bytes = round(int(selected["sample_bytes"]) / sampled_seconds * duration)
        copied_stream_bytes = round(source["copied_stream_bitrate"] * duration / 8)
        predicted_output_bytes = predicted_video_bytes + copied_stream_bytes
        elapsed = float(selected["encode_seconds"])
        quality_prediction = selected["quality"]

    realtime = sampled_seconds / elapsed
    calibration = {
        "strategy": "bounded_representative_quality_search",
        "selected_quality": selected_value,
        "quality_kind": recipe["quality"]["kind"],
        "sample_count": len(starts),
        "sample_starts_seconds": starts,
        "planning_vmaf_margin": round(planning_vmaf_margin, 3),
        "planning_score_target": round(planning_score_target, 3),
        "candidates": [
            {
                "quality_value": int(item["quality_value"]),
                "sample_bytes": int(item["sample_bytes"]),
                "encode_seconds": round(float(item["encode_seconds"]), 3),
                "predicted_score": item["quality"]["predicted_score"],
                "target_met_on_sample": bool(item["quality"]["target_met_on_sample"]),
            }
            for item in sorted(tested.values(), key=lambda item: int(item["quality_value"]))
        ],
    }
    return {
        "scope": "bounded_representative_samples",
        "sample": {
            "start_seconds": starts[len(starts) // 2],
            "duration_seconds": length,
            "count": len(starts),
            "starts_seconds": starts,
        },
        "quality": quality_prediction,
        "calibration": calibration,
        "size": {
            "predicted_output_bytes": predicted_output_bytes,
            "predicted_video_bytes": predicted_video_bytes,
            "copied_stream_bytes_estimate": copied_stream_bytes,
            "basis": "representative_calibration_samples_plus_reported_copied_stream_bitrates",
        },
        "speed": {
            "measured_realtime_factor": round(realtime, 3),
            "predicted_encode_seconds": round(duration / realtime, 3),
            "basis": "representative_calibration_sample_wall_clock",
        },
        "confidence": "representative_samples_not_guaranteed",
    }


def plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in {"plan_id", "created_at"}}


def calculate_plan_id(plan: dict[str, Any]) -> str:
    return "av1p_" + hashlib.sha256(canonical(plan_payload(plan))).hexdigest()


def evaluate(requirements_path: Path, plan_path: Path, script: Path) -> dict[str, Any]:
    if not plan_path.expanduser().resolve().parent.is_dir():
        raise PlanError(f"Plan directory does not exist: {plan_path.parent}")
    root = script.parent.resolve()
    requirements = normalize_requirements(read_json(requirements_path))
    source = probe_source(Path(requirements["input"]))
    report = capabilities(script)
    encoder, encoder_class, capability = choose_encoder(requirements, report)
    recipe = recipe_for(encoder, requirements, source, capability)
    fingerprints = {
        "implementation": implementation_fingerprint(root),
        "runtime": runtime_fingerprint(encoder),
        "source": source_fingerprint(Path(requirements["input"])),
        "requirements": {"algorithm": "sha256", "value": digest(requirements)},
    }
    prediction = predict(root, requirements, encoder, recipe, source)
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requirements": requirements,
        "fingerprints": fingerprints,
        "selection": {"encoder": encoder, "class": encoder_class, "policy_owner": "AV1Encode"},
        "recipe": recipe,
        "prediction": prediction,
        "execution": {
            "state": "ready" if prediction["quality"]["target_met_on_sample"] else "rejected",
            "reason": None if prediction["quality"]["target_met_on_sample"] else "sample_quality_below_target",
        },
    }
    plan["plan_id"] = calculate_plan_id(plan)
    atomic_json(plan_path, plan)
    return plan


def verify_plan(plan: dict[str, Any], script: Path) -> None:
    if plan.get("schema") != PLAN_SCHEMA or plan.get("protocol_version") != PROTOCOL_VERSION:
        raise PlanError("Unsupported plan schema or protocol version.")
    if plan.get("plan_id") != calculate_plan_id(plan):
        raise PlanError("Plan integrity check failed; the plan was changed after evaluation.")
    requirements = normalize_requirements(plan.get("requirements", {}))
    encoder = str((plan.get("selection") or {}).get("encoder", ""))
    encoder_class = str((plan.get("selection") or {}).get("class", ""))
    if requirements["hardware_policy"] == "manual_software":
        if (encoder, encoder_class) != ("libsvtav1", "software"):
            raise PlanError("Plan selection violates manual_software policy.")
    elif encoder not in {"av1_vaapi", "av1_nvenc", "av1_qsv"} or encoder_class != "hardware":
        raise PlanError("Plan selection violates auto_hardware_only policy.")
    source = probe_source(Path(requirements["input"]))
    recipe = plan.get("recipe")
    capability: dict[str, Any] = {}
    if encoder == "av1_vaapi" and isinstance(recipe, dict):
        bit_depth = "10-bit" if recipe.get("vaapi_upload_format") == "p010le" else "8-bit"
        capability["detail"] = str(recipe.get("vaapi_device", "")) + f", {bit_depth} sealed plan"
    expected_recipe = recipe_for(encoder, requirements, source, capability)
    quality = recipe.get("quality") if isinstance(recipe, dict) else None
    expected_quality = expected_recipe["quality"]
    maximum_quality = 255 if encoder == "av1_vaapi" else (63 if encoder == "libsvtav1" else 51)
    if (
        not isinstance(quality, dict)
        or quality.get("kind") != expected_quality["kind"]
        or quality.get("preset") != expected_quality["preset"]
        or isinstance(quality.get("value"), bool)
        or not isinstance(quality.get("value"), int)
        or not 0 <= quality["value"] <= maximum_quality
    ):
        raise PlanError("Plan recipe has an invalid calibrated quality policy.")
    expected_recipe["quality"]["value"] = quality["value"]
    if recipe != expected_recipe:
        raise PlanError("Plan recipe is not the current AV1Encode policy recipe.")
    if (plan.get("execution") or {}).get("state") != "ready":
        raise PlanError("Plan is not executable because its sampled quality target was not met.")
    root = script.parent.resolve()
    current = {
        "implementation": implementation_fingerprint(root),
        "runtime": runtime_fingerprint(encoder),
        "source": source_fingerprint(Path(requirements["input"])),
        "requirements": {"algorithm": "sha256", "value": digest(requirements)},
    }
    saved = plan.get("fingerprints")
    for name in current:
        if not isinstance(saved, dict) or saved.get(name, {}).get("value") != current[name]["value"]:
            raise PlanError(f"Plan is stale: {name} fingerprint changed; evaluate again.")


def execute(plan_path: Path, result_path: Path, script: Path) -> dict[str, Any]:
    if not result_path.expanduser().resolve().parent.is_dir():
        raise PlanError(f"Result directory does not exist: {result_path.parent}")
    plan = read_json(plan_path)
    verify_plan(plan, script)
    requirements, recipe = plan["requirements"], plan["recipe"]
    with tempfile.TemporaryDirectory(prefix="av1plan-execute-") as raw:
        legacy_result = Path(raw) / "result.json"
        quality = recipe["quality"]
        command = [
            str(script), "--machine", "--encoder", recipe["encoder"],
            "--input", requirements["input"], "--output", requirements["output"],
            "--result-json", str(legacy_result), "--overwrite", "--copy-audio", "--preserve-all",
        ]
        if quality["kind"] == "crf":
            command += ["--crf", str(quality["value"]), "--preset", str(quality["preset"])]
        else:
            command += ["--qp", str(quality["value"])]
        process = subprocess.run(command, check=False)
        legacy = read_json(legacy_result) if legacy_result.is_file() else {"status": "failed", "exit_code": process.returncode}
    result = {
        "schema": RESULT_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "plan_id": plan["plan_id"],
        "status": legacy.get("status", "failed"),
        "exit_code": process.returncode,
        "input": requirements["input"],
        "output": requirements["output"],
        "encoder": recipe["encoder"],
        "prediction": plan["prediction"],
        "executor_result": legacy,
    }
    atomic_json(result_path, result)
    return result


def negotiate(text: str) -> dict[str, Any]:
    try:
        offered = {int(item.strip()) for item in text.split(",") if item.strip()}
    except ValueError as exc:
        raise PlanError("Versions must be comma-separated integers.") from exc
    common = sorted(offered.intersection(SUPPORTED_PROTOCOLS))
    return {
        "schema": "av1encode.negotiation",
        "supported_protocol_versions": list(SUPPORTED_PROTOCOLS),
        "selected_protocol_version": common[-1] if common else None,
        "compatible": bool(common),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="AV1Plan.py")
    sub = result.add_subparsers(dest="operation", required=True)
    negotiation = sub.add_parser("negotiate")
    negotiation.add_argument("versions")
    evaluation = sub.add_parser("evaluate", aliases=["plan"])
    evaluation.add_argument("requirements", type=Path)
    evaluation.add_argument("plan_json", type=Path)
    evaluation.add_argument("encoder_script", type=Path)
    execution = sub.add_parser("execute")
    execution.add_argument("plan_json", type=Path)
    execution.add_argument("result_json", type=Path)
    execution.add_argument("encoder_script", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.operation == "negotiate":
            print(json.dumps(negotiate(args.versions), sort_keys=True))
            return 0
        if args.operation in {"evaluate", "plan"}:
            plan = evaluate(args.requirements, args.plan_json, args.encoder_script.resolve())
            print(json.dumps({"schema": "av1encode.plan-reference", "protocol_version": 2, "plan_id": plan["plan_id"], "plan": str(args.plan_json.resolve()), "prediction": plan["prediction"]}, sort_keys=True))
            return 0
        result = execute(args.plan_json, args.result_json, args.encoder_script.resolve())
        print(json.dumps(result, sort_keys=True))
        return int(result["exit_code"])
    except PlanError as exc:
        error = {"schema": "av1encode.error", "protocol_version": 2, "status": "failed", "error": str(exc)}
        if getattr(args, "operation", None) == "execute":
            try:
                atomic_json(args.result_json, error)
            except OSError:
                pass
        print(json.dumps(error, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
