# Feature Spec: v0.2a 只读零改写抓包代理（`cacherift proxy`）

> 本迭代遵守 `specs/00-constitution.md`，复用 v0.1 诊断内核与 v0.1.5 的脱敏底座。

## 范围界定

- **新增命令** `cacherift proxy`：本机反向代理。任意现有多轮 Agent 只需把 `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` 指向本机监听地址，即可无侵入抓全原始请求字节与 SSE 流并落盘 JSONL。
- **实现形态**：原生 ASGI 应用函数 + `uvicorn` + `httpx.AsyncClient`，不引 FastAPI / Starlette。可选依赖 `httpx`、`uvicorn`（extra `proxy`）；缺失时打印单行安装提示并返回 `2`。
- **默认监听** `127.0.0.1:8787`（仅回环）。
- **本期不做**：请求改写、重放、鉴权注入、非 JSON 请求体（如 multipart 上传）的记录（照常转发但不落盘）。

## 核心行为规格

1. **请求逐字节零改写（X1）**：客户端请求体字节原样转发上游，不解析后重序列化；捕获记录的 `captured_body_bytes` 就是收到的字节，`parsed_body` 由其解析得到。落盘的请求头是**客户端发来的**请求头（脱敏后），不是转发给上游的那一份。
2. **请求头转发规则**：除以下几类外原样转发——
   - 逐跳头（`connection`、`keep-alive`、`proxy-authenticate`、`proxy-authorization`、`te`、`trailer`、`transfer-encoding`、`upgrade`、`expect`）；
   - `host`（由上游 URL 决定）与 `content-length`（由 httpx 按实际字节重新计算）；
   - `accept-encoding` 固定为 `identity`，保证上游不压缩，转发给客户端的响应字节与用于解析事件的字节是同一份；
   - 代理自身的 `x-cacherift-*` 头。
3. **响应逐字节零改写**：上游的状态码、响应头（仅去掉逐跳头）与响应体字节块原样转发客户端，逐块转发不做缓冲合并。上游 4xx / 5xx 同样透传，代理不崩（X5）。
4. **凭据红线（X2）**：`Authorization` / `x-api-key` 等鉴权头**原样转发上游**，落盘与终端输出中一律替换为 `[REDACTED]`；上游响应体里回显的密钥同样擦除。URL 的 query 永不落盘（Gemini 风格的 `?key=` 不会进 JSONL），`--upstream` 含 userinfo、query、fragment 或无法解析的端口时拒绝（退出 `2`）。
5. **SSE 增量解析（X3）**：响应 `content-type` 为 `text/event-stream` 时，边转发原始字节块边增量解析事件帧（支持 LF / CRLF / CR 行结束符，多行 `data:` 以 `\n` 拼接，注释行忽略），结果写入 `response_body.events`，与 v0.1 方言适配器既有的 `events` 契约一致。
   - 终止帧：OpenAI Responses 的 `response.completed` / `response.incomplete` / `response.failed`，Anthropic 的 `message_stop`，Chat Completions 的 `data: [DONE]`。Gemini 风格的流没有终止帧：对 `gemini_generate_content`，上游流正常结束即视为到达终点。
   - 错误帧：`type` 为 `error` 的事件，以及带顶层 `error` 对象的帧（Chat Completions 的报错形态）。
   - `response_complete` 仅当「上游流正常结束」且「到达终点」且「未见错误帧」时为真。
   - 流中途中断或缺少终止帧：`response_complete=False`，方言适配器报 `UsageState.MISSING`，不补零（对齐 C6）。
   - 中断发生在响应已开始之后时，代理中止与客户端的连接（客户端读到截断的流），而不是补一个正常的结尾去冒充完整响应。
   - 适用范围：从事件里提取用量由方言适配器完成，只有 `openai_responses` 与 `anthropic_messages` 的适配器读取 `events`。三个 `usage_only` 方言（Chat Completions、DeepSeek、Gemini）的 v0.1 适配器只从非流式响应体里读取用量：它们的流式请求会被完整转发、完整抓取，`response_complete` 也如实记录，但用量记为 `MISSING`。这是 v0.1 适配器的既有限制，补齐需要先更新方言规范。
