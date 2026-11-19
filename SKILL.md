---
name: libsuite
description: "One pipeline for the research-library track: inventory with content hashes, organize/dedupe/version, file search over the index, rule-based classification, grounded Q&A over the library, and an annotation/knowledge-deposition layer. Trigger on: '科研资料管理', '文件智能检索', '资料自动分类', '知识库问答', '阅读批注与知识沉淀', 'library suite', 'classify files', 'annotate documents'. Not for: paper writing, figure drawing, or live database search."
---

# libsuite — Research-library Suite

Cover the library track with one pipeline: the same repaired index feeds search, classification, Q&A and annotation. The failure mode this skill exists to prevent: labels with no reason, answers with no source, notes pointing at files that do not exist, and duplicates treated as distinct records.

**Method in one line:** inventory + organize (hash dedup, version chains) → build the index → run the declared modes → every result keeps a path + hash.

## When to use
- Input: case file (default `/input/case.json`) with `modes`/`tasks`, `inputs`, `rules`, `categories`, `questions`, `notes`, `query`.
- Output: under `/app/output` — `library/`, `index.json`, `operations.json`, `classification.json`, `search_results.json`, `answers.json`, `annotations.json`, `result.json`.

## Modes
| mode | what it does |
|-|-|
| `organize` | content-hash dedup, version chains, naming/layout rules, operations log |
| `classify` | dictionary patterns + extension rules; unmatched → `undecided` with reason |
| `search` | keyword/field search over the index, ranked with matched terms |
| `qa` | answer each question from the index; `unanswered` when unsupported |
| `annotate` | notes → target files; unresolved targets flagged |


## Pipeline

```bash
python3 /skills/weekly-skill/scripts/libsuite.py --case /input/case.json --work /app/work
```

> **脚本路径**：本技能安装于 `/skills/weekly-skill/`，脚本绝对路径为 `/skills/weekly-skill/scripts/libsuite.py`（工作目录是 `/app/work`，不要用相对路径）。

## Rules that must hold
1. **Dedup by content hash**, version chains by logical name + mtime + hash.
2. **Every label has a reason**; `undecided` when nothing matches.
3. **Every answer cites a file + hash**; unsupported questions are `unanswered`.
4. **Annotations resolve to real files**; unresolved targets are reported.
5. **No silent skips** — all operations logged.

## Exception handling
| Situation | Behaviour |
|-|-|
| No inputs | `partial`, empty library |
| No categories | all `undecided` |
| Note target missing | `resolved: false` |
| Question with no hit | `unanswered` with note |

## Self-check
```bash
python3 -c "import json;r=json.load(open('/app/work/result.json'));print(r['case_id'], r['status'], r['modes'], r['counts'])"
python3 -c "import json;l=json.load(open('/app/work/classification.json'))['labels'];print(all('reason' in x for x in l))"
```
