#!/usr/bin/env python3
"""libflow — research file-library repair pipeline.

Reads a case.json (or a plain directory), then produces an organized, hashed,
versioned, metadata-rich, re-runnable library under the output directory.

Subcommands:
    inventory  --case PATH --out DIR      scan inputs -> inventory.json
    plan       --case PATH --out DIR      build organization plan -> plan.json
    apply      --case PATH --out DIR      execute plan -> organized tree + operations.json
    report     --case PATH --out DIR      write result.json (run after apply)

The pipeline never deletes source files: it copies into the organized tree and
records a sha256 for every source and destination object, so a reviewer can
re-run and compare hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# case parsing
# ---------------------------------------------------------------------------

# Keys we look for, in priority order. Real case.json files differ between
# tasks; we accept any of these and degrade gracefully.
INPUT_KEYS = (
    "input_dir", "input_dirs", "inputs", "source_dir", "source", "library",
    "files", "file_list", "roots", "root",
)
RULE_KEYS = (
    "rules", "directory_rules", "organization", "convention", "naming",
    "metadata_fields", "schema", "policy", "requirements",
)
VERSION_KEYS = (
    "version_notes", "versions", "version_rules", "version_chain",
    "administrative_notes", "notes",
)
ALIAS_KEYS = ("aliases", "alias", "renames", "rename_map")
QUESTION_KEYS = ("questions", "searches", "queries", "tasks", "workorder", "work_order")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return [value]


def _load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_case(case_path: Path) -> dict:
    """Load case.json (or synthesize a case from a directory path)."""
    case_path = Path(case_path)
    if case_path.is_dir():
        case = {"case_id": case_path.name, "input_dir": str(case_path)}
        return case
    raw = _load_json(case_path)
    case = dict(raw) if isinstance(raw, dict) else {"value": raw}

    case_id = (
        case.get("case_id")
        or case.get("id")
        or case.get("caseId")
        or case_path.stem
    )

    inputs = []
    for key in INPUT_KEYS:
        if key in case:
            val = case[key]
            if key == "files" and isinstance(val, list):
                # a list of files -> use their parent dirs as roots
                for f in val:
                    if isinstance(f, str):
                        inputs.append(f)
                    elif isinstance(f, dict):
                        inputs.append(f.get("path") or f.get("file") or f.get("name"))
            else:
                inputs.extend(_as_list(val))
    inputs = [i for i in inputs if isinstance(i, str) and i]

    rules = {}
    for key in RULE_KEYS:
        if key in case and isinstance(case[key], dict):
            rules.update(case[key])
    if not rules:
        # keep any scalar rule hints
        for key in RULE_KEYS:
            if key in case:
                rules[key] = case[key]

    versions = {}
    for key in VERSION_KEYS:
        if key in case:
            versions[key] = case[key]

    aliases = {}
    for key in ALIAS_KEYS:
        if isinstance(case.get(key), dict):
            aliases.update(case[key])

    questions = []
    for key in QUESTION_KEYS:
        if key in case:
            questions.extend(_as_list(case[key]))

    return {
        "case_id": case_id,
        "raw": case,
        "inputs": inputs,
        "rules": rules,
        "versions": versions,
        "aliases": aliases,
        "questions": questions,
        "task_text": case.get("task") or case.get("instruction") or case.get("prompt") or "",
    }


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".DS_Store"}


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _detect_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".pdf"}:
        return "pdf"
    if ext in {".doc", ".docx", ".odt", ".rtf", ".txt", ".md"}:
        return "document"
    if ext in {".csv", ".tsv", ".xlsx", ".xls", ".json", ".xml", ".yaml", ".yml"}:
        return "table"
    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".svg", ".gif"}:
        return "image"
    if ext in {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"}:
        return "archive"
    if ext in {".py", ".r", ".m", ".jl", ".sh", ".ipynb"}:
        return "code"
    return "other"


def collect_input_files(inputs, base_dir: Path):
    """Return a list of absolute file paths from the case's input spec."""
    files = []
    seen = set()
    for raw in inputs:
        p = Path(raw)
        if not p.is_absolute():
            # try relative to the case file's directory, then cwd
            cand = base_dir / p
            if cand.exists():
                p = cand
            else:
                p = Path(os.getcwd()) / p
        if p.is_file():
            if p.resolve() not in seen:
                seen.add(p.resolve())
                files.append(p)
        elif p.is_dir():
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in sorted(names):
                    fp = Path(root) / name
                    if fp.resolve() not in seen:
                        seen.add(fp.resolve())
                        files.append(fp)
    return sorted(files, key=lambda x: str(x))


