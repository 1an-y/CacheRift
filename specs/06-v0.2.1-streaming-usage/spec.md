# Feature Spec: v0.2.1 `usage_only` 方言流式用量补齐与价表核对

> 本迭代遵守 `specs/00-constitution.md`。它补齐的是 `specs/03-v0.2a-capture-proxy/spec.md` 第 5 条「适用范围」里记下的既有限制，改的是 v0.1 的方言适配器，所以按 `AGENTS.md` 第 3 条先改规范、再动代码。

## 范围界定

- **补齐对象**：`openai_chat`、`deepseek_chat`、`gemini_generate_content` 三个适配器的 `extract_usage()`。`cacherift proxy` 已经把它们的 SSE 响应帧完整解析进 `response_body["events"]`，但适配器只读取顶层非流式字段，流式抓包的用量因此一律是 `UsageState.MISSING`。
- **不改的部分**：
  - `proxy/sse.py` 的帧解析与 `response_complete` 语义；
  - 三个方言的 `support_level`，仍是 `usage_only`（结构诊断不加严）；
  - `src/cacherift/core/`；
  - 三个方言的**非流式**口径，与 v0.1 逐条一致。
- **价表核对**：只把能从服务商官方公开页面核实的条目补进 `default_pricing.toml`，每条注明核对日期与来源 URL。核实不到的不写，在交付汇报里逐项列明。既有条目只核对、不改动：发现与官方页面不一致时，在价表文件里该条目上方和交付汇报里同时写明，由维护者决定是否修正。
- **能力表核对**：`default_capabilities.toml` 只核对了一件事——它的型号模式是否仍然覆盖 2026-11-26 取到的各定价页上列出的型号（`deepseek-*` 覆盖 `deepseek-flash` 与 `deepseek-v4-pro`；`gemini-2.5*` 覆盖 Gemini 2.5 家族）。它关于缓存行为的声明（最小可缓存 token 数、TTL 语义、单元顺序）本迭代没有对照各家缓存文档重新核实，在交付汇报里列为未核实项。Gemini 定价页上有一条与之相关的事实：`gemini-2.5-pro` 与 `gemini-2.5-flash` 已被弃用，2026-12-01 起路由到别的型号。能力表没有任何条目被改动。

## 核心行为规格

1. **输入契约**：流式响应的 `response_body` 形如 `{"events": [...]}`，非流式响应是响应体本身。`events` 的每个元素是解析后的 JSON 对象；非 JSON 的数据帧（如终止哨兵）形如 `{"data": "[DONE]"}`。终止哨兵与带 `"usage": null` 的早期 chunk 都不是用量帧。
2. **先看完整性**：`record.response_complete is False`（中断、缺终止帧、出现错误帧）时直接返回 `UsageState.MISSING`，三种方言一致（对齐 C6）。
3. **只取末帧最终快照**：累计型的用量字段只取**最后一个**带用量对象的帧，严禁把多帧相加（宪章 §2）。
4. **`openai_chat`**：反向查找最后一个 `usage` 为对象的帧，随后走与非流式一致的口径——校验 `prompt_tokens`、`completion_tokens`、`prompt_tokens_details.cached_tokens`；缺 `prompt_tokens_details` 返回 `INCOMPLETE`，绝不静默填 `0`；`cached_tokens > prompt_tokens` 返回 `INVALID`。整条流没有任何 `usage` 帧（例如客户端没有开 `stream_options.include_usage`）时返回 `MISSING`，注记为 `missing_usage_in_stream`。
5. **`deepseek_chat`**：与 `openai_chat` 共用同一份「流式末帧 `usage` 查找」实现，不重复造。解析 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`completion_tokens`；存在 `prompt_tokens` 且 `prompt_tokens != hit + miss` 返回 `INVALID`；缺字段返回 `INCOMPLETE`；没有 `usage` 帧或 `response_complete=False` 返回 `MISSING`。
6. **`gemini_generate_content`**：反向查找最后一个 `usageMetadata`（或 `usage_metadata`）为对象的帧，只取末帧。沿用非流式口径：`promptTokenCount`、`cachedContentTokenCount`、`candidatesTokenCount` 齐全且 `cached <= prompt` 时为 `COMPLETE`；缺字段为 `INCOMPLETE`；矛盾为 `INVALID`；`response_complete=False` 为 `MISSING`。
7. **零正文**：任何状态下 `usage.raw_fields` 只含用量对象本身。流式的 `MISSING` / `INCOMPLETE` / `INVALID` 观测，经 `cacherift analyze --format json` 默认输出时不得带出 `choices` / `delta` / `candidates` 的文本片段。
8. **一份实现**：末帧用量查找与「流里没有用量」的判定各只有一处实现，放在 `dialects/base.py`，三个适配器复用。

