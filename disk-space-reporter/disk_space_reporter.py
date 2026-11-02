#!/usr/bin/env python3
"""
Disk Space Reporter
-------------------
Shows what is eating your disk space: biggest folders, biggest files, space by
type, and big files you haven't touched in a year.

It only READS. It never deletes, moves or changes anything.

How it works:
  1. Run it. A page opens in your browser (it runs only on your own PC).
  2. Pick your user folder, a whole drive, or type any folder, then press Scan.
  3. Read the report. Download the biggest files as a CSV if you want.

Pure standard library. No installs needed.

Usage:
  python disk_space_reporter.py                    # open the page in your browser
  python disk_space_reporter.py "D:\\Games"          # open the page with that folder filled in
  python disk_space_reporter.py --text "C:\\Users"   # print the report in the terminal, no page
  python disk_space_reporter.py --text . --csv big.csv --top 50
  python disk_space_reporter.py --no-browser       # start the page but don't auto-open it
"""

import argparse
import csv
import heapq
import io
import json
import os
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "1.1.0"

OLD_DAYS = 365                  # "not touched in a year"
BIG_BYTES = 100 * 1024 * 1024   # "big" = 100 MB or more (for the old-files list)

CATEGORIES = {
    "Video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg"},
    "Pictures": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".raw", ".psd", ".svg"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".odt", ".rtf", ".md"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"},
    "Disk images": {".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".vdi", ".wim"},
    "Programs": {".exe", ".dll", ".msi", ".msp", ".msix", ".sys", ".cab", ".apk", ".jar"},
    "Code": {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss", ".json", ".c", ".cpp", ".h",
             ".java", ".cs", ".go", ".rs", ".php", ".rb", ".sql", ".yml", ".yaml", ".toml"},
    "Data & logs": {".db", ".sqlite", ".log", ".dat", ".bin", ".tmp", ".cache", ".bak"},
}
EXT_TO_KIND = {ext: kind for kind, exts in CATEGORIES.items() for ext in exts}

# Windows file attribute flags
_REPARSE = 0x400            # links and junctions
_OFFLINE = 0x1000           # cloud-only placeholder
_RECALL = 0x400000          # cloud-only placeholder (OneDrive etc.)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _attrs(entry):
    try:
        return getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def is_link_or_placeholder(entry):
    """Links/junctions are never followed, and cloud-only files take no local space."""
    if entry.is_symlink():
        return True
    a = _attrs(entry)
    if a & _REPARSE and entry.is_dir(follow_symlinks=False):
        return True
    return bool(a & (_OFFLINE | _RECALL))


