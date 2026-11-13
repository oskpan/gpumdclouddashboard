#!/usr/bin/env python3
"""
gpumdq_hub - one web page that shows every PC running gpumdq.

Each PC runs `gpumdq.py serve --hub https://your.server --hub-token ... --name PC1`. It pushes a status
snapshot here over HTTPS every ~10 s (outbound only: the PCs need no open ports, no VPN, no fixed IP).
This hub stores the latest snapshot per PC and shows them on one page. It is read-only: queueing,
cancelling and killing jobs still happen on each PC.

Standard library only. Put gpumdq.py next to this file (the per-PC page reuses its dashboard).

  python3 gpumdq_hub.py add-host PC1          # prints the token to give PC1 (only the hash is stored)
  python3 gpumdq_hub.py set-password alice    # login for viewing the dashboard
  python3 gpumdq_hub.py serve --port 8780     # listen on 127.0.0.1:8780; put Caddy/nginx (HTTPS) in front
  python3 gpumdq_hub.py list                  # hosts and viewers

Optional phone alerts (job finished/failed, batch ended, PC went silent) via ntfy:
  python3 gpumdq_hub.py serve --ntfy https://ntfy.sh/<a-long-random-topic>
Config and last-known state live in $GPUMDQ_HUB_HOME (default ~/.gpumdq_hub).
"""
import argparse, base64, getpass, gzip, hashlib, hmac, json, os, re, secrets, sys, threading, time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from gpumdq import DASHBOARD as PC_DASHBOARD
except Exception:  # still works without it; the per-PC page then falls back to the overview
    PC_DASHBOARD = None

HOME = Path(os.environ.get("GPUMDQ_HUB_HOME", Path.home() / ".gpumdq_hub"))
CONF = HOME / "hub.json"
STATE = HOME / "hub_state.json"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
MAX_BODY = 8 << 20          # compressed push size limit
MAX_JSON = 64 << 20         # decompressed limit
DETAIL_WAIT = 30            # s a viewer's plot request waits for the PC to answer
AUTH_CACHE = {}             # sha256(Authorization header) -> (time, user) after a successful login


# ----------------------------------------------------------------------------- config
def load_conf():
    try:
        return json.loads(CONF.read_text())
    except (OSError, ValueError):
        return {"hosts": {}, "viewers": {}}


def save_conf(c):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(CONF)


def tok_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


def pw_hash(pw, salt=None, n=200_000):
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2_sha256${n}${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), n).hex()}"


def pw_ok(pw, stored):
    try:
        _, n, salt, _h = stored.split("$")
        return hmac.compare_digest(pw_hash(pw, salt, int(n)), stored)
    except ValueError:
        return False


