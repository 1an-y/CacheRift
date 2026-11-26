# Feature Spec: v0.1.5 受控主动探针（`cacherift probe`）

> 本迭代遵守 `specs/00-constitution.md`。与 v0.1 规范的关系见文末「v0.1 内核增补」。

## 范围界定

- **新增命令** `cacherift probe`：向目标上游发送小成本、字节级精确的对照微探针，把真实原始报文落盘为 JSONL，并直接复用 `analyze_records` 输出分级结论。
- **支持方言**：`openai_responses`、`anthropic_messages`（能力表 `full`）。其余方言显式拒绝（退出码 `2`），不为 `usage_only` 方言编造请求体格式。
- **可选依赖**：`httpx`（extra `probe`）。未安装时 `cacherift probe` 打印单行安装提示并返回 `2`；`analyze` 用户保持零网络库依赖。
- **本期不做**：流式（SSE）探针（归 `proxy`）、独立判定引擎（结论一律来自 `diagnose_turn`）、真实抓包入库（由维护者用测试 Key 执行 `--execute` 后人工归档到 `tests/fixtures/captured/`）。

## 核心行为规格

1. **默认 dry-run（P1）**：未传 `--execute` 时零 HTTP 调用、零写盘，逐项列出计划内每一步的记录 ID、字节数、估算 token、最坏成本与累计值，并标明哪些场景会因预算被跳过。
2. **事前熔断（P2）**：预算 `ProbeBudget(max_requests=12, max_cost_usd=0.05)` 有两道闸。
   - 场景级：场景按固定顺序整组执行，只有「整组最坏成本」同时满足剩余请求数与剩余金额时才纳入计划；不整组纳入的场景标记为跳过，不发送其中任何请求。一个场景都放不下时退出 `2` 且零请求。
   - 请求级：每次发送前 `BudgetTracker.reserve()` 复核「已实际花费 + 本请求最坏成本 ≤ 上限」，越界即抛 `BudgetExceededError`，越界请求不上线；已捕获记录保留落盘，退出 `1`。