## 已知局限

- Gemini 的 `streamGenerateContent` 不带 `alt=sse` 时返回 JSON 数组而不是 SSE，代理不把这种响应当作流来解析，记为不完整，用量为 `MISSING`。本迭代只对齐 `response_body["events"]` 这一种形态。
- Gemini 流没有终止帧，连接提前关闭与正常结束无法区分：这种情况下 `response_complete` 为真，用量取已收到的最后一帧。能识别的不完整只有错误帧。
- 非流式 Gemini 响应里，`usageMetadata`（或别名 `usage_metadata`）的取值不是对象时，现在会跳过它去看别名；v0.1 取第一个真值，不是对象就直接判 `MISSING`。这是对 v0.1 非流式口径唯一的有意改动，只在响应体本身畸形时才会显现。

## 测试接缝

| 接缝 | 位置 | 能抓到什么 | 抓不到什么 |
|---|---|---|---|
| A | `DialectAdapter.extract_usage(record)`，输入是经真实 `SSEAccumulator` 解析出的 `events` | 帧的选择、状态判定、桶计数、`raw_fields` 的内容 | 代理的转发与落盘 |
| B | ASGI `proxy` → JSONL → `load_capture_records` → `analyze_records` | 从线上字节到诊断结论的整条链路 | 真实厂商的帧格式（只有真实 API 能验） |
| C | `cacherift analyze --format json` 的 stdout | 报告层面的正文泄露 | 抓取文件本身（它本来就保存完整请求与响应） |

接缝 A 的测试只依赖 `cacherift.proxy.sse`（纯标准库），不导入 `httpx` / `uvicorn`，因此留在纯核心隔离验证可运行的测试集里。接缝 A 的记录由真实的 `SSEAccumulator` 从字节解析而来，`response_complete` 取它的 `complete`，与代理的判定同源：断流与错误帧都是真实构造的帧，不由测试手工设标志。

## 强制验收矩阵（Counterexamples S1–S5）

- **S1（OpenAI Chat SSE）**：多块流里早期 chunk 含 `"usage": null`，倒数第二帧带完整 `usage`（含 `prompt_tokens_details.cached_tokens`），末帧是 `data: [DONE]` → `COMPLETE` 且桶计数精确；全程没有 `usage`（未开 `include_usage`）→ `MISSING` 且零正文泄露；缺 `prompt_tokens_details` → `INCOMPLETE`。
- **S2（DeepSeek Chat SSE）**：流式末帧带 `prompt_cache_hit_tokens` + `prompt_cache_miss_tokens` → `COMPLETE`；`hit + miss != prompt_tokens` → `INVALID`；中途断流或出现 `error` 帧 → `MISSING`。
- **S3（Gemini SSE 累计快照不重加）**：3 帧流式响应带递增的 `usageMetadata`（`candidatesTokenCount` 依次为 `10`、`25`、`42`）→ 只取最后一帧的 `42`，绝不能算出 `77`；`response_complete=False` → `MISSING`。
- **S4（零正文红线回归）**：三家在流式的 `MISSING` / `INCOMPLETE` / `INVALID` 状态下，`analyze --format json` 默认输出不含任何 `choices` / `delta` / `candidates` 文本片段。
- **S5（端到端代理联调）**：三家方言各一条经 ASGI `proxy` 抓 SSE 流、落盘、重读、交给 `analyze_records` 的集成测试，`response_complete=True` 且用量不再是 `MISSING`。
