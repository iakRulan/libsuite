#!/usr/bin/env python3
"""verify — re-check a repaired library against its manifest and index.

Usage:
    python3 verify.py --out DIR

Checks:
  1. every file in manifest.json exists and its sha256 matches
  2. every source file in index.json was either copied or explicitly deduplicated
  3. no operation in operations.json is left in a failed/unknown state
  4. result.json exists and carries case_id + status

Prints a JSON verdict; exits non-zero when any hard check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MAX_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(MAX_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def load(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    issues = []
    checks = {}

    manifest_path = out / "manifest.json"
    index_path = out / "index.json"
    ops_path = out / "operations.json"
    result_path = out / "result.json"

    for p in (manifest_path, index_path, ops_path, result_path):
        if not p.exists():
            issues.append(f"missing deliverable: {p.name}")

    if issues:
        print(json.dumps({"valid": False, "issues": issues}, ensure_ascii=False, indent=2))
        return 1

    manifest = load(manifest_path)
    index = load(index_path)
    ops = load(ops_path)
    result = load(result_path)

    # 1. manifest hashes
    root = Path(manifest.get("root") or (out / "library"))
    bad_hash = []
    missing = []
    for entry in manifest.get("files", []):
        fp = root / entry["path"]
        if not fp.exists():
            missing.append(entry["path"])
            continue
        if sha256_file(fp) != entry["sha256"]:
            bad_hash.append(entry["path"])
    checks["manifest_files"] = len(manifest.get("files", []))
    checks["manifest_missing"] = len(missing)
    checks["manifest_hash_mismatch"] = len(bad_hash)
    if missing:
        issues.append(f"{len(missing)} organized files missing: {missing[:5]}")
    if bad_hash:
        issues.append(f"{len(bad_hash)} organized files failed hash verification: {bad_hash[:5]}")

    # 2. source coverage
    copied = {o["dest"] for o in ops.get("operations", []) if o["op"] == "copy"}
    deduped = {o["src"] for o in ops.get("operations", []) if o["op"] == "deduplicate"}
    source_rels = {f["rel"] for f in index.get("files", [])}
    accounted = copied | deduped
    unaccounted = [r for r in source_rels if r not in {o["src"] for o in ops.get("operations", [])}]
    checks["source_files"] = len(source_rels)
    checks["accounted"] = len(accounted)
    if unaccounted:
        issues.append(f"{len(unaccounted)} source files not accounted for: {unaccounted[:5]}")

    # 3. failed operations
    failed = [o for o in ops.get("operations", []) if o.get("status") in {"failed", "hash_mismatch"}]
    checks["failed_operations"] = len(failed)
    if failed:
        issues.append(f"{len(failed)} operations failed: {[f.get('src') for f in failed[:5]]}")

    # 4. result.json contract
    if not result.get("case_id"):
        issues.append("result.json missing case_id")
    if result.get("status") not in {"complete", "partial"}:
        issues.append(f"result.json unexpected status: {result.get('status')}")

    verdict = {
        "valid": not issues,
        "checks": checks,
        "issues": issues,
        "case_id": result.get("case_id"),
        "status": result.get("status"),
    }
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
