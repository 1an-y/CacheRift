# Feature Spec: v0.2b 结构化回归门禁与 pytest 集成（`cacherift check`）

> 本迭代遵守 `specs/00-constitution.md`。输入是 v0.1 诊断内核的输出，本迭代不新增诊断逻辑，也不设任何命中率红线。

## 范围界定

- **新增命令** `cacherift check`：对 JSONL 捕获按「结构断链规则」评估，返回 `0`（通过）、`1`（违例）或 `2`（配置错误），支持 `--format table|json|github`。
- **pytest 插件**：`cacherift_session` fixture 与 `assert_prefix_stable(records, policy=...)`，让研发在没有真实 LLM API 的单测里直接检验 Agent Prompt 构造器的跨轮前缀稳定性。通过 `pytest11` 入口自动加载。
- **本期不做**：固定命中率红线（宪法禁止）、自动修复（归 `repair`）、对 `usage_only` 方言的强判定（沿用诊断内核对这些方言的既有结论，不额外加严）。

## 核心行为规格

1. **纯函数评估**：`evaluate_diagnoses(diagnoses, policy) -> CheckReport` 不做任何 I/O，不读取正文片段，只消费 `TurnDiagnosis` 里已有的分级结论。
2. **规则 ID 命名空间**（三类，互不重名）：
   - 变更类型：`wire_only_diff`、`normal_append`、`mutated_existing`、`deleted_existing`、`reordered_existing`、`compacted_or_truncated`、`inserted_before_tail`、`config_changed`，匹配 `observed_changes[].change_kind`；
   - 候选原因码：匹配 `candidate_causes[].code`，例如 `non_cacheable_top_level_instructions`、`ambiguous_lineage`、`unexplained_low_hit`、`warm_cache_without_local_baseline`、`partial_prefix_cache_hit`、`possible_ttl_expiry`；
   - 特殊规则：`usage_invalid` / `usage_missing` / `usage_incomplete`（`usage.state`）与 `no_baseline`（`lineage.status` 为 `NO_BASELINE`）。
   - 与某个变更类型一一对应的原因码（如 `existing_prefix_mutated`）是**回声码**：不单独产生 finding（同一处变更不报两次），在配置里出现时视为对应变更类型的别名。
3. **默认策略**：
   - `error`：`mutated_existing`、`reordered_existing`、`deleted_existing`、`inserted_before_tail`、`non_cacheable_top_level_instructions`、`usage_invalid`；
   - `warning`：`compacted_or_truncated`、`config_changed`、`wire_only_diff`、`ambiguous_lineage`、`unexplained_low_hit`；
   - 其余一律放行，包括 `normal_append`（C2）、`warm_cache_without_local_baseline`（C3）与首轮 `no_baseline`。
   - 末尾会话单元上的 `mutated_existing` 默认同样是 error。它确实不是前缀断裂（见 `specs/02` 第 11 条，零命中时诊断器照样给出 `unexplained_low_hit`），但从该单元起的缓存实实在在丢了。确认这是预期行为的团队，用 `warning = ["mutated_existing"]` 降级，或用 `ignore_paths` 豁免末尾路径。
4. **策略配置**：从 `cacherift.toml` 的 `[check]` 或 `pyproject.toml` 的 `[tool.cacherift.check]` 读取，或用 `--config <path>` 指定文件。四个键 `error` / `warning` / `ignore` / `ignore_paths`。**配置里点名的规则以配置为准，没点名的沿用默认值**：把一条默认 error 降级只需写一行 `warning = ["<规则>"]`，不会连带清掉其他默认项。同一规则同时被点名到多个列表时，优先级为 `ignore` > `error` > `warning`。出现这四个键之外的键同样报错。未指定 `--config` 时依次查找当前目录的 `cacherift.toml`、`pyproject.toml`，都没有配置表则使用默认策略。
5. **未知规则 ID**：配置里出现合法 ID 之外的字符串，抛 `PolicyError`，CLI 退出 `2` 并列出全部合法 ID。宁可报错，不让拼错的规则静默失效。
6. **`ignore_paths`**：作用于 `observed_changes` 的 glob 模式列表，`[*]` 匹配任意下标，`*` 不跨越 `.`。路径命中的变更记为 `ignored`：在报告里透明列出，但不计入退出码。例如用 `$.messages[*].content[*].cache_control` 豁免 Anthropic 正常前移的会话内断点，而根级 `$.cache_control` 或 `$.tools[0].cache_control` 不受影响。
7. **行号**：finding 的 `line` 是该记录在输入序列中的序号（从 1 起），与「每行一条记录」的 JSONL 文件行号一致。
8. **默认零正文**：三种输出格式只有记录号、行号、规则 ID、JSON 路径、分级与诊断器已有的原因摘要，不含任何提示词或工具 Schema 文本。
9. **退出码**：`0` 没有 error；`1` 至少一个 error（warning 与 ignored 不影响）；`2` 配置错误——找不到捕获文件、TOML 无法解析、未知规则 ID、文件里没有配置表（显式 `--config` 时）。
10. **github 格式**：只输出工作流命令，不输出其他文本：`::error file=<capture>,line=<n>::<record>: <rule> <json_path>`，warning 同理。
11. **pytest 插件**：
    - fixture `cacherift_session` 返回一个会话对象。`add(body)` 接受请求体（`Mapping` 为解析态，`bytes` 为线上原始字节）并产出一条 `CaptureRecord`；所有记录属于同一个显式会话，每轮的父节点就是上一轮。
    - `session.assert_stable(policy=None)` 在有 error 时抛 `AssertionError`，消息逐条列出记录号、规则与路径；`session.report(policy=None)` 返回 `CheckReport`。
    - `assert_prefix_stable(records, policy=None)` 对现成的记录序列做同样的断言。
    - fixture 在 teardown 时不做隐式断言：断言必须显式调用。
