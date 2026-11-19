# 输出字段字典

## `classification.json`
`labels[]{rel, category, reason, sha256}`

## `search_results.json`
`{query, hits[]{rel, sha256, kind, matched[], score}}`

## `answers.json`
`answers[]{question_id, question, answered, answer, evidence[]{rel, sha256, matched}, note}`

## `annotations.json`
`annotations[]{note_id, target, text, resolved, sha256, note}`

## `result.json`
`case_id` / `task` / `status` / `modes[]` / `deliverables{}` / `counts{files,duplicates,version_chains,undecided,unanswered}` / `uncertainty` / `sources` / `changes`
