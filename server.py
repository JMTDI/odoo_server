#!/usr/bin/env python3
"""
Odoo Auto-Installer & Host Script
- Requires ONLY git + python3 in PATH.
- Uses 'pgserver' (pip) to run a fully embedded Postgres — no system PG needed.
- Clones Odoo 17, installs pip deps, writes config, runs on port 8000.
- Serves /files file-manager + live log on :8000/files (proxies Odoo on :8001).
"""

import os
import subprocess
import sys
import shutil
import importlib
import importlib.util
import site
import re
import tempfile
import threading
import zipfile
import json
import io
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
import http.client

# ── Config ────────────────────────────────────────────────────────────────────
ODOO_BRANCH   = "17.0"
ODOO_DIR      = os.path.join(os.path.expanduser("~"), "odoo")
ODOO_PORT     = 8000          # public port  (proxy + file manager)
ODOO_INTERNAL = 8001          # Odoo binds here, never exposed directly
ODOO_CONF     = os.path.join(os.path.expanduser("~"), "odoo.conf")
DB_USER       = "odoo"
DB_PASSWORD   = "odoo_pass_2026"
DB_NAME       = "odoo"
PG_DATA_DIR   = os.path.join(os.path.expanduser("~"), "pgdata")
ADDONS_EXTRA  = os.path.join(os.path.expanduser("~"), "odoo_addons")
# ─────────────────────────────────────────────────────────────────────────────

PATCH_DEPS = {
    r"^python-ldap\b":         "ldap3",
    r"^psycopg2\b(?!-binary)": "psycopg2-binary",
}

# ── In-memory log buffer ──────────────────────────────────────────────────────
_log_lines = []
_log_lock  = threading.Lock()

class _LogTee(io.TextIOBase):
    """Tee stdout/stderr into _log_lines and the original stream."""
    def __init__(self, original):
        self._orig = original
    def write(self, s):
        if s:
            with _log_lock:
                for line in s.splitlines(keepends=True):
                    _log_lines.append(line)
                    if len(_log_lines) > 2000:
                        _log_lines.pop(0)
        self._orig.write(s)
        self._orig.flush()
        return len(s)
    def flush(self):
        self._orig.flush()

sys.stdout = _LogTee(sys.stdout)
sys.stderr = _LogTee(sys.stderr)

# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd, check=True):
    display = " ".join(str(c) for c in cmd)
    print("\n▶  " + display)
    subprocess.run(cmd, check=check)

def step(msg):
    print("\n" + "═" * 60)
    print("  " + msg)
    print("═" * 60)

def pip_install(*packages, extra_args=None):
    extra = extra_args or []
    run([sys.executable, "-m", "pip", "install", "--quiet", *extra, *packages])
    importlib.invalidate_caches()
    for path in site.getsitepackages():
        if path not in sys.path:
            sys.path.insert(0, path)
    user_site = site.getusersitepackages()
    if user_site not in sys.path:
        sys.path.insert(0, user_site)

def import_or_install(module_name, pip_name=None):
    pip_name = pip_name or module_name
    if importlib.util.find_spec(module_name) is None:
        pip_install(pip_name)
    importlib.invalidate_caches()
    return importlib.import_module(module_name)

def patch_requirements(src_path, dst_path):
    written_replacements = set()
    with open(src_path) as f_in, open(dst_path, "w") as f_out:
        for raw_line in f_in:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                f_out.write(raw_line)
                continue
            replaced = False
            for pattern, replacement in PATCH_DEPS.items():
                if re.match(pattern, line, re.IGNORECASE):
                    if replacement is None:
                        f_out.write("# dropped (no pure wheel): " + line + "\n")
                    elif replacement not in written_replacements:
                        f_out.write(replacement + "\n")
                        written_replacements.add(replacement)
                    else:
                        f_out.write("# already added: " + replacement + "\n")
                    replaced = True
                    break
            if not replaced:
                f_out.write(raw_line)