def build_inventory(files, input_roots):
    entries = []
    for fp in files:
        st = fp.stat()
        digest = sha256_file(fp)
        rel = _relative_to_roots(fp, input_roots)
        entries.append({
            "path": str(fp),
            "rel": rel,
            "name": fp.name,
            "ext": fp.suffix.lower(),
            "kind": _detect_kind(fp),
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            "mode": oct(st.st_mode & 0o777),
            "sha256": digest,
        })
    return entries


def _relative_to_roots(fp: Path, roots):
    fp_r = fp.resolve()
    for root in roots:
        try:
            return str(fp_r.relative_to(Path(root).resolve()))
        except ValueError:
            continue
    return fp.name


# ---------------------------------------------------------------------------
# analysis: duplicates, versions, metadata
# ---------------------------------------------------------------------------

def find_duplicates(inventory):
    by_hash = {}
    for e in inventory:
        by_hash.setdefault(e["sha256"], []).append(e)
    duplicates = []
    for digest, group in by_hash.items():
        if len(group) > 1:
            group_sorted = sorted(group, key=lambda x: (x["mtime"], x["rel"]))
            duplicates.append({
                "sha256": digest,
                "size": group_sorted[0]["size"],
                "members": [g["rel"] for g in group_sorted],
                "canonical": group_sorted[0]["rel"],
            })
    return duplicates


def _normalize_stem(name: str) -> str:
    """Reduce a filename to a logical-document key.

    Version suffixes (v1, _final, _copy, (1), dates, bare numbers) are removed
    so that different versions of the same document collapse to one key. This
    is deliberately content-agnostic: the chain itself is validated by hash.
    """
    stem = Path(name).stem.lower()
    # keep letters/digits (incl. CJK) and turn everything else, incl. "_", into spaces
    stem = re.sub(r"[^\w]+", " ", stem, flags=re.UNICODE)
    stem = stem.replace("_", " ")
    drop = {"final", "draft", "copy", "old", "new", "backup", "bak", "rev",
            "version", "ver", "tmp", "temp", "md", "txt", "pdf", "doc", "docx",
            "csv", "json", "xlsx", "png", "jpg"}
    out = []
    for tok in stem.split():
        if tok in drop:
            continue
        if re.fullmatch(r"v?\d+", tok):
            continue
        if re.fullmatch(r"20\d{6}", tok) or re.fullmatch(r"20\d{2}", tok):
            continue
        out.append(tok)
    return " ".join(out)


def build_version_chains(inventory, version_notes):
    """Group files that look like versions of the same logical document."""
    groups = {}
    for e in inventory:
        key = _normalize_stem(e["name"])
        groups.setdefault(key, []).append(e)

    chains = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: (x["mtime"], x["rel"]))
        # distinct hashes only; identical hashes are duplicates, not versions
        distinct = {}
        for g in group_sorted:
            distinct.setdefault(g["sha256"], g)
        if len(distinct) < 2:
            continue
        members = list(distinct.values())
        chains.append({
            "logical": key,
            "members": [
                {
                    "rel": m["rel"],
                    "sha256": m["sha256"],
                    "mtime": m["mtime"],
                    "size": m["size"],
                }
                for m in members
            ],
            "oldest": members[0]["rel"],
            "newest": members[-1]["rel"],
        })

    # fold in explicit administrative version notes when provided
    for note in _as_list(version_notes) if isinstance(version_notes, list) else []:
        if isinstance(note, dict):
            chains.append({
                "logical": note.get("logical") or note.get("name") or "document",
                "members": note.get("members", []),
                "oldest": note.get("oldest"),
                "newest": note.get("newest"),
                "source": "administrative_note",
            })
    return chains


