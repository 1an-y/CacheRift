# Implementation Plan: v0.2c Audited Repair (`v0.2c`)

## 1. 模块边界

```text
src/cacherift/repair/
├── __init__.py
├── models.py                   # RepairEdit、RepairAuditEntry、RepairResult
├── rules.py                    # 4 条白名单规则，全部是 (body, 父轮信息) -> (新 body, edits) | None 的纯函数
├── engine.py                   # repair_records：安全阀、规则顺序、逐轮传播、哈希审计
└── cli.py                      # `cacherift repair`
tests/
└── test_repair.py              # R1–R5
```

依赖方向：`repair/*` → `core`（`freeze`、`models`）；`core` 不感知 `repair`。规则不导入方言适配器，只依赖请求体的 JSON 结构。

## 2. 关键契约与实现细节

- **`repair/rules.py`**：
  - `ALL_RULES`、`RULE_ORDER = (stabilize_tool_array_order, canonicalize_tool_key_order, migrate_gpt62_top_level_instructions, normalize_anthropic_breakpoints)`。先恢复数组顺序，再按键序对齐，后两条与 `tools` 无关。
  - 「仅键序不同」的判定沿用诊断内核同一口径：`canonical_project_bytes(sort_keys=True)` 相等。这是结构等价判定，不用于前缀等值，也不产出任何字节。
  - 工具名取 `name`，或 Chat Completions 形态的 `function.name`；都没有则只按内容匹配。
  - 每条规则只在真的改了东西时返回结果；返回的新 body 是浅拷贝加被替换的节点，未涉及的子树保持原对象。
- **`repair/engine.py`**：`repair_records(records, diagnoses, rules=ALL_RULES) -> RepairResult`。三种拒绝原因：`ambiguous_lineage`、`unsupported_capability`、`parent_not_repaired`。
  - `records` 与 `diagnoses` 必须一一对应（`analyze_records` 的输出顺序即输入顺序）。
  - 安全阀先于一切规则；`migrate_gpt62_top_level_instructions` 的适用性取自 `diag.capability.unit_order`。`ParentTurn.body` 是父轮修复后的请求体，`ParentTurn.top_level_instructions` 取自父轮**原始**请求体：迁移规则靠「父轮修复后的请求体里已经没有 `instructions`」来判断父轮确实迁移过。
  - 维护 `effective_body[record_id]`：被修复的记录记修复后的 body，其余记原始 body，供子轮作基准。
  - 修复前哈希：有 `captured_body_bytes` 时对原始字节计算（`wire_raw`），否则对 `canonical_project_bytes(parsed_body)` 计算（`projected`）；修复后恒对 `canonical_project_bytes(new_body)` 计算。
- **`repair/cli.py`**：`--rule` 可重复；`all` 展开为 `ALL_RULES`。dry-run 只向 stdout 输出摘要；`--apply --output` 写修复后的 JSONL；`--audit-out` 写审计 JSON。写文件前检查目标是否存在。

## 3. 对既定计划的取舍

- 计划把修复后的请求体写成「补丁」。实现把产物定为一份可被 `analyze` / `check` 直接读取的修复后 JSONL，外加审计 JSON：前者才是「可回放」的载体，审计负责「完整字节审计」。
- 计划未规定被修复记录的坐标系。按宪法「重序列化的 JSON 不得冒充原始请求体」，修复后的记录只能是 `parsed_body`（`PROJECTED`），不能伪造 `captured_body_bytes`。
- 计划的 `migrate_gpt62_top_level_instructions` 写的是「`openai_responses` + `gpt-6.2*`」。实现以能力表的 `unit_order` 为触发条件：这是诊断内核判定 `non_cacheable_top_level_instructions` 用的同一个事实来源，写死模型名等于在代码里复制一份能力表。
- 计划未提 `--apply` / `--output` 的参数约束与覆盖保护。实现补上：这是唯一会写「修改过的请求体」的命令，误覆盖一份真实抓包是不可恢复的损失。
