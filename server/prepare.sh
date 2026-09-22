#!/usr/bin/env bash
set -euo pipefail
# Dedicated paths only; no system Python, driver, Docker or existing app changes.
test -d /opt/fotoarchive
test -d /data/fotoarchive
python3 -m venv /opt/fotoarchive/venv
/opt/fotoarchive/venv/bin/pip install --no-cache-dir -r /opt/fotoarchive/app/server/requirements.txt
PYTHONPATH=/opt/fotoarchive/app /opt/fotoarchive/venv/bin/python /opt/fotoarchive/app/server/download_models.py
