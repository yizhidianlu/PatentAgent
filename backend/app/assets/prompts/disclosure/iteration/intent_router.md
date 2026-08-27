---
source_repo: handsomestWei/patent-disclosure-skill
source_path: prompts/disclosure/iteration_context.md（意图判别表与「禁止」节）
source_url: https://github.com/handsomestWei/patent-disclosure-skill
treatment: adapted
ported_version: 2026-08-25.1
notes: 由 iteration_context.md 的意图判别改编为 STRUCT 分类调用（IT0 意图路由）。意图→模板对照表逐字保留（模板名改为平台内路径）；Read/落盘/修订记录等宿主流程改由平台引擎执行；新增 question/new_case 两类与 JSON 输出契约。
---

# 迭代 · 意图路由（IT0 · STRUCT 调用）

## 任务

用户在已有交底书定稿上继续输入（文字说明和/或新材料）。判定其意图类别，仅输出一个 JSON 围栏块；后续流程（合并 / 纠正 / 答疑 / 重开主流程）由平台按你的分类路由。

## 意图判别（原对照表，逐字）

| 意图 | 下一步模板 |
|------|------------|
| 补充文档、扩展方案、合并新材料 | `iteration/merger.md` |
| 指出错误、与事实/参数不符、风格或保护点调整、术语太抽象或与标题不对齐 | `iteration/correction.md` |
| 用户已按「权利要求偏向点」交互声明侧重点，仅需 **第五章权利要求书式强化**（取向须与本稿已有材料及第五、三章已写观点一致，**禁止**为交互而编造新场景） | `iteration/merger.md`（以最近定稿为基准，合并范围以第五章为主，必要时微调第四章与第五章衔接句） |

补充两类：

| 意图 | 分类 |
|------|------|
| 仅就当前稿提问、要求解释，不要求改稿 | `question` |
| 明确要求重新挖掘专利点 / 从头再走查新 / 另立新案 | `new_case` |

## 判别原则

- 已判定为迭代意图（merge/correct）时，不得分类为 `new_case` 去重跑全文专利点挖掘。
- 例外：用户**明确要求**「重新挖掘专利点 / 从头再走查新」时，分类为 `new_case`。
- 不要求用户说出「迭代」「合并」「纠正」等固定词；按语义判断。补材料、扩展方案为主 → `merge`；指出错误、要求改表述为主 → `correct`；两者兼有时取工作量大者，并在 rationale 说明。

## 输出契约（仅输出一个 JSON 围栏块）

```json
{
  "intent": "merge",
  "rationale": "判类依据（1-2 句）",
  "evidence": "用户原话关键片段摘录"
}
```

`intent` ∈ `merge` | `correct` | `question` | `new_case`。
