#!/usr/bin/env python3
"""
Odoo Auto-Installer & Host Script
- Requires ONLY git + python3 in PATH.
- Uses 'pgserver' (pip) to run a fully embedded Postgres — no system PG needed.
- Clones Odoo 17, installs pip deps, writes config, runs on port 8000.
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

# ── Config ────────────────────────────────────────────────────────────────────
ODOO_BRANCH = "17.0"
ODOO_DIR    = os.path.join(os.path.expanduser("~"), "odoo")
ODOO_PORT   = 8000
ODOO_CONF   = os.path.join(os.path.expanduser("~"), "odoo.conf")
DB_USER     = "odoo"
DB_PASSWORD = "odoo_pass_2026"
DB_NAME     = "odoo"
PG_DATA_DIR = os.path.join(os.path.expanduser("~"), "pgdata")
# ──────────────────────────────────────────────────────────────────────────────

# Packages that need C compilation / system libs, mapped to pip-installable
# pure-Python (or pre-built-wheel) alternatives that Odoo 17 accepts.
PATCH_DEPS = {
    r"^python-ldap\b":  "ldap3",
    r"^psycopg2\b(?!-binary)": "psycopg2-binary",
    r"^libsass\b":      None,   # None -> drop the line
}

def run(cmd, check=True):
    display = " ".join(str(c) for c in cmd)
    print("\n▶  " + display)
    subprocess.run(cmd, check=check)

def step(msg):
    print("\n" + "═" * 60)
    print("  " + msg)
    print("═" * 60)

def pip_install(*packages, extra_args=None):
    """Install packages and immediately make them importable in this process."""
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
    """
    Read src_path and write dst_path with problematic C-extension packages
    replaced by their pure-Python / pre-built-wheel equivalents.
    """
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
    """
    Walk every .py file under odoo_dir and comment out any line that does:
      import pkg_resources
      from pkg_resources import ...
    These are all optional (deprecation warnings / metadata queries) and safe
    to remove.  This is the only reliable fix when setuptools is stripped by
    the host environment after pip install.
    """
    import_re = re.compile(
        r"^(\s*)(import pkg_resources|from pkg_resources import)\b"
    )
    patched = []
    for root, dirs, files in os.walk(odoo_dir):
        # skip hidden dirs and __pycache__
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
                if import_re.match(line):
                    new_lines.append("# patched-out (pkg_resources): " + line)
                    changed = True
                else:
                    new_lines.append(line)
            if changed:
                with open(fpath, "w", encoding="utf-8") as fh:
                    fh.writelines(new_lines)
                rel = os.path.relpath(fpath, odoo_dir)
                patched.append(rel)
                print("  patched: " + rel)
    print("  ✓  pkg_resources imports commented out in " + str(len(patched)) + " file(s)")

# ── 1. Check prerequisites ────────────────────────────────────────────────────
step("1 / 5 · Checking prerequisites")
for tool in ("git", "python3"):
    path = shutil.which(tool)
    if not path:
        print("  ✗  '" + tool + "' not found in PATH — cannot continue.")
        sys.exit(1)
    print("  ✓  " + tool + " → " + path)

# ── 2. Install pip packages ───────────────────────────────────────────────────
step("2 / 5 · Installing core pip packages")
run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
pip_install("setuptools", "wheel")
pip_install("psycopg2-binary")
pip_install("pgserver")   # self-contained Postgres server (no system PG needed)

psycopg2 = import_or_install("psycopg2", "psycopg2-binary")
pgserver  = import_or_install("pgserver", "pgserver")
print("  ✓  psycopg2 + pgserver imported")

# ── 3. Start embedded PostgreSQL via pgserver ─────────────────────────────────
step("3 / 5 · Starting embedded PostgreSQL (pgserver)")

os.makedirs(PG_DATA_DIR, exist_ok=True)
pg = pgserver.get_server(PG_DATA_DIR, cleanup_mode="stop")
print("  ✓  Postgres running — URI: " + pg.get_uri())

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

pg_host = "127.0.0.1"
pg_port = getattr(pg, "pg_port", 5432)

# ── 4. Clone Odoo & install Python deps ──────────────────────────────────────
step("4 / 5 · Cloning Odoo & installing Python requirements")
if not os.path.exists(ODOO_DIR):
    run([
        "git", "clone", "--depth", "1",
        "--branch", ODOO_BRANCH,
        "https://github.com/odoo/odoo.git",
        ODOO_DIR,
    ])
else:
    print("  Odoo already at " + ODOO_DIR + " — skipping clone.")

req_file    = os.path.join(ODOO_DIR, "requirements.txt")
patched_req = os.path.join(tempfile.gettempdir(), "odoo_requirements_patched.txt")

patch_requirements(req_file, patched_req)

run([sys.executable, "-m", "pip", "install", "--quiet",
     "--no-warn-script-location",
     "-r", patched_req])

# Force-reinstall setuptools AFTER Odoo requirements so pkg_resources is never lost
pip_install("setuptools", "wheel", extra_args=["--force-reinstall"])
print("  ✓  setuptools force-reinstalled")

# Belt-and-suspenders: comment out ALL pkg_resources imports across the Odoo tree
# so that even if setuptools gets stripped again at runtime, Odoo won't crash.
print("\n  Scanning Odoo source for pkg_resources imports...")
patch_pkg_resources(ODOO_DIR)

# ── 5. Write config & launch Odoo ────────────────────────────────────────────
step("5 / 5 · Writing odoo.conf & launching Odoo on port " + str(ODOO_PORT))

conf_content = (
    "[options]\n"
    "admin_passwd = admin\n"
    "db_host      = " + pg_host + "\n"
    "db_port      = " + str(pg_port) + "\n"
    "db_user      = " + DB_USER + "\n"
    "db_password  = " + DB_PASSWORD + "\n"
    "db_name      = " + DB_NAME + "\n"
    "addons_path  = " + ODOO_DIR + "/addons\n"
    "logfile      = False\n"
    "xmlrpc_port  = " + str(ODOO_PORT) + "\n"
)
with open(ODOO_CONF, "w") as f:
    f.write(conf_content)
print("  Config written → " + ODOO_CONF)
print("\n  🌐  Odoo starting at http://0.0.0.0:" + str(ODOO_PORT) + "\n")

odoo_bin = os.path.join(ODOO_DIR, "odoo-bin")
os.chdir(ODOO_DIR)
os.execv(sys.executable, [
    sys.executable, odoo_bin,
    "--config", ODOO_CONF,
    "--http-port", str(ODOO_PORT),
])