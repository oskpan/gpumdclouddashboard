#!/usr/bin/env bash
# Runs inside WSL. Starts the gpumdq server with your GPUMD 5.8.1 build.
export PATH="$PATH:/usr/lib/wsl/lib"            # nvidia-smi in WSL
cd "$(dirname "$(readlink -f "$0")")"
[[ -f hub.env ]] && { set -a; source ./hub.env; set +a; }   # may also set GPUMD_BIN / GPUMD_LIB for this PC
GPUMD_BIN=${GPUMD_BIN:-/home/hp/GPUMD-5.8.1/src/gpumd}     # override in hub.env
[[ -x "$GPUMD_BIN" ]] || GPUMD_BIN=$(command -v gpumd || echo "$GPUMD_BIN")
GPUMD_LIB=${GPUMD_LIB:-/home/hp/miniconda3/envs/gpumd/lib}
[[ -x "$GPUMD_BIN" ]] || { echo "GPUMD not found at $GPUMD_BIN (set GPUMD_BIN in hub.env)"; exit 1; }
if pgrep -f "gpumdq.py serve" >/dev/null; then
  echo "Restarting the running gpumdq server (running GPUMD jobs are not affected)..."
  pkill -f "gpumdq.py serve"; sleep 1
fi
echo "GPUMD : $GPUMD_BIN"
echo "Running gpumd processes in WSL right now:"
pgrep -a -x gpumd | sed 's/^/  pid /' || echo "  none"
for p in $(pgrep -x gpumd); do echo "    folder: $(readlink /proc/$p/cwd)"; done
echo
# Optional: report to the gpumdq hub on your server. Put these lines in hub.env next to this script
# (chmod 600 hub.env; see hub.env.example). Without hub.env nothing is sent anywhere.
if [[ -n "$GPUMDQ_HUB" ]]; then
  echo "Hub   : reporting to $GPUMDQ_HUB as '${GPUMDQ_NAME:-$(hostname)}'"
fi
export GPUMD_CMD="LD_LIBRARY_PATH=$GPUMD_LIB\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH} $GPUMD_BIN"
exec python3 -u gpumdq.py serve "$@"