class Scan:
    """One scan of one folder. Run it in a thread, watch the counters, call result() when done."""

    def __init__(self, root, top=25):
        self.root = os.path.normpath(str(root))
        self.top = top
        self.files = 0
        self.bytes = 0
        self.skipped = 0
        self.current = self.root
        self.cancelled = False
        self.started = time.time()
        self.finished = None
        self._own = {}                # folder -> bytes of files directly inside it
        self._biggest = []            # min-heap of (size, path, mtime)
        self._old = []                # min-heap of (size, path, mtime)
        self._kinds = {}              # kind -> [bytes, count]

    def run(self):
        cutoff = time.time() - OLD_DAYS * 86400
        stack = [self.root]
        while stack and not self.cancelled:
            folder = stack.pop()
            self.current = folder
            try:
                it = os.scandir(folder)
            except OSError:
                self.skipped += 1
                continue
            own = 0
            with it:
                for e in it:
                    try:
                        if is_link_or_placeholder(e):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        self.skipped += 1
                        continue
                    size = st.st_size
                    own += size
                    self.files += 1
                    self.bytes += size
                    kind = EXT_TO_KIND.get(os.path.splitext(e.name)[1].lower(), "Other")
                    k = self._kinds.setdefault(kind, [0, 0])
                    k[0] += size
                    k[1] += 1
                    self._keep(self._biggest, (size, e.path, st.st_mtime))
                    if size >= BIG_BYTES and st.st_mtime < cutoff:
                        self._keep(self._old, (size, e.path, st.st_mtime))
            self._own[folder] = own
        self.finished = time.time()

    def _keep(self, heap, item):
        if len(heap) < self.top:
            heapq.heappush(heap, item)
        elif item[0] > heap[0][0]:
            heapq.heapreplace(heap, item)

    def result(self):
        """Everything the report shows, as plain data."""
        # Roll folder sizes up: each folder's total = its own files + all folders below it
        total = dict(self._own)
        for d in sorted(self._own, key=lambda p: p.count(os.sep), reverse=True):
            if d == self.root:
                continue
            parent = os.path.dirname(d)
            total[parent] = total.get(parent, 0) + total[d]

        children = [(s, p) for p, s in total.items() if os.path.dirname(p) == self.root and p != self.root]
        deeper = [(s, p) for p, s in total.items()
                  if p != self.root and os.path.dirname(p) != self.root]
        now = time.time()

        def files(heap):
            return [{"path": p, "size": s, "age_days": max(0, int((now - m) / 86400))}
                    for s, p, m in sorted(heap, reverse=True)]

        try:
            du = shutil.disk_usage(self.root)
            drive = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            drive = None

        return {
            "root": self.root,
            "files": self.files,
            "bytes": self.bytes,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "seconds": round((self.finished or time.time()) - self.started, 1),
            "drive": drive,
            "children": [{"path": p, "name": os.path.basename(p), "size": s}
                         for s, p in sorted(children, reverse=True)[: self.top]],
            "deeper": [{"path": p, "size": s} for s, p in sorted(deeper, reverse=True)[: self.top]],
            "biggest": files(self._biggest),
            "old": files(self._old),
            "kinds": [{"name": n, "size": v[0], "count": v[1]}
                      for n, v in sorted(self._kinds.items(), key=lambda kv: kv[1][0], reverse=True)],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def default_drive():
    return (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"


def _drive_label(name, path, extra=""):
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None  # empty card reader, disc drive, unplugged network drive...
    return f"Whole {name} drive ({human_size(du.total)}, {human_size(du.free)} free{extra})"


def list_locations():
    """Dropdown choices: the system drive first, then your user folder, then any other drives."""
    home = str(Path.home())
    system = default_drive()
    locs = []
    others = []

    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            letter = f"{chr(65 + i)}:"
            path = letter + "\\"
            kind = k32.GetDriveTypeW(path)  # 2 = removable, 3 = fixed, 4 = network (skip CD drives and RAM disks)
            if kind not in (2, 3, 4):
                continue
            extra = {2: ", removable", 4: ", network"}.get(kind, "")
            label = _drive_label(letter, path, extra)
            if label:
                (locs if path.upper() == system.upper() else others).append({"label": label, "path": path})
    else:
        label = _drive_label("system", system)
        locs.append({"label": label or "Whole system drive (/)", "path": system})
        for base in ("/mnt", "/media", "/Volumes"):
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for n in sorted(names):
                p = os.path.join(base, n)
                for q in [p] + ([os.path.join(p, s) for s in sorted(os.listdir(p))] if os.path.isdir(p) else []):
                    if os.path.ismount(q):
                        lab = _drive_label(q, q)
                        if lab:
                            others.append({"label": lab, "path": q})

    locs.append({"label": f"User folder ({home})", "path": home})
    return locs + sorted(others, key=lambda d: d["path"])


def csv_text(result):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["size_bytes", "size", "days_since_changed", "path"])
    for f in result["biggest"]:
        w.writerow([f["size"], human_size(f["size"]), f["age_days"], f["path"]])
    return out.getvalue()


def print_text(r):
    print(f"\nScanned {r['root']}")
    print(f"{r['files']:,} files, {human_size(r['bytes'])}, {r['seconds']}s"
          + (f", {r['skipped']} places couldn't be read" if r["skipped"] else "")
          + (" (stopped early)" if r["cancelled"] else ""))
    if r["drive"]:
        d = r["drive"]
        print(f"Drive: {human_size(d['used'])} used of {human_size(d['total'])}, {human_size(d['free'])} free")

    def section(title, rows):
        print(f"\n{title}")
        print("-" * 72)
        for size, label in rows:
            print(f"{human_size(size):>10}  {label}")
        if not rows:
            print("  (none)")

    section("Space by type", [(k["size"], f"{k['name']} ({k['count']:,} files)") for k in r["kinds"]])
    section("Inside this folder", [(c["size"], c["path"]) for c in r["children"]])
    section("Biggest folders deeper down", [(c["size"], c["path"]) for c in r["deeper"]])
    section("Biggest files", [(f["size"], f"{f['path']}  ({f['age_days']}d)") for f in r["biggest"]])
    section(f"Big files (100 MB+) not touched in {OLD_DAYS} days", [(f["size"], f["path"]) for f in r["old"]])
    print()


# ---------------------------------------------------------------------------
# The browser page
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Disk Space Reporter</title>
<style>
  :root { --bg:#14161a; --card:#1d2026; --line:#2c3038; --text:#e8eaed; --dim:#9aa0a8; --accent:#4aa3df; --bar:#2f6f9f; }
  @media (prefers-color-scheme: light) { :root { --bg:#f4f5f7; --card:#fff; --line:#dcdfe4; --text:#1b1d21; --dim:#667; --bar:#8cc3e8; } }
  * { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text); font:15px/1.4 system-ui,sans-serif }
  main { max-width:1000px; margin:0 auto; padding:24px 16px 60px }
  h1 { margin:0 0 4px; font-size:22px } h2 { margin:26px 0 8px; font-size:16px }
  .sub { color:var(--dim); margin-bottom:18px }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden }
  .controls { padding:14px; display:flex; gap:8px; flex-wrap:wrap; align-items:center }
  input[type=text], select { padding:8px 10px; border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--text); font:inherit }
  input[type=text] { flex:1; min-width:240px } select { max-width:100% }
  button { font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:transparent; color:var(--text); cursor:pointer }
  button.go { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600 }
  button:disabled { opacity:.4; cursor:default }
  #progress { padding:0 14px 14px; color:var(--dim); display:none; word-break:break-all }
  #msg { margin:14px 0; padding:10px 14px; border-radius:8px; border:1px solid var(--line); display:none }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-top:18px }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px }
  .stat b { display:block; font-size:20px } .stat span { color:var(--dim); font-size:13px }
  table { width:100%; border-collapse:collapse } th,td { padding:8px 12px; text-align:left; border-bottom:1px solid var(--line); vertical-align:middle }
  th { color:var(--dim); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em }
  tr:last-child td { border-bottom:0 } td.num { white-space:nowrap; color:var(--dim); width:1% } td.path { word-break:break-all }
  .barwrap { width:140px } .bar { height:10px; border-radius:5px; background:var(--bar) }
  .empty { padding:18px; color:var(--dim) }
</style></head><body><main>
  <h1>Disk Space Reporter</h1>
  <div class="sub">See what is eating your disk. It only reads. It never deletes or changes anything.</div>
  <div class="card">
    <div class="controls">
      <select id="where" title="Pick what to scan"></select>
      <input type="text" id="path" spellcheck="false" placeholder="Or type any folder to scan">
      <button id="scan" class="go">Scan</button><button id="cancel" disabled>Stop</button>
    </div>
    <div id="progress"></div>
  </div>
  <div id="msg"></div>
  <div id="results"></div>
  <p class="sub" style="margin-top:30px"><button id="csv" disabled>Download biggest files (CSV)</button> <button id="quit">Quit</button></p>
</main>
<script>
const TOKEN = "__TOKEN__", LOCS = __LOCATIONS__;
const $ = id => document.getElementById(id);
const fmt = n => { const u=["B","KB","MB","GB","TB"]; let i=0; while(n>=1024&&i<4){n/=1024;i++} return (i?n.toFixed(1):Math.round(n))+" "+u[i]; };
const age = d => d >= 365 ? (d/365).toFixed(1)+" yr" : d >= 30 ? Math.round(d/30)+" mo" : d+" d";
function msg(t) { const m=$("msg"); m.textContent=t; m.style.display=t?"block":"none"; }

async function api(path, body) {
  const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({token:TOKEN, ...body})} : {});
  return r.json();
}
function cell(text, cls) { const td=document.createElement("td"); td.textContent=text; if (cls) td.className=cls; return td; }
function bar(pct) { const td=document.createElement("td"); td.className="barwrap"; const b=document.createElement("div"); b.className="bar"; b.style.width=Math.max(2,pct)+"%"; td.append(b); return td; }
function section(title, node) { const h=document.createElement("h2"); h.textContent=title; $("results").append(h); const c=document.createElement("div"); c.className="card"; c.append(node); $("results").append(c); }
function empty(t) { const d=document.createElement("div"); d.className="empty"; d.textContent=t; return d; }
function table(heads, rows) {
  if (!rows.length) return empty("Nothing to show.");
  const t=document.createElement("table"); const hr=document.createElement("tr");
  heads.forEach(h => { const th=document.createElement("th"); th.textContent=h; hr.append(th); }); t.append(hr);
  rows.forEach(cells => { const tr=document.createElement("tr"); cells.forEach(c => tr.append(c)); t.append(tr); });
  return t;
}
function pathCell(p) { const td=cell(p,"path"); td.title=p; return td; }

