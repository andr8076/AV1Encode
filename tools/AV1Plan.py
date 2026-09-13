#!/usr/bin/env python3
"""Protocol-v2 semantic planning for AV1Encode."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from av1plan_contract import (
    HARDWARE_ENCODERS, PLAN_SCHEMA, PROTOCOL_VERSION, REQUIREMENTS_SCHEMA,
    RESULT_SCHEMA, SUPPORTED_PROTOCOLS, PlanError, atomic_json, canonical,
    capabilities, choose_encoder, digest, implementation_fingerprint,
    normalize_requirements, planning_quality_target, probe_source, read_json,
    recipe_for, runtime_fingerprint, source_fingerprint,
)
from av1plan_execute import execute_direct
from av1plan_quality import predict


def plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {key:value for key,value in plan.items() if key not in {"plan_id","created_at"}}


def calculate_plan_id(plan: dict[str, Any]) -> str:
    return "av1p_"+hashlib.sha256(canonical(plan_payload(plan))).hexdigest()


def evaluate(requirements_path: Path, plan_path: Path, script: Path) -> dict[str, Any]:
    if not plan_path.expanduser().resolve().parent.is_dir():raise PlanError(f"Plan directory does not exist: {plan_path.parent}")
    root=script.parent.resolve();requirements=normalize_requirements(read_json(requirements_path));source=probe_source(Path(requirements["input"]));report=capabilities(script);encoder,encoder_class,capability=choose_encoder(requirements,report);recipe=recipe_for(encoder,requirements,source,capability)
    fingerprints={"implementation":implementation_fingerprint(root),"runtime":runtime_fingerprint(encoder),"source":source_fingerprint(Path(requirements["input"])),"requirements":{"algorithm":"sha256","value":digest(requirements)}}
    prediction=predict(root,requirements,encoder,recipe,source);ready=bool(prediction["quality"]["target_met_on_sample"])
    plan={"schema":PLAN_SCHEMA,"protocol_version":PROTOCOL_VERSION,"created_at":datetime.now(timezone.utc).isoformat(),"requirements":requirements,"fingerprints":fingerprints,"selection":{"encoder":encoder,"class":encoder_class,"policy_owner":"AV1Encode"},"recipe":recipe,"prediction":prediction,"execution":{"state":"ready" if ready else "rejected","reason":None if ready else "sample_quality_below_target"}}
    plan["plan_id"]=calculate_plan_id(plan);atomic_json(plan_path,plan);return plan


def verify_plan(plan: dict[str, Any], script: Path) -> None:
    if plan.get("schema")!=PLAN_SCHEMA or plan.get("protocol_version")!=PROTOCOL_VERSION:raise PlanError("Unsupported plan schema or protocol version.")
    if plan.get("plan_id")!=calculate_plan_id(plan):raise PlanError("Plan integrity check failed; the plan was changed after evaluation.")
    requirements=normalize_requirements(plan.get("requirements",{}));encoder=str((plan.get("selection") or {}).get("encoder",""));encoder_class=str((plan.get("selection") or {}).get("class",""));requested=requirements.get("requested_encoder")
    if requested and encoder!=requested:raise PlanError("Plan selection violates the requested_encoder requirement.")
    if requirements["hardware_policy"]=="manual_software":
        if (encoder,encoder_class)!=("libsvtav1","software"):raise PlanError("Plan selection violates manual_software policy.")
    elif encoder not in HARDWARE_ENCODERS or encoder_class!="hardware":raise PlanError("Plan selection violates auto_hardware_only policy.")
    source=probe_source(Path(requirements["input"]));recipe=plan.get("recipe");capability={}
    if encoder=="av1_vaapi" and isinstance(recipe,dict):capability["detail"]=str(recipe.get("vaapi_device",""))+( ", 10-bit sealed plan" if recipe.get("vaapi_upload_format")=="p010le" else ", 8-bit sealed plan")
    expected=recipe_for(encoder,requirements,source,capability);quality=recipe.get("quality") if isinstance(recipe,dict) else None;expected_quality=expected["quality"];maximum=255 if encoder=="av1_vaapi" else (63 if encoder=="libsvtav1" else 51)
    if not isinstance(quality,dict) or quality.get("kind")!=expected_quality["kind"] or quality.get("preset")!=expected_quality["preset"] or isinstance(quality.get("value"),bool) or not isinstance(quality.get("value"),int) or not 0<=quality["value"]<=maximum:raise PlanError("Plan recipe has an invalid calibrated quality policy.")
    expected["quality"]["value"]=quality["value"]
    if recipe!=expected:raise PlanError("Plan recipe is not the current AV1Encode policy recipe.")
    if (plan.get("execution") or {}).get("state")!="ready":raise PlanError("Plan is not executable because its sampled quality target was not met.")
    root=script.parent.resolve();current={"implementation":implementation_fingerprint(root),"runtime":runtime_fingerprint(encoder),"source":source_fingerprint(Path(requirements["input"])),"requirements":{"algorithm":"sha256","value":digest(requirements)}};saved=plan.get("fingerprints")
    for name,value in current.items():
        if not isinstance(saved,dict) or saved.get(name,{}).get("value")!=value["value"]:raise PlanError(f"Plan is stale: {name} fingerprint changed; evaluate again.")


def execute(plan_path: Path, result_path: Path, script: Path) -> dict[str, Any]:
    if not result_path.expanduser().resolve().parent.is_dir():raise PlanError(f"Result directory does not exist: {result_path.parent}")
    plan=read_json(plan_path);verify_plan(plan,script);requirements,recipe=plan["requirements"],plan["recipe"];executor=execute_direct(requirements,recipe,probe_source(Path(requirements["input"])))
    result={"schema":RESULT_SCHEMA,"protocol_version":PROTOCOL_VERSION,"plan_id":plan["plan_id"],"status":"ok","exit_code":0,"input":requirements["input"],"output":requirements["output"],"encoder":recipe["encoder"],"prediction":plan["prediction"],"executor_result":executor};atomic_json(result_path,result);return result


def negotiate(text: str) -> dict[str, Any]:
    try:offered={int(item.strip()) for item in text.split(",") if item.strip()}
    except ValueError as exc:raise PlanError("Versions must be comma-separated integers.") from exc
    common=sorted(offered.intersection(SUPPORTED_PROTOCOLS));return {"schema":"av1encode.negotiation","supported_protocol_versions":list(SUPPORTED_PROTOCOLS),"selected_protocol_version":common[-1] if common else None,"compatible":bool(common)}


def parser()->argparse.ArgumentParser:
    result=argparse.ArgumentParser(prog="AV1Plan.py");sub=result.add_subparsers(dest="operation",required=True);n=sub.add_parser("negotiate");n.add_argument("versions");e=sub.add_parser("evaluate",aliases=["plan"]);e.add_argument("requirements",type=Path);e.add_argument("plan_json",type=Path);e.add_argument("encoder_script",type=Path);x=sub.add_parser("execute");x.add_argument("plan_json",type=Path);x.add_argument("result_json",type=Path);x.add_argument("encoder_script",type=Path);return result


def main(argv:list[str]|None=None)->int:
    args=parser().parse_args(argv)
    try:
        if args.operation=="negotiate":print(json.dumps(negotiate(args.versions),sort_keys=True));return 0
        if args.operation in {"evaluate","plan"}:
            plan=evaluate(args.requirements,args.plan_json,args.encoder_script.resolve());print(json.dumps({"schema":"av1encode.plan-reference","protocol_version":2,"plan_id":plan["plan_id"],"plan":str(args.plan_json.resolve()),"prediction":plan["prediction"]},sort_keys=True));return 0
        result=execute(args.plan_json,args.result_json,args.encoder_script.resolve());print(json.dumps(result,sort_keys=True));return int(result["exit_code"])
    except PlanError as exc:
        error={"schema":"av1encode.error","protocol_version":2,"status":"failed","error":str(exc)}
        if getattr(args,"operation",None)=="execute":
            try:atomic_json(args.result_json,error)
            except OSError:pass
        print(json.dumps(error,sort_keys=True),file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