6. **失败路径**：上游连不上或响应头未到就出错 → 返回 `502` 与一个不含上游细节的 JSON 错误体，记录一条 `response_complete=False` 的捕获。
   - 客户端在请求体**上传中途**断开 → 不转发任何内容，也不落盘（不存在一条完整的请求）。
   - 客户端在请求体上传完成之后的任意时刻断开——包括上游还在生成响应头的时候——代理立即取消上游请求并关闭上游流（不让上游继续生成），记录为不完整。为此，对断开的监听从请求体读完就开始，而不是等上游响应头到达之后。
   - **抓取是观察者**：落盘失败（如磁盘已满）或实时诊断出错（如畸形的用量字段）只向 stderr 记一行异常类型，绝不影响已经发往客户端的响应。实现上总是先把客户端响应收尾，再落盘。
7. **记录身份**：按请求路径自动识别方言——`/responses` → `openai_responses`，`/messages` → `anthropic_messages`，`/chat/completions` → `openai_chat`（上游主机为 `api.deepseek.com` 时为 `deepseek_chat`），`:generateContent` / `:streamGenerateContent` → `gemini_generate_content`。服务商按上游主机名识别，识别不出即为 `unknown`，不按方言猜。`--dialect` / `--provider` 可显式覆盖。
   - 请求体不是 JSON、或自动识别不出方言且未给 `--dialect` 时：照常转发，不落盘。
   - `endpoint_path` 只含路径，不含 query。
8. **会话归组**：仅当客户端显式带 `x-cacherift-session-id` 头时写入 `client_session_id`（该头不转发上游）；否则留空，交给 `LineageResolver` 自然归组并保留 `C4` 歧义。
9. **并发安全（X4）**：多个客户端并发时各流独立、互不串扰；JSONL 追加写入由异步锁串行化，锁内只有文件写入与 flush，不包网络 I/O。
10. **`--live` 实时摘要**：每条记录落盘后向 stderr 打印一行（记录号、方言、模型、命中数/总数、变更类型、血缘状态与依据、候选原因代码），不含任何正文片段。摘要通过增量诊断会话逐条产出，每条记录只分析一次，不在事件循环里重算整段会话。
11. **默认零正文**：除 `--live` 的一行摘要外代理不打印任何请求/响应内容。
12. **输出文件**：追加模式；每条记录一行，可被 `cacherift analyze` / `cacherift check` / `cacherift repair` 直接读取。
13. **已知限制**：`--live` 把诊断状态留在内存里，占用随抓取量线性增长（实测约为提示词总量的三倍）。需要长时间运行时，建议不加 `--live`，事后对落盘文件执行 `cacherift analyze`，结论完全相同。

## 强制验收矩阵（Counterexamples X1–X5）

- **X1**：请求体（含非规范空白与 `\uXXXX` 转义、多字节 UTF-8）与响应字节到达上游/客户端时逐字节不变；记录的 `captured_body_bytes` 等于客户端发来的字节。
- **X2**：上游收到完整的 `Authorization` / `x-api-key`；JSONL、终端输出中只有 `[REDACTED]`；响应体回显的密钥被擦除；query 中的密钥不落盘。
- **X3**：完整 SSE 流 → `response_body.events` 齐全、`response_complete=True`、用量 `COMPLETE`；中途断流与缺终止帧 → `response_complete=False`、用量 `MISSING` 且 `buckets is None`；Anthropic 累计型 `message_delta.usage` 只取最终快照。
- **X4**：两个并发会话各自收到自己的响应内容；JSONL 两行均可独立解析，且各自与自己的请求体对应。
- **X5**：上游 4xx / 5xx 的状态码、响应头、响应体原样到达客户端，随后的请求照常代理；上游不可达时客户端得到 502。