function render(r) {
  $("results").textContent = "";
  const stats = document.createElement("div"); stats.className = "stats";
  const items = [[fmt(r.bytes), "found in "+r.files.toLocaleString()+" files"], [r.seconds+" s", "scan time"]];
  if (r.drive) items.push([fmt(r.drive.free), "free of "+fmt(r.drive.total)+" on this drive"]);
  if (r.skipped) items.push([r.skipped, "places couldn't be read (try running as administrator)"]);
  if (r.cancelled) items.push(["Stopped", "partial results"]);
  items.forEach(([b, s]) => { const d=document.createElement("div"); d.className="stat"; const bb=document.createElement("b"); bb.textContent=b; const sp=document.createElement("span"); sp.textContent=s; d.append(bb, sp); stats.append(d); });
  $("results").append(stats);

  const maxKind = Math.max(1, ...r.kinds.map(k=>k.size));
  section("Space by type", table(["Type","Files","Size",""], r.kinds.map(k => [cell(k.name), cell(k.count.toLocaleString(),"num"), cell(fmt(k.size),"num"), bar(k.size/maxKind*100)])));
  const maxC = Math.max(1, ...r.children.map(c=>c.size));
  section("Inside this folder", table(["Folder","Size",""], r.children.map(c => [pathCell(c.name || c.path), cell(fmt(c.size),"num"), bar(c.size/maxC*100)])));
  section("Biggest folders deeper down", table(["Folder","Size"], r.deeper.map(c => [pathCell(c.path), cell(fmt(c.size),"num")])));
  section("Biggest files", table(["File","Size","Last changed"], r.biggest.map(f => [pathCell(f.path), cell(fmt(f.size),"num"), cell(age(f.age_days)+" ago","num")])));
  section("Big files (100 MB+) not touched in a year", table(["File","Size","Last changed"], r.old.map(f => [pathCell(f.path), cell(fmt(f.size),"num"), cell(age(f.age_days)+" ago","num")])));
  $("csv").disabled = false;
}

