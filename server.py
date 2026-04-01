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
import time
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
    r"^libsass\b":      None,   # None → drop the line
}

def run(cmd, check=True):
    display = " ".join(str(c) for c in cmd)
    print(f"\n▶  {display}")
    subprocess.run(cmd, check=check)

def step(msg):
    print(f"\n{'═'*60}")
    print(f"  {msg}")
    print('═'*60)

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
                        f_out.write(f"# dropped (no pure wheel): {line}\n")
                    elif replacement not in written_replacements:
                        f_out.write(f"{replacement}\n")
                        written_replacements.add(replacement)
                    else:
                        f_out.write(f"# already added: {replacement}\n")
                    replaced = True
                    break
            if not replaced:
                f_out.write(raw_line)

# ── 1. Check prerequisites ────────────────────────────────────────────────────
step("1 / 5 · Checking prerequisites")
for tool in ("git", "python3"):
    path = shutil.which(tool)
    if not path:
        print(f"  ✗  '{tool}' not found in PATH — cannot continue.")
        sys.exit(1)
    print(f"  ✓  {tool} → {path}")

# ── 2. Install pip packages ───────────────────────────────────────────────────
step("2 / 5 · Installing psycopg2-binary and pgserver")
pip_install("pip", extra_args=["--upgrade"])
pip_install("setuptools")          # provides pkg_resources (required by Odoo)
pip_install("psycopg2-binary")
pip_install("pgserver")            # self-contained Postgres server (no system PG needed)

psycopg2 = import_or_install("psycopg2", "psycopg2-binary")
pgserver  = import_or_install("pgserver", "pgserver")
print("  ✓  psycopg2 + pgserver imported")

# ── 3. Start embedded PostgreSQL via pgserver ────────────────────────────────
step("3 / 5 · Starting embedded PostgreSQL (pgserver)")

os.makedirs(PG_DATA_DIR, exist_ok=True)
pg = pgserver.get_server(PG_DATA_DIR, cleanup_mode="stop")
print(f"  ✓  Postgres running — URI: {pg.get_uri()}")

def pg_exec(sql):
    try:
        conn = psycopg2.connect(pg.get_uri("postgres"))
        conn.autocommit = True
        conn.cursor().execute(sql)
        conn.close()
        print(f"  SQL ok: {sql[:80]}")
    except Exception as e:
        print(f"  SQL notice (non-fatal): {e}")

pg_exec(f"CREATE ROLE {DB_USER} LOGIN CREATEDB PASSWORD '{DB_PASSWORD}';")
pg_exec(f"CREATE DATABASE {DB_NAME} OWNER {DB_USER};")

# Grab port from the running pgserver instance for odoo.conf
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
    print(f"  Odoo already at {ODOO_DIR} — skipping clone.")

req_file    = os.path.join(ODOO_DIR, "requirements.txt")
patched_req = os.path.join(tempfile.gettempdir(), "odoo_requirements_patched.txt")

pip_install("wheel")
pip_install("setuptools")          # ensure pkg_resources is present after venv pip upgrades
patch_requirements(req_file, patched_req)

run([sys.executable, "-m", "pip", "install", "--quiet",
     "--no-warn-script-location",
     "-r", patched_req])

# ── 5. Write config & launch Odoo ────────────────────────────────────────────
step("5 / 5 · Writing odoo.conf & launching Odoo on port " + str(ODOO_PORT))

conf_content = (
    "[options]\n"
    f"admin_passwd = admin\n"
    f"db_host      = {pg_host}\n"
    f"db_port      = {pg_port}\n"
    f"db_user      = {DB_USER}\n"
    f"db_password  = {DB_PASSWORD}\n"
    f"db_name      = {DB_NAME}\n"
    f"addons_path  = {ODOO_DIR}/addons\n"
    "logfile      = False\n"
    f"xmlrpc_port  = {ODOO_PORT}\n"
)
with open(ODOO_CONF, "w") as f:
    f.write(conf_content)
print(f"  Config written → {ODOO_CONF}")
print(f"\n  🌐  Odoo starting at http://0.0.0.0:{ODOO_PORT}\n")

odoo_bin = os.path.join(ODOO_DIR, "odoo-bin")
os.chdir(ODOO_DIR)
os.execv(sys.executable, [
    sys.executable, odoo_bin,
    "--config", ODOO_CONF,
    "--http-port", str(ODOO_PORT),
])