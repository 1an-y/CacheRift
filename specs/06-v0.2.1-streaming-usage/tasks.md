# v0.2.1 Tasks（按顺序逐片推进，每片先 Red 后 Green）

- [x] **T1 `openai_chat` 流式用量（S1 + S5 的 Chat Completions 一条）**：`dialects/base.py` 增加共用的 `is_streamed` / `find_usage`；`openai_chat.py` 的 `extract_usage()` 改用它。
  - 验收：`.venv/bin/pytest tests/test_dialects.py -k "s1"` + `.venv/bin/pytest tests/test_proxy.py -k "s5 and chat"`
- [x] **T2 `deepseek_chat` 流式用量（S2 + S5 的 DeepSeek 一条）**：`deepseek_chat.py` 复用同一份末帧查找。
  - 验收：`.venv/bin/pytest tests/test_dialects.py -k "s2"` + `.venv/bin/pytest tests/test_proxy.py -k "s5 and deepseek"`
- [x] **T3 `gemini_generate_content` 流式用量（S3 + S5 的 Gemini 一条）**：`gemini_generate.py` 复用同一份末帧查找。
  - 验收：`.venv/bin/pytest tests/test_dialects.py -k "s3"` + `.venv/bin/pytest tests/test_proxy.py -k "s5 and gemini"`
- [x] **T4 零正文红线回归（S4）**：三家在流式 `MISSING` / `INCOMPLETE` / `INVALID` 下的 `analyze --format json` 输出不含正文。
  - 验收：`.venv/bin/pytest tests/test_dialects.py -k "s4"`
- [x] **T5 价表核对与文档同步**：核实并补入 `default_pricing.toml`；更新 `specs/03` 第 5 条与 `README.md` 的 proxy 一节。
  - 验收：`.venv/bin/pytest tests/test_usage_and_pricing.py tests/test_probe.py -k "not upstream"` + `.venv/bin/mypy src tests`
- [x] **T6 全量门禁**：既有 205 项测试的断言一条不动，全部仍然通过。
  - 验收：`.venv/bin/pytest -q` + `.venv/bin/ruff check src tests` + `.venv/bin/mypy src tests`
