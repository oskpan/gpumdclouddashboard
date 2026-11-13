#!/usr/bin/env python3
"""
gpumdq - a tiny local job queue + live monitor for GPUMD.

  python gpumdq.py serve                      # start scheduler + dashboard (http://127.0.0.1:8765)
  python gpumdq.py submit runs/300K runs/400K # queue job directories (each has run.in, model.xyz, ...)
  python gpumdq.py list                       # show queue / running / history
  python gpumdq.py cancel 7                   # cancel queued or kill running job
  python gpumdq.py top 7                      # move job to front of queue
  python gpumdq.py serve --hub https://server.example --hub-token XXXX --name PC1
                                              # also push status to a gpumdq_hub.py server (read-only monitoring)

Pure standard library (Python 3.8+). Works on Linux and Windows.
"""
import argparse, gzip, hashlib, json, os, platform, shutil, signal, socket, sqlite3, subprocess, sys, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8765
VERSION = "2026.11.13-hub"
HOME = Path(os.environ.get("GPUMDQ_HOME", Path.home() / ".gpumdq"))
LOGNAME = "gpumdq.log"          # gpumd stdout/stderr is written here, inside the job directory
IS_WIN = os.name == "nt"

# ----------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, workdir TEXT NOT NULL, cmd TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',     -- queued|running|done|failed|cancelled
  priority INTEGER NOT NULL DEFAULT 0,       -- higher runs first
  gpu_req INTEGER,                           -- NULL = any GPU
  gpu INTEGER, pid INTEGER, exit_code INTEGER,
  thermo_offset INTEGER DEFAULT 0, expected_rows INTEGER DEFAULT 0, total_steps INTEGER DEFAULT 0,
  submitted REAL, started REAL, ended REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root TEXT, script TEXT, cmdline TEXT, pid INTEGER, pstart REAL,
  gpu INTEGER, first_seen REAL, last_seen REAL, ended REAL,
  UNIQUE(pid, pstart)
);"""


class DB:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.c.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).lastrowid


# ----------------------------------------------------------------------------- GPUMD file parsing
def parse_run_in(workdir):
    """Return (total_steps, expected thermo.out rows). dump_* settings in GPUMD apply only to the
    next `run`, so each run block is counted with its own dump_thermo interval."""
    total, rows, dump = 0, 0, None
    try:
        text = (Path(workdir) / "run.in").read_text(errors="ignore")
    except OSError:
        return 0, 0
    for line in text.splitlines():
        tok = line.split("#")[0].split()
        if not tok:
            continue
        kw = tok[0].lower()
        try:
            if kw == "dump_thermo":
                dump = int(float(tok[1]))
            elif kw == "run":
                n = int(float(tok[1]))
                total += n
                if dump:
                    rows += n // dump
                dump = None
        except (IndexError, ValueError):
            pass
    return total, rows


def count_lines(path):
    try:
        with open(path, "rb") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def tail(path, n=40, maxbytes=64_000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - maxbytes))
            data = f.read().decode(errors="replace")
        return data.replace("\r", "\n").splitlines()[-n:]
    except OSError:
        return []


def read_thermo(workdir, offset, max_points=600):
    """Rows of thermo.out written by this job (skip `offset` pre-existing rows), downsampled.
    Columns: T K U Pxx Pyy Pzz Pyz Pxz Pxy box..."""
    rows = []
    try:
        with open(Path(workdir) / "thermo.out", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i < offset or not ln.strip():
                    continue
                try:
                    rows.append([float(v) for v in ln.split()[:6]])
                except ValueError:
                    pass
    except OSError:
        pass
    stride = max(1, len(rows) // max_points)
    idx = list(range(0, len(rows), stride))
    if rows and idx[-1] != len(rows) - 1:
        idx.append(len(rows) - 1)
    pick = [rows[i] for i in idx]
    col = lambda k: [r[k] if len(r) > k else None for r in pick]
    p = [(r[3] + r[4] + r[5]) / 3 if len(r) >= 6 else None for r in pick]
    return {"i": idx, "T": col(0), "K": col(1), "U": col(2), "P": p, "n": len(rows)}


# ----------------------------------------------------------------------------- GPU + process helpers
def gpu_stats():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    res = []
    for ln in out.strip().splitlines():
        f = [s.strip() for s in ln.split(",")]
        num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None
        if len(f) >= 7:
            res.append({"index": int(f[0]), "name": f[1], "util": num(f[2]), "mem_used": num(f[3]),
                        "mem_total": num(f[4]), "temp": num(f[5]), "power": num(f[6])})
    return res


def pid_alive(pid):
    if not pid:
        return False
    if IS_WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # zombie check (Linux)
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def _procs_psutil():
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        try:
            out.append({"pid": p.info["pid"], "ppid": p.info["ppid"], "name": p.info["name"] or "",
                        "start": p.info["create_time"], "_p": p})
        except Exception:
            pass
    return out


def _procs_proc():  # Linux fallback without psutil
    out, boot = [], None
    try:
        boot = float([l for l in open("/proc/stat") if l.startswith("btime")][0].split()[1])
        hz = os.sysconf("SC_CLK_TCK")
    except Exception:
        hz = 100
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            st = (d / "stat").read_text()
            name = st[st.index("(") + 1:st.rindex(")")]
            f = st[st.rindex(")") + 2:].split()
            out.append({"pid": int(d.name), "ppid": int(f[1]), "name": name,
                        "start": (boot or 0) + int(f[19]) / hz, "_p": None})
        except Exception:
            pass
    return out


def _cwd(pr):
    try:
        if pr["_p"] is not None:
            return pr["_p"].cwd()
        return os.readlink(f"/proc/{pr['pid']}/cwd")
    except Exception:
        return None


def _env_cvd(pr):
    try:
        if pr["_p"] is not None:
            return pr["_p"].environ().get("CUDA_VISIBLE_DEVICES")
        env = Path(f"/proc/{pr['pid']}/environ").read_bytes().split(b"\0")
        for e in env:
            if e.startswith(b"CUDA_VISIBLE_DEVICES="):
                return e.split(b"=", 1)[1].decode()
    except Exception:
        pass
    return None


def gpu_pid_map():
    """pid -> GPU index, from nvidia-smi compute apps."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        r = lambda q: subprocess.run([exe, q, "--format=csv,noheader"], capture_output=True, text=True,
                                     timeout=5).stdout.strip().splitlines()
        uuid2idx = {}
        for ln in r("--query-gpu=index,uuid"):
            i, u = [s.strip() for s in ln.split(",")]
            uuid2idx[u] = int(i)
        m = {}
        for ln in r("--query-compute-apps=pid,gpu_uuid"):
            pid, u = [s.strip() for s in ln.split(",")[:2]]
            if pid.isdigit() and u in uuid2idx:
                m[int(pid)] = uuid2idx[u]
        return m
    except Exception:
        return {}


def _stem(name):
    n = os.path.basename(name.strip().strip('"')).lower()
    return n[:-4] if n.endswith(".exe") else n


def find_external_gpumd(exe_names, own_pids):
    """Find gpumd processes NOT started by gpumdq (e.g. launched by hand in a terminal)."""
    try:
        procs = _procs_psutil()
    except ImportError:
        procs = _procs_proc() if Path("/proc").exists() else []
    by_pid = {p["pid"]: p for p in procs}

    def owned(p):
        seen = 0
        while p and seen < 50:
            if p["pid"] in own_pids:
                return True
            p, seen = by_pid.get(p["ppid"]), seen + 1
        return False

    hits = [p for p in procs if _stem(p["name"]) in exe_names and not owned(p)]
    if not hits:
        return []
    gmap = gpu_pid_map()
    res = []
    for p in hits:
        gpu = gmap.get(p["pid"])
        if gpu is None:
            cvd = _env_cvd(p)
            if cvd and cvd.split(",")[0].strip().isdigit():
                gpu = int(cvd.split(",")[0])
        res.append({"pid": p["pid"], "workdir": _cwd(p), "gpu": gpu, "start": p["start"], "name": p["name"],
                    "driver": find_driver(p, by_pid)})
    return res


