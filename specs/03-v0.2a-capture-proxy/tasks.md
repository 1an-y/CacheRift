# v0.2a Tasks（按顺序逐片推进）

- [x] **T1 SSE 增量解析器**：实现 `proxy/sse.py`。
  - 验收：`.venv/bin/pytest tests/test_proxy.py -k "sse"`（跨块切断的行、CRLF/CR 行结束符、多行 data、注释行、终止帧与 error 帧、残帧不分发）
- [x] **T2 记录器与脱敏**：实现 `proxy/recorder.py`，`io/redact.py` 增补 `split_clean_url`。
  - 验收：`.venv/bin/pytest tests/test_proxy.py -k "recorder or live"`（并发写串行化、每行可解析、`--live` 零正文）
- [x] **T3 ASGI 代理应用**：实现 `proxy/app.py`。
  - 验收：`.venv/bin/pytest tests/test_proxy.py -k "x1 or x2 or x3 or x4 or x5 or passthrough or identity or disconnect"`（X1–X5 端到端，零端口）
- [x] **T4 CLI**：实现 `proxy/cli.py` 并接入 `cli.py` 分发表。
  - 验收：`.venv/bin/pytest tests/test_proxy.py`（X1–X5 全绿）+ `.venv/bin/ruff check src tests` + `.venv/bin/mypy src tests`