3. **凭据红线（P3）**：API Key 只从环境变量读取（`--api-key-env`，默认 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`），不接受命令行明文。落盘 JSONL 的 `request_headers` 与一切终端/JSON 输出中，凭据类请求头的值一律替换为 `[REDACTED]`；上游响应体里回显的 Key 同样被擦除。`--base-url` 含 userinfo、query、fragment 或无法解析的端口时拒绝（退出 `2`），避免 URL 泄露进日志与报错。
4. **字节一致（P4）**：场景直接构造 UTF-8 `bytes`；发出的 `request.content` 原样写入 `captured_body_bytes`，`parsed_body` 由其解析得到，全程只有一次序列化，`WIRE_RAW` 偏移可用。
5. **结论分级（P5）**：探针结果直接走 `analyze_records`，不另写判定逻辑。基线双发均零命中时保留 `unexplained_low_hit` 与 `unresolved`，不定责网关吞参数。
6. **血缘显式**：探针是客户端，明确知道每一步的对照基线。每个场景一个 `client_session_id`，非首步显式写 `parent_record_id`（血缘 Tier 1），场景之间不互相污染候选集。
7. **成本诚实**：token 估算沿用项目既有口径 `heuristic_bytes_div_4`（原始请求体字节数 / 4），输出中标注为估算；每请求用 `max_output_tokens` / `max_tokens` 钉死输出上界。最坏成本 = max(全部输入按未缓存计价, 全部输入按所选 TTL 档写入计价) + 输出上界，通过 `core.pricing.estimate_cost` 以合成 `UsageObservation` 计算，不另写费率逻辑。响应返回后以实际用量替换估算，后续熔断按实际值复核。模型未登记价表规则时无法估算：dry-run 把相应场景标为 `unpriced`（不是「预算不足」）并在 stderr 说明 `--execute` 会被拒绝；`--execute` 直接拒绝（退出 `2`），报错指向缺失的价表规则而不是预算上限。
8. **先拒绝、后花钱**：`--execute` 把全部配置检查（是否允许执行、是否有可纳入的场景、成本能否界定、Key 是否就绪）放在第一个请求发出之前；落盘文件以独占方式创建，目标已存在或不可写同样在发请求之前拒绝（退出 `2`）。每条响应到达后立即写出并 flush，运行中途被打断时已经付费换来的抓包不会丢。
9. **缓存隔离**：共享前缀内嵌「run ID + 场景名」nonce，同一 run 内完全确定，跨 run 不复用上一次的缓存状态。
10. **响应完整性**：`response_complete` 仅当上游返回 2xx 且响应体是完整 JSON 对象时为真；HTTP 错误、网络异常、非 JSON 响应体一律为假，方言适配器据此报 `UsageState.MISSING`，不补零。遇到非 2xx 或网络异常时立即停止后续发送并退出 `1`，已捕获记录保留落盘。

## 内置场景（按执行顺序）

| 场景 | 方言 | 请求 | 步骤（`parent` 为对照基线） |
|---|---|---|---|
| `baseline_identical_pair` | 全部 | 2 | `round1` 冷写；`round2`（parent=round1）共享 ≥1200 估算 token 的确定性前缀，仅末尾短问题不同 |
| `wire_vs_key_reorder` | 全部 | 3 | `base` 规范序列化；`wire_only`（parent=base）仅改空白/缩进与 `\uXXXX` 转义，解析结构逐键相同；`reordered_keys`（parent=base）仅重排 `tools[0]` Schema 键序 |
| `tool_tail_insertion` | 全部 | 2 | `round1` tools=[T1]；`round2`（parent=round1）在 `$.tools` 末尾追加 T2，历史按轮追加 |
| `gpt62_instructions_placement` | `openai_responses` | 4 | 顶层 `$.instructions` 一对（`top_round1`、`top_round2`）对照 `$.input[0]` developer 消息一对（`dev_round1`、`dev_round2`），前缀文本相同 |
| `anthropic_breakpoints` | `anthropic_messages` | 3 | `write` system 块断点冷写；`read`（parent=write）断点不动、历史追加；`slide`（parent=read）在最后一条消息上新生断点 |

- `anthropic_messages` 下所有场景的静态前缀都带块级 `cache_control`（默认 `ttl: "5m"`；`--ttl 1h` 时同时携带 `anthropic-beta: extended-cache-ttl-2025-04-11`）。
- 共享前缀的估算 token 目标 = max(1200, 能力表 `min_cache_tokens` × 1.2)；1.2 是对估算误差的留量，不是厂商常量。能力表未登记时取 1200。

## v0.1 内核增补（`core/diagnoser.py`）

11. **仅末尾单元改写不算前缀断裂**：与父节点相比，若所有破坏性变更都是当前请求最后一个会话单元（`DisplayRole.CURRENT_INPUT`）上的 `MUTATED_EXISTING`，则 `cache_read == 0` 且血缘 `MATCHED` 且未超 TTL 时，仍然输出 `unexplained_low_hit` 及其多候选原因与 `unresolved`，证据中标注被改写的末尾路径。`partial_prefix_cache_hit` 的判定条件不变。
   - 理由：末尾单元之前的共享前缀逐字节未变，零命中无法由该变更解释；v0.1 把它一律归为「前缀被改写」会在探针基线上给出误导性结论。
   - `diagnose_turn` 新增关键字参数 `current_units`；不传时保持 v0.1 行为。`analyze_records` / `DiagnosisSession` 始终传入。

## 强制验收矩阵（Counterexamples P1–P5）

- **P1**：未传 `--execute` 时默认 dry-run，零 HTTP 调用，准确列出每步预算估算与总额。
- **P2**：`--max-cost-usd` / `--max-requests` 事前熔断，不发出越界请求；预算不足以容纳任何一个场景时零请求并退出 `2`。
- **P3**：`Authorization` / `x-api-key` 的值绝不写入 JSONL 或报告；响应体中回显的 Key 被擦除。
- **P4**：`request.content` 与 `CaptureRecord.captured_body_bytes` 逐字节一致，且等于场景构造的字节；`wire_only` 与 `base` 解析后 `order_sensitive_equal` 为真而字节不同。
- **P5**：基线双发均零命中时输出 `unexplained_low_hit` + `unresolved`，不出现任何定责网关的结论；默认旗舰模型在默认预算下 `--scenario all` 可整组纳入。
