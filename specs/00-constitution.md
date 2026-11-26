# CacheRift Constitution

## 1. 证据诚实原则（不可违背）

- 严禁把重新序列化的 JSON 冒充原始 HTTP 请求体：`captured_body_bytes` 与 `projected_bytes` 必须物理分离，所有字节偏移必须显式携带坐标系（`WIRE_RAW` 或 `PROJECTED`）。
- 严禁把观测事实、厂商诊断与归因推断压扁为单一枚举或未校准的浮点置信度；证据必须标注级别：`OBSERVED`、`PROVIDER_REPORTED`、`INFERRED`、`UNRESOLVED`。
- 缺失不得补零，歧义不得强选，未知不得回退到跨厂商魔数（禁止硬编码 70% 命中红线或 2400 起缓阈值）。
- 协议方言（dialect）与服务商身份（provider）、模型能力、版本化价表必须解耦。

## 2. 口径与不可变性红线

- 存入 `CaptureRecord` 及诊断结论的嵌套 JSON 对象必须经 `freeze_json()` 递归深冻结（`MappingProxyType` / `tuple`）。
- 禁止使用 Python 原生 `dict == dict` / `MappingProxyType == MappingProxyType` 判断前缀结构等价（会忽略键顺序并混淆 `True == 1`）；必须保留原始解析键序，使用 `order_sensitive_equal()` 或 `projected_bytes`。
- 严禁对客户端原始 JSON 对象做 `sort_keys=True` 重序列化后冒充原始字节或用于前缀等值判定。
- `extract_usage()` 只产出互不重叠的计数桶与 `UsageState`，严禁在其中计算价格或反事实浪费金额。
- Anthropic 总输入分母严格等于 `uncached_input + cache_read + sum(cache_writes)`，禁止同时累加 `cache_creation_input_tokens` 与 `cache_creation` 分档明细。
- 累计型流式事件（如 Anthropic `message_delta.usage`）只取最终快照，严禁重复相加。

## 3. 架构与隐私边界

- `src/cacherift/core/` 严禁执行网络 I/O、磁盘读写或自动修改请求体（禁止导入 `httpx`、`requests`、`urllib`、`socket`、`pathlib`、`os`、`rich`）。
- 所有默认输出仅包含 `json_path`、长度、SHA-256 指纹与 `ByteSpan`；正文片段（`before_snippet` / `after_snippet`）默认必须为 `None`，仅在显式开启 `--include-snippets`（或 `include_snippets=True`）时输出。

## 4. 三层测试纪律

- `tests/fixtures/synthetic/` 仅验证本地规则契约。
- `tests/fixtures/captured/` 仅存放脱敏真实抓包。
- 严禁将自造模拟响应命名为已证实的真实网关故障。
