# v0.2b Tasks（按顺序逐片推进）

- [x] **T1 策略与规则命名空间**：实现 `check/policy.py`（默认策略、只覆盖点名规则，未点名沿用默认、ignore > error > warning、未知 ID 报错、TOML 加载）。
  - 验收：`.venv/bin/pytest tests/test_check.py -k "policy"`（默认分级、`cacherift.toml` / `pyproject.toml` / `--config`、未知 ID 抛 `PolicyError`）
- [x] **T2 纯函数评估器**：实现 `check/evaluator.py`（`Finding` / `CheckReport` / `evaluate_diagnoses` / 路径 glob）。
  - 验收：`.venv/bin/pytest tests/test_check.py -k "evaluator or k1 or k2 or k3"`（K1 零误报、K2 必阻断并指向精确路径、K3 滑动断点豁免）
- [x] **T3 pytest 插件**：实现 `check/pytest_plugin.py`，登记 `pytest11` 入口，新增 `tests/conftest.py`。
  - 验收：`.venv/bin/pytest tests/test_check.py -k "k5 or session or assert_prefix"`（fixture 端到端、稳定通过、抖动失败且零正文）
- [x] **T4 CLI**：实现 `check/cli.py` 并接入 `cli.py` 分发表。
  - 验收：`.venv/bin/pytest tests/test_check.py`（K1–K5 全绿，含三种输出格式与退出码）+ `.venv/bin/ruff check src tests` + `.venv/bin/mypy src tests`