META_PATTERNS = [
    ("project", r"(?:proj|project)[_\-]?([a-z0-9]+)", "filename"),
    ("year", r"(?<!\d)(20\d{2})(?!\d)", "filename"),
    ("sample", r"(?:sample|samp|s)[_\-]?(\w+)", "filename"),
    ("instrument", r"(?:instrument|inst|device)[_\-]?(\w+)", "filename"),
    ("author", r"(?:author|by)[_\-]?([a-z]+)", "filename"),
]


def extract_metadata(entry, inventory, sidecars):
    """Extract metadata fields with explicit provenance."""
    meta = {}
    provenance = {}
    name = entry["name"]

    for field, pattern, source in META_PATTERNS:
        m = re.search(pattern, name, re.IGNORECASE)
        if m:
            meta[field] = m.group(1)
            provenance[field] = f"filename:{pattern}"

    # content sniff for PDF/doc: first line often a title
    if entry["kind"] == "document" and entry["size"] < 20_000_000:
        try:
            with open(entry["path"], "rb") as fh:
                head = fh.read(4096)
            text = head.decode("utf-8", errors="ignore")
            for line in text.splitlines():
                line = line.strip()
                if len(line) > 8 and not line.startswith(("%", "\\", "{")):
                    meta.setdefault("title_guess", line[:120])
                    provenance["title_guess"] = "content:first_text_line"
                    break
        except Exception:
            pass

    # sidecar metadata (foo.json next to foo.pdf)
    side = sidecars.get(entry["rel"])
    if side:
        for k, v in side.items():
            meta[k] = v
            provenance[k] = "sidecar:" + Path(entry["rel"]).name + ".json"

    return meta, provenance


# ---------------------------------------------------------------------------
# organization plan
# ---------------------------------------------------------------------------

DEFAULT_RULES = {
    "layout": "by_type",       # by_type | by_year | by_project | flat
    "naming": "keep",          # keep | normalize
    "deduplicate": True,
    "keep_source": True,
}


def resolve_rules(rules: dict) -> dict:
    resolved = dict(DEFAULT_RULES)
    if not rules:
        return resolved
    for key in DEFAULT_RULES:
        for rk, rv in rules.items():
            if rk.lower().replace("-", "_") == key:
                resolved[key] = rv
    # directory template: e.g. "{project}/{year}/{type}"
    for rk, rv in rules.items():
        if "template" in rk.lower() and isinstance(rv, str):
            resolved["template"] = rv
    return resolved


def plan_organization(inventory, rules, duplicates, chains):
    resolved = resolve_rules(rules)
    actions = []
    seen_dest = {}

    for e in inventory:
        dest = _dest_path(e, resolved, chains)
        if dest is None:
            actions.append({"op": "skip", "src": e["rel"], "reason": "no destination could be derived"})
            continue
        if dest in seen_dest:
            actions.append({
                "op": "conflict",
                "src": e["rel"],
                "src_path": e["path"],
                "dest": dest,
                "reason": f"destination already claimed by {seen_dest[dest]}",
            })
            continue
        seen_dest[dest] = e["rel"]
        if resolved.get("deduplicate") and any(e["rel"] in d["members"][1:] for d in duplicates):
            actions.append({
                "op": "deduplicate",
                "src": e["rel"],
                "src_path": e["path"],
                "dest": dest,
                "sha256": e["sha256"],
                "canonical": next(d["canonical"] for d in duplicates if e["rel"] in d["members"]),
            })
            continue
        actions.append({"op": "copy", "src": e["rel"], "src_path": e["path"], "dest": dest, "sha256": e["sha256"]})
    return resolved, actions


def _dest_path(entry, rules, chains):
    rel = Path(entry["rel"])
    name = rel.name
    if rules.get("naming") == "normalize":
        name = _normalize_filename(name)

    template = rules.get("template")
    if template:
        parts = _template_parts(template, entry, chains)
        return str(Path(*parts) / name) if parts else name

    layout = rules.get("layout", "by_type")
    if layout == "by_type":
        return str(Path(entry["kind"]) / name)
    if layout == "by_year":
        year = (re.search(r"(20\d{2})", name) or [None, "undated"])[1]
        return str(Path(year) / name)
    if layout == "flat":
        return name
    return str(Path(entry["kind"]) / name)


