#!/bin/bash
# Rendus Unreal d'un secteur (Movie Render Queue) puis copie dans build/hq/renders/unreal_<plan>.png
# Usage : ue_render.sh <dossier_secteur> [2560x1440]
set -euo pipefail
SD="$1"; RES="${2:-2560x1440}"
SLUG=$(basename "$SD")
NAME="Atlas$(echo "$SLUG" | awk -F- '{for(i=1;i<=NF;i++) printf toupper(substr($i,1,1)) substr($i,2)}' | cut -c1-40)"
PROJ="$(cd "$(dirname "$0")/.." && pwd)/unreal/$NAME"
UE=$(ls -d "/Users/Shared/Epic Games"/UE_5.* 2>/dev/null | sort | tail -1)
[ -f "$PROJ/$NAME.uproject" ] || { echo "Error: projet Unreal absent ($PROJ) : lance « Paquet Unreal »"; exit 1; }
[ -n "$UE" ] || { echo "Error: Unreal Engine non installé"; exit 1; }
echo "@@PROGRESS 0.05 Unreal ($RES)"
# l'éditeur reste masqué pendant les rendus
( for i in $(seq 1 1200); do osascript -e 'tell application "System Events" to if exists process "UnrealEditor" then set visible of process "UnrealEditor" to false' >/dev/null 2>&1; sleep 2; done ) & HID=$!
trap 'kill $HID 2>/dev/null || true' EXIT
ATLAS_AUTORUN=atlas_render.py ATLAS_QUIT=1 ATLAS_RES="$RES" ATLAS_SPATIAL=8 ATLAS_WARMUP=96 ATLAS_EV=0.1 \
  "$UE/Engine/Binaries/Mac/UnrealEditor" "$PROJ/$NAME.uproject" -unattended -nosplash >/dev/null 2>&1 || true
grep -E "\[Atlas\]|LogPython: Error" "$HOME/Library/Logs/Unreal Engine/${NAME}Editor/$NAME.log" | sed 's/^.*LogPython: //' || true
mkdir -p "$SD/build/hq/renders"
n=0
for f in "$PROJ/Saved/Atlas/renders/"*.png; do
  [ -f "$f" ] || continue
  cp "$f" "$SD/build/hq/renders/unreal_$(basename "$f")"; n=$((n+1))
done
echo "@@PROGRESS 1 terminé"
echo "ATLAS_UE_RENDER_OK $n rendus"
[ "$n" -gt 0 ]
