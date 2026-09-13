#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from av1plan_contract import PlanError, planning_quality_target


def software_filter(recipe: dict[str, Any]) -> str:
    filters: list[str] = []
    if recipe["denoise"]["mode"] == "hqdn3d": filters.append(recipe["denoise"]["filter"])
    resolution = recipe["resolution"]
    if resolution["mode"] != "source": filters.append(f"scale={resolution['width']}:{resolution['height']}:flags=lanczos")
    return ",".join(filters)


def video_encode_args(encoder: str, recipe: dict[str, Any], sample: bool = False) -> tuple[list[str], list[str]]:
    quality, global_args, output_args = recipe["quality"], [], []
    filt, stream = software_filter(recipe), ("" if sample else ":0")
    if encoder == "libsvtav1":
        if filt: output_args += [f"-vf:v{stream}", filt]
        output_args += [f"-c:v{stream}", "libsvtav1", f"-crf:v{stream}", str(quality["value"]), f"-preset:v{stream}", str(quality["preset"]), f"-pix_fmt:v{stream}", "yuv420p10le"]
    elif encoder == "av1_nvenc":
        if filt: output_args += [f"-vf:v{stream}", filt]
        output_args += [f"-c:v{stream}", encoder, f"-rc:v{stream}", "vbr", f"-cq:v{stream}", str(quality["value"]), f"-preset:v{stream}", "slow", f"-pix_fmt:v{stream}", "yuv420p10le"]
    elif encoder == "av1_qsv":
        if filt: output_args += [f"-vf:v{stream}", filt]
        output_args += [f"-c:v{stream}", encoder, f"-global_quality:v{stream}", str(quality["value"]), f"-preset:v{stream}", "slow", f"-pix_fmt:v{stream}", "yuv420p10le"]
    elif encoder == "av1_vaapi":
        global_args += ["-init_hw_device", f"vaapi=va:{recipe['vaapi_device']}", "-filter_hw_device", "va"]
        filters = ([filt] if filt else []) + [f"format={recipe['vaapi_upload_format']}", "hwupload"]
        output_args += [f"-vf:v{stream}", ",".join(filters), f"-c:v{stream}", "av1_vaapi", f"-rc_mode:v{stream}", "CQP", f"-global_quality:v{stream}", str(quality["value"]), f"-enc_time_base:v{stream}", "demux"]
    else: raise PlanError(f"No AV1 recipe exists for encoder: {encoder}")
    return global_args, output_args


def encode_sample(source: Path, destination: Path, encoder: str, recipe: dict[str, Any], start: float, length: float) -> float:
    global_args, video_args = video_encode_args(encoder, recipe, sample=True)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *global_args, "-ss", f"{start:.6f}", "-t", f"{length:.6f}", "-i", str(source), "-map", "0:V:0", "-an", "-sn", "-dn", *video_args, "-f", "matroska", str(destination)]
    before = time.monotonic(); result = subprocess.run(args, text=True, capture_output=True, check=False); elapsed = time.monotonic() - before
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 0: raise PlanError(result.stderr.strip() or "Sample encoding failed.")
    return max(elapsed, .001)


def extract_reference(source: Path, destination: Path, start: float, length: float, recipe: dict[str, Any]) -> None:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.6f}", "-t", f"{length:.6f}", "-i", str(source), "-map", "0:V:0", "-an", "-sn", "-dn"]
    resolution = recipe["resolution"]
    if resolution["mode"] != "source": command += ["-vf", f"scale={resolution['width']}:{resolution['height']}:flags=lanczos"]
    command += ["-c:v", "ffv1", str(destination)]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0 or not destination.is_file(): raise PlanError(result.stderr.strip() or "Could not extract an evaluation reference sample.")


def quality_tools(root: Path, metric: str) -> tuple[str, dict[str, str], str]:
    if metric == "ssim_percent": return "ffmpeg", os.environ.copy(), metric
    module_path = root / "tools" / "AV1Compare.py"
    spec = importlib.util.spec_from_file_location("av1compare_runtime", module_path)
    if spec is None or spec.loader is None: raise PlanError("Could not load the VMAF runtime selector.")
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    selected = module.select_quality_tools(); return selected.ffmpeg, selected.env, metric


