#!/usr/bin/env bash
# Reapply the multi-connection upload patch into the running bot container.
#
# The MLTB bot runs with NO bind-mount: source lives inside the image at /app.
# `docker cp` copies survive a plain restart but are lost on container
# recreation (docker compose up --force-recreate / rebuild). Run this after any
# recreate to restore the patch, then restart the bot.
#
# Usage: ./apply_parallel_upload.sh
set -euo pipefail

REPO="/root/mirrorbot"
CT="mirrorbot-app-1"

echo "[1/3] Copying patched source into ${CT}..."
docker cp "${REPO}/bot/core/parallel_upload.py" "${CT}:/app/bot/core/parallel_upload.py"
docker cp "${REPO}/bot/core/telegram_manager.py" "${CT}:/app/bot/core/telegram_manager.py"
docker cp "${REPO}/bot/helper/mirror_leech_utils/upload_utils/buzzheavier_uploader.py" "${CT}:/app/bot/helper/mirror_leech_utils/upload_utils/buzzheavier_uploader.py"

echo "[2/3] Restarting bot (this drops any in-flight download/upload)..."
docker restart "${CT}"

echo "[3/3] Waiting for startup, verifying patch..."
sleep 14
if docker logs "${CT}" 2>&1 | grep -q "Parallel multi-connection save_file patch applied"; then
  echo "OK: patch applied and active."
else
  echo "WARN: patch log not found — inspect: docker logs ${CT} | tail -40"
  exit 1
fi