SHELLS = {"bash", "sh", "dash", "zsh", "ksh"}


def _cmdline(pr):
    try:
        if pr["_p"] is not None:
            return pr["_p"].cmdline()
        raw = Path(f"/proc/{pr['pid']}/cmdline").read_bytes()
        return [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    except Exception:
        return []


def find_driver(p, by_pid):
    """Walk up from a gpumd process to the outermost script that launched it
    (e.g. `bash run_all.sh` -> `python3 workflow.py` -> gpumd). Interactive shells have no script argument
    and are skipped, so the result is the batch script you started."""
    found, cur, n = None, by_pid.get(p["ppid"]), 0
    while cur and cur["pid"] > 1 and n < 50:
        argv = _cmdline(cur)
        if argv:
            exe = _stem(argv[0])
            script = None
            if exe in SHELLS or exe.startswith("python"):
                ext = (".sh", ".bash") if exe in SHELLS else (".py",)
                for a in argv[1:]:
                    if not a.startswith("-") and a.endswith(ext):
                        script = a
                        break
            if script:
                cwd = _cwd(cur) or "/"
                path = os.path.normpath(os.path.join(cwd, script))
                found = {"pid": cur["pid"], "key": proc_key(cur["pid"]), "script": path,
                         "root": os.path.dirname(path), "cmdline": " ".join(argv)}
        cur, n = by_pid.get(cur["ppid"]), n + 1
    return found


def proc_key(pid):
    """Stable identity of a live process (None if gone), used to tell a batch driver from a later process
    that reuses its pid. On Linux/WSL this is the raw start tick count from /proc/<pid>/stat; wall-clock start
    times derived from btime drift on WSL and must not be used as an identity."""
    if Path(f"/proc/{pid}/stat").exists():
        try:
            st = Path(f"/proc/{pid}/stat").read_text()
            f = st[st.rindex(")") + 2:].split()
            return None if f[0] == "Z" else float(f[19])
        except Exception:
            return None
    try:
        import psutil
        return round(psutil.Process(pid).create_time(), 0)
    except Exception:
        return None


def proc_start(pid):
    """Start time of a live process (None if gone)."""
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except ImportError:
        pass
    except Exception:
        return None
    if not pid_alive(pid):
        return None
    try:
        boot = float([l for l in open("/proc/stat") if l.startswith("btime")][0].split()[1])
        st = Path(f"/proc/{pid}/stat").read_text()
        return boot + int(st[st.rindex(")") + 2:].split()[19]) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


# ----------------------------------------------------------------------------- batch tracking
DONE_MARKERS = ("COMPLETED.json", ".done", "DONE")
SKIP_DIRS = ("archive", "__pycache__", "node_modules")
PLAN_DIR = Path(__file__).resolve().parent / "plans"


def load_plan(root):
    """Optional ordered stage list, so stages whose folders don't exist yet are shown too.
    Looked for as <root>/gpumdq_plan.txt or <gpumdq dir>/plans/*.plan with a matching `root:` line.
    Format: one stage per line: `relative/dir  [steps]`; '#' comments."""
    cands = [Path(root) / "gpumdq_plan.txt"]
    if PLAN_DIR.is_dir():
        cands += sorted(PLAN_DIR.glob("*.plan"))
    for f in cands:
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        out, ok = [], f.name == "gpumdq_plan.txt"
        for ln in lines:
            ln = ln.split("#")[0].strip()
            if not ln:
                continue
            if ln.lower().startswith("root:"):
                ok = os.path.normpath(ln[5:].strip()) == os.path.normpath(root)
                continue
            parts = ln.split()
            steps = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            out.append((parts[0].strip("/"), steps))
        if ok and out:
            return out
    return []


def stage_status(d, running_dirs):
    if str(d) in running_dirs:
        return "running"
    if any((d / m).exists() for m in DONE_MARKERS):
        return "done"
    log = d / "gpumd.log"
    if log.exists():
        if any("Finished running GPUMD" in l for l in tail(log, 5)):
            return "done"
        return "partial"
    if (d / "run.in").exists():
        return "ready"
    return "planned"


def scan_batch(root, running_dirs, max_dirs=3000):
    rootp = Path(root)
    found = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(x for x in dns if not x.startswith(".") and not any(k in x.lower() for k in SKIP_DIRS))
        if "run.in" in fns:
            found.append(os.path.relpath(dp, root))
        if len(found) > max_dirs:
            break
    plan = load_plan(root)
    order = [r for r, _ in plan] + [r for r in found if r not in {p for p, _ in plan}]
    plan_steps = dict(plan)
    stages = []
    for rel in order:
        d = rootp / rel
        st = stage_status(d, running_dirs)
        steps = parse_run_in(d)[0] if (d / "run.in").exists() else plan_steps.get(rel)
        stages.append({"rel": rel, "path": str(d), "status": st, "steps": steps or plan_steps.get(rel)})
    return stages, bool(plan)


def kill_tree(pid):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            os.killpg(pid, signal.SIGTERM)
    except Exception:
        pass


# ----------------------------------------------------------------------------- scheduler
class Scheduler:
    def __init__(self, db, gpus, cmd, poll=2.0):
        self.db, self.gpus, self.cmd, self.poll = db, gpus, cmd, poll
        self.procs = {}           # job id -> Popen (jobs started by this server process)
        self.stop = False
        self.gpu_cache, self.gpu_t = [], 0
        self.external = []        # gpumd processes running outside gpumdq
        self.scan_cache = {}      # batch id -> (time, (stages, has_plan), running dirs)
        self.ext_hold = {}        # gpu (or None) -> last time an external gpumd was seen on it
        self.hold_s = 60          # keep that GPU reserved this long after an external run ends,
                                  # so a batch script's next stage isn't pre-empted by the queue
        c = cmd.strip()
        first = c[1:].split('"')[0] if c.startswith('"') else (c.split()[0] if c else "gpumd")
        self.exe_names = {"gpumd", _stem(first)}
        self.adopt()
        self.dedupe_batches()

    def dedupe_batches(self):
        """Merge duplicate batch records (same driver pid + script) left by older versions."""
        for g in self.db.q("SELECT pid, script, MIN(id) keep, COUNT(*) n FROM batches GROUP BY pid, script HAVING n > 1"):
            self.db.x("DELETE FROM batches WHERE pid=? AND script=? AND id<>?", (g["pid"], g["script"], g["keep"]))
        for b in self.db.q("SELECT * FROM batches"):
            k = proc_key(b["pid"])
            if b["pstart"] is not None and b["pstart"] > 1e8:   # old wall-clock identity -> convert
                if k is not None and self._same_cmd(b):     # same driver still running -> re-attach
                    self.db.x("UPDATE batches SET pstart=?, ended=NULL WHERE id=?", (k, b["id"]))
                else:
                    self.db.x("UPDATE batches SET pstart=-id, ended=COALESCE(ended, ?) WHERE id=?", (time.time(), b["id"]))

    @staticmethod
    def _same_cmd(b):
        try:
            return " ".join(a.decode() for a in Path(f"/proc/{b['pid']}/cmdline").read_bytes().split(b"\0") if a) == b["cmdline"]
        except Exception:
            return False

    def adopt(self):
        """After a server restart, keep tracking jobs that are still running (they run detached)."""
        for j in self.db.q("SELECT * FROM jobs WHERE status='running'"):
            if not pid_alive(j["pid"]):
                self.finish(j, None)

    def finish(self, j, code):
        rows = count_lines(Path(j["workdir"]) / "thermo.out") - j["thermo_offset"]
        if code is None:  # unknown exit code (adopted job): judge by output
            ok = j["expected_rows"] and rows >= j["expected_rows"]
        else:
            ok = code == 0
        cur = self.db.q("SELECT status FROM jobs WHERE id=?", (j["id"],))
        status = "cancelled" if cur and cur[0]["status"] == "cancelled" else ("done" if ok else "failed")
        self.db.x("UPDATE jobs SET status=?, exit_code=?, ended=? WHERE id=?", (status, code, time.time(), j["id"]))

    def scan_external(self):
        own = {j["pid"] for j in self.db.q("SELECT pid FROM jobs WHERE status IN ('running','cancelled') AND ended IS NULL")}
        own |= {p.pid for p in self.procs.values()}
        try:
            self.external = find_external_gpumd(self.exe_names, own)
            now = time.time()
            for e in self.external:
                self.ext_hold[e["gpu"]] = now
                d = e.get("driver")
                if d and d["key"] is not None:
                    self.db.x("""INSERT INTO batches(root,script,cmdline,pid,pstart,gpu,first_seen,last_seen)
                                 VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(pid,pstart)
                                 DO UPDATE SET last_seen=excluded.last_seen, gpu=COALESCE(excluded.gpu, gpu)""",
                              (d["root"], d["script"], d["cmdline"], d["pid"], d["key"], e["gpu"], now, now))
            for b in self.db.q("SELECT * FROM batches WHERE ended IS NULL"):
                if proc_key(b["pid"]) != b["pstart"]:
                    self.db.x("UPDATE batches SET ended=? WHERE id=?", (now, b["id"]))
                else:
                    self.ext_hold[b["gpu"]] = now   # driver alive -> keep its GPU reserved between stages
        except Exception as e:
            print("external scan error:", e, file=sys.stderr)

    def busy_gpus(self):
        busy = {j["gpu"] for j in self.db.q("SELECT gpu FROM jobs WHERE status='running'")}
        now = time.time()
        held = [g for g, t in self.ext_hold.items() if now - t < self.hold_s]
        for g in sorted(held, key=lambda g: g is None):
            if g is not None:
                busy.add(g)
            else:  # GPU unknown (e.g. WSL, where nvidia-smi lists no processes): assume the first free GPU
                free = [x for x in self.gpus if x not in busy]
                if free:
                    busy.add(free[0])
        return busy

    def launch(self, j, gpu):
        wd = Path(j["workdir"])
        if not (wd / "run.in").exists():
            self.db.x("UPDATE jobs SET status='failed', note='run.in not found', ended=? WHERE id=?",
                      (time.time(), j["id"]))
            return
        total, rows = parse_run_in(wd)
        offset = count_lines(wd / "thermo.out")   # GPUMD appends to existing output files
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu))
        log = open(wd / LOGNAME, "a", buffering=1)
        log.write(f"\n===== gpumdq job {j['id']} start {time.ctime()} on GPU {gpu}: {j['cmd']} =====\n")
        # Windows: own hidden console, so jobs survive closing the server window
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} if IS_WIN else {"start_new_session": True}
        try:
            p = subprocess.Popen(j["cmd"], shell=True, cwd=str(wd), env=env, stdout=log,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
        except Exception as e:
            self.db.x("UPDATE jobs SET status='failed', note=?, ended=? WHERE id=?", (str(e), time.time(), j["id"]))
            return
        finally:
            log.close()
        self.procs[j["id"]] = p
        self.db.x("""UPDATE jobs SET status='running', gpu=?, pid=?, started=?, thermo_offset=?,
                     expected_rows=?, total_steps=? WHERE id=?""",
                  (gpu, p.pid, time.time(), offset, rows, total, j["id"]))

    def tick(self):
        for j in self.db.q("SELECT * FROM jobs WHERE status IN ('running','cancelled') AND pid IS NOT NULL AND ended IS NULL"):
            p = self.procs.get(j["id"])
            code = p.poll() if p else (None if pid_alive(j["pid"]) else "gone")
            if code is None:
                continue
            self.procs.pop(j["id"], None)
            self.finish(j, None if code == "gone" else code)
        self.scan_external()
        free = [g for g in self.gpus if g not in self.busy_gpus()]
        for g in free:
            nxt = self.db.q("""SELECT * FROM jobs WHERE status='queued' AND (gpu_req IS NULL OR gpu_req=?)
                               ORDER BY priority DESC, id ASC LIMIT 1""", (g,))
            if nxt:
                self.launch(nxt[0], g)

    def run(self):
        while not self.stop:
            try:
                self.tick()
            except Exception as e:
                print("scheduler error:", e, file=sys.stderr)
            time.sleep(self.poll)

    def cancel(self, jid):
        j = self.db.q("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            return False
        j = j[0]
        if j["status"] == "queued":
            self.db.x("UPDATE jobs SET status='cancelled', ended=? WHERE id=?", (time.time(), jid))
        elif j["status"] == "running":
            self.db.x("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
            kill_tree(j["pid"])
        return True

    def batch_views(self):
        now = time.time()
        bs = self.db.q("SELECT * FROM batches WHERE ended IS NULL OR ended > ? ORDER BY id DESC", (now - 7 * 86400,))
        running = {e["workdir"]: e for e in self.external if e["workdir"]}
        views = []
        for b in bs[:10]:
            key = b["id"]
            c = self.scan_cache.get(key)
            if not c or now - c[0] > 20 or set(running) != c[2]:
                try:
                    stages, has_plan = scan_batch(b["root"], set(running))
                except Exception as ex:
                    stages, has_plan = [], False
                c = self.scan_cache[key] = (now, (stages, has_plan), set(running))
            stages, has_plan = c[1]
            stages = [dict(x) for x in stages]
            tot = sum(x["steps"] or 0 for x in stages)
            done = sum(x["steps"] or 0 for x in stages if x["status"] == "done")
            rate, cur = None, None
            for x in stages:
                if x["status"] == "running" and x["path"] in running:
                    ev = ext_view(running[x["path"]])
                    x["progress"] = ev.get("progress")
                    done += (ev.get("steps_done") or 0)
                    if ev.get("steps_done") and ev.get("elapsed"):
                        rate = ev["steps_done"] / ev["elapsed"]
                    cur = x["rel"]
            alive = b["ended"] is None
            cnt = {k: sum(1 for x in stages if x["status"] == k) for k in ("done", "running", "partial", "ready", "planned")}
            views.append({"id": b["id"], "root": b["root"], "name": Path(b["root"]).name, "script": b["script"],
                          "cmdline": b["cmdline"], "pid": b["pid"], "alive": alive, "gpu": b["gpu"],
                          "started": b["first_seen"], "ended": b["ended"], "stages": stages, "counts": cnt,
                          "has_plan": has_plan, "steps_total": tot, "steps_done": done, "current": cur,
                          "progress": done / tot if tot else None,
                          "eta": (tot - done) / rate if rate and alive else None})
        return views

    def gpu_info(self):
        if time.time() - self.gpu_t > 2:
            self.gpu_cache, self.gpu_t = gpu_stats(), time.time()
        return self.gpu_cache


# ----------------------------------------------------------------------------- job view (progress / ETA)
def job_view(j):
    j = dict(j)
    wd = Path(j["workdir"])
    if j["status"] == "running":
        rows = count_lines(wd / "thermo.out") - (j["thermo_offset"] or 0)
        exp = j["expected_rows"] or 0
        frac = min(rows / exp, 1.0) if exp else None
        el = time.time() - (j["started"] or time.time())
        j.update(rows=rows, progress=frac, elapsed=el,
                 eta=(el / frac - el) if frac and frac > 0.002 else None,
                 steps_done=int(frac * j["total_steps"]) if frac is not None else None)
        last = read_last_thermo(wd / "thermo.out")
        if last:
            j.update(T=last[0], U=last[2] if len(last) > 2 else None)
    elif j["status"] in ("done", "failed", "cancelled") and j["started"]:
        j["elapsed"] = (j["ended"] or time.time()) - j["started"]
        if j["status"] == "done":
            j["progress"] = 1.0
    elif j["status"] == "queued":
        total, _ = parse_run_in(wd)
        j["total_steps"] = total
    return j


def ext_view(e):
    """Progress for a gpumd we did not start. We don't know how many thermo.out rows predate the run,
    so if thermo.out holds output from earlier runs the estimate is too high (capped at 100%)."""
    v = {"pid": e["pid"], "gpu": e["gpu"], "workdir": e["workdir"], "status": "external",
         "name": Path(e["workdir"]).name if e["workdir"] else e["name"], "elapsed": time.time() - e["start"]}
    if not e["workdir"]:
        v["note"] = "working folder not readable"
        return v
    wd = Path(e["workdir"])
    total, exp = parse_run_in(wd)
    rows = count_lines(wd / "thermo.out")
    frac = min(rows / exp, 1.0) if exp else None
    el = v["elapsed"]
    v.update(total_steps=total, expected_rows=exp, rows=rows, progress=frac,
             eta=(el / frac - el) if frac and 0.002 < frac < 1 else None,
             steps_done=int(frac * total) if frac is not None else None)
    last = read_last_thermo(wd / "thermo.out")
    if last:
        v.update(T=last[0], U=last[2] if len(last) > 2 else None)
    return v


def read_last_thermo(path):
    t = tail(path, 2, 4096)
    for ln in reversed(t):
        try:
            return [float(v) for v in ln.split()]
        except ValueError:
            continue
    return None


def build_state(s, history=50):
    """Snapshot shown by the local dashboard (/api/state) and pushed to a hub."""
    jobs = s.db.q("""SELECT * FROM jobs WHERE status IN ('queued','running')
                   OR id IN (SELECT id FROM jobs WHERE status NOT IN ('queued','running') ORDER BY id DESC LIMIT ?)
                   ORDER BY id""", (history,))
    return {"gpus": s.gpu_info(), "sched_gpus": s.gpus, "cmd": s.cmd,
            "jobs": [job_view(j) for j in jobs], "now": time.time(),
            "external": [ext_view(e) for e in s.external], "batches": s.batch_views()}


# ----------------------------------------------------------------------------- hub push (optional)
def _binary_sha256(cmd):
    """SHA256 of the gpumd executable named in the command (recorded so the hub can show which build each PC runs)."""
    for tok in reversed(cmd.replace('"', " ").split()):
        if "=" in tok and not tok.startswith("/"):
            continue
        p = shutil.which(tok) or (tok if os.path.isfile(tok) else None)
        if p:
            h = hashlib.sha256()
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                return p, h.hexdigest()
            except OSError:
                return p, None
    return None, None


def _thin(series, n=200):
    """Keep at most n points of each thermo series (the hub only draws small plots)."""
    out = dict(series)
    k = len(series.get("T") or [])
    if k > n:
        stride = k / n
        idx = sorted({int(i * stride) for i in range(n)} | {k - 1})
        for key in ("i", "T", "K", "U", "P"):
            if key in out and out[key] is not None:
                out[key] = [out[key][i] for i in idx]
    return out


def detail_for(s, key):
    """Thermo + log for one item, in the same shape the local /api/... endpoints return.
    key is 'job:<id>', 'ext:<pid>' or 'stage:<path>'; returns None if this PC would not show it either."""
    kind, _, k = key.partition(":")
    try:
        if kind == "job":
            j = s.db.q("SELECT * FROM jobs WHERE id=?", (int(k),))
            if not j:
                return None
            j = j[0]
            return {"thermo": _thin(read_thermo(j["workdir"], j["thermo_offset"] or 0), 300),
                    "log": {"lines": tail(Path(j["workdir"]) / LOGNAME, 60)}}
        if kind == "ext":
            e = next((e for e in s.external if e["pid"] == int(k)), None)
            if not e or not e["workdir"]:
                return None
            lines = ["(started outside gpumdq and no gpumd.log in its folder: its output is in the terminal where it was launched)"]
            for nm in ("gpumd.log", "log.out", LOGNAME):
                if (Path(e["workdir"]) / nm).exists():
                    lines = [f"[{nm}]"] + tail(Path(e["workdir"]) / nm, 60)
                    break
            return {"thermo": _thin(read_thermo(e["workdir"], 0), 300), "log": {"lines": lines}}
        if kind == "stage":
            roots = [b["root"] for b in s.db.q("SELECT root FROM batches")]
            if not k or not any(os.path.normpath(k).startswith(os.path.normpath(r) + os.sep) for r in roots):
                return None
            return {"thermo": _thin(read_thermo(k, 0), 300), "log": {"lines": tail(Path(k) / "gpumd.log", 60)}}
    except (ValueError, OSError):
        return None
    return None


def hub_payload(s, name, binary, every, want=(), with_running=False):
    st = build_state(s, history=30)
    keys = list(want)
    if with_running:   # refresh plots of everything that is running, so a click on the hub is instant
        keys += [f"job:{j['id']}" for j in st["jobs"] if j["status"] == "running"]
        keys += [f"ext:{e['pid']}" for e in st["external"]]
    detail = {}
    for k in dict.fromkeys(keys):
        d = detail_for(s, k)
        detail[k] = d if d is not None else {"missing": True}
    st["detail"] = detail
    st["push_every"] = every
    st["host"] = {"name": name, "hostname": socket.gethostname(), "platform": platform.platform(),
                  "python": platform.python_version(), "gpumdq": VERSION,
                  "gpumd_binary": binary[0], "gpumd_sha256": binary[1]}
    return st


class Pusher(threading.Thread):
    """Every `every` seconds POST the current snapshot (gzip JSON) to <hub>/api/push.
    Outbound HTTPS only: nothing on this PC has to accept incoming connections. When someone opens a plot
    on the hub, the hub's reply asks for it and the next push (sent right away) carries it."""

    def __init__(self, sched, hub, token, name, every=10.0, detail_every=60.0):
        super().__init__(daemon=True)
        self.s, self.url, self.token, self.name, self.every = sched, hub.rstrip("/") + "/api/push", token, name, every
        self.detail_every = detail_every
        self.binary = _binary_sha256(sched.cmd)
        self.fail_since, self.want, self.last_detail = None, [], 0.0

    def push_once(self):
        with_running = time.time() - self.last_detail > self.detail_every
        st = hub_payload(self.s, self.name, self.binary, self.every, self.want, with_running)
        body = gzip.compress(json.dumps(st).encode())
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip",
            "Authorization": "Bearer " + self.token, "User-Agent": "gpumdq/" + VERSION})
        r = json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")
        if with_running:
            self.last_detail = time.time()
        self.want = [str(k) for k in (r.get("want") or [])][:20]
        return r

    def run(self):
        while not self.s.stop:
            try:
                self.push_once()
                if self.fail_since:
                    print(f"gpumdq: hub reachable again ({self.url})", file=sys.stderr)
                self.fail_since = None
            except Exception as e:
                if not self.fail_since:
                    print(f"gpumdq: cannot push to hub {self.url}: {e} (will keep retrying quietly)", file=sys.stderr)
                    self.fail_since = time.time()
                self.want = []
            if self.want:          # somebody is waiting for a plot: answer now
                time.sleep(0.3)
                continue
            # wait `every` s, but stop early when the hub has asked for something (checked on next push)
            time.sleep(self.every)


# ----------------------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    sched: Scheduler = None

    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def job(self, jid):
        r = self.sched.db.q("SELECT * FROM jobs WHERE id=?", (jid,))
        return r[0] if r else None

    def do_GET(self):
        s, db = self.sched, self.sched.db
        parts = self.path.split("?")[0].strip("/").split("/")
        if self.path in ("/", "/index.html"):
            return self.send(DASHBOARD, ctype="text/html; charset=utf-8")
        if parts[:2] == ["api", "state"] and len(parts) == 2:
            return self.send(build_state(s))
        if parts[:2] == ["api", "stage"] and len(parts) == 3:
            from urllib.parse import urlparse, parse_qs
            p = parse_qs(urlparse(self.path).query).get("p", [""])[0]
            roots = [b["root"] for b in db.q("SELECT root FROM batches")]
            if not p or not any(os.path.normpath(p).startswith(os.path.normpath(r) + os.sep) for r in roots):
                return self.send({"error": "not a tracked stage"}, 404)
            if parts[2] == "thermo":
                return self.send(read_thermo(p, 0))
            if parts[2] == "log":
                return self.send({"lines": tail(Path(p) / "gpumd.log", 60)})
        if len(parts) == 4 and parts[:2] == ["api", "ext"]:
            e = next((e for e in s.external if e["pid"] == int(parts[2])), None)
            if not e or not e["workdir"]:
                return self.send({"error": "no such process"}, 404)
            if parts[3] == "thermo":
                return self.send(read_thermo(e["workdir"], 0))
            if parts[3] == "log":
                for name in ("gpumd.log", "log.out", LOGNAME):
                    if (Path(e["workdir"]) / name).exists():
                        return self.send({"lines": [f"[{name}]"] + tail(Path(e["workdir"]) / name, 60)})
                return self.send({"lines": ["(started outside gpumdq and no gpumd.log in its folder: its output is in the terminal where it was launched)"]})
        if len(parts) == 4 and parts[:2] == ["api", "job"]:
            j = self.job(int(parts[2]))
            if not j:
                return self.send({"error": "no such job"}, 404)
            if parts[3] == "thermo":
                return self.send(read_thermo(j["workdir"], j["thermo_offset"] or 0))
            if parts[3] == "log":
                return self.send({"lines": tail(Path(j["workdir"]) / LOGNAME, 60)})
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        s, db = self.sched, self.sched.db
        parts = self.path.strip("/").split("/")
        try:
            b = self.body()
        except Exception:
            return self.send({"error": "bad json"}, 400)
        if parts == ["api", "submit"]:
            ids, errs = [], []
            for wd in b.get("workdirs", []):
                p = Path(wd).expanduser().resolve()
                if not (p / "run.in").exists():
                    errs.append(f"{p}: no run.in")
                    continue
                ids.append(db.x("""INSERT INTO jobs(name,workdir,cmd,priority,gpu_req,submitted)
                                   VALUES(?,?,?,?,?,?)""",
                                (b.get("name") or p.name, str(p), b.get("cmd") or s.cmd,
                                 int(b.get("priority") or 0), b.get("gpu"), time.time())))
            return self.send({"ids": ids, "errors": errs})
        if len(parts) == 4 and parts[:2] == ["api", "batch"] and parts[3] == "forget":
            db.x("DELETE FROM batches WHERE id=? AND ended IS NOT NULL", (int(parts[2]),))
            return self.send({"ok": True})
        if len(parts) == 4 and parts[:2] == ["api", "ext"] and parts[3] == "kill":
            pid = int(parts[2])
            if any(e["pid"] == pid for e in s.external):
                kill_tree(pid) if IS_WIN else os.kill(pid, signal.SIGTERM)
                return self.send({"ok": True})
            return self.send({"error": "not an external gpumd"}, 404)
        if len(parts) == 4 and parts[:2] == ["api", "job"]:
            jid, act = int(parts[2]), parts[3]
            j = self.job(jid)
            if not j:
                return self.send({"error": "no such job"}, 404)
            if act == "cancel":
                s.cancel(jid)
            elif act == "priority":
                db.x("UPDATE jobs SET priority=? WHERE id=?", (int(b["priority"]), jid))
            elif act == "top":
                mx = db.q("SELECT MAX(priority) m FROM jobs WHERE status='queued'")[0]["m"] or 0
                db.x("UPDATE jobs SET priority=? WHERE id=?", (mx + 1, jid))
            elif act == "requeue":
                db.x("""UPDATE jobs SET status='queued', gpu=NULL, pid=NULL, exit_code=NULL, started=NULL,
                        ended=NULL, note=NULL WHERE id=? AND status IN ('done','failed','cancelled')""", (jid,))
            elif act == "delete":
                db.x("DELETE FROM jobs WHERE id=? AND status NOT IN ('running')", (jid,))
            return self.send({"ok": True})
        self.send({"error": "not found"}, 404)


def serve(args):
    db = DB(HOME / "jobs.db")
    gpus = [int(g) for g in args.gpus.split(",")] if args.gpus else [g["index"] for g in gpu_stats()] or [0]
    sched = Scheduler(db, gpus, args.cmd, args.poll)
    threading.Thread(target=sched.run, daemon=True).start()
    Handler.sched = sched
    if args.hub:
        if not args.hub_token:
            sys.exit("--hub needs --hub-token (or GPUMDQ_HUB_TOKEN)")
        if args.hub.startswith("http://") and not any(h in args.hub for h in ("://127.0.0.1", "://localhost")):
            print("gpumdq: WARNING hub URL is plain http - the token is sent unencrypted; use https", file=sys.stderr)
        Pusher(sched, args.hub, args.hub_token, args.name, args.push_every).start()
        print(f"gpumdq: pushing status as '{args.name}' to {args.hub} every {args.push_every:g} s")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"gpumdq: GPUs {gpus}, command '{args.cmd}', db {HOME / 'jobs.db'}")
    print(f"gpumdq: dashboard at http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}")
    print("gpumdq: running jobs keep going if you stop this server; they are re-attached on restart.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sched.stop = True


# ----------------------------------------------------------------------------- CLI client
def api(args, path, data=None):
    url = f"http://{args.server}{path}"
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach gpumdq server at {args.server} ({e}). Start it with: python gpumdq.py serve")


def fmt_t(s):
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def cli_list(args):
    st = api(args, "/api/state")
    for g in st["gpus"]:
        print(f"GPU{g['index']} {g['name']}: {g['util']}% util, {g['mem_used']:.0f}/{g['mem_total']:.0f} MiB, {g['temp']}°C")
    print(f"\n{'ID':>4} {'STATUS':<10} {'GPU':>3} {'PRI':>3} {'PROGRESS':>8} {'ELAPSED':>8} {'ETA':>8}  NAME")
    jobs = st["jobs"] if args.all else [j for j in st["jobs"] if j["status"] in ("queued", "running")]
    for b in st.get("batches", []):
        c = b["counts"]
        pr = f"{b['progress'] * 100:.1f}%" if b.get("progress") is not None else "-"
        print(f"\nBATCH {b['name']}  ({'running, pid ' + str(b['pid']) if b['alive'] else 'stopped'})  {b['script']}")
        print(f"  {c['done']} done, {c['running']} running, {c['partial']} partial, {c['ready'] + c['planned']} pending"
              f"  |  steps {pr}  |  ETA {fmt_t(b.get('eta'))}")
        if b["current"]:
            print(f"  now: {b['current']}")
        nxt = [x["rel"] for x in b["stages"] if x["status"] in ("ready", "planned")][:3]
        if nxt:
            print("  next: " + ", ".join(nxt))
    for e in st.get("external", []):
        pr = f"{e['progress'] * 100:.1f}%" if e.get("progress") is not None else "-"
        print(f"{'ext':>4} {'external':<10} {e['gpu'] if e['gpu'] is not None else '?':>3} {'':>3} {pr:>8} "
              f"{fmt_t(e.get('elapsed')):>8} {fmt_t(e.get('eta')):>8}  {e['name']}  (pid {e['pid']}, {e['workdir']})")
    for j in sorted(jobs, key=lambda j: ({"running": 0, "queued": 1}.get(j["status"], 2), -j["priority"], j["id"])):
        pr = f"{j['progress'] * 100:.1f}%" if j.get("progress") is not None else "-"
        print(f"{j['id']:>4} {j['status']:<10} {j['gpu'] if j['gpu'] is not None else '-':>3} {j['priority']:>3} "
              f"{pr:>8} {fmt_t(j.get('elapsed')):>8} {fmt_t(j.get('eta')):>8}  {j['name']}  ({j['workdir']})")


def main():
    ap = argparse.ArgumentParser(description="Local job queue and monitor for GPUMD")
    ap.add_argument("--server", default=f"127.0.0.1:{DEFAULT_PORT}", help="host:port of the gpumdq server")
    sub = ap.add_subparsers(dest="cmd_name", required=True)

    s = sub.add_parser("serve", help="run scheduler + web dashboard")
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to watch from phone/LAN (no auth!)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--gpus", help="comma list of GPU indices to schedule on (default: all from nvidia-smi)")
    s.add_argument("--cmd", default=os.environ.get("GPUMD_CMD", "gpumd"),
                   help="command run in each job dir (default 'gpumd'; e.g. '/opt/GPUMD/src/gpumd')")
    s.add_argument("--poll", type=float, default=2.0)
    s.add_argument("--hub", default=os.environ.get("GPUMDQ_HUB"), help="URL of a gpumdq_hub.py server to push status to")
    s.add_argument("--hub-token", default=os.environ.get("GPUMDQ_HUB_TOKEN"), help="this PC's token on the hub")
    s.add_argument("--name", default=os.environ.get("GPUMDQ_NAME", socket.gethostname()), help="name shown on the hub")
    s.add_argument("--push-every", type=float, default=float(os.environ.get("GPUMDQ_PUSH_EVERY", 10)))

    s = sub.add_parser("submit", help="queue one or more job directories")
    s.add_argument("dirs", nargs="+")
    s.add_argument("-p", "--priority", type=int, default=0)
    s.add_argument("-g", "--gpu", type=int, help="pin to this GPU index")
    s.add_argument("-n", "--name")
    s.add_argument("--cmd", help="override command for these jobs")

    s = sub.add_parser("list", help="show jobs");  s.add_argument("-a", "--all", action="store_true")
    for name in ("cancel", "top", "requeue", "delete"):
        sub.add_parser(name).add_argument("ids", nargs="+", type=int)
    s = sub.add_parser("priority"); s.add_argument("id", type=int); s.add_argument("value", type=int)

    a = ap.parse_args()
    if a.cmd_name == "serve":
        return serve(a)
    if a.cmd_name == "submit":
        r = api(a, "/api/submit", {"workdirs": [str(Path(d).resolve()) for d in a.dirs], "priority": a.priority,
                                   "gpu": a.gpu, "name": a.name if len(a.dirs) == 1 else None, "cmd": a.cmd})
        print("queued jobs:", r["ids"]); [print("skipped:", e) for e in r["errors"]]
    elif a.cmd_name == "list":
        cli_list(a)
    elif a.cmd_name == "priority":
        api(a, f"/api/job/{a.id}/priority", {"priority": a.value})
    else:
        for i in a.ids:
            api(a, f"/api/job/{i}/{a.cmd_name}", {})
        print("ok")


# ----------------------------------------------------------------------------- dashboard (single page)
DASHBOARD = r"""<!doctype html><html><head><meta charset="utf-8"><title>GPUMD queue</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--pn:#171a21;--ln:#262a33;--tx:#e6e8eb;--mu:#8b93a1;--ac:#4f9cf9;--ok:#3ecf8e;--wa:#f5a524;--er:#f2555a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 22px;border-bottom:1px solid var(--ln)}
header h1{font-size:16px;margin:0;font-weight:600}header .mu{margin-left:auto}
main{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:18px;padding:18px 22px}
@media(max-width:1000px){main{grid-template-columns:1fr}}
.pn{background:var(--pn);border:1px solid var(--ln);border-radius:10px;padding:14px 16px;margin-bottom:18px}
h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mu);margin:0 0 10px;font-weight:600}
.mu{color:var(--mu)}.gpus{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.gpu{border:1px solid var(--ln);border-radius:8px;padding:10px 12px}.gpu b{font-weight:600}
.bar{height:6px;background:#232733;border-radius:3px;overflow:hidden;margin:4px 0 8px}.bar i{display:block;height:100%;background:var(--ac)}
.row{display:flex;justify-content:space-between;font-size:12px;color:var(--mu)}
table{width:100%;border-collapse:collapse}td,th{padding:7px 6px;border-bottom:1px solid var(--ln);text-align:left;white-space:nowrap}
th{font-size:11px;color:var(--mu);font-weight:500;text-transform:uppercase}tr.j{cursor:pointer}tr.j:hover{background:#1c2029}tr.sel{background:#1e2533}
.pg{width:140px;height:8px;background:#232733;border-radius:4px;overflow:hidden;display:inline-block;vertical-align:middle;margin-right:6px}.pg i{display:block;height:100%;background:var(--ok)}
.st{font-size:11px;padding:1px 7px;border-radius:9px;border:1px solid}.running{color:var(--ok)}.queued{color:var(--ac)}.done{color:var(--mu)}.failed{color:var(--er)}.cancelled{color:var(--wa)}
button{background:#232733;color:var(--tx);border:1px solid var(--ln);border-radius:6px;padding:3px 9px;cursor:pointer;font-size:12px}button:hover{border-color:var(--ac)}
input{background:#0f1115;color:var(--tx);border:1px solid var(--ln);border-radius:6px;padding:6px 8px;font-size:13px}
.path{max-width:260px;overflow:hidden;text-overflow:ellipsis;font-size:12px}
pre{background:#0b0d11;border:1px solid var(--ln);border-radius:6px;padding:8px;font-size:11px;max-height:260px;overflow:auto;margin:0;white-space:pre-wrap}
canvas{width:100%;height:120px;display:block;margin-bottom:6px}.bt{border:1px solid var(--ln);border-radius:8px;padding:10px 12px;margin-bottom:10px}.bt .hd{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.bt .big{height:8px;background:#232733;border-radius:4px;overflow:hidden;margin:8px 0 6px}.bt .big i{display:block;height:100%;background:var(--ok)}
.chips{display:flex;flex-wrap:wrap;gap:3px;margin:6px 0}.chip{width:12px;height:12px;border-radius:3px;background:#2a2f3a;cursor:pointer}
.chip.done{background:#3a6b55}.chip.running{background:var(--ok);box-shadow:0 0 0 2px #3ecf8e55}.chip.partial{background:var(--er)}.chip.ready{background:#35507a}.chip.planned{background:#2a2f3a;border:1px dashed #4a5263}
summary{cursor:pointer;color:var(--mu);font-size:12px}.sl td{padding:3px 6px;font-size:12px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:12px;margin-bottom:10px}
</style></head><body>
<header><h1>GPUMD queue</h1><span class="mu" id="sum"></span><span class="mu" id="clk"></span></header>
<main><div>
 <div class="pn"><h2>GPUs</h2><div class="gpus" id="gpus"></div></div>
 <div class="pn" id="batchpn" style="display:none"><h2>Batches <span class="mu" style="text-transform:none;letter-spacing:0">(detected automatically from your shell scripts)</span></h2><div id="batches"></div></div>
 <div class="pn"><h2>Running</h2><table id="run"></table></div>
 <div class="pn"><h2>Queue</h2><table id="que"></table>
   <div style="display:flex;gap:6px;margin-top:12px;flex-wrap:wrap">
   <input id="wd" placeholder="job directory (contains run.in)" style="flex:1;min-width:240px">
   <input id="pri" type="number" value="0" style="width:64px" title="priority">
   <input id="gq" placeholder="GPU (any)" style="width:90px"><button onclick="submit()">Queue</button></div>
   <div class="mu" id="msg" style="font-size:12px;margin-top:6px"></div></div>
 <div class="pn"><h2>History</h2><table id="his"></table></div>
</div><div><div class="pn" id="det" style="position:sticky;top:12px"><h2>Job detail</h2><div class="mu">Click a job to see its thermo plots and log.</div></div></div></main>
<script>
let S=null,sel=null;const openB=new Set();
const $=id=>document.getElementById(id);
const ft=s=>s==null?'–':s>=86400?Math.floor(s/86400)+'d '+Math.floor(s%86400/3600)+'h':s>=3600?Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m':Math.floor(s/60)+'m '+String(Math.floor(s%60)).padStart(2,'0')+'s';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const post=(u,b={})=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
async function act(id,a){if(a=='cancel'&&!confirm('Cancel job '+id+'?'))return;await post(`/api/job/${id}/${a}`);load()}
async function submit(){const w=$('wd').value.trim();if(!w)return;const g=$('gq').value.trim();
 const r=await post('/api/submit',{workdirs:[w],priority:+$('pri').value||0,gpu:g===''?null:+g});
 $('msg').textContent=r.ids.length?'Queued job '+r.ids.join(', '):r.errors.join('; ');if(r.ids.length)$('wd').value='';load()}
function row(j,kind){const p=j.progress;const btn=[];
 if(kind=='que')btn.push(`<button onclick="event.stopPropagation();act(${j.id},'top')">↑ top</button>`);
 if(kind!='his')btn.push(`<button onclick="event.stopPropagation();act(${j.id},'cancel')">✕</button>`);
 else btn.push(`<button onclick="event.stopPropagation();act(${j.id},'requeue')">↻ requeue</button>`);
 const cells={run:[j.id,esc(j.name),'GPU '+j.gpu,`<span class=pg><i style="width:${(p||0)*100}%"></i></span>${p==null?'?':(p*100).toFixed(1)+'%'}`,ft(j.elapsed),ft(j.eta),j.T!=null?j.T.toFixed(1)+' K':'–'],
  que:[j.id,esc(j.name),j.priority,j.gpu_req??'any',(j.total_steps||0).toLocaleString()+' steps',`<span class=path title="${esc(j.workdir)}">${esc(j.workdir)}</span>`],
  his:[j.id,esc(j.name),`<span class="st ${j.status}">${j.status}</span>`,ft(j.elapsed),j.ended?new Date(j.ended*1000).toLocaleString():'–',esc(j.note||'')]}[kind];
 return `<tr class="j ${sel=='j'+j.id?'sel':''}" onclick="pick('j${j.id}')">${cells.map(c=>`<td>${c}</td>`).join('')}<td style="text-align:right">${btn.join(' ')}</td></tr>`}
function extRow(e){const p=e.progress;return `<tr class="j ${sel=='e'+e.pid?'sel':''}" onclick="pick('e${e.pid}')"><td><span class="st cancelled" title="started outside gpumdq">ext</span></td><td>${esc(e.name)}</td><td>${e.gpu==null?'GPU ?':'GPU '+e.gpu}</td>
 <td><span class=pg><i style="width:${(p||0)*100}%"></i></span>${p==null?'?':'~'+(p*100).toFixed(1)+'%'}</td><td>${ft(e.elapsed)}</td><td>${ft(e.eta)}</td><td>${e.T!=null?e.T.toFixed(1)+' K':'–'}</td>
 <td style="text-align:right"><button onclick="event.stopPropagation();killExt(${e.pid})">✕</button></td></tr>`}
async function killExt(pid){if(!confirm('Kill gpumd process '+pid+'? It was not started by gpumdq.'))return;await post(`/api/ext/${pid}/kill`);load()}
const H={run:['#','name','gpu','progress','elapsed','eta','T',''],que:['#','name','pri','gpu','length','directory',''],his:['#','name','status','wall time','finished','note','']};
function table(id,kind,js,empty){$(id).innerHTML=js.length?'<tr>'+H[kind].map(h=>`<th>${h}</th>`).join('')+'</tr>'+js.map(j=>row(j,kind)).join(''):`<tr><td class=mu>${empty}</td></tr>`}
async function load(){try{S=await (await fetch('/api/state')).json()}catch(e){$('sum').textContent='server not reachable';return}
 const J=S.jobs,X=S.external||[],R=J.filter(j=>j.status=='running'),Q=J.filter(j=>j.status=='queued').sort((a,b)=>b.priority-a.priority||a.id-b.id),D=J.filter(j=>!['running','queued'].includes(j.status)).reverse();
 $('sum').textContent=`${R.length} running${X.length?' + '+X.length+' external':''} · ${Q.length} queued`;$('clk').textContent=new Date().toLocaleTimeString();
 $('gpus').innerHTML=S.gpus.length?S.gpus.map(g=>{const job=R.find(j=>j.gpu==g.index)||X.filter(e=>e.gpu==g.index).map(e=>({id:'ext pid '+e.pid,name:e.name}))[0];return `<div class=gpu><b>GPU ${g.index}</b> <span class=mu>${esc(g.name)}</span>
  <div class=row><span>util</span><span>${g.util??'–'}%</span></div><div class=bar><i style="width:${g.util||0}%"></i></div>
  <div class=row><span>memory</span><span>${(g.mem_used/1024).toFixed(1)} / ${(g.mem_total/1024).toFixed(1)} GiB</span></div><div class=bar><i style="width:${g.mem_used/g.mem_total*100}%"></i></div>
  <div class=row><span>${g.temp??'–'}°C · ${g.power!=null?g.power.toFixed(0)+' W':''}</span><span>${job?'job '+job.id+' · '+esc(job.name):(S.sched_gpus.includes(g.index)?'idle':'not scheduled')}</span></div></div>`}).join(''):'<div class=mu>nvidia-smi not found</div>';
 table('run','run',R,'Nothing running');if(X.length){if(!R.length)$('run').innerHTML='<tr>'+H.run.map(h=>`<th>${h}</th>`).join('')+'</tr>';$('run').innerHTML+=X.map(extRow).join('')}table('que','que',Q,'Queue is empty');table('his','his',D,'No finished jobs yet');
 batches(S.batches||[]);if(sel)detail()}
function pick(id){sel=id;load()}
function tog(id,el){el.open?openB.add(id):openB.delete(id)}
function batches(B){$('batchpn').style.display=B.length?'':'none';
 $('batches').innerHTML=B.map(b=>{const c=b.counts,p=b.progress,pend=c.ready+c.planned;
  const chips=b.stages.map(x=>`<span class="chip ${x.status}" title="${esc(x.rel)} · ${x.status}${x.steps?' · '+x.steps.toLocaleString()+' steps':''}" onclick="pickStage('${encodeURIComponent(x.path)}','${x.status}')"></span>`).join('');
  const rows=b.stages.map(x=>`<tr class="j" onclick="pickStage('${encodeURIComponent(x.path)}','${x.status}')"><td><span class="st ${x.status=='running'?'running':x.status=='done'?'done':x.status=='partial'?'failed':'queued'}">${x.status}</span></td><td>${esc(x.rel)}</td><td style="text-align:right">${x.steps?x.steps.toLocaleString():'–'}</td><td>${x.progress!=null?(x.progress*100).toFixed(1)+'%':''}</td></tr>`).join('');
  return `<div class=bt><div class=hd><b>${esc(b.name)}</b><span class="st ${b.alive?'running':'done'}">${b.alive?'running':'stopped'}</span>
   <span class=mu style="font-size:12px">${esc(b.cmdline)} · pid ${b.pid}${b.gpu!=null?' · GPU '+b.gpu:''}</span>${b.alive?'':`<button style="margin-left:auto" onclick="post('/api/batch/${b.id}/forget').then(load)">forget</button>`}</div>
   <div class=big><i style="width:${(p||0)*100}%"></i></div>
   <div class=row><span>${c.done} done · ${c.running} running · ${pend} pending${c.partial?' · <span style=color:var(--er)>'+c.partial+' partial</span>':''}${b.has_plan?'':' · <span title="no plan file: only stages whose folders already exist are listed">stages known so far</span>'}</span>
   <span>${p!=null?(p*100).toFixed(1)+'% of steps':''}${b.eta?' · ETA ~'+ft(b.eta):''}</span></div>
   ${b.current?`<div class=row style="margin-top:4px"><span>now: ${esc(b.current)}</span></div>`:''}
   <div class=chips>${chips}</div>
   <details ${openB.has(b.id)?'open':''} ontoggle="tog(${b.id},this)"><summary>all ${b.stages.length} stages</summary><table class=sl>${rows}</table></details></div>`}).join('')}
function pickStage(p,st){const path=decodeURIComponent(p);const e=(S.external||[]).find(x=>x.workdir==path);if(e)return pick('e'+e.pid);if(st=='planned'||st=='ready')return;pick('p'+path)}
function plot(cv,ys,label,color){const c=cv.getContext('2d'),w=cv.width=cv.clientWidth*devicePixelRatio,h=cv.height=cv.clientHeight*devicePixelRatio;c.clearRect(0,0,w,h);
 const v=ys.filter(y=>y!=null);if(v.length<2){c.fillStyle='#8b93a1';c.font=`${11*devicePixelRatio}px sans-serif`;c.fillText(label+': waiting for data',8,18*devicePixelRatio);return}
 let lo=Math.min(...v),hi=Math.max(...v);if(hi==lo){hi+=1;lo-=1}const pad=18*devicePixelRatio;
 c.strokeStyle='#262a33';c.strokeRect(0,0,w,h);c.beginPath();c.strokeStyle=color;c.lineWidth=1.5*devicePixelRatio;
 ys.forEach((y,i)=>{if(y==null)return;const X=i/(ys.length-1)*w,Y=pad+(1-(y-lo)/(hi-lo))*(h-pad*1.6);i?c.lineTo(X,Y):c.moveTo(X,Y)});c.stroke();
 c.fillStyle='#8b93a1';c.font=`${11*devicePixelRatio}px sans-serif`;c.fillText(`${label}  last ${v[v.length-1].toPrecision(6)}   [${lo.toPrecision(5)} … ${hi.toPrecision(5)}]`,6,13*devicePixelRatio)}
async function detail(){if(sel[0]=='p')return stageDetail(sel.slice(1));const ext=sel[0]=='e',key=+sel.slice(1);
 const j=ext?(S.external||[]).find(x=>x.pid==key):S.jobs.find(x=>x.id==key);if(!j)return;const base=ext?`/api/ext/${key}`:`/api/job/${key}`;
 if(ext)Object.assign(j,{cmd:'(started outside gpumdq, pid '+key+')',exit_code:null});
 const [th,lg]=await Promise.all([fetch(base+'/thermo').then(r=>r.json()),fetch(base+'/log').then(r=>r.json())]);
 const d=$('det');if(!d.querySelector('canvas')||d.dataset.id!=sel){d.dataset.id=sel;d.innerHTML=`<h2>${ext?'External pid '+key:'Job '+key} · ${esc(j.name)}</h2><div class=kv id=kv></div><canvas id=cT></canvas><canvas id=cU></canvas><canvas id=cP></canvas><h2 style="margin-top:10px">log</h2><pre id=lg></pre>`}
 $('kv').innerHTML=[['status',`<span class="st ${j.status}">${j.status}</span>`],['directory',esc(j.workdir)],['command',esc(j.cmd)],['steps',(j.total_steps||0).toLocaleString()+(j.steps_done!=null?' (≈'+j.steps_done.toLocaleString()+' done)':'')],['thermo rows',th.n+(j.expected_rows?' / '+j.expected_rows:'')],['exit code',j.exit_code??'–']].map(([k,v])=>`<span class=mu>${k}</span><span>${v}</span>`).join('');
 plot($('cT'),th.T,'Temperature (K)','#f5a524');plot($('cU'),th.U,'Potential energy (eV)','#4f9cf9');plot($('cP'),th.P,'Pressure (GPa)','#3ecf8e');
 const pre=$('lg'),bottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-4;pre.textContent=lg.lines.join('\n')||'(no output yet)';if(bottom)pre.scrollTop=pre.scrollHeight}
async function stageDetail(path){const q='?p='+encodeURIComponent(path);
 const [th,lg]=await Promise.all([fetch('/api/stage/thermo'+q).then(r=>r.json()),fetch('/api/stage/log'+q).then(r=>r.json())]);
 let st=null;(S.batches||[]).forEach(b=>b.stages.forEach(x=>{if(x.path==path)st=x}));
 const d=$('det');if(d.dataset.id!=sel){d.dataset.id=sel;d.innerHTML=`<h2>Stage · ${esc(path.split('/').slice(-3).join('/'))}</h2><div class=kv id=kv></div><canvas id=cT></canvas><canvas id=cU></canvas><canvas id=cP></canvas><h2 style="margin-top:10px">gpumd.log</h2><pre id=lg></pre>`}
 $('kv').innerHTML=[['status',st?`<span class="st ${st.status=='done'?'done':'failed'}">${st.status}</span>`:'–'],['directory',esc(path)],['steps',st&&st.steps?st.steps.toLocaleString():'–'],['thermo rows',th.n??'–']].map(([k,v])=>`<span class=mu>${k}</span><span>${v}</span>`).join('');
 if(th.T){plot($('cT'),th.T,'Temperature (K)','#f5a524');plot($('cU'),th.U,'Potential energy (eV)','#4f9cf9');plot($('cP'),th.P,'Pressure (GPa)','#3ecf8e')}
 $('lg').textContent=(lg.lines||[]).join('\n')||'(no gpumd.log)'}
load();setInterval(load,3000);
</script></body></html>"""

if __name__ == "__main__":
    main()