def measure_quality(root: Path, reference: Path, candidate: Path, quality: dict[str, Any], sample_duration: float) -> dict[str, Any]:
    metric = quality["metric"]; ffmpeg, env, selected_metric = quality_tools(root, metric)
    if selected_metric == "vmaf":
        with tempfile.TemporaryDirectory(prefix="av1plan-vmaf-") as raw:
            log, decoded = Path(raw)/"vmaf.json", Path(raw)/"candidate.mkv"
            dec = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(candidate), "-map", "0:V:0", "-an", "-sn", "-dn", "-c:v", "ffv1", str(decoded)], text=True, capture_output=True, check=False)
            if dec.returncode != 0: raise PlanError(dec.stderr.strip() or "Could not normalize AV1 sample for VMAF.")
            graph = f"[0:v]setpts=PTS-STARTPTS[dist];[1:v]setpts=PTS-STARTPTS[ref];[dist][ref]libvmaf=log_fmt=json:log_path={log}"
            result = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(decoded), "-i", str(reference), "-lavfi", graph, "-f", "null", "-"], env=env, text=True, capture_output=True, check=False)
            if result.returncode != 0: raise PlanError(result.stderr.strip() or "VMAF measurement failed.")
            data = json.loads(log.read_text()); scores = [float(f["metrics"]["vmaf"]) for f in data.get("frames", []) if isinstance(f.get("metrics",{}).get("vmaf"),(int,float))]
            if not scores: raise PlanError("VMAF produced no frame scores.")
            ordered = sorted(scores); p10 = ordered[max(0, math.ceil(len(ordered)*.1)-1)]; longest=current=0.0
            for score in scores:
                if score < quality["sustained_floor"]: current += sample_duration/len(scores); longest=max(longest,current)
                else: current=0.0
            mean=float(data["pooled_metrics"]["vmaf"]["mean"]); met=mean>=quality["target"] and p10>=quality["p10_minimum"] and longest<quality["maximum_sustained_seconds"]
            return {"metric":metric,"predicted_score":round(mean,3),"predicted_p10":round(p10,3),"predicted_longest_below_floor_seconds":round(longest,3),"target":quality["target"],"target_met_on_sample":met,"assessment":"mean_p10_and_sustained_sample_policy"}
    result=subprocess.run([ffmpeg,"-hide_banner","-v","info","-i",str(candidate),"-i",str(reference),"-lavfi","[0:v]setpts=PTS-STARTPTS[dist];[1:v]setpts=PTS-STARTPTS[ref];[dist][ref]ssim","-f","null","-"],env=env,text=True,capture_output=True,check=False)
    match=re.search(r"All:([0-9.]+)",result.stderr)
    if result.returncode!=0 or not match: raise PlanError(result.stderr.strip() or "SSIM measurement failed.")
    score=float(match.group(1))*100; return {"metric":metric,"predicted_score":round(score,3),"target":quality["target"],"target_met_on_sample":score>=quality["target"],"assessment":"mean_only; p10 and sustained thresholds require vmaf"}


