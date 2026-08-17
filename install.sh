#!/usr/bin/env bash
# Helmward native install script
# Usage: sudo ./install.sh [INSTALL_DIR]
#   INSTALL_DIR defaults to /opt/helmward
#
# Run this from the root of the cloned Helmward repo (the folder
# containing control-plane/ and dashboard/). Safe to re-run: it will
# never overwrite an existing database or an existing helmward.env.

set -euo pipefail

INSTALL_DIR="${1:-/opt/helmward}"
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pass() { echo "[ OK ] $*"; }
info() { echo "[ .. ] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
[ "$(id -u)" -eq 0 ] || fail "Run as root: sudo ./install.sh [INSTALL_DIR]"

for f in control-plane/app/main.py control-plane/requirements.txt \
         control-plane/schema.sql control-plane/task_templates.json \
         dashboard/dashboard.html; do
    [ -f "$PKG_ROOT/$f" ] || fail "Missing $f -- run install.sh from the root of the cloned repo."
done
pass "Package contents verified (running from $PKG_ROOT)"

# ---------------------------------------------------------- system deps
info "Installing system dependencies (apt)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || echo "[warn] apt-get update reported errors (possibly an unrelated repo) -- continuing"
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates sqlite3 >/dev/null \
    || fail "apt-get install of required packages failed"
pass "System dependencies installed"

# ------------------------------------------------------------ file layout
mkdir -p "$INSTALL_DIR/control-plane/data" "$INSTALL_DIR/wiki"

if [ "$PKG_ROOT" = "$INSTALL_DIR" ]; then
    pass "Package root is the install dir -- skipping file copy"
else
    info "Copying application files to $INSTALL_DIR..."
    cp -r "$PKG_ROOT/control-plane/app"                 "$INSTALL_DIR/control-plane/"
    cp    "$PKG_ROOT/control-plane/requirements.txt"    "$INSTALL_DIR/control-plane/"
    cp    "$PKG_ROOT/control-plane/schema.sql"          "$INSTALL_DIR/control-plane/"
    cp    "$PKG_ROOT/control-plane/task_templates.json" "$INSTALL_DIR/control-plane/"
    rm -rf "$INSTALL_DIR/dashboard"
    cp -r "$PKG_ROOT/dashboard" "$INSTALL_DIR/dashboard"
    pass "Application files in place (existing data/ untouched)"
fi

# ----------------------------------------------------------------- venv
VENV="$INSTALL_DIR/control-plane/venv"
if [ ! -x "$VENV/bin/python" ]; then
    info "Creating Python venv..."
    python3 -m venv "$VENV"
fi
info "Installing Python requirements..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/control-plane/requirements.txt"
pass "Python environment ready ($("$VENV/bin/python" --version))"

# ------------------------------------------------------- database (first run only)
DB="$INSTALL_DIR/control-plane/data/agentos.db"
if [ -f "$DB" ]; then
    pass "Existing database found -- leaving it untouched"
else
    info "Initializing database from schema.sql (first run)..."
    sqlite3 "$DB" < "$INSTALL_DIR/control-plane/schema.sql"
    pass "Database initialized at $DB"
fi
# Note: the app also applies schema.sql idempotently on every startup
# (CREATE TABLE IF NOT EXISTS), so upgrades that add tables are picked up
# automatically without touching existing data.

# -------------------------------------------------------- environment file
ENV_FILE="$INSTALL_DIR/helmward.env"
if [ -f "$ENV_FILE" ]; then
    pass "Existing $ENV_FILE found -- leaving it untouched"
else
    cat > "$ENV_FILE" <<EOF
# Helmward control plane configuration.
# Edit values here, then: systemctl restart helmward-control-plane
DB_PATH=$INSTALL_DIR/control-plane/data/agentos.db
SCHEMA_PATH=$INSTALL_DIR/control-plane/schema.sql
TASK_TEMPLATES_PATH=$INSTALL_DIR/control-plane/task_templates.json
DASHBOARD_DIR=$INSTALL_DIR/dashboard
WIKI_DIR=$INSTALL_DIR/wiki

# Optional -- local LLM model switching (see Setup Guide, Step 6):
#LLAMA_SWAP_URL=http://<your-inference-host>:8081
#HERMES_WEBUI_URL=http://<your-agent-host>:8787

# Optional -- Telegram notifications for approvals:
#TELEGRAM_BOT_TOKEN=
#TELEGRAM_CHAT_ID=
EOF
    pass "Wrote $ENV_FILE"
fi

# ---------------------------------------------------------- systemd unit
info "Installing systemd unit..."
cat > /etc/systemd/system/helmward-control-plane.service <<EOF
[Unit]
Description=Helmward Control Plane (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR/control-plane
EnvironmentFile=$INSTALL_DIR/helmward.env
ExecStart=$INSTALL_DIR/control-plane/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now helmward-control-plane.service
pass "Service enabled and started"

# ------------------------------------------------------------ health check
info "Waiting for control plane health check..."
HEALTH_OK=0
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done

echo
if [ "$HEALTH_OK" -eq 1 ]; then
    echo "=============================================="
    echo " INSTALL PASSED"
    echo "   healthz : $(curl -fsS http://127.0.0.1:8080/healthz)"
    echo "   dashboard: http://<this-host>:8080/dashboard.html"
    echo "   config   : $ENV_FILE"
    echo "=============================================="
else
    echo "=============================================="
    echo " INSTALL FAILED -- /healthz did not respond within 30s"
    echo " Inspect logs with:"
    echo "   journalctl -u helmward-control-plane -n 50 --no-pager"
    echo " Or run the diagnostic tool:"
    echo "   $VENV/bin/python $INSTALL_DIR/control-plane/../tools/doctor.py"
    echo "=============================================="
    exit 1
fi