def _template_parts(template, entry, chains):
    parts = []
    for tok in template.split("/"):
        m = re.match(r"^\{(.+)\}$", tok)
        if not m:
            parts.append(tok)
            continue
        field = m.group(1).strip()
        val = ""
        if field == "type":
            val = entry["kind"]
        elif field == "year":
            mm = re.search(r"(20\d{2})", entry["name"])
            val = mm.group(1) if mm else "undated"
        elif field == "ext":
            val = entry["ext"].lstrip(".") or "bin"
        elif field == "logical":
            val = _logical_name(entry, chains)
        else:
            val = "misc"
        parts.append(_sanitize(str(val)))
    return parts


def _logical_name(entry, chains):
    for chain in chains:
        for m in chain["members"]:
            if m["rel"] == entry["rel"]:
                return chain["logical"].replace(" ", "_")
    return _normalize_stem(entry["name"]).replace(" ", "_")


def _normalize_filename(name: str) -> str:
    stem = Path(name).stem
    ext = Path(name).suffix
    stem = re.sub(r"[\s]+", "_", stem)
    stem = re.sub(r"[^\w一-鿿\.\-]", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return (stem or "file") + ext


def _sanitize(text: str) -> str:
    text = re.sub(r"[^\w一-鿿\.\-]", "_", text)
    return re.sub(r"_+", "_", text).strip("_") or "misc"


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def apply_plan(actions, source_root: Path, dest_root: Path):
    operations = []
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    for act in actions:
        op = act["op"]
        src = act.get("src")
        if op == "copy":
            srcp = _resolve_source(act.get("src_path") or src, source_root)
            destp = dest_root / act["dest"]
            try:
                destp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srcp, destp)
                digest = sha256_file(destp)
                ok = digest == act["sha256"]
                operations.append({
                    "op": "copy", "src": src, "dest": act["dest"],
                    "src_sha256": act["sha256"], "dest_sha256": digest,
                    "verified": ok, "status": "ok" if ok else "hash_mismatch",
                })
            except Exception as exc:  # noqa: BLE001
                operations.append({
                    "op": "copy", "src": src, "dest": act["dest"],
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                })
        elif op == "deduplicate":
            operations.append({
                "op": "deduplicate", "src": src, "kept": act.get("canonical"),
                "sha256": act.get("sha256"), "status": "ok",
            })
        elif op == "skip":
            operations.append({"op": "skip", "src": src, "reason": act.get("reason"), "status": "skipped"})
        elif op == "conflict":
            operations.append({
                "op": "conflict", "src": src, "dest": act.get("dest"),
                "reason": act.get("reason"), "status": "conflict",
            })
    return operations


def _resolve_source(src: str, source_root: Path) -> Path:
    p = Path(src)
    if p.is_absolute() and p.exists():
        return p
    cand = Path(source_root) / src
    if cand.exists():
        return cand
    return p


# ---------------------------------------------------------------------------
# search index
# ---------------------------------------------------------------------------

def build_index(inventory, metadata_by_rel, chains, duplicates):
    return {
        "files": [
            {
                "rel": e["rel"],
                "kind": e["kind"],
                "sha256": e["sha256"],
                "size": e["size"],
                "metadata": metadata_by_rel.get(e["rel"], {}),
            }
            for e in inventory
        ],
        "version_chains": chains,
        "duplicates": duplicates,
    }


