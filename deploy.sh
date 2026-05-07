#!/bin/bash
# Deploy all tool packages to fgstools LXC and restart the service.
# Run from the CWLNG repo root: bash deploy.sh
# Restart uses SIGKILL so systemd auto-restarts (no PTY/sudo needed).

set -e
LXC="povniouk@192.168.8.117"
DEST="~/spec-qa"
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "[deploy] Copying tool1_spec_qa (app.py, static, loaders)..."
scp "$REPO/tool1_spec_qa/app.py" \
    "$REPO/tool1_spec_qa/pdf_loader.py" \
    "$REPO/tool1_spec_qa/retriever.py" \
    "$LXC:$DEST/"

ssh "$LXC" "mkdir -p $DEST/static"
scp "$REPO/tool1_spec_qa/static/index.html" \
    "$LXC:$DEST/static/index.html"

echo "[deploy] Copying tool2_spi_checker..."
ssh "$LXC" "mkdir -p $DEST/tool2_spi_checker"
scp "$REPO/tool2_spi_checker/__init__.py" \
    "$REPO/tool2_spi_checker/spi_checker.py" \
    "$LXC:$DEST/tool2_spi_checker/"

echo "[deploy] Copying tool5_email_tracker..."
ssh "$LXC" "mkdir -p $DEST/tool5_email_tracker"
scp "$REPO/tool5_email_tracker/__init__.py" \
    "$LXC:$DEST/tool5_email_tracker/"

echo "[deploy] Copying tool8_doc_register..."
ssh "$LXC" "mkdir -p $DEST/tool8_doc_register"
scp "$REPO/tool8_doc_register/__init__.py" \
    "$REPO/tool8_doc_register/doc_register.py" \
    "$LXC:$DEST/tool8_doc_register/"

echo "[deploy] Cleaning up legacy files..."
ssh "$LXC" "rm -f $DEST/email_tracker.py $DEST/index.html"

echo "[deploy] Restarting fgstools (SIGKILL → systemd auto-restart)..."
ssh "$LXC" "kill -9 \$(pgrep -f 'venv/bin/python app.py') 2>/dev/null || true"
sleep 4

echo "[deploy] Checking status..."
ssh "$LXC" "systemctl is-active fgstools && pgrep -a -f 'python app.py'"

echo "[deploy] Done."