function setBusy(b) { $("scan").disabled=b; $("cancel").disabled=!b; $("where").disabled=b; }

async function poll() {
  const s = await api("/api/status?token="+TOKEN);
  const p = $("progress");
  if (s.state === "running") {
    p.style.display="block"; p.textContent = s.files.toLocaleString()+" files, "+fmt(s.bytes)+" so far. Looking in: "+s.current;
    setTimeout(poll, 600);
  } else {
    p.style.display="none"; setBusy(false);
    if (s.state === "done") { msg(""); render(s.result); }
    else if (s.state === "error") msg(s.error);
  }
}

const sel = $("where");
LOCS.forEach(l => { const o=document.createElement("option"); o.value=l.path; o.textContent=l.label; sel.append(o); });
{ const o=document.createElement("option"); o.value=""; o.textContent="Other folder (type it in the box)"; sel.append(o); }
const syncSelect = () => { const m = LOCS.find(l => l.path === $("path").value.trim()); sel.value = m ? m.path : ""; };
sel.onchange = () => { if (sel.value) $("path").value = sel.value; else $("path").focus(); };
$("path").addEventListener("input", syncSelect);
$("scan").onclick = async () => {
  msg(""); $("csv").disabled = true;
  const r = await api("/api/start", {path: $("path").value.trim()});
  if (!r.ok) { msg(r.error); return; }
  setBusy(true); $("results").textContent = ""; poll();
};
$("cancel").onclick = () => api("/api/cancel", {});
$("csv").onclick = () => { location.href = "/api/csv?token="+TOKEN; };
$("quit").onclick = async () => { await api("/api/quit", {}); document.body.innerHTML = "<main><h1>Done. You can close this tab.</h1></main>"; };
$("path").value = __START__;
syncSelect();
$("path").addEventListener("keydown", e => { if (e.key === "Enter") $("scan").click(); });
</script></body></html>
"""


def make_handler(state):
    """state: dict with token, port, start_path, top, scan, thread, result, error, server."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _host_ok(self):
            # Blocks other websites from talking to this local server
            return self.headers.get("Host", "") in (f"127.0.0.1:{state['port']}", f"localhost:{state['port']}")

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "{}")
            path, _, query = self.path.partition("?")
            if path == "/":
                page = (PAGE.replace("__TOKEN__", state["token"])
                        .replace("__LOCATIONS__", json.dumps(list_locations()).replace("<", "\\u003c"))
                        .replace("__START__", json.dumps(state["start_path"])))
                return self._send(200, page, "text/html")
            if f"token={state['token']}" not in query:
                return self._send(403, "{}")
            if path == "/api/status":
                scan = state["scan"]
                if scan and state["thread"] and state["thread"].is_alive():
                    return self._json({"state": "running", "files": scan.files, "bytes": scan.bytes,
                                       "current": scan.current})
                if state["error"]:
                    return self._json({"state": "error", "error": state["error"]})
                if state["result"]:
                    return self._json({"state": "done", "result": state["result"]})
                return self._json({"state": "idle"})
            if path == "/api/csv" and state["result"]:
                return self._send(200, csv_text(state["result"]), "text/csv",
                                  {"Content-Disposition": 'attachment; filename="biggest-files.csv"'})
            self._send(404, "{}")

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, "{}")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._send(400, "{}")
            if body.get("token") != state["token"]:
                return self._send(403, "{}")

            if self.path == "/api/start":
                if state["thread"] and state["thread"].is_alive():
                    return self._json({"ok": False, "error": "A scan is already running."})
                folder = os.path.expanduser(str(body.get("path", "")).strip().strip('"'))
                if not folder or not os.path.isdir(folder):
                    return self._json({"ok": False, "error": "That folder doesn't exist. Check the path and try again."})
                scan = Scan(folder, state["top"])
                state.update(scan=scan, result=None, error=None)

                def work():
                    try:
                        scan.run()
                        state["result"] = scan.result()
                    except Exception as e:  # show it on the page instead of dying quietly
                        state["error"] = f"Scan failed: {e}"

                t = threading.Thread(target=work, daemon=True)
                state["thread"] = t
                t.start()
                return self._json({"ok": True})

            if self.path == "/api/cancel":
                if state["scan"]:
                    state["scan"].cancelled = True
                return self._json({"ok": True})

            if self.path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=state["server"].shutdown, daemon=True).start()
                return
            self._send(404, "{}")

    return Handler


