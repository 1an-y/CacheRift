# v0.1.5 Tasks（按顺序逐片推进）

- [x] **T1 内核增补：仅末尾单元改写不算前缀断裂**：`core/diagnoser.py` 增加 `current_units` 关键字参数与 `tail_only_edit` 判定，`DiagnosisSession` 传入当前单元。
  - 验收：`.venv/bin/pytest tests/test_probe.py -k "p5 or tail_only"` + `tests/test_diagnoser_counterexamples.py`（C5 行为不变）
- [x] **T2 预算与最坏成本**：实现 `probe/budget.py`（`ProbeBudget`、`BudgetTracker`、`BudgetExceededError`、`worst_case_cost`、场景整组纳入）。
  - 验收：`.venv/bin/pytest tests/test_probe.py -k "budget"`（P2 的单元层：越界请求前抛错、未登记价表返回 `None`）
- [x] **T3 确定性场景**：实现 `probe/scenarios.py`，直接构造 UTF-8 bytes。
  - 验收：`.venv/bin/pytest tests/test_probe.py -k "scenario"`（wire 变体解析等价而字节不同、请求数、跨方言形状）
- [x] **T4 Runner**：实现 `probe/runner.py`（计划、发送、脱敏、结算、落 `CaptureRecord`）。
  - 验收：`.venv/bin/pytest tests/test_probe.py -k "runner"`（P2、P3、P4、P5 端到端，`httpx.MockTransport` 零网络）
- [x] **T5 CLI**：实现 `probe/cli.py` 并接入 `cli.py` 分发表。
  - 验收：`.venv/bin/pytest tests/test_probe.py`（P1–P5 全绿）+ `.venv/bin/ruff check src tests` + `.venv/bin/mypy src tests`
