#!/usr/bin/env bash
# Vidéos de test reproductibles (graine fixe) + vérité terrain : ./scripts/gen_videos.sh [nombre] [durée_s]
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
python3 -m vidp fetch-models --dir models --animals data/animals
python3 -m vidp synth --out data/videos --animals data/animals --count "${1:-8}" --duration "${2:-20}"