def patch_pkg_resources(odoo_dir):
    import_re = re.compile(r"^\s*(import pkg_resources|from pkg_resources\b)")
    usage_re  = re.compile(r"\bPkgResourcesDeprecationWarning\b")
    patched = []
    for root, dirs, files in os.walk(odoo_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            new_lines = []
            changed = False
            for line in lines:
                if import_re.match(line) or usage_re.search(line):
                    new_lines.append("# patched-out (pkg_resources): " + line)
                    changed = True
                else:
                    new_lines.append(line)
            if changed:
                with open(fpath, "w", encoding="utf-8") as fh:
                    fh.writelines(new_lines)
                patched.append(os.path.relpath(fpath, odoo_dir))
    print("  ✓  pkg_resources patched in " + str(len(patched)) + " file(s)")

def parse_pgserver_connection(pg):
    raw_uri = pg.get_uri("postgres")
    print("  pgserver URI: " + raw_uri)
    parsed = urlparse(raw_uri)
    if parsed.hostname and parsed.port:
        print("  mode: TCP  host=" + parsed.hostname + "  port=" + str(parsed.port))
        return parsed.hostname, parsed.port
    qs = parse_qs(parsed.query)
    if "host" in qs:
        socket_dir = qs["host"][0]
        print("  mode: Unix socket  dir=" + socket_dir)
        return socket_dir, None
    raise RuntimeError("Cannot determine pgserver connection from URI: " + raw_uri)

def is_db_initialized(psycopg2, pg):
    try:
        conn = psycopg2.connect(pg.get_uri(DB_NAME))
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'ir_module_module');"
        )
        result = cur.fetchone()[0]
        conn.close()
        return result
    except Exception as e:
        print("  DB check notice: " + str(e))
        return False

def update_addons_path():
    """Rewrite addons_path in odoo.conf to include all addon subdirs of ADDONS_EXTRA."""
    if not os.path.exists(ODOO_CONF):
        return
    extra_dirs = [ADDONS_EXTRA]
    if os.path.isdir(ADDONS_EXTRA):
        for name in sorted(os.listdir(ADDONS_EXTRA)):
            full = os.path.join(ADDONS_EXTRA, name)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, "__manifest__.py")):
                extra_dirs.append(full)
    addons_path = ODOO_DIR + "/addons," + ",".join(extra_dirs)
    with open(ODOO_CONF) as f:
        content = f.read()
    content = re.sub(r"(?m)^addons_path\s*=.*$", "addons_path  = " + addons_path, content)
    with open(ODOO_CONF, "w") as f:
        f.write(content)

# ── File-Manager HTML ─────────────────────────────────────────────────────────

