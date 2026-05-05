#!/usr/bin/env python3
"""
Run this once to assemble synergy/backend/synergy.py from parts.
Usage:  cd synergy/backend && python combine.py
"""
import os, glob

here = os.path.dirname(os.path.abspath(__file__))
parts_dir = os.path.join(here, "parts")
out_path  = os.path.join(here, "synergy.py")

part_files = sorted(glob.glob(os.path.join(parts_dir, "part_*.py")))
if not part_files:
    raise SystemExit("No part files found in parts/")

content = ""
for pf in part_files:
    with open(pf, "r", encoding="utf-8") as f:
        content += f.read()

with open(out_path, "w", encoding="utf-8") as f:
    f.write(content)

print(f"Assembled {len(part_files)} parts -> synergy.py ({len(content)} chars)")
print("You can now delete synergy/backend/parts/ if you like.")
