#!/usr/bin/env python3
"""Operator CLI for durable BIMA Discovery history; no network access."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'multi'))
from discovery_history_v11 import cli

if __name__ == '__main__':
    raise SystemExit(cli())
