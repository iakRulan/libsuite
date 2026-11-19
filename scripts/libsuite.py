#!/usr/bin/env python3
"""libsuite — one pipeline for the research-library track.

Usage:
    python3 libsuite.py --case /input/case.json --work /app/work

Modes (from case.modes / case.tasks, else inferred):
  organize   inventory + content-hash dedup + version chains + manifest (libflow)
  search     keyword/field search over the repaired index
  classify   assign each file to a category using dictionary rules + name patterns
  qa         answer the case's questions from the index (grounded or unanswered)
  annotate   build an annotation / knowledge-deposition layer (notes -> files)

Every answer and label keeps a source path + hash. Nothing is invented: a file
that cannot be classified is `undecided`, and a question without support is
`unanswered`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import libflow as L

VERSION = "1.0.0"


def load(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def load_case(case_path: Path) -> dict:
    case_path = Path(case_path)
    if case_path.is_dir():
        return {"case_id": case_path.name, "modes": ["organize"], "raw": {}, "inputs": []}
    raw = load(case_path)
    case = dict(raw) if isinstance(raw, dict) else {"value": raw}
    modes = as_list(case.get("modes") or case.get("tasks"))
    task = str(case.get("task") or case.get("task_type") or "").lower()
    if not modes:
        for key, m in (("search", "search"), ("classif", "classify"), ("qa", "qa"),
                       ("answer", "qa"), ("annotat", "annotate"), ("note", "annotate"),
                       ("organ", "organize"), ("version", "organize")):
            if key in task:
                modes = [m]
                break
        modes = modes or ["organize"]
    return {
        "case_id": case.get("case_id") or case.get("id") or case_path.stem,
        "raw": case,
        "modes": modes,
        "inputs": as_list(case.get("inputs") or case.get("input_dir") or case.get("files")),
        "rules": case.get("rules") or {},
        "categories": case.get("categories") or case.get("classes") or {},
        "questions": as_list(case.get("questions") or case.get("queries")),
        "notes": as_list(case.get("notes") or case.get("annotations")),
        "query": case.get("query") or case.get("search") or "",
    }


def build_library(case, base: Path, work: Path):
    """Run the libflow organize phase and return (inventory, index, library_root)."""
    files = L.collect_input_files(case["inputs"], base)
    roots = [str(Path(i).resolve()) for i in case["inputs"] if Path(i).exists()] or [str(base)]
    inventory = L.build_inventory(files, roots)
    duplicates = L.find_duplicates(inventory)
    chains = L.build_version_chains(inventory, case["raw"].get("version_notes") or {})
    rules, actions = L.plan_organization(inventory, case["rules"], duplicates, chains)
    lib_root = work / "library"
    operations = L.apply_plan(actions, base, lib_root)
    index = L.build_index(inventory, {}, chains, duplicates)
    return inventory, index, lib_root, operations, duplicates, chains


def classify(entry, categories):
    """Assign a category by explicit dictionary rules then name patterns."""
    if not categories:
        return "undecided", "no categories supplied"
    name = (entry.get("name") or "") + " " + (entry.get("rel") or "")
    meta = entry.get("metadata") or {}
    for cat, spec in categories.items():
        if isinstance(spec, dict):
            pats = spec.get("patterns") or spec.get("match") or []
            if isinstance(pats, str):
                pats = [pats]
            for p in pats:
                if re.search(str(p), name, re.IGNORECASE):
                    return cat, f"matched pattern {p}"
            exts = [str(e).lower() for e in as_list(spec.get("extensions"))]
            if entry.get("ext", "").lower() in exts:
                return cat, "matched extension"
        elif isinstance(spec, str) and re.search(spec, name, re.IGNORECASE):
            return cat, f"matched pattern {spec}"
    return "undecided", "no category matched"


def search_index(query, inventory):
    terms = [t for t in re.findall(r"[a-z0-9_]{2,}", (query or "").lower())]
    hits = []
    for e in inventory:
        hay = (e["rel"] + " " + e["name"] + " " + e["kind"]).lower()
        matched = [t for t in terms if t in hay]
        if matched:
            hits.append({"rel": e["rel"], "sha256": e["sha256"], "kind": e["kind"],
                         "matched": matched, "score": len(matched)})
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits


def answer_questions(questions, inventory):
    out = []
    for i, q in enumerate(questions):
        qid = str(q.get("id") or q.get("question_id") or f"Q{i+1}") if isinstance(q, dict) else f"Q{i+1}"
        text = (q.get("question") or q.get("q") or str(q)) if isinstance(q, dict) else str(q)
        hits = search_index(text, inventory)
        out.append({
            "question_id": qid, "question": text,
            "answered": bool(hits),
            "answer": None,
            "evidence": [{"rel": h["rel"], "sha256": h["sha256"], "matched": h["matched"]} for h in hits[:5]],
            "note": None if hits else "库中无可定位依据",
        })
    return out


def build_annotations(notes, inventory):
    by_rel = {e["rel"]: e for e in inventory}
    out = []
    for i, n in enumerate(notes):
        if isinstance(n, dict):
            target = n.get("target") or n.get("file") or ""
            text = n.get("text") or n.get("note") or ""
        else:
            target, text = "", str(n)
        hit = by_rel.get(target)
        out.append({
            "note_id": f"N{i+1}", "target": target, "text": text[:300],
            "resolved": hit is not None,
            "sha256": hit["sha256"] if hit else None,
            "note": None if hit else "目标文件不在库中",
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--work", required=True)
    args = ap.parse_args(argv)

    case = load_case(Path(args.case))
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    base = Path(args.case).parent if Path(args.case).is_file() else Path(args.case)

    inventory, index, lib_root, operations, duplicates, chains = build_library(case, base, work)

    outputs = {}
    outputs["library"] = "library/"
    save(work / "operations.json", {"case_id": case["case_id"], "operations": operations,
                                    "counts": L._count_ops(operations)})
    save(work / "index.json", index)
    outputs["index"] = "index.json"
    outputs["operations"] = "operations.json"

    if "classify" in case["modes"]:
        labels = []
        for e in inventory:
            cat, reason = classify(e, case["categories"])
            labels.append({"rel": e["rel"], "category": cat, "reason": reason, "sha256": e["sha256"]})
        save(work / "classification.json", {"case_id": case["case_id"], "labels": labels})
        outputs["classification"] = "classification.json"
    if "search" in case["modes"]:
        hits = search_index(case["query"], inventory)
        save(work / "search_results.json", {"case_id": case["case_id"], "query": case["query"], "hits": hits[:50]})
        outputs["search"] = "search_results.json"
    if "qa" in case["modes"]:
        ans = answer_questions(case["questions"], inventory)
        save(work / "answers.json", {"case_id": case["case_id"], "answers": ans})
        outputs["answers"] = "answers.json"
    if "annotate" in case["modes"]:
        ann = build_annotations(case["notes"], inventory)
        save(work / "annotations.json", {"case_id": case["case_id"], "annotations": ann})
        outputs["annotations"] = "annotations.json"

    counts = {"files": len(inventory), "duplicates": len(duplicates), "version_chains": len(chains)}
    if "classify" in case["modes"]:
        counts["undecided"] = sum(1 for e in inventory if classify(e, case["categories"])[0] == "undecided")
    if "qa" in case["modes"]:
        counts["unanswered"] = sum(1 for a in answer_questions(case["questions"], inventory) if not a["answered"])

    result = {
        "case_id": case["case_id"],
        "task": "research-library-suite",
        "status": "complete" if inventory else "partial",
        "modes": case["modes"],
        "deliverables": outputs,
        "counts": counts,
        "uncertainty": ["分类基于字典与文件名模式，未命中者标 undecided；问答仅依据库内文件。"],
        "sources": [str(Path(args.case))],
        "changes": [{"step": "organize", "detail": f"{len(inventory)} files"},
                    {"step": "run", "detail": ",".join(case["modes"])}],
    }
    save(work / "result.json", result)
    print(f"libsuite: modes={case['modes']} files={len(inventory)} -> {work / 'result.json'}")
    return result


if __name__ == "__main__":
    main()
