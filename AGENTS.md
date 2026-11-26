# AGENTS.md — CacheRift (`cacherift`)

Prompt Cache Reliability for AI Agents — **Find the break. Restore the cache.**

## 开发工作流（SDD 强制顺序）

1. 动手改代码前，必须先读取 `specs/00-constitution.md` 与所要改动的迭代目录（`specs/<NN>-*/`）下的 `spec.md`、`plan.md`、`tasks.md`。内核与 `analyze` 在 `01`；四个外围命令分别在 `02`（probe）、`03`（proxy）、`04`（check）、`05`（repair）；方言适配器的流式用量与价表核对在 `06`。
2. 每次只推进 `tasks.md` 中的一个任务切片：先补齐或核对失败测试（Red），再写最小实现至全绿（Green），通过验收命令后勾选任务项。
3. 若发现协议细节与 `spec.md` 冲突，停止修改断言，汇报冲突点并先更新规范；严禁弱化测试来凑绿灯。

## 工具链与门禁命令

- 运行测试：`.venv/bin/pytest -q`（若宿主机使用 `uv`：`uv run pytest -q`）
- 静态检查：`.venv/bin/ruff check src tests`
- 类型检查：`.venv/bin/mypy src tests`
- CLI 入口：`.venv/bin/cacherift --version`

## 架构与代码硬约束

- `src/cacherift/core/` 禁止导入 `httpx`、`requests`、`urllib`、`socket`、`pathlib`、`os`、`rich`，禁止文件读写、网络调用或自动修改请求体。
- 严禁对客户端原始 JSON 对象做 `sort_keys=True` 重序列化，严禁把重序列化字节冒充 `captured_body_bytes`；比较结构等价必须保持键顺序并区分 `True != 1`（使用 `order_sensitive_equal` / `projected_bytes`）。
- 禁止未校准置信度、固定命中率红线（如 `0.70`）、无来源模型常量（如硬编码 `2400`）；不确定即显式输出 `UNRESOLVED` / `unknown` / `None`。
- 默认零正文输出：CLI 与 JSON 报告默认只输出 `json_path`、长度、哈希与 `ByteSpan`，正文片段仅在 `--include-snippets` 时输出。
- 测试证据分层：`tests/fixtures/synthetic/` 仅代表合成契约样例，`tests/fixtures/captured/` 存放脱敏真实抓包，不得将合成模拟响应命名为已证实的真实网关故障。
- `core/` 不得反向导入 `cli`、`io` 或 `probe` / `proxy` / `check` / `repair`（`tests/test_span_and_freeze.py` 的 AST 守卫同时检查两条方向）。

## 外围命令的边界

- `probe` 与 `proxy` 的 `cli.py` 不得在模块顶层导入 `httpx` / `uvicorn`：`cacherift --help` 与每个子命令的 `--help` 必须在只装了核心依赖的环境里可用，缺依赖时只打印一行安装提示并返回 `2`。
- API Key 只从环境变量读取，不接受命令行明文；落盘的请求头一律经 `io/redact.py`；URL 的 query 永不落盘。
- `proxy` 不得解析后重序列化请求体；`probe` 场景构造的字节就是发出的字节，`captured_body_bytes` 只能来自实际发送的内容。
- 抓取是观察者：`proxy` 里落盘或实时诊断失败只记一行日志，不得影响已经发往客户端的响应；客户端响应总是先收尾再落盘。
- `probe --execute` 先 `preflight` 再发第一个请求（配置问题一律在花钱之前拒绝），抓包逐条落盘。
- `repair` 只允许 `repair/rules.py` 里的白名单规则；被修复的记录只能以 `parsed_body` 写出（`PROJECTED`），不得伪造 `captured_body_bytes`；默认 dry-run，不覆盖已有文件。
- `check` 不得引入固定命中率红线；配置里点名的规则以配置为准，未点名的沿用默认，未知规则 ID 直接报错。
- 真实 socket 的测试只监听回环网卡的临时端口；其余测试必须零网络。
