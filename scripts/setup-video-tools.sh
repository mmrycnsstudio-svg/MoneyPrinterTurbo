#!/usr/bin/env bash
# Install the fork's locked dependencies and Blender on Ubuntu/Debian.
set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
umask 077

fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || fail 'This installer supports Linux. Use Ubuntu/Debian or a Claude Code cloud session.'
command -v python3 >/dev/null || fail 'Python 3.11 or later is required.'
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or later is required"'

packages=()
command -v blender >/dev/null || packages+=(blender)
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || packages+=(ffmpeg)
if ! command -v uv >/dev/null && [[ ! -x .tools/uv/bin/uv ]]; then
    python3 -c 'import ensurepip' 2>/dev/null || packages+=(python3-venv)
fi
if ((${#packages[@]})); then
    command -v apt-get >/dev/null || fail "Install these system packages first: ${packages[*]}"
    apt_command=(apt-get)
    if ((EUID != 0)); then
        command -v sudo >/dev/null && sudo -n true 2>/dev/null || fail "Administrator access is needed to install: ${packages[*]}. Run this installer in a root-enabled Ubuntu/Debian environment."
        apt_command=(sudo -n apt-get)
    fi
    "${apt_command[@]}" update
    "${apt_command[@]}" install -y --no-install-recommends "${packages[@]}"
fi

if command -v uv >/dev/null; then
    uv_command="$(command -v uv)"
else
    if [[ ! -x .tools/uv/bin/uv ]]; then
        python3 -m venv .tools/uv
        .tools/uv/bin/python -m pip install --disable-pip-version-check 'uv==0.13.9'
    fi
    uv_command="$project_dir/.tools/uv/bin/uv"
fi

# Install into the project venv, without modifying the lockfile or system Python.
"$uv_command" sync --frozen --no-dev --python "$(command -v python3)"
if [[ ! -e config.toml ]]; then
    cp -- config.example.toml config.toml
fi
mkdir -p storage/video-tools
bash scripts/check-video-tools.sh
printf '\nInstallation verified. Start MoneyPrinterTurbo with: bash webui.sh\n'
