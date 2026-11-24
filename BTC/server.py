#!/usr/bin/env python3
"""Compatibility launcher: starts BTC and Gold together on the shared desk."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk.server import Engine, Handler, LocalServer, get_json, liquidation, main
if __name__ == '__main__':
    main()
