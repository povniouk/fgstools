#!/bin/bash
# Deploy all tool packages to fgstools LXC and restart the service.
# Run from the CWLNG repo root: bash deploy.sh
# Requires SSH access to 192.168.8.117 (uses PTY for sudo restart).

set -e
LXC="povniouk@192.168.8.117"
DEST="~/spec-qa"
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "[deploy] Copying tool1_spec_qa (app.py, static, loaders)..."
scp "$REPO/tool1_spec_qa/app.py" \
    "$REPO/tool1_spec_qa/pdf_loader.py" \
    "$REPO/tool1_spec_qa/retriever.py" \
    "$LXC:$DEST/"

scp "$REPO/tool1_spec_qa/static/index.html" \
    "$LXC:$DEST/static/"

echo "[deploy] Copying tool2_spi_checker..."
ssh "$LXC" "mkdir -p $DEST/tool2_spi_checker"
scp "$REPO/tool2_spi_checker/__init__.py" \
    "$REPO/tool2_spi_checker/spi_checker.py" \
    "$LXC:$DEST/tool2_spi_checker/"

echo "[deploy] Copying tool5_email_tracker..."
ssh "$LXC" "mkdir -p $DEST/tool5_email_tracker"
scp "$REPO/tool5_email_tracker/__init__.py" \
    "$LXC:$DEST/tool5_email_tracker/"

echo "[deploy] Cleaning up legacy files..."
ssh "$LXC" "rm -f $DEST/email_tracker.py $DEST/index.html"

echo "[deploy] Restarting fgstools service..."
ssh -t "$LXC" "sudo /usr/bin/systemctl restart fgstools"

echo "[deploy] Checking status..."
ssh "$LXC" "sudo /usr/bin/systemctl status fgstools --no-pager | head -5" 2>/dev/null || \
ssh -t "$LXC" "sudo /usr/bin/systemctl status fgstools --no-pager | head -5"

echo "[deploy] Done."
