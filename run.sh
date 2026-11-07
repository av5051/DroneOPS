#!/usr/bin/env bash
# Run DroneOps from source. First run needs internet to install packages.
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec python3 app.py