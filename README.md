# libsuite — 科研资料管理综合 Skill

一条管线覆盖资料管理赛道：整理（哈希去重/版本链）、智能检索、自动分类、知识库问答、批注沉淀。

## 依赖
- Python 3.9+，仅标准库；同目录 `libflow.py`、`libverify.py`。

## 用法
```bash
python3 scripts/libsuite.py --case /input/case.json --work /app/work
```

输入示例：
```json
{
  "case_id": "demo",
  "modes": ["organize", "classify", "search", "qa"],
  "inputs": ["input"],
  "categories": {"report": {"patterns": ["report"]}},
  "questions": [{"id": "Q1", "question": "report version"}],
  "query": "report"
}
```
> 示意示例，不是正式案例结果。

## 输出
| 文件 | 内容 |
|-|-|
| `library/` | 归位后的目录树 |
| `index.json` | 文件 + 元数据 + 版本链 + 重复组 |
| `operations.json` | 操作日志 |
| `classification.json` | 每文件类别 + 理由 |
| `search_results.json` | 命中（含 matched terms） |
| `answers.json` | 逐题回答 + 证据文件/哈希 |
| `annotations.json` | 批注 → 文件解析结果 |

## 关键设计
内容哈希去重；版本链；分类有理由、未命中标 undecided；问答引用文件+哈希；批注目标解析校验。

## 局限
- 分类依赖字典/文件名；语义分类需模型补充。

## 版本
libsuite 1.0.0。