def run_web(start_path, top, open_browser, port):
    state = {"token": secrets.token_urlsafe(16), "port": port, "start_path": start_path, "top": top,
             "scan": None, "thread": None, "result": None, "error": None}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    state["port"] = server.server_address[1]
    state["server"] = server
    url = f"http://127.0.0.1:{state['port']}/"
    print(f"Page is running at {url}  (Ctrl+C or the Quit button to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="See what is eating your disk space. Read-only.")
    ap.add_argument("path", nargs="?", help="folder to start with (default: your user folder)")
    ap.add_argument("--text", action="store_true", help="scan and print the report in the terminal (no page)")
    ap.add_argument("--csv", metavar="FILE", help="with --text: also save the biggest files to this CSV")
    ap.add_argument("--top", type=int, default=25, help="rows per list (default 25)")
    ap.add_argument("--no-browser", action="store_true", help="start the page but don't open it")
    ap.add_argument("--port", type=int, default=0, help="port for the page (default: any free one)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()

    start = args.path or str(Path.home())
    if args.text:
        if not os.path.isdir(start):
            sys.exit(f"Not a folder: {start}")
        print(f"Scanning {start} ...")
        scan = Scan(start, args.top)
        try:
            scan.run()
        except KeyboardInterrupt:
            scan.cancelled = True
            scan.finished = time.time()
        result = scan.result()
        print_text(result)
        if args.csv:
            Path(args.csv).write_text(csv_text(result), encoding="utf-8", newline="")
            print(f"Saved {args.csv}")
        return
    run_web(start, args.top, not args.no_browser, args.port)


if __name__ == "__main__":
    main()