def _fm_html(rel_path, entries):
    parts = [""] + [p for p in rel_path.split("/") if p]
    breadcrumbs = ""
    for i, part in enumerate(parts):
        href  = "/files/" + "/".join(parts[1:i+1])
        label = "addons" if i == 0 else part
        if i == len(parts) - 1:
            breadcrumbs += '<span style="font-weight:bold">' + label + "</span>"
        else:
            breadcrumbs += '<a href="' + href + '">' + label + "</a> / "

    rows = ""
    if rel_path:
        parent = "/".join(rel_path.rstrip("/").split("/")[:-1])
        rows += "<tr><td><a href='/files/" + parent + "'>⬆ ..</a></td><td></td><td></td></tr>"
    for name, is_dir, size in entries:
        href     = "/files/" + (rel_path + "/" if rel_path else "") + quote(name)
        icon     = "📁" if is_dir else "📄"
        sz       = "" if is_dir else "{:,} B".format(size)
        dl       = "" if is_dir else "<a href='/files-dl/" + (rel_path + "/" if rel_path else "") + quote(name) + "'>⬇</a>"
        del_path = quote((rel_path + "/" if rel_path else "") + name)
        rows += (
            "<tr>"
            "<td>" + icon + " <a href='" + href + "'>" + name + "</a></td>"
            "<td>" + sz + "</td>"
            "<td>" + dl + " <a style='color:#cb2431' href='/files-delete?path=" + del_path + "' "
            "onclick=\"return confirm('Delete " + name.replace("'", "\\'") + "?')\">🗑</a></td>"
            "</tr>"
        )

    return """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Addon File Manager</title>
<style>
  body{font-family:sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}
  h1{color:#333}
a{color:#0366d6;text-decoration:none} a:hover{text-decoration:underline}
table{width:100%;border-collapse:collapse;margin-top:1rem}
th{background:#f6f8fa;text-align:left;padding:.5rem .75rem;border-bottom:2px solid #e1e4e8}
td{padding:.4rem .75rem;border-bottom:1px solid #eaecef;word-break:break-all}
.card{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:1rem;margin-top:1.5rem}
input[type=text]{padding:.4rem;border:1px solid #ccc;border-radius:4px;width:220px}
button{padding:.4rem .9rem;background:#2ea44f;color:#fff;border:none;border-radius:4px;cursor:pointer}
button:hover{background:#22863a}
#drop{border:2px dashed #0366d6;border-radius:6px;padding:2rem;text-align:center;
        color:#0366d6;margin-top:1rem;cursor:pointer;transition:background .15s}
#drop.over{background:#e8f4fd}
#status{margin-top:.75rem;font-weight:bold;min-height:1.4em}
#logbox{background:#111;color:#0f0;font-family:monospace;font-size:.78rem;
          height:260px;overflow-y:auto;padding:.6rem;border-radius:4px;white-space:pre-wrap;margin-top:.5rem}
</style>
</head><body>
<h1>📦 Addon File Manager</h1>
<p>📍 """ + breadcrumbs + """</p>

<table>
  <thead><tr><th>Name</th><th>Size</th><th>Actions</th></tr></thead>
  <tbody>" + rows + "</tbody>
</table>

<div class="card">
  <strong>New folder</strong><br><br>
  <form method="POST" action="/files-mkdir">
    <input type="hidden" name="path" value=""" + rel_path + "">
    <input type="text" name="name" placeholder="folder-name" required>
    <button type="submit">Create</button>
  </form>
</div>

<div class="card">
  <strong>Upload file or .zip addon (zip is auto-extracted)</strong>
  <div id="drop">Drop files here or click to browse
    <input type="file" id="fi" multiple style="display:none">
  </div>
  <div id="status"></div>
</div>

<div class="card">
  <strong>Server log</strong>
  <div id="logbox">Loading…</div>
</div>

<script>
var CUR_PATH = """ + rel_path + """;
var drop   = document.getElementById('drop');
var fi     = document.getElementById('fi');
var status = document.getElementById('status');