# ----------------------------------------------------------------------------- hub state
class Hub:
    def __init__(self, ntfy=None, stale=None):
        self.lock = threading.Condition()
        self.hosts = {}          # name -> {"state", "t", "ip", "detail": {key: (t, data)}, "want": {key: t}}
        self.ntfy, self.stale_override = ntfy, stale
        self.dirty, self.offline_sent = False, set()
        try:
            for name, h in json.loads(STATE.read_text()).items():
                self.hosts[name] = {"state": h["state"], "t": h["t"], "ip": h.get("ip"), "detail": {}, "want": {}}
                if time.time() - h["t"] > self.stale_after(self.hosts[name]) + 20:  # +20: state is saved every 15 s
                    self.offline_sent.add(name)  # already silent before this restart: no new alert, but "back" later
        except (OSError, ValueError, KeyError):
            pass

    def stale_after(self, h):
        if self.stale_override:
            return self.stale_override
        return max(60.0, 6 * float((h.get("state") or {}).get("push_every") or 10))

    def ingest(self, name, ip, st):
        now = time.time()
        detail = st.pop("detail", {}) or {}
        with self.lock:
            h = self.hosts.setdefault(name, {"state": None, "t": 0, "ip": None, "detail": {}, "want": {}})
            prev, was_silent = h["state"], name in self.offline_sent
            h.update(state=st, t=now, ip=ip)
            for k, d in detail.items():
                h["detail"][k] = (now, d)
                h["want"].pop(k, None)
            # forget plots nobody looked at for a while
            h["detail"] = {k: v for k, v in h["detail"].items() if now - v[0] < 600}
            h["want"] = {k: t for k, t in h["want"].items() if now - t < DETAIL_WAIT + 5}
            want = list(h["want"])
            self.dirty = True
            self.offline_sent.discard(name)
            self.lock.notify_all()
        if was_silent and prev is not None:
            self.alert(f"{name} is reporting again", tags="green_circle")
        if prev is not None:
            self.diff_alerts(name, prev, st)
        return want

    def diff_alerts(self, name, a, b):
        old = {j["id"]: j["status"] for j in a.get("jobs", [])}
        for j in b.get("jobs", []):
            if old.get(j["id"]) == "running" and j["status"] in ("done", "failed"):
                ok = j["status"] == "done"
                self.alert(f"{name}: job {j['id']} {Path(j['workdir']).name} {j['status']}"
                           + ("" if ok else f" (exit {j.get('exit_code')})"), tags="white_check_mark" if ok else "x")
        oldb = {x["id"]: x for x in a.get("batches", [])}
        for x in b.get("batches", []):
            o = oldb.get(x["id"])
            if o and o.get("alive") and not x.get("alive"):
                c = x.get("counts") or {}
                bad = c.get("partial", 0)
                self.alert(f"{name}: batch {x['name']} ended - {c.get('done', 0)} stages done"
                           + (f", {bad} incomplete" if bad else ""), tags="checkered_flag" if not bad else "warning")

    def alert(self, msg, tags=""):
        if not self.ntfy:
            return
        def send():
            try:
                req = urllib.request.Request(self.ntfy, data=msg.encode(), method="POST",
                                             headers={"Title": "gpumdq", "Tags": tags})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                print(f"hub: ntfy failed: {e}", file=sys.stderr)
        threading.Thread(target=send, daemon=True).start()

    def watchdog(self):
        while True:
            time.sleep(15)
            now = time.time()
            with self.lock:
                silent = [n for n, h in self.hosts.items()
                          if now - h["t"] > self.stale_after(h) and n not in self.offline_sent]
                self.offline_sent.update(silent)
                dirty, self.dirty = self.dirty, False
                snap = {n: {"state": h["state"], "t": h["t"], "ip": h["ip"]} for n, h in self.hosts.items()} if dirty else None
            for n in silent:
                self.alert(f"{n} has not reported for {int(now - self.hosts[n]['t'])} s "
                           "(PC asleep/offline, network down, or gpumdq stopped)", tags="red_circle")
            if snap is not None:
                try:
                    HOME.mkdir(parents=True, exist_ok=True)
                    tmp = STATE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(snap))
                    tmp.replace(STATE)
                except OSError as e:
                    print(f"hub: cannot save state: {e}", file=sys.stderr)

    def summary(self):
        now = time.time()
        out = []
        with self.lock:
            items = sorted(self.hosts.items())
        for name, h in items:
            st = h["state"] or {}
            age = now - h["t"]
            jobs = st.get("jobs", [])
            out.append({
                "name": name, "age": age, "online": age <= self.stale_after(h), "ip": h["ip"],
                "host": st.get("host", {}), "gpus": st.get("gpus", []),
                "running": [{k: j.get(k) for k in ("id", "workdir", "gpu", "progress", "eta", "elapsed", "T")}
                            for j in jobs if j["status"] == "running"],
                "queued": sum(1 for j in jobs if j["status"] == "queued"),
                "external": [{k: e.get(k) for k in ("pid", "name", "workdir", "gpu", "progress", "eta", "elapsed", "T")}
                             for e in st.get("external", [])],
                "batches": [{k: b.get(k) for k in ("id", "name", "root", "alive", "progress", "eta", "counts",
                                                   "current", "steps_done", "steps_total", "ended")}
                            for b in st.get("batches", [])],
            })
        return {"now": now, "hosts": out}

    def get_detail(self, name, key):
        """Latest plot data for one item; asks the PC for it and waits briefly when we have none fresh."""
        deadline = time.time() + DETAIL_WAIT
        with self.lock:
            h = self.hosts.get(name)
            if not h:
                return None
            have = h["detail"].get(key)
            if have and have[1].get("missing"):
                have = None                       # maybe it exists now (e.g. a job queued since)
            if have and time.time() - have[0] < 10:
                return have[1]
            h["want"][key] = time.time()          # ask the PC on its next push
            if have:
                return have[1]                    # serve what we have meanwhile; the next view gets the update
            while time.time() < deadline:
                self.lock.wait(deadline - time.time())
                got = h["detail"].get(key)
                if got and got is not have:
                    return got[1]
            return have[1] if have else {"timeout": True}


