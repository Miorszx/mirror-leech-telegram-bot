#!/usr/bin/env bash
# Reapply the local source patches into the running bot container.
#
# The MLTB bot runs with NO bind-mount: source lives inside the image at /app.
# `docker cp` copies survive a plain restart but are lost on container
# recreation (docker compose up --force-recreate / rebuild). Run this after any
# recreate to restore the patches, then restart the bot.
#
# Usage: ./apply_parallel_upload.sh
set -euo pipefail

REPO="/root/mirrorbot"
CT="mirrorbot-app-1"

echo "[1/3] Copying patched source into ${CT}..."
docker cp "${REPO}/bot/core/parallel_upload.py" "${CT}:/app/bot/core/parallel_upload.py"
docker cp "${REPO}/bot/core/telegram_manager.py" "${CT}:/app/bot/core/telegram_manager.py"
docker cp "${REPO}/bot/helper/mirror_leech_utils/upload_utils/buzzheavier_uploader.py" "${CT}:/app/bot/helper/mirror_leech_utils/upload_utils/buzzheavier_uploader.py"
# AllDebrid src fix (status/speed) + storage housekeeping (post-task delete +
# retention sweep) + the config keys and wiring they depend on.
docker cp "${REPO}/bot/helper/mirror_leech_utils/download_utils/alldebrid_resolver.py" "${CT}:/app/bot/helper/mirror_leech_utils/download_utils/alldebrid_resolver.py"
docker cp "${REPO}/bot/helper/mirror_leech_utils/download_utils/alldebrid_cleanup.py" "${CT}:/app/bot/helper/mirror_leech_utils/download_utils/alldebrid_cleanup.py"
docker cp "${REPO}/bot/core/config_manager.py" "${CT}:/app/bot/core/config_manager.py"
docker cp "${REPO}/bot/__main__.py" "${CT}:/app/bot/__main__.py"
docker cp "${REPO}/bot/helper/listeners/task_listener.py" "${CT}:/app/bot/helper/listeners/task_listener.py"

docker cp "${REPO}/bot/helper/mirror_leech_utils/download_utils/direct_link_generator.py" "${CT}:/app/bot/helper/mirror_leech_utils/download_utils/direct_link_generator.py"

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
