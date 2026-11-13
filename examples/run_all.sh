#!/usr/bin/env bash
# Minimal gpumdq-friendly batch driver. Put it in the study's top folder; every stage is a subfolder with run.in.
# Start with:  nohup bash run_all.sh > run_all.out 2>&1 &     (inside the WSL distro where gpumdq runs)
set -u
cd "$(dirname "$(readlink -f "$0")")"
export CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-0}
GPUMD=${GPUMD_BIN:-gpumd}            # full path allowed; the binary itself must be named gpumd

STAGES=(
  S3_111/seed1
  S3_111/seed2
  S9_114/seed1
)

for d in "${STAGES[@]}"; do
  if [[ -f "$d/gpumd.log" ]] && grep -q "Finished running GPUMD" "$d/gpumd.log"; then
    echo "skip $d (done)"; continue
  fi
  [[ -f "$d/run.in" ]] || { echo "missing $d/run.in"; exit 1; }
  echo "$(date '+%F %T') start $d"
  (cd "$d" && "$GPUMD" > gpumd.log 2>&1) || echo "$(date '+%F %T') FAILED $d"
done
echo "$(date '+%F %T') batch finished"