def answer_questions(questions, index):
    """Answer free-text questions against the repaired index, honestly.

    Each answer records how it was derived. A question that cannot be grounded
    in the index is marked answered=False with an explicit note — never guessed.
    """
    answers = []
    for q in questions:
        if isinstance(q, dict):
            text = q.get("q") or q.get("question") or q.get("query") or json.dumps(q, ensure_ascii=False)
            qid = q.get("id") or q.get("qid")
        else:
            text = str(q)
            qid = None
        low = text.lower()
        derived = None
        method = None

        if "duplicate" in low:
            dups = index.get("duplicates", [])
            derived = {
                "count": len(dups),
                "groups": [
                    {"sha256": d["sha256"][:12], "members": d["members"], "kept": d["canonical"]}
                    for d in dups
                ],
            }
            method = "content_hash_grouping"

        if derived is None and ("version" in low or "canonical" in low or "latest" in low or "newest" in low):
            chains = index.get("version_chains", [])
            derived = [
                {"logical": c["logical"], "oldest": c["oldest"], "newest": c["newest"], "members": len(c["members"])}
                for c in chains
            ]
            method = "version_chain"

        if derived is None:
            tokens = [t for t in re.findall(r"[a-z0-9_]{3,}", low)
                      if t not in {"the", "and", "for", "what", "which", "list", "find", "show", "who", "when", "how"}]
            hits = []
            for e in index.get("files", []):
                hay = (e["rel"] + " " + json.dumps(e.get("metadata", {}), ensure_ascii=False)).lower()
                if all(t in hay for t in tokens):
                    hits.append({"rel": e["rel"], "metadata": e.get("metadata", {})})
            if hits:
                derived = hits
                method = "metadata_lookup"

        answers.append({
            "id": qid,
            "question": text,
            "answered": derived is not None,
            "method": method,
            "answer": derived,
            "note": None if derived is not None else "no record in the repaired library grounds this question",
        })
    return answers


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)


def cmd_inventory(args):
    case = load_case(Path(args.case))
    base = Path(args.case).parent if Path(args.case).is_file() else Path(args.case)
    files = collect_input_files(case["inputs"], base)
    roots = [str(Path(i).resolve()) for i in case["inputs"] if Path(i).exists()] or [str(base)]
    inventory = build_inventory(files, roots)
    out = Path(args.out)
    write_json(out / "inventory.json", {
        "case_id": case["case_id"],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "input_roots": roots,
        "file_count": len(inventory),
        "files": inventory,
    })
    print(f"inventory: {len(inventory)} files -> {out / 'inventory.json'}")
    return case, inventory, roots


def cmd_plan(args):
    case = load_case(Path(args.case))
    base = Path(args.case).parent if Path(args.case).is_file() else Path(args.case)
    files = collect_input_files(case["inputs"], base)
    roots = [str(Path(i).resolve()) for i in case["inputs"] if Path(i).exists()] or [str(base)]
    inventory = build_inventory(files, roots)
    duplicates = find_duplicates(inventory)
    chains = build_version_chains(inventory, case["versions"])
    rules, actions = plan_organization(inventory, case["rules"], duplicates, chains)
    out = Path(args.out)
    write_json(out / "plan.json", {
        "case_id": case["case_id"],
        "rules": rules,
        "actions": actions,
        "duplicates": duplicates,
        "version_chains": chains,
    })
    print(f"plan: {len(actions)} actions -> {out / 'plan.json'}")
    return case, inventory, roots, duplicates, chains, rules, actions


def cmd_apply(args):
    case = load_case(Path(args.case))
    base = Path(args.case).parent if Path(args.case).is_file() else Path(args.case)
    files = collect_input_files(case["inputs"], base)
    roots = [str(Path(i).resolve()) for i in case["inputs"] if Path(i).exists()] or [str(base)]
    inventory = build_inventory(files, roots)
    duplicates = find_duplicates(inventory)
    chains = build_version_chains(inventory, case["versions"])
    rules, actions = plan_organization(inventory, case["rules"], duplicates, chains)

    out = Path(args.out)
    org_root = out / "library"
    operations = apply_plan(actions, base, org_root)

    # metadata + sidecars
    sidecars = _load_sidecars(inventory)
    metadata_by_rel = {}
    provenance_by_rel = {}
    for e in inventory:
        meta, prov = extract_metadata(e, inventory, sidecars)
        metadata_by_rel[e["rel"]] = meta
        provenance_by_rel[e["rel"]] = prov

    index = build_index(inventory, metadata_by_rel, chains, duplicates)
    answers = answer_questions(case["questions"], index)

    write_json(out / "operations.json", {
        "case_id": case["case_id"],
        "operations": operations,
        "counts": _count_ops(operations),
    })
    write_json(out / "index.json", index)
    write_json(out / "metadata.json", {"metadata": metadata_by_rel, "provenance": provenance_by_rel})
    write_json(out / "answers.json", {"answers": answers})

    # manifest of the organized tree
    manifest = _manifest(org_root)
    write_json(out / "manifest.json", manifest)

    print(f"apply: {len(operations)} operations, org tree at {org_root}")
    return case, inventory, operations, index, chains, duplicates, answers, org_root


