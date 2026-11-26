# Feature Spec: v0.1 可信离线诊断闭环

## 范围界定

- **完整支持方言**：
  - `openai_responses`（含 `prompt_cache_diagnostics`、模型代际如 GPT-6.2 `instructions` 位于 `input` 后的投影顺序）
  - `anthropic_messages`（含 `diagnostics.previous_message_id`、区分根级 `$.cache_control` 与块级断点、分档写入计数桶）
- **部分支持方言（显式标注 `usage_only`）**：`openai_chat`、`deepseek_chat`、`gemini_generate_content`。
- **本期不做**：常驻代理服务器（`proxy`）、自动热修改写（`repair`）、在线探针（`probe`）、CI 门禁封装（`check`），留待 v0.1.5+。

## 核心行为规格

1. **原始定位**：存在 `captured_body_bytes` 时，通过原始 UTF-8 扫描器记录节点 `WIRE_RAW` 字节起止偏移；仅导入解析态 JSON 时，`wire_span` 必须为 `None`，差异偏移标注为 `PROJECTED`。
2. **血缘归组优先级**：
   1) 显式客户端元数据（`parent_record_id` / `client_session_id`）
   2) 上游响应 ID 引用链（`previous_response_id` / `comparison_response_id` / `diagnostics.previous_message_id`）
   3) 完整历史延续匹配
   4) 启发式共享前缀匹配（多候选同等合理时输出 `LineageStatus.AMBIGUOUS` 且 `parent_record_id=None`）
   - `prompt_cache_key` 不得作为可靠 session ID。
3. **变更分类**：以上一请求已存在单元在当前请求中是否保持前缀与边界不变为基准，精确区分：
   - `WIRE_ONLY_DIFF`
   - `NORMAL_APPEND`
   - `INSERTED_BEFORE_TAIL`
   - `MUTATED_EXISTING`
   - `DELETED_EXISTING`
   - `REORDERED_EXISTING`
   - `COMPACTED_OR_TRUNCATED`
   - `CONFIG_CHANGED`
4. **价表估算**：由独立 `pricing.py` 按指定 `pricing_table_id` 计算 `CostEstimate`；未指定价表、未知模型或非 `COMPLETE` 用量时返回 `None`，允许 `estimated_input_delta_vs_uncached_usd` 为负值。

## 强制验收矩阵（Counterexamples C1–C10）

- **C1**：仅 JSON 空白/换行/Unicode 转义变化 -> 仅报 `WIRE_ONLY_DIFF`（`WIRE_RAW`），不报结构破坏。
- **C2**：多轮正常追加（上轮 user + assistant 进入历史）-> 报 `NORMAL_APPEND`，不报历史改写。
- **C3**：首个被捕获请求已有 `cache_read > 0` -> `NO_BASELINE` 与正数读取并存，严禁判冷启动。
- **C4**：双终端共享完全相同前缀且无元数据 -> `AMBIGUOUS` 并列出全部候选父节点，且 `parent_record_id=None`。
- **C5**：请求后部变化但前部仍命中 -> `observed_changes` 与正数 `cache_read` 共存，不互斥覆盖。
- **C6**：SSE 流中断 / 缺 usage / Anthropic 累计 `message_delta.usage` -> 分别标 `UsageState.MISSING`、不补零、只取最终快照不重复累加。
- **C7**：工具数组末尾新增工具（位于 system/history 之前）-> 标 `INSERTED_BEFORE_TAIL`，不标安全追加。
- **C8**：连续两次同前缀请求均 `cache_read == 0` -> 输出 `unexplained_low_hit` + 多候选原因 + `unresolved`，严禁定责网关吞参数。
- **C9**：前缀变化与超默认 TTL 同时发生 -> `candidate_causes` 同时包含两项及各自证据，不做单选裁决。
- **C10**：仅导入解析态 dict 且未开 `--include-snippets` -> 坐标系为 `PROJECTED`（`wire_span=None`）且报告零正文片段。

## 后记（v0.1.5–v0.2c 迭代对内核的增补）

以下三项在外围命令的迭代中落地，均不改变上面 C1–C10 的断言。

- **仅末尾会话单元的改写不算前缀断裂**：`diagnose_turn` 增加关键字参数 `current_units`；没有任何破坏性变更的轮次不算「末尾改写」，仍按原口径输出。详见 `specs/02-v0.1.5-controlled-probe/spec.md` 第 11 条。
- **`missing_usage` 的 `raw_fields` 只保留响应体里的 `usage` 对象**：v0.1 把整个响应体放了进去，导致 `analyze --format json` 在没有 `--include-snippets` 时也会带出模型输出或错误文本，与「默认零正文」红线冲突。
- **用量无法解析时记 `UsageState.INVALID`**：适配器遇到无法转成数字的 token 计数这类异常数据时抛出的异常，由诊断会话转成带 `usage_not_parseable:<异常类型>` 注记的 `INVALID` 观测。流水线不再因此崩溃，也不会留下半登记的记录。`cacherift analyze` 遇到无法解析的捕获文件时同样改为单行报错并退出 `1`。
- **0 token 写入桶不阻断成本估算**：方言适配器（`openai_responses`、`anthropic_messages`）在 `cache_write_tokens == 0` 时不产出写入桶（负值仍判 `UsageState.INVALID`）；`core/pricing.py:estimate_cost()` 对 `tokens == 0` 的写入桶直接跳过，仅在存在非零写入且价表无对应写入档单价时返回 `None`。