# ----------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    hub: Hub = None
    no_auth = False
    server_version = "gpumdq-hub"

    def log_message(self, *a):
        pass

    def client_ip(self):
        # trust X-Forwarded-For only from a local reverse proxy
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
        return self.client_address[0]

    def send(self, obj, code=200, ctype="application/json", extra=None):
        body = obj if isinstance(obj, bytes) else (obj.encode() if isinstance(obj, str) else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def viewer_ok(self):
        if self.no_auth:
            return True
        viewers = load_conf().get("viewers", {})
        auth = self.headers.get("Authorization", "")
        ck = hashlib.sha256(auth.encode()).hexdigest()
        hit = AUTH_CACHE.get(ck)
        if hit and time.time() - hit[0] < 600 and hit[1] in viewers:   # skip the slow hash on every poll
            return True
        if auth.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                if user in viewers and pw_ok(pw, viewers[user]):
                    if len(AUTH_CACHE) > 1000:
                        AUTH_CACHE.clear()
                    AUTH_CACHE[ck] = (time.time(), user)
                    return True
            except Exception:
                pass
            time.sleep(1.0)  # slow down guessing
        self.send({"error": "login required"}, 401, extra={"WWW-Authenticate": 'Basic realm="gpumdq hub", charset="UTF-8"'})
        return False

    # ---- agents
    def do_POST(self):
        if urlparse(self.path).path != "/api/push":
            return self.send({"error": "read-only hub: queue/cancel/kill on the PC itself"}, 405)
        auth = self.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        th = tok_hash(tok) if tok else ""
        name = next((n for n, h in load_conf().get("hosts", {}).items() if th and hmac.compare_digest(h, th)), None)
        if not name:
            time.sleep(1.0)
            return self.send({"error": "unknown token"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self.send({"error": "bad size"}, 413)
        raw = self.rfile.read(n)
        try:
            if self.headers.get("Content-Encoding") == "gzip":
                d = gzip.GzipFile(fileobj=__import__("io").BytesIO(raw))
                raw = d.read(MAX_JSON + 1)
                if len(raw) > MAX_JSON:
                    return self.send({"error": "too large"}, 413)
            st = json.loads(raw)
            assert isinstance(st, dict)
        except Exception:
            return self.send({"error": "bad payload"}, 400)
        want = self.hub.ingest(name, self.client_ip(), st)
        self.send({"ok": True, "name": name, "want": want})

    # ---- viewers
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/healthz":
            return self.send({"ok": True})
        if not self.viewer_ok():
            return
        if path in ("/", "/index.html"):
            return self.send(OVERVIEW, ctype="text/html; charset=utf-8")
        if path == "/api/hosts":
            return self.send(self.hub.summary())
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "h" and NAME_RE.match(parts[1]):
            name, rest = parts[1], parts[2:]
            with self.hub.lock:
                h = self.hub.hosts.get(name)
            if not h:
                return self.send({"error": "unknown PC"}, 404)
            if rest in ([], ["index.html"]):
                if not path.endswith("/"):
                    return self.send(b"", 302, extra={"Location": f"/h/{name}/"})
                return self.send(pc_page(name), ctype="text/html; charset=utf-8")
            if rest == ["api", "meta"]:
                return self.send({"age": time.time() - h["t"], "online": time.time() - h["t"] <= self.hub.stale_after(h),
                                  "host": (h["state"] or {}).get("host", {})})
            if rest == ["api", "state"]:
                st = dict(h["state"] or {})
                st["hub_age"] = time.time() - h["t"]
                return self.send(st)
            if len(rest) == 4 and rest[0] == "api" and rest[1] in ("job", "ext") and rest[3] in ("thermo", "log"):
                return self.send_detail(name, f"{rest[1]}:{rest[2]}", rest[3])
            if len(rest) == 3 and rest[:2] == ["api", "stage"] and rest[2] in ("thermo", "log"):
                p = parse_qs(u.query).get("p", [""])[0]
                return self.send_detail(name, f"stage:{p}", rest[2])
        self.send({"error": "not found"}, 404)

    def send_detail(self, name, key, what):
        d = self.hub.get_detail(name, key)
        if d is None:
            return self.send({"error": "unknown PC"}, 404)
        if d.get("missing") or d.get("timeout"):
            msg = ("the PC did not answer in time (offline, or gpumdq there is older than the hub)" if d.get("timeout")
                   else "the PC no longer has this item")
            if what == "log":
                return self.send({"lines": [f"({msg})"]})
            return self.send({"i": [], "T": [], "K": [], "U": [], "P": [], "n": 0})
        return self.send(d.get(what) or {})


def pc_page(name):
    """The PC's own dashboard, served read-only from the hub's copy of its data."""
    if not PC_DASHBOARD:
        return f'<meta http-equiv="refresh" content="0;url=/">gpumdq.py is missing next to gpumdq_hub.py'
    shim = """<script>(function(){const B=%s;const of=window.fetch.bind(window);
window.fetch=function(u,o){if(typeof u==='string'&&u.startsWith('/api/')){
 if(o&&o.method&&o.method.toUpperCase()!=='GET')return Promise.resolve(new Response(JSON.stringify({error:'read-only hub view: do this on the PC itself'}),{headers:{'Content-Type':'application/json'}}));
 u=B+u}return of(u,o)};
window.confirm=function(m){alert('This is the read-only hub view. Do this on the PC itself.');return false};
async function meta(){try{const m=await (await of(B+'/api/meta')).json();const e=document.getElementById('hubage');
 const a=m.age,t=a<90?Math.round(a)+' s':a<5400?Math.round(a/60)+' min':(a/3600).toFixed(1)+' h';
 e.textContent=(m.online?'updated ':'NOT REPORTING - last update ')+t+' ago';e.style.color=m.online?'':'var(--er)'}catch(x){}}
document.addEventListener('DOMContentLoaded',()=>{meta();setInterval(meta,5000)});})();</script>
<style>#wd,#pri,#gq,#wd~button,#msg{display:none!important}.hubnav{color:var(--ac);text-decoration:none;font-size:13px}</style>
""" % json.dumps("/h/" + quote(name))
    html = PC_DASHBOARD.replace("<script>", shim + "<script>", 1)
    html = html.replace("<title>GPUMD queue</title>", f"<title>{name} · gpumdq hub</title>", 1)
    html = html.replace('<header><h1>GPUMD queue</h1>',
                        f'<header><a class="hubnav" href="/">&larr; all PCs</a><h1>{name}</h1>'
                        f'<span class="mu" id="hubage" style="font-size:12px"></span>', 1)
    return html


OVERVIEW = r"""<!doctype html><html><head><meta charset="utf-8"><title>gpumdq hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--pn:#171a21;--ln:#262a33;--tx:#e6e8eb;--mu:#8b93a1;--ac:#4f9cf9;--ok:#3ecf8e;--wa:#f5a524;--er:#f2555a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 22px;border-bottom:1px solid var(--ln)}
header h1{font-size:16px;margin:0;font-weight:600}header .mu{margin-left:auto}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:18px;padding:18px 22px}
@media(max-width:480px){main{grid-template-columns:1fr;padding:12px}}
.pc{background:var(--pn);border:1px solid var(--ln);border-radius:10px;padding:14px 16px;cursor:pointer;text-decoration:none;color:inherit;display:block}
.pc:hover{border-color:var(--ac)}.pc.off{border-color:#5a2a2d}.pc.off>:not(.hd){opacity:.45}
.hd{display:flex;align-items:center;gap:8px}.hd b{font-size:15px}.dot{width:9px;height:9px;border-radius:50%;background:var(--ok)}.off .dot{background:var(--er)}
.mu{color:var(--mu)}.sm{font-size:12px}.warn{color:var(--wa)}
h3{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mu);margin:12px 0 6px;font-weight:600}
.bar{height:6px;background:#232733;border-radius:3px;overflow:hidden;margin:3px 0 6px}.bar i{display:block;height:100%;background:var(--ac)}
.pg i{background:var(--ok)}.row{display:flex;justify-content:space-between;gap:10px;font-size:12px}.row span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{color:var(--mu);font-size:12px}
</style></head><body>
<header><h1>gpumdq hub</h1><span class="mu sm" id="sum"></span></header>
<main id="m"><div class="mu">loading…</div></main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ft=s=>s==null?'–':s>=86400?Math.floor(s/86400)+'d '+Math.floor(s%86400/3600)+'h':s>=3600?Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m':Math.floor(s/60)+'m '+String(Math.floor(s%60)).padStart(2,'0')+'s';
const ago=a=>a<90?Math.round(a)+' s':a<5400?Math.round(a/60)+' min':(a/3600).toFixed(1)+' h';
const pct=p=>p==null?'–':(100*p).toFixed(1)+'%';
const base=p=>String(p||'').split(/[\\/]/).filter(Boolean).pop()||'';
function item(name,p,eta,sub){return `<div class="row"><span>${esc(name)}</span><span class="mu">${pct(p)}${eta!=null?' · ETA '+ft(eta):''}</span></div>
 <div class="bar pg"><i style="width:${p==null?0:Math.min(100,100*p)}%"></i></div>${sub?`<div class="mu sm" style="margin:-4px 0 6px">${sub}</div>`:''}`}
async function load(){let D;try{D=await (await fetch('api/hosts')).json()}catch(e){$('sum').textContent='hub not reachable';return}
 const shas=new Set(D.hosts.map(h=>h.host.gpumd_sha256).filter(Boolean));
 const on=D.hosts.filter(h=>h.online).length;
 $('sum').textContent=`${on}/${D.hosts.length} PCs reporting · ${new Date().toLocaleTimeString()}`;
 if(!D.hosts.length){$('m').innerHTML='<div class="mu">No PC has reported yet. Run <code>gpumdq_hub.py add-host NAME</code> and start gpumdq on that PC with --hub/--hub-token.</div>';return}
 $('m').innerHTML=D.hosts.map(h=>{
  const g=(h.gpus||[]).map(x=>`<div class="row"><span>GPU ${x.index} ${esc(x.name||'')}</span><span class="mu">${x.util??'–'}% · ${x.mem_used!=null?(x.mem_used/1024).toFixed(1)+'/'+(x.mem_total/1024).toFixed(0)+' GB':''}${x.temp!=null?' · '+x.temp+'°C':''}</span></div><div class="bar"><i style="width:${x.util||0}%"></i></div>`).join('');
  const bs=(h.batches||[]).filter(b=>b.alive);
  const B=bs.map(b=>item(b.name,b.progress,b.eta,`${b.counts?b.counts.done:0} done${b.current?' · now '+esc(b.current):''}`)).join('');
  const ended=(h.batches||[]).filter(b=>!b.alive).slice(0,2).map(b=>`<div class="row sm"><span class="mu">${esc(b.name)}</span><span class="mu">ended ${b.ended?ago(D.now-b.ended)+' ago':''}${b.counts&&b.counts.partial?' · <span class=warn>'+b.counts.partial+' incomplete</span>':''}</span></div>`).join('');
  const R=(h.running||[]).map(j=>item(`#${j.id} ${base(j.workdir)}`,j.progress,j.eta,`GPU ${j.gpu??'?'}${j.T!=null?' · T '+j.T.toFixed(0)+' K':''}`)).join('')
   +(h.external||[]).filter(e=>!bs.some(b=>e.workdir&&b.root&&e.workdir.startsWith(b.root))).map(e=>item(e.name,e.progress,e.eta,`pid ${e.pid} · GPU ${e.gpu??'?'}`)).join('');
  const sha=h.host.gpumd_sha256;const mism=sha&&shas.size>1;
  return `<a class="pc ${h.online?'':'off'}" href="h/${encodeURIComponent(h.name)}/">
   <div class="hd"><span class="dot"></span><b>${esc(h.name)}</b><span class="mu sm">${esc(h.host.hostname||'')}</span>
   <span class="mu sm" style="margin-left:auto;${h.online?'':'color:var(--er)'}">${h.online?'updated':'silent for'} ${ago(h.age)}${h.online?' ago':''}</span></div>
   <div class="sm ${mism?'warn':'mu'}" title="${esc(h.host.gpumd_binary||'')}">gpumd ${sha?sha.slice(0,12):'binary unknown'}${mism?' · differs from other PCs':''}${h.queued?` · ${h.queued} queued`:''}</div>
   <h3>GPUs</h3>${g||'<div class=empty>no GPU info</div>'}
   <h3>Batches</h3>${B||'<div class=empty>none running</div>'}${ended}
   <h3>Running</h3>${R||'<div class=empty>nothing outside batches</div>'}</a>`}).join('')}
load();setInterval(load,5000);
</script></body></html>"""


# ----------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 and put an HTTPS reverse proxy in front")
    s.add_argument("--port", type=int, default=8780)
    s.add_argument("--ntfy", default=os.environ.get("GPUMDQ_NTFY"), help="ntfy topic URL for phone alerts")
    s.add_argument("--stale", type=float, help="seconds of silence before a PC counts as offline (default 6x its push interval, min 60)")
    s.add_argument("--no-auth", action="store_true", help="no viewer login (testing on localhost only)")
    a = sub.add_parser("add-host"); a.add_argument("name")
    r = sub.add_parser("remove-host"); r.add_argument("name")
    p = sub.add_parser("set-password"); p.add_argument("user")
    sub.add_parser("list")
    args = ap.parse_args()
    c = load_conf()

    if args.cmd == "add-host":
        if not NAME_RE.match(args.name):
            sys.exit("name: letters, digits, _ . - (max 32)")
        tok = secrets.token_urlsafe(32)
        c["hosts"][args.name] = tok_hash(tok)
        save_conf(c)
        print(f"token for {args.name} (shown once; re-run add-host to replace it):\n\n  {tok}\n")
        print(f"on {args.name}:  GPUMDQ_HUB=https://<your server> GPUMDQ_HUB_TOKEN={tok} GPUMDQ_NAME={args.name} ./start_wsl.sh")
    elif args.cmd == "remove-host":
        c["hosts"].pop(args.name, None)
        save_conf(c)
        print(f"removed {args.name} (its last snapshot stays visible until you delete {STATE} or restart with it gone)")
    elif args.cmd == "set-password":
        pw = getpass.getpass(f"password for {args.user}: ")
        if len(pw) < 10:
            sys.exit("use at least 10 characters")
        if pw != getpass.getpass("again: "):
            sys.exit("passwords differ")
        c["viewers"][args.user] = pw_hash(pw)
        save_conf(c)
        print("saved")
    elif args.cmd == "list":
        print("hosts:  ", ", ".join(sorted(c["hosts"])) or "(none)")
        print("viewers:", ", ".join(sorted(c["viewers"])) or "(none)")
    elif args.cmd == "serve":
        if not args.no_auth and not c.get("viewers"):
            sys.exit("no viewer login yet: run  gpumdq_hub.py set-password <user>  first (or --no-auth for a local test)")
        if args.no_auth and args.host not in ("127.0.0.1", "localhost", "::1"):
            sys.exit("--no-auth is only allowed on 127.0.0.1")
        if PC_DASHBOARD is None:
            print("hub: gpumdq.py not found next to this file - per-PC pages are disabled", file=sys.stderr)
        hub = Hub(args.ntfy, args.stale)
        threading.Thread(target=hub.watchdog, daemon=True).start()
        Handler.hub, Handler.no_auth = hub, args.no_auth
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
        srv.daemon_threads = True
        print(f"gpumdq hub on http://{args.host}:{args.port}  ({len(c['hosts'])} PCs registered; state in {HOME})")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
