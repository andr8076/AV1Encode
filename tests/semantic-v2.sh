#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/av1encode-semantic-v2.XXXXXX")
trap 'rm -rf -- "$TMP"' EXIT

ffmpeg -hide_banner -encoders 2>/dev/null | awk 'NF >= 2 && $2 == "libsvtav1" {found=1} END {exit(found ? 0 : 1)}' || {
    printf 'libsvtav1 unavailable; semantic software integration skipped.\n'
    exit 0
}

ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i 'testsrc2=size=320x180:rate=12:duration=2' \
    -f lavfi -i 'sine=frequency=440:duration=2' \
    -map 0:v -map 1:a -c:v ffv1 -c:a pcm_s16le "$TMP/source.mkv"

cat > "$TMP/requirements.json" <<JSON
{
  "schema": "av1encode.requirements",
  "protocol_version": 2,
  "input": "$TMP/source.mkv",
  "output": "$TMP/output.mkv",
  "hardware_policy": "manual_software",
  "requested_encoder": "libsvtav1",
  "quality": {
    "mode": "required",
    "metric": "ssim_percent",
    "target": 80,
    "p10_minimum": 76,
    "sustained_floor": 74,
    "maximum_sustained_seconds": 1
  },
  "optimization": {"primary": "smallest_output", "secondary": "fastest_encoding"},
  "video": {"maximum_height": 144, "denoise": "required"},
  "preservation": {"streams": "all", "chapters": true, "metadata": true},
  "audio": {"mode": "archive_optimize"},
  "evaluation": {"sample_seconds": 1}
}
JSON

"$ROOT/AV1Encode.sh" --machine-evaluate "$TMP/requirements.json" --plan-json "$TMP/plan.json" >/dev/null
python3 - "$TMP/plan.json" <<'PY'
import json, sys
plan=json.load(open(sys.argv[1], encoding='utf-8'))
assert plan['selection']['encoder'] == 'libsvtav1'
assert plan['selection']['class'] == 'software'
assert plan['recipe']['resolution']['height'] == 144
assert plan['recipe']['resolution']['mode'] == 'maximum_height'
assert plan['recipe']['denoise']['mode'] == 'hqdn3d'
assert plan['recipe']['audio']['mode'] == 'archive_optimize'
assert plan['execution']['state'] == 'ready'
assert plan['prediction']['quality']['target_met_on_sample'] is True
PY

"$ROOT/AV1Encode.sh" --execute-plan "$TMP/plan.json" --result-json "$TMP/result.json" >/dev/null
[[ -s "$TMP/output.mkv" ]]
[[ $(ffprobe -v error -select_streams V:0 -show_entries stream=codec_name -of csv=p=0 "$TMP/output.mkv") == av1 ]]
[[ $(ffprobe -v error -select_streams V:0 -show_entries stream=height -of csv=p=0 "$TMP/output.mkv") == 144 ]]
if ffmpeg -hide_banner -encoders 2>/dev/null | awk 'NF >= 2 && $2 == "libopus" {found=1} END {exit(found ? 0 : 1)}'; then
    [[ $(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$TMP/output.mkv") == opus ]]
fi

python3 - "$TMP/requirements.json" "$TMP/off.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding='utf-8'))
value['output']=sys.argv[2].replace('off.json','off-output.mkv')
value['quality']={'mode':'off','metric':'vmaf','target':0,'p10_minimum':0,'sustained_floor':0,'maximum_sustained_seconds':1}
value['video']={'maximum_height':None,'denoise':'never'}
value['audio']={'mode':'copy_all'}
json.dump(value, open(sys.argv[2], 'w', encoding='utf-8'))
PY
"$ROOT/AV1Encode.sh" --machine-evaluate "$TMP/off.json" --plan-json "$TMP/off-plan.json" >/dev/null
python3 - "$TMP/off-plan.json" <<'PY'
import json, sys
plan=json.load(open(sys.argv[1], encoding='utf-8'))
assert plan['prediction']['quality']['metric'] == 'disabled'
assert plan['execution']['state'] == 'ready'
PY

PYTHONPATH="$ROOT/tools" python3 - <<'PY'
from av1plan_contract import choose_encoder
req={'hardware_policy':'auto_hardware_only','requested_encoder':'av1_nvenc'}
report={'auto_encoder':'av1_qsv','encoders':[{'name':'av1_nvenc','usable':True,'class':'hardware'},{'name':'av1_qsv','usable':True,'class':'hardware'}]}
assert choose_encoder(req, report)[:2] == ('av1_nvenc','hardware')
PY

printf 'Extended protocol-v2 semantic ownership tests passed.\n'
