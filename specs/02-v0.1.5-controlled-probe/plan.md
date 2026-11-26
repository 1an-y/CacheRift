# Implementation Plan: v0.1.5 Controlled Probe (`v0.1.5`)

## 1. 模块边界

```text
src/cacherift/
├── cli.py                         # 分发表；子命令模块提供 register_parser / run_command
├── io/
│   ├── capture.py                 # JSONL 读写（v0.1，不变）
│   └── redact.py                  # [新增] 请求头脱敏、密钥片段提取、响应体密钥擦除
├── core/
│   └── diagnoser.py               # [增补] 仅末尾会话单元改写 ≠ 前缀断裂
├── capabilities/
│   └── registry.py                # [增补] PricingRegistry.default_table_id()
└── probe/
    ├── __init__.py
    ├── budget.py                  # ProbeBudget / BudgetTracker / BudgetExceededError / worst_case_cost
    ├── scenarios.py               # 5 组确定性微探针，直接产出 UTF-8 bytes
    ├── runner.py                  # ProbeRunner：计划、发送、落 CaptureRecord
    └── cli.py                     # `cacherift probe`
tests/
└── test_probe.py                  # P1–P5
```

依赖方向：`probe/*` → `cli.analyze_records`、`core`、`dialects`、`capabilities`、`io`；`core` 不感知 `probe`（由 `tests/test_span_and_freeze.py` 的 AST 守卫保证）。

## 2. 关键契约与实现细节

- **`cli.py` 分发**：各子命令模块实现 `register_parser(subparsers) -> ArgumentParser` 与 `run_command(args) -> int`。模块本身不导入可选依赖；`run_command` 先调用 `require_modules(command, extra, *modules)`，缺依赖时抛 `MissingDependencyError`，由 `main()` 打印单行提示并返回 `2`。
- **`io/redact.py`**：`redact_headers(headers)` 对名字含 `authorization`/`api-key`/`token`/`secret`/`cookie`/`key` 等标记的头部做值替换（按子串匹配，宁可多脱不可漏脱）；`secret_fragments(headers)` 取出鉴权头里的裸密钥；`scrub_secrets(value, secrets)` 在解析后的字符串上替换，避开 JSON 转义造成的漏匹配。
- **`core/diagnoser.py`**：`diagnose_turn(..., current_units=())`。当 `current_units` 非空时计算 `tail_only_edit`：破坏性变更全部是 `MUTATED_EXISTING` 且 `json_path` 等于最后一个会话单元的路径。低命中推断的条件由 `not has_breaking_change` 放宽为 `not has_breaking_change or tail_only_edit`；`partial_prefix_cache_hit` 仍用 `has_breaking_change`。
- **`probe/budget.py`**：
  - `ProbeBudget(max_requests=12, max_cost_usd=0.10, execute=False)`。
  - `worst_case_cost(identity, body_bytes, table, max_output_tokens)`：对「全未缓存」与「所选写入档全量写入」两种合成用量分别调用 `estimate_cost` 取最大值；无价表规则返回 `None`。
  - `BudgetTracker`：`reserve(worst_case_usd)` 发送前复核并抛 `BudgetExceededError`；`settle(actual_usd, worst_case_usd)` 在响应后以实际值结算（用量缺失时保留最坏估算）。
  - `fits_budget`：按场景顺序贪心，整组纳入。
- **`probe/scenarios.py`**：`build_scenarios(dialect, model, *, ttl, nonce, min_cache_tokens, prefix_tokens=None)` 返回 `ProbeScenario` 序列，每步 `ProbeStep(label, intent, body, parent)`；前缀字节按 `target_tokens * 5` 精确以 UTF-8 字节长度（含换行符）生成；`openai_responses` 下的 `baseline_identical_pair` 两步均携带固定 `prompt_cache_key = f"{nonce}:baseline_identical_pair"`。序列化样式只有两种：规范紧凑（`ensure_ascii=False`）与 wire 变体（`indent=2`、`ensure_ascii=True`，并把前缀中的 `CacheRift` 写成 `C\u0061cheRift`）。
- **`probe/runner.py`**：`ProbeRunner(identity, url=..., api_key_env=..., ttl=..., budget=..., transport=..., clock=..., prefix_tokens=...)`，`transport` 可注入（单测用 `httpx.MockTransport`）。`plan()` 产出带估算的 `ProbePlan`（`unpriced` 列出无法界定成本的场景）；单根场景共用 `<run_id>:<scenario>` 作为 `session_id`，多根场景按每个根链路拆分为 `<run_id>:<scenario>:<root>`。`preflight(plan)` 在任何请求发出之前抛出全部配置类问题（缺 Key、未登记价表、预算放不下、未开 `execute`）并返回要用的 Key；`execute(plan, on_record=...)` 产出 `ProbeRunResult(records, stopped_reason, spent_usd, warnings)`，每条记录产生时立刻回调 `on_record`，遇到 `output > MAX_OUTPUT_TOKENS` 记录告警，上游非 2xx、网络异常、预算熔断都只停止并记录原因，不丢已捕获记录。
- **`probe/cli.py`**：退出码 `0` 成功（含 dry-run），`1` 运行中途停止（熔断、上游错误、有场景被预算跳过），`2` 配置错误。支持 `--prefix-tokens` 显式覆盖前缀目标；`--max-cost-usd` 默认 `0.10`；JSON 与表格执行结果均会输出 `warnings`（统一写 `warning:` 到 stderr，JSON 同时含 `warnings` 字段）。`--execute` 且未给 `--output` 时写到当前目录 `cacherift-probe-<run_id>.jsonl`；dry-run 永不写盘。`--execute` 先 `preflight`，再以 `"x"` 模式独占创建落盘文件，然后带 `on_record` 逐条落盘。

## 3. 对既定计划的取舍

- 计划写的是「可注入 httpx.Client」；实现改为可注入 `httpx` transport（`httpx.MockTransport` 是 transport 而非 Client），由 runner 统一持有并关闭 Client。
- 计划把预算熔断写成逐请求复核；实现保留逐请求复核，并增加「场景整组纳入」这一道闸，避免探针在一个场景跑到一半时被预算打断、留下无法解读的半成品。
- 计划未规定 `--pricing-table` 缺省值；实现缺省取价表注册表中 `default-*` 的最新一张，否则 `--execute` 永远无法估算成本。
