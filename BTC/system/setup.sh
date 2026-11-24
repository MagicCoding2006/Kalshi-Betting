#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
python3 -m venv --system-site-packages .venv-btc-research
.venv-btc-research/bin/python -m pip install -r BTC/system/requirements.txt
