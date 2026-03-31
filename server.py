#!/usr/bin/env python3
"""
Odoo Auto-Installer & Host Script
- Requires ONLY git + python3 in PATH (no psql, no apt, no sudo).
- Uses psycopg2 (installed via pip) to talk to Postgres — no psql binary needed.
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

# ── Config ────────────────────────────────────────────────────────────────────
ODOO_BRANCH = "17.0"
ODOO_DIR    = os.path.join(os.path.expanduser("~"), "odoo")
ODOO_PORT   = 8000
ODOO_CONF   = os.path.join(os.path.expanduser("~"), "odoo.conf")
DB_HOST     = "127.0.0.1"
DB_PORT     = 5432
DB_USER     = "odoo"
DB_PASSWORD = "odoo_pass_2026"
DB_NAME     = "odoo"
# ──────────────────────────────────────────────────────────────────────────────

def run(cmd, check=True):
    display = " ".join(str(c) for c in cmd)
    print(f"\n▶  {display}")
    subprocess.run(cmd, check=check)

def step(msg):
    print(f"\n{'═'*60}")
    print(f"  {msg}")
    print('═'*60)

def pip_install(*packages):
    """Install packages and make them importable in the current process."""
    run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", *packages])
    # Reload site paths so newly installed packages are discoverable
    importlib.invalidate_caches()
    for path in site.getsitepackages():
        if path not in sys.path:
            sys.path.insert(0, path)
    # Also check user site
    user_site = site.getusersitepackages()
    if user_site not in sys.path:
        sys.path.insert(0, user_site)

def import_or_install(module_name, pip_name=None):
    """Import a module, installing it first if missing."""
    pip_name = pip_name or module_name
    if importlib.util.find_spec(module_name) is None:
        print(f"  Installing {pip_name}…")
        pip_install(pip_name)
    importlib.invalidate_caches()
    return importlib.import_module(module_name)

# ── 1. Check prerequisites ────────────────────────────────────────────────────
step("1 / 5 · Checking prerequisites")
for tool in ("git", "python3"):
    path = shutil.which(tool)
    if not path:
        print(f"  ✗  '{tool}' not found in PATH — cannot continue.")
        sys.exit(1)
    print(f"  ✓  {tool} → {path}")

# ── 2. Bootstrap pip & install psycopg2-binary ────────────────────────────────
step("2 / 5 · Bootstrapping pip and installing psycopg2-binary")
pip_install("pip")
pip_install("psycopg2-binary")

psycopg2 = import_or_install("psycopg2", "psycopg2-binary")
print("  ✓  psycopg2 imported successfully")

# ── 3. Start PostgreSQL & create role/db ──────────────────────────────────────
step("3 / 5 · Configuring PostgreSQL")

pg_start_candidates = [
    ["pg_ctlcluster", "15", "main", "start"],
    ["pg_ctlcluster", "16", "main", "start"],
    ["pg_ctlcluster", "14", "main", "start"],
    ["pg_ctlcluster", "13", "main", "start"],
    ["service", "postgresql", "start"],
    ["pg_ctl", "-D", "/var/lib/postgresql/data", "start"],
    ["pg_ctl", "-D", "/usr/local/pgsql/data", "start"],
]
for cmd in pg_start_candidates:
    if shutil.which(cmd[0]):
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            print(f"  ✓  Started postgres via: {' '.join(cmd)}")
            break

print("  Waiting for postgres to accept connections…")
pg_superusers = ["postgres", os.environ.get("USER", ""), os.environ.get("PGUSER", "")]
pg_connected_user = None

for attempt in range(30):
    for user in pg_superusers:
        if not user:
            continue
        try:
            conn = psycopg2.connect(host=DB_HOST, port=DB_PORT,
                                    dbname="postgres", user=user,
                                    connect_timeout=2)
            conn.close()
            pg_connected_user = user
            print(f"  ✓  Postgres is up — connected as '{user}' (attempt {attempt+1})")
            break
        except Exception:
            pass
    if pg_connected_user:
        break
    time.sleep(1)
else:
    print("  ✗  Could not connect to Postgres after 30s. Make sure it's running on 127.0.0.1:5432")
    sys.exit(1)

def pg_exec(sql):
    """Execute SQL as the discovered superuser; ignore non-fatal errors."""
    try:
        conn = psycopg2.connect(host=DB_HOST, port=DB_PORT,
                                dbname="postgres", user=pg_connected_user,
                                connect_timeout=5)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
        cur.close()
        conn.close()
        print(f"  SQL ok: {sql[:80]}")
    except Exception as e:
        print(f"  SQL notice (non-fatal): {e}")

pg_exec(f"CREATE ROLE {DB_USER} LOGIN CREATEDB PASSWORD '{DB_PASSWORD}';")
pg_exec(f"CREATE DATABASE {DB_NAME} OWNER {DB_USER};")

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

req_file = os.path.join(ODOO_DIR, "requirements.txt")
pip_install("wheel")
run([sys.executable, "-m", "pip", "install", "--quiet",
     "--no-warn-script-location", "-r", req_file])
run([sys.executable, "-m", "pip", "install", "--quiet",
     "--force-reinstall", "psycopg2-binary"], check=False)

# ── 5. Write config & launch Odoo ────────────────────────────────────────────
step("5 / 5 · Writing odoo.conf & launching Odoo on port " + str(ODOO_PORT))

conf_content = (
    "[options]\n"
    f"admin_passwd = admin\n"
    f"db_host      = {DB_HOST}\n"
    f"db_port      = {DB_PORT}\n"
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