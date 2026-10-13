#!/usr/bin/env python3
"""
Compare two nesting diagnostic JSON outputs from different platforms.

Usage:
    python3 compare_nesting_results.py linux_result.json macos_result.json
    
Or paste JSON directly:
    python3 compare_nesting_results.py --stdin
    # Then paste first JSON, press Enter, paste second JSON, press Ctrl+D
"""

import json
import sys
import argparse


def load_json(source):
    if source == "-":
        return json.load(sys.stdin)
    with open(source) as f:
        return json.load(f)


def fmt(v):
    if isinstance(v, float):
        return f"{v:.6f}"
    return str(v)


def compare_dicts(d1, d2, path="", diffs=None):
    if diffs is None:
        diffs = []
    all_keys = set(d1.keys()) | set(d2.keys())
    for k in sorted(all_keys):
        p = f"{path}.{k}" if path else k
        if k not in d1:
            diffs.append(f"{p}: MISSING in first (second={fmt(d2[k])})")
        elif k not in d2:
            diffs.append(f"{p}: MISSING in second (first={fmt(d1[k])})")
        elif isinstance(d1[k], dict) and isinstance(d2[k], dict):
            compare_dicts(d1[k], d2[k], p, diffs)
        elif isinstance(d1[k], list) and isinstance(d2[k], list):
            if len(d1[k]) != len(d2[k]):
                diffs.append(f"{p}: LIST LEN {len(d1[k])} vs {len(d2[k])}")
            else:
                for i, (v1, v2) in enumerate(zip(d1[k], d2[k])):
                    if isinstance(v1, dict) and isinstance(v2, dict):
                        compare_dicts(v1, v2, f"{p}[{i}]", diffs)
                    elif v1 != v2:
                        diffs.append(f"{p}[{i}]: {fmt(v1)} vs {fmt(v2)}")
        elif d1[k] != d2[k]:
            diffs.append(f"{p}: {fmt(d1[k])} vs {fmt(d2[k])}")
    return diffs


def compare_sheets(s1, s2):
    diffs = []
    max_sheets = max(len(s1), len(s2))
    for i in range(max_sheets):
        if i >= len(s1):
            diffs.append(f"Sheet {i+1}: MISSING in first")
            continue
        if i >= len(s2):
            diffs.append(f"Sheet {i+1}: MISSING in second")
            continue
        sh1, sh2 = s1[i], s2[i]
        if sh1["n"] != sh2["n"]:
            diffs.append(f"Sheet {i+1} part count: {sh1['n']} vs {sh2['n']}")
        if abs(sh1["area"] - sh2["area"]) > 0.01:
            diffs.append(f"Sheet {i+1} used area: {sh1['area']:.3f} vs {sh2['area']:.3f}")
        if abs(sh1["eff"] - sh2["eff"]) > 0.01:
            diffs.append(f"Sheet {i+1} efficiency: {sh1['eff']:.2f}% vs {sh2['eff']:.2f}%")
        
        # Compare placements
        parts1 = {p["id"]: p for p in sh1["parts"]}
        parts2 = {p["id"]: p for p in sh2["parts"]}
        all_ids = set(parts1) | set(parts2)
        for pid in sorted(all_ids):
            if pid not in parts1:
                diffs.append(f"  Sheet {i+1} {pid}: MISSING in first")
            elif pid not in parts2:
                diffs.append(f"  Sheet {i+1} {pid}: MISSING in second")
            else:
                p1, p2 = parts1[pid], parts2[pid]
                dx, dy, da = p1["x"]-p2["x"], p1["y"]-p2["y"], p1["a"]-p2["a"]
                if abs(dx) > 0.001 or abs(dy) > 0.001 or da != 0:
                    diffs.append(f"  Sheet {i+1} {pid}: pos=({dx:+.3f},{dy:+.3f}) angle={da:+.0f}")
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file1", nargs="?", help="First JSON file (or - for stdin)")
    ap.add_argument("file2", nargs="?", help="Second JSON file")
    ap.add_argument("--stdin", action="store_true", help="Read two JSON objects from stdin")
    args = ap.parse_args()
    
    if args.stdin:
        print("Paste first JSON, press Enter, then second JSON, then Ctrl+D:")
        data = sys.stdin.read().strip().split("\n", 1)
        if len(data) != 2:
            print("Expected two JSON objects separated by newline")
            return 1
        r1, r2 = json.loads(data[0]), json.loads(data[1])
    elif args.file1 and args.file2:
        r1, r2 = load_json(args.file1), load_json(args.file2)
    else:
        ap.print_help()
        return 1
    
    print(f"Comparing:")
    print(f"  1: {r1.get('platform', 'unknown')}  FreeCAD {r1.get('freecad', '?')}  Shapely {r1.get('shapely', '?')}  GEOS {r1.get('geos', '?')}")
    print(f"  2: {r2.get('platform', 'unknown')}  FreeCAD {r2.get('freecad', '?')}  Shapely {r2.get('shapely', '?')}  GEOS {r2.get('geos', '?')}")
    print()
    
    # Platform/environment diffs
    env_diffs = compare_dicts(
        {k: r1.get(k) for k in ["platform", "freecad", "shapely", "geos", "numpy", "params"]},
        {k: r2.get(k) for k in ["platform", "freecad", "shapely", "geos", "numpy", "params"]},
        "env"
    )
    if env_diffs:
        print("ENVIRONMENT DIFFERENCES:")
        for d in env_diffs: print(f"  {d}")
        print()
    
    # Result diffs
    result_diffs = compare_dicts(
        {k: r1.get(k) for k in ["sheets", "placed", "unplaced", "wall_time", "steps"]},
        {k: r2.get(k) for k in ["sheets", "placed", "unplaced", "wall_time", "steps"]},
        "result"
    )
    if result_diffs:
        print("TOP-LEVEL RESULT DIFFERENCES:")
        for d in result_diffs: print(f"  {d}")
        print()
    
    # Sheet-by-sheet
    sheet_diffs = compare_sheets(r1.get("sheets_detail", []), r2.get("sheets_detail", []))
    if sheet_diffs:
        print("SHEET/PLACEMENT DIFFERENCES:")
        for d in sheet_diffs: print(f"  {d}")
        print()
    
    # Unplaced parts
    unplaced1 = set(r1.get("unplaced_ids", []))
    unplaced2 = set(r2.get("unplaced_ids", []))
    if unplaced1 != unplaced2:
        print("UNPLACED PARTS DIFFER:")
        for p in sorted(unplaced1 - unplaced2): print(f"  Only in first: {p}")
        for p in sorted(unplaced2 - unplaced1): print(f"  Only in second: {p}")
        print()
    
    if not env_diffs and not result_diffs and not sheet_diffs and unplaced1 == unplaced2:
        print("✓ RESULTS ARE IDENTICAL")
        return 0
    else:
        print("✗ RESULTS DIFFER")
        return 1


if __name__ == "__main__":
    sys.exit(main())