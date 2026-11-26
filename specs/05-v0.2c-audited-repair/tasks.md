# v0.2c Tasks（按顺序逐片推进）

- [x] **T1 审计模型**：实现 `repair/models.py`（`RepairEdit` / `RepairAuditEntry` / `RepairResult`，哈希与坐标系字段）。
  - 验收：`.venv/bin/mypy src`（纯数据类，无行为；由 T2–T4 的测试覆盖）
- [x] **T2 白名单规则**：实现 `repair/rules.py` 四条纯函数。
  - 验收：`.venv/bin/pytest tests/test_repair.py -k "rule"`（键序对齐取父轮对象而非字母序、数组恢复顺序且新工具挪到末尾、被改内容但同名的工具留在原位、静态 `instructions` 才迁移、Anthropic 只留最后一条消息的断点）
- [x] **T3 引擎与安全阀**：实现 `repair/engine.py`（安全阀、规则顺序、逐轮传播、哈希审计）。
  - 验收：`.venv/bin/pytest tests/test_repair.py -k "r2 or r3 or r4 or r5 or engine"`（R2–R5）
- [x] **T4 CLI**：实现 `repair/cli.py` 并接入 `cli.py` 分发表。
  - 验收：`.venv/bin/pytest tests/test_repair.py`（R1–R5 全绿）+ `.venv/bin/ruff check src tests` + `.venv/bin/mypy src tests`