def _load_sidecars(inventory):
    """Map relative path -> sidecar metadata dict (foo.json next to foo.pdf)."""
    sidecars = {}
    by_abs = {e["path"]: e for e in inventory}
    for e in inventory:
        fp = Path(e["path"])
        for ext in (".json", ".yaml", ".yml", ".txt"):
            side = fp.parent / (fp.stem + ext)
            if side.exists() and side.resolve() != fp.resolve():
                try:
                    sidecars[e["rel"]] = _load_json(side)
                except Exception:
                    pass
    return sidecars


def _count_ops(operations):
    counts = {}
    for op in operations:
        counts[op["op"]] = counts.get(op["op"], 0) + 1
    return counts


def _manifest(root: Path):
    entries = []
    if not root.exists():
        return {"root": str(root), "files": []}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            entries.append({
                "path": str(p.relative_to(root)),
                "size": p.stat().st_size,
                "sha256": sha256_file(p),
            })
    return {"root": str(root), "file_count": len(entries), "files": entries}


def cmd_report(args):
    case = load_case(Path(args.case))
    out = Path(args.out)
    index = _load_json(out / "index.json") if (out / "index.json").exists() else {"files": []}
    ops = _load_json(out / "operations.json") if (out / "operations.json").exists() else {"operations": [], "counts": {}}
    manifest = _load_json(out / "manifest.json") if (out / "manifest.json").exists() else {"files": []}
    answers = _load_json(out / "answers.json") if (out / "answers.json").exists() else {"answers": []}

    failed = [o for o in ops.get("operations", []) if o.get("status") in {"failed", "hash_mismatch"}]
    skipped = [o for o in ops.get("operations", []) if o.get("status") == "skipped"]
    conflicts = [o for o in ops.get("operations", []) if o.get("status") == "conflict"]

    result = {
        "case_id": case["case_id"],
        "task": "research-file-library-repair",
        "status": "complete" if not failed else "partial",
        "deliverables": {
            "organized_library": "library/",
            "manifest": "manifest.json",
            "index": "index.json",
            "metadata": "metadata.json",
            "operations": "operations.json",
            "answers": "answers.json",
        },
        "counts": {
            "source_files": len(index.get("files", [])),
            "organized_files": len(manifest.get("files", [])),
            "operations": ops.get("counts", {}),
            "failed": len(failed),
            "skipped": len(skipped),
            "conflicts": len(conflicts),
            "duplicates": len(index.get("duplicates", [])),
            "version_chains": len(index.get("version_chains", [])),
        },
        "answers": answers.get("answers", []),
        "uncertainty": _uncertainty(case, index, failed, skipped),
        "sources": case["inputs"],
        "changes": [
            {"step": "inventory", "detail": f"{len(index.get('files', []))} source files hashed (sha256)"},
            {"step": "organize", "detail": f"{ops.get('counts', {})} operations applied to library/"},
        ],
    }
    write_json(out / "result.json", result)
    print(f"report: {out / 'result.json'} status={result['status']}")
    return result


def _uncertainty(case, index, failed, skipped):
    notes = []
    if failed:
        notes.append(f"{len(failed)} operations failed; see operations.json")
    if skipped:
        notes.append(f"{len(skipped)} files skipped; see operations.json")
    if not case["rules"]:
        notes.append("no explicit directory rules in case.json; default by_type layout used")
    if not case["versions"]:
        notes.append("no administrative version notes supplied; version chains inferred from filenames + mtime")
    return notes


def main(argv=None):
    parser = argparse.ArgumentParser(description="libflow — research file-library repair pipeline")
    parser.add_argument("--version", action="version", version=f"libflow {VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("inventory", "plan", "apply", "report"):
        p = sub.add_parser(name)
        p.add_argument("--case", required=True)
        p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    {"inventory": cmd_inventory, "plan": cmd_plan, "apply": cmd_apply, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
