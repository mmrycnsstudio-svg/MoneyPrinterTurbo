#!/usr/bin/env bash
# No paid services, API keys, speech services or stock downloads are used here.
set -Eeuo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
[[ -x .venv/bin/python ]] || { echo 'Run bash scripts/setup-video-tools.sh first.' >&2; exit 1; }

.venv/bin/python - <<'PY'
import importlib
for module in ('moviepy', 'streamlit', 'fastapi', 'uvicorn', 'app.services.video', 'app.services.voice'):
    importlib.import_module(module)
print('MoneyPrinterTurbo Python imports: OK')
PY
.venv/bin/python cli.py --help >/dev/null
blender --version | sed -n '1p'
ffmpeg -version | sed -n '1p'

mkdir -p storage/video-tools
check_dir="$(mktemp -d "$project_dir/storage/video-tools/check.XXXXXX")"
trap 'rm -rf -- "$check_dir"' EXIT
if ! blender --background --factory-startup --threads 2 --python-exit-code 1 \
    --python scripts/blender-smoke.py -- --output-dir "$check_dir" > "$check_dir/blender.log" 2>&1; then
    tail -n 40 "$check_dir/blender.log" >&2
    exit 1
fi
ffmpeg -hide_banner -loglevel error -y -framerate 8 \
    -i "$check_dir/frame-%04d.png" -an -c:v libx264 -pix_fmt yuv420p \
    -movflags +faststart "$check_dir/blender-smoke.mp4"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,nb_frames \
    -of json "$check_dir/blender-smoke.mp4" > "$check_dir/probe.json"
.venv/bin/python - "$check_dir/probe.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding='utf-8') as stream:
    video = json.load(stream)['streams'][0]
assert (video['width'], video['height'], int(video['nb_frames'])) == (180, 320, 8), video
print('Blender CPU render + FFmpeg MP4: OK (180x320, 8 frames)')
PY
mv -- "$check_dir/blender-smoke.mp4" storage/video-tools/blender-smoke.mp4
mv -- "$check_dir/blender-smoke.blend" storage/video-tools/blender-smoke.blend
