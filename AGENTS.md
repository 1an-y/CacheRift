# AGENTS.md — CacheRift (`cacherift`)

Prompt Cache Reliability for AI Agents — **Find the break. Restore the cache.**

## 开发工作流（SDD 强制顺序）

1. 动手改代码前，必须先读取 `specs/00-constitution.md` 与当前迭代的 `specs/01-v0.1-offline-diagnostics/spec.md`、`plan.md`、`tasks.md`。
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
