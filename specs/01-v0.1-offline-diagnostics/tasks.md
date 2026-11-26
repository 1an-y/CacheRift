# v0.1 Tasks（按顺序逐片推进）

- [x] **T1 底座与深冻结**：实现 `core/freeze.py` 与 `core/models.py`。
  - 验收：`.venv/bin/pytest tests/test_span_and_freeze.py -k "freeze or order_sensitive or canonical or models"` + `.venv/bin/mypy src`
- [x] **T2 原始 JSON 字节扫描与捕获加载**：实现 `core/span.py`（不依赖重新序列化的 UTF-8 节点偏移扫描）与 `io/capture.py`。
  - 验收：`.venv/bin/pytest tests/test_span_and_freeze.py`（覆盖 C1、C10 原始/解析态坐标契约与核心零 I/O）
- [x] **T3 能力表与方言解析**：实现 `capabilities/registry.py`（未登记返回 `unknown`）、完整版 `openai_responses.py` / `anthropic_messages.py` 及 `usage_only` 方言。
  - 验收：`.venv/bin/pytest tests/test_dialects.py`（覆盖 Anthropic 分母、GPT-6.2 instructions 顺序、C6 流式累计与缺失）
- [x] **T4 血缘归组引擎**：实现 `core/lineage.py` 4 级优先级解析与歧义保留。
  - 验收：`.venv/bin/pytest tests/test_lineage.py`（覆盖 C3、C4、不把 `prompt_cache_key` 当 session ID）
- [x] **T5 增长感知 Diff 引擎**：实现 `core/diff.py`，区分 Wire Diff、正常追加、上游层次插入与既有改写。
  - 验收：`.venv/bin/pytest tests/test_diff_growth.py`（覆盖 C1、C2、C5、C7）
- [x] **T6 独立价表与多证据诊断器**：实现 `core/pricing.py` 与 `core/diagnoser.py`，跑通全部 10 项反例门禁。
  - 验收：`.venv/bin/pytest tests/test_usage_and_pricing.py tests/test_diagnoser_counterexamples.py`（C1–C10 全绿）
- [x] **T7 离线诊断 CLI**：实现 `cli.py`（`cacherift analyze` 默认脱敏输出，支持 `--include-snippets` 与 `--pricing-table`）。
  - 验收：`.venv/bin/pytest -v` + `.venv/bin/ruff check src tests` + `.venv/bin/mypy src tests`
