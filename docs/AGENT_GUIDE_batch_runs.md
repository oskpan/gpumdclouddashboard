# Guide for agents: organising GPUMD batch runs so gpumdq tracks them

These rules apply to every PC that runs GPUMD (each runs `gpumdq` inside WSL and may report to the gpumdq hub).
gpumdq does **not** read your scripts in advance. It sees only (a) jobs submitted to its queue and
(b) `gpumd` processes it finds running, which it links back to the script that started them.
If you follow the rules below, progress, ETA, stage status and the hub page work without any extra setup.
Paths, folder names and GPUMD install locations may differ between PCs; the conventions may not.

---

## 1. Choose how the batch runs

| Situation | Use |
|---|---|
| Stages are independent (each folder already contains complete inputs) | **Queue mode**: `python3 gpumdq.py submit <dir> <dir> ...` (run from the gpumdq folder). Exact progress, automatic GPU scheduling, finish/fail alerts on the hub. |
| A stage needs the output of the previous one (e.g. relax, then MCMD, then tension from the final frame), or folders are generated on the fly | **Script mode**: one driver script per study, following section 2. |

Do not run a driver script and queued jobs on the same GPU at the same time. gpumdq keeps a GPU reserved
while an external gpumd is running there, and for 60 s after it ends, so queued jobs wait until the
script is finished.

---

## 2. Script-mode rules (all mandatory)

1. **One driver script, placed in the study's top folder.** The folder that contains the script becomes the
   batch folder, and stages are found by scanning it for subfolders that contain `run.in`.
   Do not keep drivers in a shared `~/scripts/` or `~/bin/` folder.
2. **The script's file name ends in `.sh` / `.bash` (shell) or `.py` (Python)** and is started as a file:
   `bash run_all.sh`, `./run_all.sh`, `nohup bash run_all.sh > run_all.out 2>&1 &`, or inside tmux.
   Never paste a loop into an interactive terminal: the runs would show up only as separate "ext" runs,
   with no batch and no ETA.
3. **If one script calls others, the outermost script defines the batch.** Put the master script in the folder
   that contains all its stages.
4. **Run gpumd with its working directory set to the stage folder**, and write its screen output to
   `gpumd.log` in that folder:
   `(cd "$d" && gpumd > gpumd.log 2>&1)`
   gpumdq decides done or partial from that file.
5. **The executable must be called `gpumd`** (or have the same name as the binary in `GPUMD_CMD` on that PC).
   Use the full path if needed, but do not rename the binary or hide it behind a differently named wrapper.
6. **Keep the script in the foreground while each gpumd runs** (no `gpumd &` followed by the script exiting).
   If the script ends, the batch is marked ended.
7. **Use one fresh folder per run.** GPUMD appends to `thermo.out`; rerunning in a used folder makes progress read
   too high. To redo a stage, use a new folder (e.g. `seed1_rerun1`). Do not delete or overwrite old results.
8. **`run.in`: put a `dump_thermo N` line before every `run` line.** Progress is computed as
   rows in `thermo.out` / Σ(run steps ÷ dump_thermo interval). A `run` block without `dump_thermo` makes progress show `?`.
9. **Set the GPU explicitly in the script:** `export CUDA_VISIBLE_DEVICES=0`. Under WSL nvidia-smi
   cannot report which process uses which GPU, so this variable is how gpumdq knows.
10. **Start everything inside the same WSL distro where gpumdq runs.** gpumd started from Windows or from another
    distro is invisible to it.
11. **Folder names:** stage folders must not start with `.`, and must not contain `archive`, `__pycache__`
    or `node_modules` anywhere in the name (such folders are skipped). At most ~3000 stage folders per batch.
12. **Make the script safe to restart:** skip stages whose `gpumd.log` already contains `Finished running GPUMD`,
    or that contain a `DONE` / `.done` / `COMPLETED.json` file.

### Stage status as gpumdq reports it

| Status | Condition |
|---|---|
| running | a gpumd process has this folder as its working directory |
| done | `gpumd.log` tail contains `Finished running GPUMD`, or `DONE` / `.done` / `COMPLETED.json` exists |
| partial | `gpumd.log` exists without the finish line (crashed, killed or interrupted) |
| ready | `run.in` exists, not started |
| planned | listed in a plan file only; folder not created yet |

---

## 3. Driver template

```bash
#!/usr/bin/env bash
# run_all.sh - lives in the study folder; every stage is a subfolder containing run.in
set -u
cd "$(dirname "$(readlink -f "$0")")"
export CUDA_VISIBLE_DEVICES=0
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
```

If a stage prepares the next stage's input (copying the last frame to `model.xyz`, writing `run.in` and so on),
do it in the script between gpumd calls, still inside the study folder.

---

## 4. Plan file (when stage folders are created during the batch)

If later stage folders don't exist when the batch starts, register them so they count toward progress and ETA.
Create `<gpumdq folder>/plans/<study_name>.plan` (or `gpumdq_plan.txt` in the study folder, which needs no `root:` line):

```
root: /mnt/e/my_project/GB_xxx_20261113            # absolute WSL path of the folder holding the driver script
# relative/stage/dir     total_steps (optional, used until run.in exists)
S3_111/seed1/relax       50000
S3_111/seed1/mcmd        200000
S3_111/seed1/tension     300000
```

One stage per line, in run order; `#` starts a comment. The `root:` path must match the driver script's folder exactly.

---

## 5. Check after starting

1. From the gpumdq folder, run `python3 gpumdq.py list`. Within a few seconds a line `BATCH <folder name>` must appear
   with the expected stage count and a non-zero step total.
2. On the dashboard (local http://localhost:8765, or the hub), the batch card must show `now: <current stage>`
   and a progress bar that moves.
3. If the run appears only as **ext** with no batch: rules 1–3 or 6 were broken.
   If progress shows `?`: rule 8. If it jumps to ~100% at once: rule 7.
4. Note in the study's README which PC runs it, the driver script name, and the GPUMD binary
   (the hub shows each PC's gpumd SHA256; results that will be compared across PCs should come from the same build).

---

## 6. Do not

- Start gpumd from a pasted loop, a Jupyter cell, or Windows-side tools.
- Reuse a finished stage folder for a new run, or delete earlier outputs to make room.
- Put the hub token on a command line, in a script or in a README. It belongs only in `hub.env` (chmod 600).
- Kill or restart gpumdq to "fix" a batch. Running gpumd processes are unaffected, and gpumdq re-attaches on restart.