drop.addEventListener('click', function(){ fi.click(); });
drop.addEventListener('dragover', function(e){ e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', function(){ drop.classList.remove('over'); });
drop.addEventListener('drop', function(e){
  e.preventDefault(); drop.classList.remove('over');
  uploadFiles(e.dataTransfer.files);
});
fi.addEventListener('change', function(){ uploadFiles(fi.files); });

function uploadFiles(files) {
  var arr = Array.from(files);
  if (!arr.length) return;
  var idx = 0;
  function next() {
    if (idx >= arr.length) {
      status.textContent = '✓ All done — reloading…';
      setTimeout(function(){ location.reload(); }, 900);
      return;
    }
    var file = arr[idx++];
    status.textContent = 'Uploading ' + file.name + ' (' + idx + '/' + arr.length + ')…';
    var fd = new FormData();
    fd.append('path', CUR_PATH);
    fd.append('file', file, file.name);
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/files-upload');
    xhr.onload = function() {
      var resp;
      try { resp = JSON.parse(xhr.responseText); } catch(e) { resp = {ok:false, error:xhr.responseText}; }
      if (resp.ok) {
        status.textContent = '✓ ' + file.name + ' uploaded';
        next();
      } else {
        status.textContent = '✗ Error: ' + (resp.error || 'unknown');
      }
    };
    xhr.onerror = function(){ status.textContent = '✗ Network error'; };
    xhr.send(fd);
  }
  next();
}

// ── Live log ──────────────────────────────────────────────────────────────────
var logbox = document.getElementById('logbox');
function fetchLog() {
  fetch('/files-log')
    .then(function(r){ return r.text(); })
    .then(function(t){
      logbox.textContent = t;
      logbox.scrollTop = logbox.scrollHeight;
    })
    .catch(function(){});
}
fetchLog();
setInterval(fetchLog, 2500);
</script>
</body></html>"""

# ── Multipart parser ──────────────────────────────────────────────────────────

def parse_multipart(raw_bytes, boundary_bytes):
    """
    Parse a multipart/form-data body.
    Returns dict: field_name -> (filename_or_None, bytes_value)
    """
    fields = {}
    delim = b"--" + boundary_bytes
    parts = raw_bytes.split(delim)
    for part in parts:
        # strip leading CRLF
        if part in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if b"\r\n\r\n" not in part:
            continue
        header_raw, _, body = part.partition(b"\r\n\r\n")
        # strip trailing CRLF / closing --
        if body.endswith(b"\r\n"):
            body = body[:-2]
        header_str = header_raw.decode("utf-8", errors="replace")
        nm = re.search(r'name="([^"]*)"', header_str)
        fn = re.search(r'filename="([^"]*)"', header_str)
        if not nm:
            continue
        fields[nm.group(1)] = (fn.group(1) if fn else None, body)
    return fields

# ── Request Handler ───────────────────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence access log

    def _safe_path(self, rel):
        rel  = unquote(rel).lstrip("/")
        full = os.path.realpath(os.path.join(ADDONS_EXTRA, rel))
        base = os.path.realpath(ADDONS_EXTRA)
        if not full.startswith(base):
            return None
        return full

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        # live log endpoint
        if path == "/files-log":
            with _log_lock:
                text = "".join(_log_lines)
            self._reply(200, "text/plain; charset=utf-8", text.encode("utf-8", errors="replace"))
            return

        # file download
        if path.startswith("/files-dl/"):
            rel  = path[len("/files-dl/"):]  
            full = self._safe_path(rel)
            if not full or not os.path.isfile(full):
                self._reply(404, "text/plain", b"Not found")
                return
            with open(full, "rb") as fh:
                data = fh.read()
            fname = os.path.basename(full)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="' + fname + '"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # delete (via GET with confirmation in JS)
        if path == "/files-delete":
            qs   = parse_qs(parsed.query)
            rel  = qs.get("path", [""])[0]
            full = self._safe_path(rel)
            if not full or not os.path.exists(full):
                self._reply(404, "text/plain", b"Not found")
                return
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            update_addons_path()
            parent = "/".join(rel.strip("/").split("/")[:-1])
            self._redirect("/files/" + parent)
            return

        # file-manager browser
        if path.startswith("/files"):
            rel  = path[len("/files"):].lstrip("/")
            full = self._safe_path(rel)
            if not full:
                self._reply(403, "text/plain", b"Forbidden")
                return
            if os.path.isfile(full):
                with open(full, "rb") as fh:
                    data = fh.read()
                self._reply(200, "application/octet-stream", data)
                return
            os.makedirs(full, exist_ok=True)
            entries = []
            for name in sorted(os.listdir(full)):
                fp = os.path.join(full, name)
                entries.append((name, os.path.isdir(fp),
                                 0 if os.path.isdir(fp) else os.path.getsize(fp)))
            self._reply(200, "text/html; charset=utf-8", _fm_html(rel, entries).encode("utf-8"))
            return

        self._proxy()

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/files-mkdir":
            body   = self._read_urlform()
            parent = body.get("path", [""])[0]
            name   = body.get("name", [""])[0].strip().replace("/", "_")
            if not name:
                self._reply(400, "text/plain", b"Bad name")
                return
            full = self._safe_path(parent + "/" + name)
            if full:
                os.makedirs(full, exist_ok=True)
            self._redirect("/files/" + parent)
            return

        if path == "/files-upload":
            ctype  = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length)

            # extract boundary
            boundary = None
            for part in ctype.split(";"):
                part = part.strip()
                if part.lower().startswith("boundary="):
                    boundary = part[9:].strip('"\'').encode()
                    break

            if not boundary:
                self._json({"ok": False, "error": "no multipart boundary found"})
                return

            fields = parse_multipart(raw, boundary)

            path_field = fields.get("path")
            file_field = fields.get("file")

            if not file_field or not file_field[0]:
                self._json({"ok": False, "error": "no file in upload"})
                return

            rel_path  = path_field[1].decode("utf-8", errors="replace") if path_field else ""
            filename  = file_field[0]
            file_data = file_field[1]

            dest_dir = self._safe_path(rel_path)
            if not dest_dir:
                self._json({"ok": False, "error": "invalid path"})
                return
            os.makedirs(dest_dir, exist_ok=True)

            if filename.lower().endswith(".zip"):
                tmp = os.path.join(tempfile.gettempdir(), filename)
                with open(tmp, "wb") as fh:
                    fh.write(file_data)
                try:
                    with zipfile.ZipFile(tmp, "r") as zf:
                        zf.extractall(dest_dir)
                    print("  ✓  Extracted zip: " + filename + " → " + dest_dir)
                except zipfile.BadZipFile as e:
                    self._json({"ok": False, "error": "bad zip: " + str(e)})
                    return
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            else:
                dest = os.path.join(dest_dir, filename)
                with open(dest, "wb") as fh:
                    fh.write(file_data)
                print("  ✓  Uploaded: " + filename + " → " + dest)

            update_addons_path()
            self._json({"ok": True})
            return

        self._proxy()

    def do_PUT(self):     self._proxy()
    def do_DELETE(self):  self._proxy()
    def do_PATCH(self):   self._proxy()
    def do_HEAD(self):    self._proxy()
    def do_OPTIONS(self): self._proxy()

    # ── internals ─────────────────────────────────────────────────────────────

    def _proxy(self):
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "connection", "transfer-encoding")}
        headers["Host"] = "127.0.0.1:" + str(ODOO_INTERNAL)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", ODOO_INTERNAL, timeout=120)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(("Proxy error: " + str(e)).encode())

    def _reply(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self._reply(200, "application/json", data)

    def _read_urlform(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw)


def start_proxy():
    server = HTTPServer(("0.0.0.0", ODOO_PORT), ProxyHandler)
    print("  ✓  Proxy+FileManager on :" + str(ODOO_PORT) + "  →  Odoo on :" + str(ODOO_INTERNAL))
    server.serve_forever()

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
step("1 / 5 · Checking prerequisites")
for tool in ("git", "python3"):
    p = shutil.which(tool)
    if not p:
        print("  ✗  '" + tool + "' not found in PATH — cannot continue.")
        sys.exit(1)
    print("  ✓  " + tool + " → " + p)

# ── 2. Install pip packages ───────────────────────────────────────────────────
step("2 / 5 · Installing core pip packages")
run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
pip_install("setuptools", "wheel")
pip_install("psycopg2-binary", "pgserver", "libsass")

psycopg2 = import_or_install("psycopg2", "psycopg2-binary")
pgserver  = import_or_install("pgserver", "pgserver")
print("  ✓  psycopg2 + pgserver imported")

# ── 3. Start embedded PostgreSQL ──────────────────────────────────────────────
step("3 / 5 · Starting embedded PostgreSQL (pgserver)")
os.makedirs(PG_DATA_DIR, exist_ok=True)
pg = pgserver.get_server(PG_DATA_DIR, cleanup_mode="stop")
print("  ✓  Postgres running")

pg_host, pg_port = parse_pgserver_connection(pg)

def pg_exec(sql):
    try:
        conn = psycopg2.connect(pg.get_uri("postgres"))
        conn.autocommit = True
        conn.cursor().execute(sql)
        conn.close()
        print("  SQL ok: " + sql[:80])
    except Exception as e:
        print("  SQL notice (non-fatal): " + str(e))

pg_exec("CREATE ROLE " + DB_USER + " LOGIN CREATEDB PASSWORD '" + DB_PASSWORD + "';")
pg_exec("CREATE DATABASE " + DB_NAME + " OWNER " + DB_USER + ";")

# ── 4. Clone Odoo & install Python deps ──────────────────────────────────────
step("4 / 5 · Cloning Odoo & installing Python requirements")
if not os.path.exists(ODOO_DIR):
    run(["git", "clone", "--depth", "1", "--branch", ODOO_BRANCH,
         "https://github.com/odoo/odoo.git", ODOO_DIR])
else:
    print("  Odoo already at " + ODOO_DIR + " — skipping clone.")

os.makedirs(ADDONS_EXTRA, exist_ok=True)

req_file    = os.path.join(ODOO_DIR, "requirements.txt")
patched_req = os.path.join(tempfile.gettempdir(), "odoo_requirements_patched.txt")
patch_requirements(req_file, patched_req)
run([sys.executable, "-m", "pip", "install", "--quiet",
     "--no-warn-script-location", "-r", patched_req])

pip_install("setuptools", "wheel", extra_args=["--force-reinstall"])
print("  ✓  setuptools force-reinstalled")

print("\n  Scanning Odoo source for pkg_resources references...")
patch_pkg_resources(ODOO_DIR)

# ── 5. Write config & launch Odoo ────────────────────────────────────────────
step("5 / 5 · Writing odoo.conf & launching Odoo on internal port " + str(ODOO_INTERNAL))

if pg_port is None:
    db_conn_lines = "db_host      = " + pg_host + "\n"
    print("  socket mode → db_host=" + pg_host)
else:
    db_conn_lines = "db_host      = " + pg_host + "\ndb_port      = " + str(pg_port) + "\n"
    print("  tcp mode    → db_host=" + pg_host + "  db_port=" + str(pg_port))

conf_content = (
    "[options]\n"
    "admin_passwd = admin\n"
    + db_conn_lines
    + "db_user      = " + DB_USER + "\n"
    + "db_password  = " + DB_PASSWORD + "\n"
    + "db_name      = " + DB_NAME + "\n"
    + "addons_path  = " + ODOO_DIR + "/addons," + ADDONS_EXTRA + "\n"
    + "logfile      = False\n"
    + "xmlrpc_port  = " + str(ODOO_INTERNAL) + "\n"
)
with open(ODOO_CONF, "w") as f:
    f.write(conf_content)
print("  Config written → " + ODOO_CONF)

# ── Start proxy thread ────────────────────────────────────────────────────────
t = threading.Thread(target=start_proxy, daemon=True)
t.start()
print("\n  🌐  Odoo public URL  → http://0.0.0.0:" + str(ODOO_PORT))
print("  📁  File manager     → http://0.0.0.0:" + str(ODOO_PORT) + "/files\n")

# ── Check if DB needs initialising ───────────────────────────────────────────
db_initialized = is_db_initialized(psycopg2, pg)
if db_initialized:
    print("  ✓  Database already initialized — starting normally")
    extra_args = []
else:
    print("  ⚠  Database not initialized — running with -i base (first run)")
    extra_args = ["-i", "base"]

odoo_bin = os.path.join(ODOO_DIR, "odoo-bin")
os.chdir(ODOO_DIR)
# subprocess.run (not os.execv) keeps the proxy daemon thread alive
subprocess.run([
    sys.executable, odoo_bin,
    "--config", ODOO_CONF,
    "--http-port", str(ODOO_INTERNAL),
    *extra_args,
])