def predict(root: Path, requirements: dict[str, Any], encoder: str, recipe: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    duration=float(source["duration_seconds"]); length=min(float(requirements["evaluation"]["sample_seconds"]),duration); max_start=max(0.0,duration-length)
    starts=[]
    for fraction in (.10,.50,.90):
        value=round(min(max(0.0,duration*fraction-length/2),max_start),6)
        if not starts or abs(value-starts[-1])>.001: starts.append(value)
    mode=requirements["quality"].get("mode","required"); target,margin=planning_quality_target(requirements["quality"]); tested={}
    with tempfile.TemporaryDirectory(prefix="av1plan-sample-") as raw:
        temp=Path(raw); refs=[]
        if mode=="required":
            for i,start in enumerate(starts):
                ref=temp/f"ref-{i}.mkv"; extract_reference(Path(requirements["input"]),ref,start,length,recipe); refs.append(ref)
        def evaluate(value:int)->dict[str,Any]:
            if value in tested:return tested[value]
            r=copy.deepcopy(recipe);r["quality"]["value"]=value;samples=[]
            for i,start in enumerate(starts):
                candidate=temp/f"candidate-{value}-{i}.mkv";elapsed=encode_sample(Path(requirements["input"]),candidate,encoder,r,start,length)
                q={"metric":"disabled","predicted_score":None,"target":0.0,"target_met_on_sample":True,"assessment":"quality_check_disabled_by_caller"} if mode=="off" else measure_quality(root,refs[i],candidate,requirements["quality"],length)
                samples.append({"start_seconds":start,"sample_bytes":candidate.stat().st_size,"encode_seconds":elapsed,"quality":q})
            if mode=="off": aggregate=dict(samples[0]["quality"])
            else:
                aggregate=dict(min(samples,key=lambda x:float(x["quality"]["predicted_score"]))["quality"]);aggregate["predicted_score"]=round(min(float(x["quality"]["predicted_score"]) for x in samples),3)
                if all("predicted_p10" in x["quality"] for x in samples): aggregate["predicted_p10"]=round(min(float(x["quality"]["predicted_p10"]) for x in samples),3)
                if all("predicted_longest_below_floor_seconds" in x["quality"] for x in samples): aggregate["predicted_longest_below_floor_seconds"]=round(max(float(x["quality"]["predicted_longest_below_floor_seconds"]) for x in samples),3)
                aggregate.update({"planning_target":round(target,3),"planning_margin":round(margin,3),"target_met_on_sample":all(bool(x["quality"]["target_met_on_sample"]) for x in samples) and float(aggregate["predicted_score"])>=target,"assessment":"representative_windows_all_must_pass_with_planning_margin"})
            tested[value]={"quality_value":value,"sample_bytes":sum(int(x["sample_bytes"]) for x in samples),"encode_seconds":sum(float(x["encode_seconds"]) for x in samples),"quality":aggregate};return tested[value]
        initial=int(recipe["quality"]["value"])
        if mode=="off": selected=evaluate(initial)
        else:
            minimum,maximum,step=((0,255,32) if encoder=="av1_vaapi" else ((0,63,8) if encoder=="libsvtav1" else (0,51,8)))
            first=evaluate(initial); passing=initial if first["quality"]["target_met_on_sample"] else None; failing=None if passing is not None else initial
            if passing is not None:
                probe=initial
                while probe<maximum:
                    val=min(maximum,probe+step); item=evaluate(val)
                    if item["quality"]["target_met_on_sample"]: passing=probe=val
                    else: failing=val; break
                    if probe==maximum:break
            else:
                probe=initial
                while probe>minimum:
                    val=max(minimum,probe-step);item=evaluate(val)
                    if item["quality"]["target_met_on_sample"]:passing=val;break
                    failing=probe=val
            if passing is not None and failing is not None:
                while failing-passing>1:
                    val=(passing+failing)//2;item=evaluate(val)
                    if item["quality"]["target_met_on_sample"]:passing=val
                    else:failing=val
            if requirements["optimization"]["primary"]=="highest_quality":evaluate(minimum)
            candidates=[x for x in tested.values() if x["quality"]["target_met_on_sample"]] or list(tested.values())
            def objective(item,name):
                if name=="smallest_output":return float(item["sample_bytes"])
                if name=="fastest_encoding":return float(item["encode_seconds"])
                return -float(item["quality"]["predicted_score"])
            selected=min(candidates,key=lambda x:(objective(x,requirements["optimization"]["primary"]),objective(x,requirements["optimization"]["secondary"]),int(x["quality_value"])))
        value=int(selected["quality_value"]);recipe["quality"]["value"]=value;sampled=length*len(starts);video_bytes=round(int(selected["sample_bytes"])/sampled*duration)
        audio_source=sum(int(x["bitrate"] or 0) for x in source["audio_tracks"]);other=max(0,int(source["copied_stream_bitrate"])-audio_source);extra=round((int(recipe["audio"]["estimated_bitrate"])+other)*duration/8);elapsed=float(selected["encode_seconds"])
    return {"scope":"bounded_representative_samples","sample":{"start_seconds":starts[len(starts)//2],"duration_seconds":length,"count":len(starts),"starts_seconds":starts},"quality":selected["quality"],"calibration":{"strategy":"bounded_representative_quality_search" if mode=="required" else "bounded_representative_default_setting_probe","selected_quality":value,"quality_kind":recipe["quality"]["kind"],"sample_count":len(starts),"sample_starts_seconds":starts,"planning_vmaf_margin":round(margin,3) if mode=="required" else 0.0,"planning_score_target":round(target,3) if mode=="required" else 0.0,"candidates":[{"quality_value":int(x["quality_value"]),"sample_bytes":int(x["sample_bytes"]),"encode_seconds":round(float(x["encode_seconds"]),3),"predicted_score":x["quality"]["predicted_score"],"target_met_on_sample":bool(x["quality"]["target_met_on_sample"])} for x in sorted(tested.values(),key=lambda x:int(x["quality_value"]))]},"size":{"predicted_output_bytes":video_bytes+extra,"predicted_video_bytes":video_bytes,"copied_stream_bytes_estimate":extra,"basis":"representative_samples_plus_planned_audio_and_reported_copied_stream_bitrates"},"speed":{"measured_realtime_factor":round(sampled/elapsed,3),"predicted_encode_seconds":round(duration/(sampled/elapsed),3),"basis":"representative_sample_wall_clock"},"confidence":"representative_samples_not_guaranteed"}
