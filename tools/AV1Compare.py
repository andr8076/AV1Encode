#!/usr/bin/env python3
"""Plan and evaluate completed-video VMAF acceptance checks.

Cheap encoder calibration is deliberately separate from acceptance of the actual
completed output. Sampled mode is bounded and reproducible; full mode scores the
complete requested timeline and is intentionally more expensive.

Coverage is evidence-based. A requested window is not counted as measured merely
because it appears in the sample plan or because FFmpeg produced a non-empty VMAF
log. The evaluator independently inspects decoded frame timestamps/durations from
both the reference and completed output, checks that VMAF scored the expected
candidate frame population, and reports only the timeline overlap confirmed by
that evidence.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Sequence

POLICY_VERSION = "completed-video-quality-v4-boundary-overlap"
FRAME_BOUNDARY_TOLERANCE_SECONDS = 0.050
STREAM_ENDPOINT_TOLERANCE_SECONDS = 0.100
FRAME_SELECTION_EPSILON_SECONDS = 0.000001
VMAF_FRAME_COUNT_TOLERANCE = 1
VMAF_FRAME_COUNT_TOLERANCE_MIN_FRAMES = 30
INITIAL_PROBE_SEEK_BACK_SECONDS = 2.0
FALLBACK_PROBE_SEEK_BACK_SECONDS = 60.0


@dataclass(frozen=True)
class Window:
    kind: str
    start: float
    length: float

    @property
    def end(self) -> float:
        return self.start + self.length


@dataclass(frozen=True)
class FrameObservation:
    pts: float
    duration: float | None = None


@dataclass(frozen=True)
class TimedScore:
    start: float
    end: float
    score: float


def _finite(value: object) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _clamp_window(start: float, length: float, duration: float, kind: str) -> Window:
    if duration <= 0 or length <= 0:
        raise ValueError("invalid duration/window")
    length = min(length, duration)
    start = max(0.0, min(start, max(0.0, duration - length)))
    return Window(kind, start, length)


def _overlap_fraction(a: Window, b: Window) -> float:
    left = max(a.start, b.start)
    right = min(a.end, b.end)
    if right <= left:
        return 0.0
    return (right - left) / min(a.length, b.length)


def union_intervals(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    clean = sorted((float(start), float(end)) for start, end in intervals if end > start)
    if not clean:
        return []
    merged: list[tuple[float, float]] = []
    left, right = clean[0]
    for start, end in clean[1:]:
        if start <= right:
            right = max(right, end)
        else:
            merged.append((left, right))
            left, right = start, end
    merged.append((left, right))
    return merged


def interval_coverage(intervals: Sequence[tuple[float, float]]) -> float:
    return sum(end - start for start, end in union_intervals(intervals))


def union_coverage(windows: Sequence[Window]) -> float:
    return interval_coverage([(w.start, w.end) for w in windows])


def intersect_intervals(
    first: Sequence[tuple[float, float]], second: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    a = union_intervals(first)
    b = union_intervals(second)
    result: list[tuple[float, float]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        left = max(a[i][0], b[j][0])
        right = min(a[i][1], b[j][1])
        if right > left:
            result.append((left, right))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return result


def plan_uniform_windows(
    duration: float,
    sample_seconds: float = 4.0,
    interval_seconds: float = 300.0,
    min_samples: int = 5,
    max_samples: int = 16,
    complexity_slots: int = 2,
) -> list[Window]:
    duration = _finite(duration)
    sample_seconds = _finite(sample_seconds)
    interval_seconds = _finite(interval_seconds)
    if duration <= 0 or sample_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("duration, sample seconds and interval must be positive")
    if min_samples < 1 or max_samples < 1 or min_samples > max_samples:
        raise ValueError("invalid sample bounds")
    if max_samples > 64:
        raise ValueError("max_samples exceeds hard safety bound")
    complexity_slots = max(0, min(complexity_slots, max_samples - 1))

    if duration <= sample_seconds * min_samples:
        count = max(1, min(max_samples, math.ceil(duration / sample_seconds)))
        windows: list[Window] = []
        cursor = 0.0
        for _ in range(count):
            remaining = duration - cursor
            if remaining <= 0:
                break
            length = min(sample_seconds, remaining)
            windows.append(Window("short-full", cursor, length))
            cursor += length
        return windows

    uniform_cap = max(1, max_samples - complexity_slots)
    intervals = max(1, math.ceil(duration / interval_seconds))
    desired = min_samples + max(0, intervals - 1)
    count = min(uniform_cap, desired)
    length = min(sample_seconds, duration)
    windows = []
    for index in range(count):
        center = duration * (index + 0.5) / count
        windows.append(_clamp_window(center - length / 2.0, length, duration, "uniform"))
    return windows


def packet_complexity_candidates(
    input_path: str,
    duration: float,
    sample_seconds: float,
    limit: int,
    ffprobe: str = "ffprobe",
) -> list[Window]:
    """Return deterministic high-packet-rate windows as inexpensive scene hints."""
    if limit <= 0:
        return []
    command = [
        ffprobe, "-v", "error", "-select_streams", "V:0", "-show_packets",
        "-show_entries", "packet=pts_time,size", "-of", "csv=p=0", input_path,
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return []

    buckets: dict[int, int] = {}
    assert process.stdout is not None
    try:
        for row in csv.reader(process.stdout):
            if len(row) < 2:
                continue
            try:
                pts = float(row[0])
                size = int(row[1])
            except (ValueError, OverflowError):
                continue
            if not math.isfinite(pts) or pts < 0 or size <= 0:
                continue
            bucket = int(pts // sample_seconds)
            buckets[bucket] = buckets.get(bucket, 0) + size
    finally:
        process.stdout.close()
    if process.wait() != 0:
        return []

    result: list[Window] = []
    for bucket, _bytes in sorted(buckets.items(), key=lambda item: (-item[1], item[0])):
        candidate = _clamp_window(bucket * sample_seconds, sample_seconds, duration, "complexity")
        if any(_overlap_fraction(candidate, existing) >= 0.5 for existing in result):
            continue
        result.append(candidate)
        if len(result) >= limit:
            break
    return result


def add_complexity_windows(
    uniform: Sequence[Window], candidates: Sequence[Window], max_samples: int
) -> list[Window]:
    windows = list(uniform)
    for candidate in candidates:
        if len(windows) >= max_samples:
            break
        if any(_overlap_fraction(candidate, existing) >= 0.5 for existing in windows):
            continue
        windows.append(candidate)
    return sorted(windows, key=lambda w: (w.start, w.kind))


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        raise ValueError("no values")
    if not 0 <= percent <= 100:
        raise ValueError("percentile outside 0..100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def read_vmaf_log(path: str) -> tuple[float, list[float], list[int]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    pooled = _finite(data["pooled_metrics"]["vmaf"]["mean"])
    raw_frames = data["frames"]
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("missing VMAF frames")
    frames: list[float] = []
    frame_numbers: list[int] = []
    for index, frame in enumerate(raw_frames):
        if not isinstance(frame, dict):
            raise ValueError("malformed VMAF frame record")
        number = frame.get("frameNum")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("missing/malformed VMAF frame number")
        if number != index:
            raise ValueError("non-contiguous VMAF frame numbers")
        value = _finite(frame["metrics"]["vmaf"])
        if not 0.0 <= value <= 100.0:
            raise ValueError("VMAF frame outside 0..100")
        frames.append(value)
        frame_numbers.append(number)
    if not 0.0 <= pooled <= 100.0:
        raise ValueError("pooled VMAF outside 0..100")
    arithmetic_mean = sum(frames) / len(frames)
    if abs(pooled - arithmetic_mean) > 0.05:
        raise ValueError(
            f"VMAF pooled mean {pooled:.6f} inconsistent with frame mean {arithmetic_mean:.6f}"
        )
    return pooled, frames, frame_numbers


def read_manifest(path: str) -> list[tuple[Window, str]]:
    rows: list[tuple[Window, str]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 4:
                raise ValueError(f"invalid measurement row {line_number}")
            kind, start_raw, length_raw, log_path = fields
            start = _finite(start_raw)
            length = _finite(length_raw)
            if start < 0 or length <= 0 or not log_path:
                raise ValueError(f"invalid measurement row {line_number}")
            rows.append((Window(kind, start, length), log_path))
    if not rows:
        raise ValueError("no completed-video quality measurements")
    return rows


@lru_cache(maxsize=16)
def probe_stream_start(path: str, ffprobe: str = "ffprobe") -> float:
    command = [
        ffprobe, "-v", "error", "-select_streams", "V:0",
        "-show_entries", "stream=start_time", "-of", "default=nw=1:nk=1", path,
    ]
    process = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if process.returncode != 0:
        raise ValueError(f"ffprobe stream origin failed: {process.stderr.strip() or process.returncode}")
    value = process.stdout.strip().splitlines()
    if not value or value[0] in ("", "N/A"):
        return 0.0
    try:
        return _finite(value[0])
    except ValueError as exc:
        raise ValueError("invalid video stream start_time") from exc


def _parse_frame_line(line: str) -> FrameObservation | None:
    fields: dict[str, str] = {}
    for part in line.strip().split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key] = value
    pts_raw = fields.get("best_effort_timestamp_time") or fields.get("pts_time")
    if not pts_raw or pts_raw == "N/A":
        return None
    try:
        pts = _finite(pts_raw)
    except (TypeError, ValueError):
        return None
    duration: float | None = None
    duration_raw = fields.get("duration_time") or fields.get("pkt_duration_time")
    if duration_raw and duration_raw != "N/A":
        try:
            parsed = _finite(duration_raw)
        except (TypeError, ValueError):
            parsed = 0.0
        if parsed > 0:
            duration = parsed
    return FrameObservation(pts=pts, duration=duration)


def _probe_frames_once(
    path: str, window: Window, seek_back: float, ffprobe: str
) -> list[FrameObservation]:
    timestamp_origin = probe_stream_start(path, ffprobe)
    probe_start = timestamp_origin + max(0.0, window.start - max(0.0, seek_back))
    command = [
        ffprobe, "-v", "error", "-select_streams", "V:0",
        "-read_intervals", f"{probe_start:.9f}%",
        "-show_frames",
        "-show_entries", "frame=pts_time,best_effort_timestamp_time,duration_time,pkt_duration_time",
        "-of", "compact=p=0:nk=0", path,
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        raise ValueError(f"ffprobe frame evidence unavailable: {exc}") from exc

    observations: list[FrameObservation] = []
    deliberately_stopped = False
    assert process.stdout is not None
    try:
        for line in process.stdout:
            observation = _parse_frame_line(line)
            if observation is None:
                continue
            observation = FrameObservation(
                pts=observation.pts - timestamp_origin, duration=observation.duration
            )
            if observations and observation.pts + 1e-9 < observations[-1].pts:
                process.kill()
                process.wait()
                raise ValueError("non-monotonic decoded frame timestamps")
            if observations and abs(observation.pts - observations[-1].pts) <= 1e-9:
                continue
            observations.append(observation)
            # One frame after the requested end is enough to infer the display
            # duration of the preceding VFR frame when frame duration is absent.
            if observation.pts > window.end + FRAME_BOUNDARY_TOLERANCE_SECONDS:
                deliberately_stopped = True
                process.terminate()
                break
    finally:
        process.stdout.close()

    try:
        returncode = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait()
    stderr = ""
    if process.stderr is not None:
        stderr = process.stderr.read().strip()
        process.stderr.close()
    if not deliberately_stopped and returncode != 0:
        raise ValueError(f"ffprobe frame evidence failed: {stderr or returncode}")
    if not observations:
        raise ValueError("no decoded frame timestamp evidence")
    return observations


def frame_display_interval(
    observations: Sequence[FrameObservation], index: int
) -> tuple[float, float] | None:
    frame = observations[index]
    if index + 1 < len(observations) and observations[index + 1].pts > frame.pts:
        end = observations[index + 1].pts
    elif frame.duration is not None and frame.duration > 0:
        end = frame.pts + frame.duration
    else:
        return None
    if not math.isfinite(end) or end <= frame.pts:
        return None
    return frame.pts, end


def frame_spans(observations: Sequence[FrameObservation]) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for index in range(len(observations)):
        interval = frame_display_interval(observations, index)
        if interval is not None:
            spans.append(interval)
    return spans


def analyze_timeline_evidence(
    observations: Sequence[FrameObservation], window: Window, *, stream_endpoint: bool = False
) -> dict[str, object]:
    spans = frame_spans(observations)
    clipped = [
        (max(start, window.start), min(end, window.end))
        for start, end in spans
        if end > window.start and start < window.end
    ]
    merged = union_intervals(clipped)
    coverage = interval_coverage(merged)
    if merged:
        start_gap = max(0.0, merged[0][0] - window.start)
        end_gap = max(0.0, window.end - merged[-1][1])
        internal_gap = max(
            (max(0.0, merged[index + 1][0] - merged[index][1])
             for index in range(len(merged) - 1)), default=0.0,
        )
    else:
        start_gap = end_gap = window.length
        internal_gap = 0.0

    selected: list[FrameObservation] = []
    selected_intervals: list[tuple[float, float] | None] = []
    selected_durations: list[float] = []
    missing_timing = 0
    for frame_index, frame in enumerate(observations):
        # Completed-output sample starts are snapped to candidate frame PTS.
        # Count the same half-open [start,end) population that FFmpeg feeds to
        # libvmaf, without using the wider coverage-gap tolerance.
        if (
            frame.pts + FRAME_SELECTION_EPSILON_SECONDS < window.start
            or frame.pts >= window.end - FRAME_SELECTION_EPSILON_SECONDS
        ):
            continue
        selected.append(frame)
        interval = frame_display_interval(observations, frame_index)
        if interval is None:
            selected_intervals.append(None)
            selected_durations.append(0.0)
            missing_timing += 1
            continue
        span_start, span_end = interval
        clipped_start = max(span_start, window.start)
        clipped_end = min(span_end, window.end)
        if clipped_end <= clipped_start:
            selected_intervals.append(None)
            selected_durations.append(0.0)
            missing_timing += 1
            continue
        selected_intervals.append((clipped_start, clipped_end))
        selected_durations.append(clipped_end - clipped_start)

    endpoint_tolerance = (
        STREAM_ENDPOINT_TOLERANCE_SECONDS if stream_endpoint
        else FRAME_BOUNDARY_TOLERANCE_SECONDS
    )
    reasons: list[str] = []
    if start_gap > FRAME_BOUNDARY_TOLERANCE_SECONDS:
        reasons.append(f"start-gap {start_gap:.6f}s > {FRAME_BOUNDARY_TOLERANCE_SECONDS:.3f}s")
    if internal_gap > FRAME_BOUNDARY_TOLERANCE_SECONDS:
        reasons.append(f"internal-gap {internal_gap:.6f}s > {FRAME_BOUNDARY_TOLERANCE_SECONDS:.3f}s")
    if end_gap > endpoint_tolerance:
        reasons.append(f"end-gap {end_gap:.6f}s > {endpoint_tolerance:.3f}s")
    if not selected:
        reasons.append("no candidate frames in requested interval")
    if missing_timing:
        reasons.append(f"missing display timing for {missing_timing} selected frame(s)")
    return {
        "coverage_seconds": coverage,
        "intervals": merged,
        "frame_count": len(selected),
        "frame_intervals": selected_intervals,
        "frame_durations": selected_durations,
        "observed_frames": len(observations),
        "start_gap": start_gap,
        "end_gap": end_gap,
        "max_internal_gap": internal_gap,
        "complete": not reasons,
        "reasons": reasons,
    }


def probe_frame_timeline(
    path: str, window: Window, ffprobe: str = "ffprobe"
) -> list[FrameObservation]:
    observations = _probe_frames_once(
        path, window, INITIAL_PROBE_SEEK_BACK_SECONDS, ffprobe
    )
    initial = analyze_timeline_evidence(observations, window)
    if initial["start_gap"] <= FRAME_BOUNDARY_TOLERANCE_SECONDS or window.start <= 0:
        return observations
    # Seeking can legitimately land after the desired display frame on sparse
    # or very-low-frame-rate material. Retry once farther back; this is bounded
    # and does not change the acceptance threshold.
    return _probe_frames_once(path, window, FALLBACK_PROBE_SEEK_BACK_SECONDS, ffprobe)


def snap_windows_to_frame_starts(
    windows: Sequence[Window], input_path: str, duration: float, ffprobe: str = "ffprobe",
    evidence_provider: Callable[[str, Window, str], list[FrameObservation]] | None = None,
) -> list[Window]:
    """Snap sampled starts backward to decoded candidate frame boundaries.

    FFmpeg input-side seeking can otherwise round a fractional request to a
    neighbouring frame differently from ffprobe's half-open population count.
    Snapping to a proven candidate PTS keeps the VMAF sequence and independent
    timing evidence one-to-one without guessing an average frame rate.
    """
    provider = evidence_provider or probe_frame_timeline
    snapped: list[Window] = []
    for window in windows:
        if window.start <= FRAME_SELECTION_EPSILON_SECONDS:
            snapped.append(window)
            continue
        try:
            observations = provider(input_path, window, ffprobe)
            eligible = [
                frame.pts for frame in observations
                if frame.pts >= 0
                and frame.pts <= window.start + FRAME_SELECTION_EPSILON_SECONDS
                and frame.pts + window.length <= duration + STREAM_ENDPOINT_TOLERANCE_SECONDS
            ]
        except (OSError, TypeError, ValueError):
            eligible = []
        if eligible:
            snapped.append(Window(window.kind, max(eligible), window.length))
        else:
            snapped.append(window)
    return snapped


def map_vmaf_scores_to_timeline(
    scores: Sequence[float], frame_numbers: Sequence[int], candidate_evidence: dict[str, object]
) -> list[TimedScore]:
    """Map libvmaf sequence records to candidate presentation intervals.

    libvmaf frameNum is a sequence index, not a timestamp. The production graph
    does not convert FPS or subsample frames, and it feeds the completed candidate
    as the distorted stream after PTS normalization. Therefore sequence index i
    can be associated with candidate interval i only when the independently
    probed candidate frame population matches exactly and every interval has
    trustworthy timing evidence.
    """
    intervals = candidate_evidence.get("frame_intervals")
    if not isinstance(intervals, list):
        raise ValueError("candidate frame timing evidence missing")
    if len(scores) != len(intervals):
        raise ValueError(
            f"exact VMAF/timeline mapping unavailable: {len(scores)} VMAF record(s) "
            f"for {len(intervals)} candidate frame interval(s)"
        )
    if list(frame_numbers) != list(range(len(scores))):
        raise ValueError("VMAF frame numbers are not a contiguous sequence")

    mapped: list[TimedScore] = []
    previous_start = -math.inf
    for index, (score, interval) in enumerate(zip(scores, intervals)):
        if interval is None or not isinstance(interval, tuple) or len(interval) != 2:
            raise ValueError(f"candidate timing missing for VMAF sequence index {index}")
        start = _finite(interval[0])
        end = _finite(interval[1])
        if end <= start:
            raise ValueError(f"invalid candidate display interval for VMAF sequence index {index}")
        if start + FRAME_SELECTION_EPSILON_SECONDS < previous_start:
            raise ValueError("candidate display intervals are not presentation-ordered")
        mapped.append(TimedScore(start=start, end=end, score=_finite(score)))
        previous_start = start
    return mapped


def longest_sustained_low_quality(
    timed_scores: Sequence[TimedScore], sustained_floor: float
) -> float:
    """Measure low-quality time on the sampled presentation timeline.

    Unioning the actual low-score display intervals merges degradation across
    truly adjacent sample-window boundaries, leaves unsampled gaps disconnected,
    and prevents overlapping sample windows from double-counting the same time.
    """
    low_intervals = [
        (item.start, item.end)
        for item in timed_scores
        if item.score < sustained_floor and item.end > item.start
    ]
    merged = union_intervals(low_intervals)
    return max((end - start for start, end in merged), default=0.0)


def _evidence_error(prefix: str, reasons: Sequence[str]) -> str:
    return f"{prefix}: " + ", ".join(reasons)


def evaluate_manifest(
    manifest_path: str,
    duration: float,
    threshold: float,
    low_percentile: float = 10.0,
    percentile_delta: float = 4.0,
    sustained_delta: float = 6.0,
    sustained_seconds: float = 1.0,
    *,
    reference_path: str,
    candidate_path: str,
    ffprobe: str = "ffprobe",
    evidence_provider: Callable[[str, Window, str], list[FrameObservation]] | None = None,
) -> dict[str, object]:
    duration = _finite(duration)
    threshold = _finite(threshold)
    low_percentile = _finite(low_percentile)
    percentile_delta = _finite(percentile_delta)
    sustained_delta = _finite(sustained_delta)
    sustained_seconds = _finite(sustained_seconds)
    if duration <= 0 or not 0 <= threshold <= 100:
        raise ValueError("invalid duration/threshold")
    if not 0 <= low_percentile <= 50:
        raise ValueError("low percentile must be between 0 and 50")
    if percentile_delta < 0 or sustained_delta < 0 or sustained_seconds <= 0:
        raise ValueError("invalid local-quality settings")
    if not reference_path or not candidate_path:
        raise ValueError("reference and candidate are required for timeline evidence")

    rows = read_manifest(manifest_path)
    provider = evidence_provider or probe_frame_timeline
    all_frames: list[float] = []
    window_means: list[float] = []
    sustained_floor = threshold - sustained_delta
    evidence_reasons: list[str] = []
    confirmed_intervals: list[tuple[float, float]] = []
    timed_scores: list[TimedScore] = []
    window_evidence: list[dict[str, object]] = []

    for index, (window, log_path) in enumerate(rows, 1):
        if window.end > duration + STREAM_ENDPOINT_TOLERANCE_SECONDS:
            raise ValueError(f"measurement window {index} exceeds source duration")
        pooled, frames, frame_numbers = read_vmaf_log(log_path)
        window_means.append(pooled)
        all_frames.extend(frames)

        reference_observations = provider(reference_path, window, ffprobe)
        candidate_observations = provider(candidate_path, window, ffprobe)
        stream_endpoint = window.end >= duration - STREAM_ENDPOINT_TOLERANCE_SECONDS
        reference_evidence = analyze_timeline_evidence(
            reference_observations, window, stream_endpoint=stream_endpoint
        )
        candidate_evidence = analyze_timeline_evidence(
            candidate_observations, window, stream_endpoint=stream_endpoint
        )
        overlap = intersect_intervals(
            reference_evidence["intervals"], candidate_evidence["intervals"]
        )
        timeline_overlap = interval_coverage(overlap)
        expected_frames = int(candidate_evidence["frame_count"])
        scored_frames = len(frames)

        current_reasons: list[str] = []
        if not reference_evidence["complete"]:
            current_reasons.append(
                _evidence_error("reference timeline incomplete", reference_evidence["reasons"])
            )
        if not candidate_evidence["complete"]:
            current_reasons.append(
                _evidence_error("candidate timeline incomplete", candidate_evidence["reasons"])
            )
        frame_count_difference = abs(scored_frames - expected_frames)
        allowed_frame_difference = (
            VMAF_FRAME_COUNT_TOLERANCE
            if expected_frames >= VMAF_FRAME_COUNT_TOLERANCE_MIN_FRAMES else 0
        )
        if frame_count_difference > allowed_frame_difference:
            current_reasons.append(
                f"VMAF scored {scored_frames} frame(s), candidate evidence has {expected_frames} "
                f"frame(s) in the window (allowed difference {allowed_frame_difference})"
            )

        mapped_scores: list[TimedScore] = []
        try:
            mapped_scores = map_vmaf_scores_to_timeline(
                frames, frame_numbers, candidate_evidence
            )
        except (TypeError, ValueError) as exc:
            current_reasons.append(f"VMAF timing alignment failed: {exc}")

        # Even with valid endpoints, a large uncovered overlap would indicate
        # contradictory evidence. Do not convert planned duration into coverage.
        missing_overlap = max(0.0, window.length - timeline_overlap)
        overlap_tolerance = (
            2 * FRAME_BOUNDARY_TOLERANCE_SECONDS
            + (STREAM_ENDPOINT_TOLERANCE_SECONDS if stream_endpoint
               else FRAME_BOUNDARY_TOLERANCE_SECONDS)
        )
        if missing_overlap > overlap_tolerance:
            current_reasons.append(
                f"timeline overlap {timeline_overlap:.6f}s of requested {window.length:.6f}s"
            )
        if current_reasons:
            evidence_reasons.extend(
                f"window {index} ({window.kind} {window.start:.6f}s): {reason}"
                for reason in current_reasons
            )
            confirmed = 0.0
        else:
            confirmed = timeline_overlap
            confirmed_intervals.extend(overlap)
            timed_scores.extend(mapped_scores)

        window_evidence.append({
            "index": index,
            "kind": window.kind,
            "start": window.start,
            "requested_seconds": window.length,
            "timeline_overlap_seconds": timeline_overlap,
            "confirmed_seconds": confirmed,
            "reference_frames": int(reference_evidence["frame_count"]),
            "candidate_frames": expected_frames,
            "vmaf_frames": scored_frames,
            "timed_vmaf_frames": len(mapped_scores) if not current_reasons else 0,
            "timing_alignment": "sequence-index-to-candidate-pts" if not current_reasons else "unavailable",
            "complete": not current_reasons,
            "reasons": current_reasons,
        })

    if not all_frames or not window_means:
        raise ValueError("empty VMAF measurements")

    longest_sustained = longest_sustained_low_quality(timed_scores, sustained_floor)
    aggregate_mean = sum(all_frames) / len(all_frames)
    low_value = percentile(all_frames, low_percentile)
    minimum_window = min(window_means)
    percentile_floor = threshold - percentile_delta
    quality_reasons: list[str] = []
    if aggregate_mean < threshold:
        quality_reasons.append(f"mean-vmaf {aggregate_mean:.3f} < target {threshold:.3f}")
    if minimum_window < threshold:
        quality_reasons.append(f"window-mean {minimum_window:.3f} < target {threshold:.3f}")
    if low_value < percentile_floor:
        quality_reasons.append(
            f"p{low_percentile:g}-vmaf {low_value:.3f} < floor {percentile_floor:.3f}"
        )
    if longest_sustained + 1e-9 >= sustained_seconds:
        quality_reasons.append(
            f"sustained-low-quality {longest_sustained:.3f}s >= {sustained_seconds:.3f}s "
            f"below {sustained_floor:.3f}"
        )

    windows = [window for window, _ in rows]
    requested_coverage_seconds = min(duration, union_coverage(windows))
    confirmed_coverage_seconds = min(duration, interval_coverage(confirmed_intervals))
    if evidence_reasons:
        status = "error"
        reasons = evidence_reasons
    elif quality_reasons:
        status = "reject"
        reasons = quality_reasons
    else:
        status = "pass"
        reasons = []

    return {
        "policy": POLICY_VERSION,
        "status": status,
        "windows": len(windows),
        "frames": len(all_frames),
        "timed_frames": len(timed_scores),
        "timing_alignment": "vmaf-sequence-to-candidate-presentation-interval",
        "requested_coverage_seconds": requested_coverage_seconds,
        "requested_coverage_percent": requested_coverage_seconds * 100.0 / duration,
        "coverage_seconds": confirmed_coverage_seconds,
        "coverage_percent": confirmed_coverage_seconds * 100.0 / duration,
        "evidence_complete": not evidence_reasons,
        "boundary_tolerance_seconds": FRAME_BOUNDARY_TOLERANCE_SECONDS,
        "stream_endpoint_tolerance_seconds": STREAM_ENDPOINT_TOLERANCE_SECONDS,
        "frame_selection_epsilon_seconds": FRAME_SELECTION_EPSILON_SECONDS,
        "vmaf_frame_count_tolerance": VMAF_FRAME_COUNT_TOLERANCE,
        "vmaf_frame_count_tolerance_min_frames": VMAF_FRAME_COUNT_TOLERANCE_MIN_FRAMES,
        "mean_vmaf": aggregate_mean,
        "minimum_window_mean": minimum_window,
        "low_percentile": low_percentile,
        "low_percentile_vmaf": low_value,
        "low_percentile_floor": percentile_floor,
        "sustained_floor": sustained_floor,
        "longest_sustained_seconds": longest_sustained,
        "window_evidence": window_evidence,
        "reasons": reasons,
    }


def higher_quality(encoder: str, quality: int, step: int) -> int | None:
    if step < 1:
        raise ValueError("retry step must be positive")
    lower_is_better = {"av1_vaapi", "av1_nvenc", "av1_qsv"}
    higher_is_better: set[str] = set()
    if encoder in lower_is_better:
        candidate = max(1, quality - step)
        return candidate if candidate < quality else None
    if encoder in higher_is_better:
        candidate = min(100, quality + step)
        return candidate if candidate > quality else None
    return None

import hashlib
import os
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "2.0"
DEFAULT_TARGET = 92.0
SAMPLE_SECONDS = 4.0
INTERVAL_SECONDS = 300.0
MIN_SAMPLES = 5
MAX_SAMPLES = 16
COMPLEXITY_SAMPLES = 2
LOW_PERCENTILE = 10.0
PERCENTILE_DELTA = 4.0
SUSTAINED_DELTA = 6.0
SUSTAINED_SECONDS = 1.0
QUALITY_RELEASE_BASE = "https://github.com/andr8076/AV1Encode/releases/download/quality-runtime-latest"


@dataclass(frozen=True)
class QualityTools:
    ffmpeg: str
    ffprobe: str
    env: dict[str, str]


def _run(command: list[str], *, env: dict[str, str] | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _probe_json(ffprobe: str, path: str, env: dict[str, str] | None = None) -> dict[str, object]:
    process = _run([
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path
    ], env=env)
    if process.returncode != 0:
        raise ValueError(process.stderr.strip() or f"ffprobe failed for {path}")
    return json.loads(process.stdout)


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TiB"


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _fps(value: object) -> float:
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    except (TypeError, ValueError):
        return 0.0


def _bitrate(value: object) -> str:
    bitrate = _number(value)
    return f"{bitrate / 1_000_000:.2f} Mb/s" if bitrate else "unknown"


def _stream_tags(stream: dict[str, object]) -> str:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
    bits: list[str] = []
    if tags.get("language"):
        bits.append(f"lang={tags['language']}")
    if tags.get("title"):
        bits.append(f"title={tags['title']}")
    if disposition.get("default"):
        bits.append("default")
    if disposition.get("forced"):
        bits.append("forced")
    if disposition.get("hearing_impaired"):
        bits.append("hearing-impaired")
    return ", ".join(bits) if bits else "-"


def _media_summary(label: str, path: str, data: dict[str, object]) -> tuple[int, float, dict[str, int]]:
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    size = os.path.getsize(path)
    duration = _number(fmt.get("duration"))
    types = ("video", "audio", "subtitle", "data", "attachment")
    counts = {
        kind: sum(1 for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == kind)
        for kind in types
    }
    print(f"\n{label}")
    print("─" * 72)
    print(f"File:       {path}")
    print(f"Size:       {_human_bytes(size)} ({size:,} bytes)")
    print(f"Duration:   {duration:.3f} s")
    print(f"Container:  {fmt.get('format_long_name') or fmt.get('format_name') or 'unknown'}")
    print(f"Bitrate:    {_bitrate(fmt.get('bit_rate'))}")
    print("Tracks:     " + " | ".join(f"{kind}={counts[kind]}" for kind in types))
    for raw_stream in streams:
        if not isinstance(raw_stream, dict):
            continue
        stream = raw_stream
        kind = str(stream.get("codec_type", "unknown"))
        index = stream.get("index", "?")
        codec = str(stream.get("codec_name", "unknown"))
        if kind == "video":
            rate = _fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0")
            extra = (
                f"{stream.get('width', '?')}x{stream.get('height', '?')}, "
                f"{stream.get('pix_fmt', '?')}, {rate:.3f} fps, {_bitrate(stream.get('bit_rate'))}, "
                f"profile={stream.get('profile', '?')}"
            )
            disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
            if disposition.get("attached_pic"):
                extra += ", attached-pic"
        elif kind == "audio":
            extra = (
                f"{stream.get('sample_rate', '?')} Hz, {stream.get('channels', '?')} ch, "
                f"{stream.get('channel_layout', '?')}, {_bitrate(stream.get('bit_rate'))}, "
                f"profile={stream.get('profile', '?')}"
            )
        else:
            extra = f"codec={codec}"
        print(f"  #{str(index):<2} {kind:<10} {codec:<14} {extra}; {_stream_tags(stream)}")
    return size, duration, counts


def print_media_comparison(reference: str, candidate: str, ffprobe: str) -> tuple[dict[str, object], dict[str, object]]:
    reference_data = _probe_json(ffprobe, reference)
    candidate_data = _probe_json(ffprobe, candidate)
    ref_size, ref_duration, ref_counts = _media_summary("ORIGINAL", reference, reference_data)
    cand_size, cand_duration, cand_counts = _media_summary("CANDIDATE", candidate, candidate_data)
    saving = (1.0 - cand_size / ref_size) * 100.0 if ref_size else 0.0
    ratio = cand_size / ref_size if ref_size else 0.0
    print("\nCOMPARISON")
    print("═" * 72)
    print(
        f"Size:       {_human_bytes(ref_size)} -> {_human_bytes(cand_size)}  "
        f"({saving:+.2f}% saved; candidate={ratio:.3f}x original)"
    )
    print(
        f"Duration:   {ref_duration:.3f}s -> {cand_duration:.3f}s  "
        f"(delta {cand_duration - ref_duration:+.3f}s)"
    )
    for kind in ("video", "audio", "subtitle", "data", "attachment"):
        mark = "OK" if ref_counts[kind] == cand_counts[kind] else "CHANGED"
        print(f"{kind.capitalize():<11}{ref_counts[kind]} -> {cand_counts[kind]}  [{mark}]")
    return reference_data, candidate_data


def _download(url: str, destination: Path) -> None:
    last_error: Exception | None = None
    for _ in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "AV1Compare/2.0"})
            with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except Exception as exc:  # bounded retry; surfaced if all attempts fail
            last_error = exc
    raise RuntimeError(f"download failed: {url}: {last_error}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"unsafe archive path: {member.name}")
        tar.extractall(destination)


def select_quality_tools() -> QualityTools:
    override_ffmpeg = os.environ.get("ENCODEAV1_COMPARE_FFMPEG")
    override_ffprobe = os.environ.get("ENCODEAV1_COMPARE_FFPROBE")
    base_env = os.environ.copy()
    if override_ffmpeg and override_ffprobe:
        return QualityTools(override_ffmpeg, override_ffprobe, base_env)

    system_ffmpeg = shutil.which("ffmpeg")
    system_ffprobe = shutil.which("ffprobe")
    if system_ffmpeg and system_ffprobe:
        filters = _run([system_ffmpeg, "-hide_banner", "-filters"], env=base_env)
        if filters.returncode == 0 and re.search(r"(^|\s)libvmaf(\s|$)", filters.stdout):
            return QualityTools(system_ffmpeg, system_ffprobe, base_env)

    system_name = platform.system()
    machine = platform.machine().lower()
    if system_name == "Linux":
        os_name = "linux"
    elif system_name == "Darwin":
        os_name = "macos"
    else:
        raise RuntimeError("Unsupported OS for managed VMAF runtime.")
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError("Unsupported architecture for managed VMAF runtime.")

    target = f"{os_name}-{arch}"
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache = cache_root / "AV1Encode" / "quality-runtime" / target
    runtime = cache / "runtime"
    ffmpeg = runtime / "bin" / "ffmpeg"
    ffprobe = runtime / "bin" / "ffprobe"

    if not (ffmpeg.is_file() and os.access(ffmpeg, os.X_OK) and ffprobe.is_file() and os.access(ffprobe, os.X_OK)):
        cache.mkdir(parents=True, exist_ok=True)
        print("Downloading optional AV1Encode VMAF quality runtime...")
        pointer = f"av1encode-quality-runtime-{target}.current"
        with tempfile.TemporaryDirectory(prefix="av1compare-runtime-", dir=cache) as temp_raw:
            temp = Path(temp_raw)
            pointer_path = temp / pointer
            _download(f"{QUALITY_RELEASE_BASE}/{pointer}", pointer_path)
            asset = pointer_path.read_text(encoding="utf-8").strip()
            pattern = rf"av1encode-quality-runtime-{re.escape(target)}-[0-9a-f]{{40}}\.tar\.gz"
            if re.fullmatch(pattern, asset) is None:
                raise RuntimeError("Invalid runtime pointer.")
            archive = temp / asset
            checksum = temp / f"{asset}.sha256"
            _download(f"{QUALITY_RELEASE_BASE}/{asset}", archive)
            _download(f"{QUALITY_RELEASE_BASE}/{asset}.sha256", checksum)
            expected = checksum.read_text(encoding="utf-8").split()[0].lower()
            actual = _sha256(archive)
            if re.fullmatch(r"[0-9a-f]{64}", expected) is None or expected != actual:
                raise RuntimeError("Quality runtime checksum verification failed.")
            extracted = temp / "extracted"
            extracted.mkdir()
            _safe_extract(archive, extracted)
            staged = extracted / "runtime"
            if not (staged / "bin" / "ffmpeg").is_file() or not (staged / "bin" / "ffprobe").is_file():
                raise RuntimeError("Quality runtime archive is incomplete.")
            replacement = cache / "runtime.new"
            if replacement.exists():
                shutil.rmtree(replacement)
            shutil.move(str(staged), str(replacement))
            if runtime.exists():
                shutil.rmtree(runtime)
            replacement.rename(runtime)

    env = os.environ.copy()
    if os_name == "linux":
        lib = str(runtime / "lib")
        env["LD_LIBRARY_PATH"] = lib + ((":" + env["LD_LIBRARY_PATH"]) if env.get("LD_LIBRARY_PATH") else "")
        # Timeline-evidence helpers call ffprobe internally; keep their child environment identical.
        os.environ["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH"]
    return QualityTools(str(ffmpeg), str(ffprobe), env)


def _source_geometry(ffprobe: str, reference: str, env: dict[str, str]) -> tuple[float, int, int, str, str]:
    process = _run([
        ffprobe, "-v", "error", "-select_streams", "V:0", "-show_streams", "-show_format", "-of", "json", reference
    ], env=env)
    if process.returncode != 0:
        raise ValueError(process.stderr.strip() or "Could not inspect source video.")
    data = json.loads(process.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("Could not determine source video stream.")
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Could not determine source display geometry.")
    duration = _number((data.get("format") or {}).get("duration"))
    if duration <= 0:
        duration = _number(stream.get("duration"))
    if duration <= 0:
        raise ValueError("Could not determine source duration.")
    sar_raw = str(stream.get("sample_aspect_ratio") or "1:1")
    try:
        sar_n, sar_d = (int(part) for part in sar_raw.split(":", 1))
        if sar_n <= 0 or sar_d <= 0:
            raise ValueError
    except (ValueError, TypeError):
        sar_n = sar_d = 1
    rotation = 0
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                rotation = int(side_data["rotation"])
            except (TypeError, ValueError):
                pass
    rotation %= 360
    display_width = max(2, int((width * sar_n / sar_d) / 2.0 + 0.5) * 2)
    display_height = height
    if rotation in (90, 270):
        display_width, display_height = display_height, display_width
    long_side = max(display_width, display_height)
    short_side = min(display_width, display_height)
    if long_side >= 3840 and short_side >= 2160:
        model, label = "vmaf_4k_v0.6.1", "4K/1.5H"
    else:
        model, label = "vmaf_v0.6.1", "1080p/3H"
    return duration, display_width, display_height, model, label


def _quality_plan(candidate: str, duration: float, mode: str, ffprobe: str) -> list[Window]:
    if mode == "full":
        return [Window("full", 0.0, duration)]
    windows = plan_uniform_windows(
        duration, SAMPLE_SECONDS, INTERVAL_SECONDS, MIN_SAMPLES, MAX_SAMPLES, COMPLEXITY_SAMPLES
    )
    short_full = bool(windows) and all(window.kind == "short-full" for window in windows)
    if not short_full and COMPLEXITY_SAMPLES > 0:
        candidates = packet_complexity_candidates(
            candidate, duration, SAMPLE_SECONDS, COMPLEXITY_SAMPLES, ffprobe
        )
        windows = add_complexity_windows(windows, candidates, MAX_SAMPLES)
    return snap_windows_to_frame_starts(windows, candidate, duration, ffprobe)


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def run_quality_comparison(reference: str, candidate: str, mode: str, target: float) -> int:
    tools = select_quality_tools()
    filters = _run([tools.ffmpeg, "-hide_banner", "-filters"], env=tools.env)
    if filters.returncode != 0 or re.search(r"(^|\s)libvmaf(\s|$)", filters.stdout) is None:
        raise RuntimeError("Selected FFmpeg does not provide libvmaf.")

    duration, display_width, display_height, model, model_label = _source_geometry(
        tools.ffprobe, reference, tools.env
    )
    windows = _quality_plan(candidate, duration, mode, tools.ffprobe)
    if not windows:
        raise RuntimeError("VMAF sample plan was empty.")

    threads = min(8, max(1, os.cpu_count() or 1))
    ratio = f"{display_width}/{display_height}"
    normalize = (
        "scale=w='max(2,trunc(iw*if(eq(sar,0),1,sar)/2)*2)':"
        "h='max(2,trunc(ih/2)*2)':flags=bicubic:in_range=auto:out_range=tv,setsar=1"
    )
    fit = (
        f"scale=w='if(gt(a,{ratio}),{display_width},-2)':"
        f"h='if(gt(a,{ratio}),-2,{display_height})':flags=bicubic:in_range=tv:out_range=tv,"
        f"pad={display_width}:{display_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p"
    )
    percentile_floor = target - PERCENTILE_DELTA
    sustained_floor = target - SUSTAINED_DELTA
    print("\nVIDEO QUALITY — Hardcore Archive policy")
    print("═" * 72)
    print(f"Mode:       {mode}")
    print(f"Canvas:     {display_width}x{display_height} (source display resolution)")
    print(f"Model:      {model} ({model_label})")
    print(
        f"Target:     VMAF {target:g} | p{LOW_PERCENTILE:g} floor {percentile_floor:.3f} | "
        f"sustained floor {sustained_floor:.3f} for {SUSTAINED_SECONDS:g}s"
    )
    print(f"Samples:    {len(windows)}")

    with tempfile.TemporaryDirectory(prefix="av1compare-") as temp_raw:
        temp = Path(temp_raw)
        manifest = temp / "manifest.tsv"
        rows: list[str] = []
        for index, window in enumerate(windows, 1):
            log = temp / f"vmaf-{index}.json"
            log_filter = _escape_filter_value(str(log))
            graph = (
                f"[0:v:0]settb=AVTB,setpts=PTS-STARTPTS,{normalize},{fit}[ref];"
                f"[1:v:0]settb=AVTB,setpts=PTS-STARTPTS,{normalize},{fit}[dist];"
                f"[dist][ref]libvmaf=model='version={model}':log_fmt=json:log_path={log_filter}:"
                f"n_threads={threads}:n_subsample=1:ts_sync_mode=nearest"
            )
            print(
                f"Sample {index:2d}/{len(windows):<2d} {window.kind:<10} "
                f"at {window.start:8.3f}s for {window.length:.3f}s ... ",
                end="", flush=True,
            )
            process = _run([
                tools.ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
                "-ss", f"{window.start:.6f}", "-t", f"{window.length:.6f}", "-i", reference,
                "-ss", f"{window.start:.6f}", "-t", f"{window.length:.6f}", "-i", candidate,
                "-filter_complex", graph, "-an", "-f", "null", "-",
            ], env=tools.env)
            if process.returncode != 0 or not log.is_file() or log.stat().st_size == 0:
                print("FAILED")
                diagnostic = process.stderr.strip()
                if diagnostic:
                    print(diagnostic, file=sys.stderr)
                raise RuntimeError("VMAF measurement failed; quality result is not trustworthy.")
            print("measured")
            rows.append(f"{window.kind}\t{window.start:.6f}\t{window.length:.6f}\t{log}\n")
        manifest.write_text("".join(rows), encoding="utf-8")

        try:
            result = evaluate_manifest(
                str(manifest), duration, target, LOW_PERCENTILE, PERCENTILE_DELTA,
                SUSTAINED_DELTA, SUSTAINED_SECONDS,
                reference_path=reference, candidate_path=candidate, ffprobe=tools.ffprobe,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            print(f"Quality evaluation error: {exc}", file=sys.stderr)
            return 2

    print("\nQUALITY RESULT")
    print("═" * 72)
    print(f"Status:            {str(result.get('status', 'error')).upper()}")
    print(f"Policy:            {result.get('policy', '?')}")
    print(f"Mean VMAF:         {float(result.get('mean_vmaf', 0.0)):.3f}")
    print(f"Worst window mean: {float(result.get('minimum_window_mean', 0.0)):.3f}")
    print(
        f"p{result.get('low_percentile', '?')} VMAF:          "
        f"{float(result.get('low_percentile_vmaf', 0.0)):.3f}  "
        f"(floor {float(result.get('low_percentile_floor', 0.0)):.3f})"
    )
    print(
        f"Sustained low:     {float(result.get('longest_sustained_seconds', 0.0)):.3f}s  "
        f"(reject at >= {SUSTAINED_SECONDS:.3f}s below {float(result.get('sustained_floor', 0.0)):.3f})"
    )
    print(
        f"Coverage:          {float(result.get('coverage_seconds', 0.0)):.3f}s / "
        f"{float(result.get('requested_coverage_seconds', 0.0)):.3f}s requested "
        f"({float(result.get('coverage_percent', 0.0)):.2f}% of timeline confirmed)"
    )
    print(
        f"VMAF frames:       {result.get('frames', '?')} "
        f"({result.get('timed_frames', '?')} timing-mapped)"
    )
    reasons = result.get("reasons") or []
    if reasons:
        print("Reasons:")
        for reason in reasons:
            print(f"  - {reason}")
    if mode == "sampled":
        print("Scope:             sampled; unsampled timeline regions are not claimed as measured.")
    else:
        print("Scope:             full timeline evidence required.")
    if result.get("status") == "pass":
        return 0
    if result.get("status") == "reject":
        return 3
    return 2


def user_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="AV1Compare.py",
        description=(
            "Compare two media files including size, container/stream metadata, all tracks, "
            "and Hardcore Archive-style VMAF quality validation."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sampled", action="store_const", const="sampled", dest="mode", help="VMAF sampled mode (default)")
    mode.add_argument("--full", action="store_const", const="full", dest="mode", help="VMAF full-timeline mode (slow)")
    parser.set_defaults(mode="sampled")
    parser.add_argument("--no-quality", action="store_true", help="Metadata/size/track comparison only")
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET, metavar="VMAF", help="Quality target (default: 92)")
    parser.add_argument("--version", action="version", version=f"AV1Compare.py {VERSION}")
    parser.add_argument("original", help="Original/reference media file")
    parser.add_argument("candidate", help="Encoded/candidate media file")
    return parser


def main() -> int:
    args = user_parser().parse_args()
    if not 0.0 <= args.target <= 100.0:
        print("--target must be between 0 and 100.", file=sys.stderr)
        return 2
    reference = os.path.abspath(args.original)
    candidate = os.path.abspath(args.candidate)
    if not os.path.isfile(reference):
        print(f"Original not found: {args.original}", file=sys.stderr)
        return 2
    if not os.path.isfile(candidate):
        print(f"Candidate not found: {args.candidate}", file=sys.stderr)
        return 2
    system_ffprobe = shutil.which("ffprobe")
    if not system_ffprobe:
        print("Missing dependency: ffprobe", file=sys.stderr)
        return 2
    try:
        print_media_comparison(reference, candidate, system_ffprobe)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"Media comparison failed: {exc}", file=sys.stderr)
        return 2
    if args.no_quality:
        return 0
    try:
        return run_quality_comparison(reference, candidate, args.mode, args.target)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"Metadata comparison completed, but VMAF quality comparison failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
