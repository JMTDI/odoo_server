#!/usr/bin/env python3
"""
Odoo Auto-Installer & Host Script
Installs dependencies, configures PostgreSQL, clones Odoo, and runs it on port 8000.
Run with: sudo python3 setup_odoo.py
"""

import os
import subprocess
import sys
import time

# ── Config ────────────────────────────────────────────────────────────────────
ODOO_BRANCH   = "17.0"
ODOO_DIR      = os.path.join(os.path.expanduser("~"), "odoo")
ODOO_PORT     = 8000
ODOO_CONF     = os.path.join(os.path.expanduser("~"), "odoo.conf")
DB_USER       = "odoo"
DB_PASSWORD   = "odoo_pass_2026"
# ──────────────────────────────────────────────────────────────────────────────


def run(cmd, check=True, shell=False, env=None):
    """Run a shell command, print it, and raise on failure."""
    display = cmd if isinstance(cmd, str) else " ".join(cmd)
    print(f"\n▶  {display}")
    return subprocess.run(cmd, check=check, shell=shell, env=env)


def step(msg):
    print(f"\n{'═'*60}")
    print(f"  {msg}")
    print('═'*60)


# ── 1. System packages ────────────────────────────────────────────────────────
step("1 / 6 · Installing system packages")
run(["apt-get", "update", "-y"])
run([
    "apt-get", "install", "-y",
    "git", "python3-pip", "python3-dev", "python3-venv",
    "build-essential", "libxml2-dev", "libxslt1-dev",
    "libldap2-dev", "libsasl2-dev", "libssl-dev", "libffi-dev",
    "libpq-dev", "libjpeg-dev", "libpng-dev", "liblcms2-dev",
    "libzip-dev", "node-less", "npm",
    "postgresql", "postgresql-client",
    "wkhtmltopdf",           # for PDF reports
])


# ── 2. PostgreSQL setup ───────────────────────────────────────────────────────
step("2 / 6 · Configuring PostgreSQL")
run(["service", "postgresql", "start"])
time.sleep(2)

# Create the DB role (ignore error if it already exists)
create_role_sql = (
    f"DO $$ BEGIN "
    f"  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{DB_USER}') THEN "
    f"    CREATE ROLE {DB_USER} LOGIN CREATEDB PASSWORD '{DB_PASSWORD}'; "
    f"  END IF; "
    f"END $$;"
)
run(["sudo", "-u", "postgres", "psql", "-c", create_role_sql], check=False)


# ── 3. Clone Odoo ─────────────────────────────────────────────────────────────
step("3 / 6 · Cloning Odoo")
if not os.path.exists(ODOO_DIR):
    run([
        "git", "clone",
        "--depth", "1",
        "--branch", ODOO_BRANCH,
        "https://github.com/odoo/odoo.git",
        ODOO_DIR,
    ])
else:
    print(f"  Odoo already cloned at {ODOO_DIR}, skipping.")


# ── 4. Python dependencies ────────────────────────────────────────────────────
step("4 / 6 · Installing Python requirements")
req_file = os.path.join(ODOO_DIR, "requirements.txt")
run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
run([sys.executable, "-m", "pip", "install", "wheel"])
run([sys.executable, "-m", "pip", "install", "-r", req_file])
# psycopg2 is required; prefer the binary build as a fallback
run([sys.executable, "-m", "pip", "install", "psycopg2-binary"], check=False)


# ── 5. Write odoo.conf ────────────────────────────────────────────────────────
step("5 / 6 · Writing odoo.conf")
conf_content = f"""[options]
admin_passwd = admin
db_host      = 127.0.0.1
db_port      = 5432
db_user      = {DB_USER}
db_password  = {DB_PASSWORD}
addons_path  = {ODOO_DIR}/addons
logfile      = /var/log/odoo.log
xmlrpc_port  = {ODOO_PORT}
"""
with open(ODOO_CONF, "w") as f:
    f.write(conf_content)
print(f"  Config written to {ODOO_CONF}")


# ── 6. Launch Odoo ────────────────────────────────────────────────────────────
step("6 / 6 · Starting Odoo on port " + str(ODOO_PORT))
print(f"\n  🌐  Open http://<your-server-ip>:{ODOO_PORT}  in your browser.\n")

odoo_bin = os.path.join(ODOO_DIR, "odoo-bin")
os.chdir(ODOO_DIR)

# Replace the current process with Odoo (Ctrl-C stops it cleanly)
os.execv(sys.executable, [
    sys.executable, odoo_bin,
    "--config", ODOO_CONF,
    "--http-port", str(ODOO_PORT),
])
