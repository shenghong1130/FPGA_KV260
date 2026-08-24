#!/usr/bin/env python3
"""Remove stale PYNQ PL metadata at boot before a worker loads its overlay."""

from pathlib import Path

from pynq.pl_server import global_state


removed = 0
for state_file in Path(global_state.STATE_DIR).glob("global_pl_state_*.json"):
    state_file.unlink(missing_ok=True)
    removed += 1

print(f"PYNQ PL state: cleared {removed} stale file(s)")
