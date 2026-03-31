#!/usr/bin/env python3
"""
Odoo Auto-Installer & Host Script
- NO apt/sudo required — assumes git, python3, postgresql are pre-installed.
- Installs Python deps via pip, configures PostgreSQL, clones Odoo, runs on port 8000.
"""

import os
import subprocess
import sys
import time
import shutil

# ── Config ────────────────────────────────────────────────────────────────────
ODOO_BRANCH  = "17.0"
ODOO_DIR     = os.path.join(os.path.expanduser("~"), "odoo")
ODOO_PORT    = 8000
ODOO_CONF    = os.path.join(os.path.expanduser("~"), "odoo.conf")
DB_USER      = "odoo"
DB_PASSWORD  = "odoo_pass_2026"
DB_NAME      = "odoo"
# ──────────────────────────────────────────────────────────────────────────────

def run(cmd, check=True, shell=False, input=None):
    display = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    print(f"\n▶  {display}")
    return subprocess.run(cmd, check=check, shell=shell, input=input,
                          capture_output=False)

def step(msg):
    print(f"\n{'═'*60}")
    print(f"  {msg}")
    print('═'*60)

def pg_run(sql, db="postgres"):
    """Run a SQL command via psql as the current user (no sudo needed in most containers)."""
    run(["psql", "-U", "postgres", "-d", db, "-c", sql], check=False)

# ── 1. Check prerequisites ────────────────────────────────────────────────────
step("1 / 5 · Checking prerequisites")
for tool in ("git", "python3", "psql"):
    path = shutil.which(tool)
    if not path:
        print(f"  ✗  '{tool}' not found in PATH — cannot continue.")
        sys.exit(1)
    print(f"  ✓  {tool} → {path}")

# ── 2. PostgreSQL setup ───────────────────────────────────────────────────────
step("2 / 5 · Configuring PostgreSQL")

# Try to start postgres if a socket/pid isn't already up
pg_start_candidates = [
    ["pg_ctlcluster", "15", "main", "start"],
    ["pg_ctlcluster", "14", "main", "start"],
    ["pg_ctlcluster", "16", "main", "start"],
    ["service", "postgresql", "start"],
    ["pg_ctl", "-D", "/var/lib/postgresql/data", "start"],
]
for cmd in pg_start_candidates:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode == 0:
        print(f"  ✓  Started postgres via: {' '.join(cmd)}")
        break
time.sleep(3)

# Create role + database (errors are non-fatal — they already exist on re-runs)
pg_run(f"CREATE ROLE {DB_USER} LOGIN CREATEDB PASSWORD '{DB_PASSWORD}';")
pg_run(f"CREATE DATABASE {DB_NAME} OWNER {DB_USER};")

# ── 3. Clone Odoo ─────────────────────────────────────────────────────────────
step("3 / 5 · Cloning Odoo")
if not os.path.exists(ODOO_DIR):
    run([
        "git", "clone",
        "--depth", "1",
        "--branch", ODOO_BRANCH,
        "https://github.com/odoo/odoo.git",
        ODOO_DIR,
    ])
else:
    print(f"  Odoo already cloned at {ODOO_DIR} — skipping.")

# ── 4. Python dependencies ────────────────────────────────────────────────────
step("4 / 5 · Installing Python requirements (pip only)")
req_file = os.path.join(ODOO_DIR, "requirements.txt")

run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
run([sys.executable, "-m", "pip", "install", "wheel", "--quiet"])
run([sys.executable, "-m", "pip", "install", "-r", req_file,
     "--quiet", "--no-warn-script-location"])
# psycopg2-binary doesn't need libpq headers — safe in locked containers
run([sys.executable, "-m", "pip", "install", "psycopg2-binary", "--quiet"], check=False)

# ── 5. Write config & launch Odoo ────────────────────────────────────────────
step("5 / 5 · Writing odoo.conf & starting Odoo on port " + str(ODOO_PORT))

conf_content = f"""[options]\nadmin_passwd = admin\ndb_host      = 127.0.0.1\ndb_port      = 5432\ndb_user      = {DB_USER}\ndb_password  = {DB_PASSWORD}\ndb_name      = {DB_NAME}\naddons_path  = {ODOO_DIR}/addons\nlogfile      = False\nxmlrpc_port  = {ODOO_PORT}\n"""
with open(ODOO_CONF, "w") as f:
    f.write(conf_content)
print(f"  Config written → {ODOO_CONF}")

print(f"\n  🌐  Odoo starting at http://0.0.0.0:{ODOO_PORT}\n")

odoo_bin = os.path.join(ODOO_DIR, "odoo-bin")
os.chdir(ODOO_DIR)

# exec() replaces this process — Ctrl-C / SIGTERM stops Odoo cleanly
os.execv(sys.executable, [
    sys.executable, odoo_bin,
    "--config", ODOO_CONF,
    "--http-port", str(ODOO_PORT),
])