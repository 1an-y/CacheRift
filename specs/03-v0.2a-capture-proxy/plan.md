# Implementation Plan: v0.2a Capture Proxy (`v0.2a`)

## 1. 模块边界

```text
src/cacherift/
├── cli.py                         # 分发表登记 `cacherift.proxy.cli`；DiagnosisSession 供 --live 增量诊断
├── io/
│   ├── redact.py                  # [增补] reject_secret_url：拒绝带 userinfo/query/fragment 的 URL
│   └── capture.py                 # dump_capture_record 追加落盘（v0.1，不变）
└── proxy/
    ├── __init__.py
    ├── sse.py                     # SSEAccumulator：增量解析，不扣留字节
    ├── recorder.py                # CaptureRecorder：异步安全追加写 + --live 摘要
    ├── app.py                     # ProxyApp：原生 ASGI 反向代理
    └── cli.py                     # `cacherift proxy`
tests/
└── test_proxy.py                  # X1–X5（httpx.ASGITransport + httpx.MockTransport，零端口）
```

依赖方向：`proxy/*` → `cli.DiagnosisSession`、`core`、`dialects`、`io`；`core` 不感知 `proxy`。

## 2. 关键契约与实现细节

- **`proxy/sse.py`**：`SSEAccumulator(require_terminal_frame=True)`；对没有终止帧约定的方言传 `False`，此时流的正常结束即视为到达终点。`feed(chunk)` 把字节追加到内部缓冲，按行取出完整行交给帧状态机（`event` / `data` 字段，空行分发）。缓冲里只留尚未成行的尾巴。`events` 为解析后的事件字典列表；`data` 是 JSON 对象时原样保留，缺 `type` 而有 `event:` 名时补上；不是 JSON 时记为 `{"data": "<原文>"}`。`terminal_seen` / `error_seen` 记录终止帧与错误帧。`finish()` 丢弃未完成的残帧（规范要求不分发）。
- **`proxy/app.py`**：`ProxyApp(upstream=..., transport=..., recorder=..., dialect="auto", provider=None, pricing_table_id=None)`，`transport` 可注入（单测用 `httpx.MockTransport`）。
  - 生命周期：`scope["type"] == "lifespan"` 时在 shutdown 关闭 `httpx.AsyncClient`；未走 lifespan 的宿主可调用 `await app.aclose()`。
  - 请求体读完后 `build_request(method, url, content=body, headers=转发头)`，`send(..., stream=True)`，`aiter_raw()` 逐块转发（原始字节，不解码内容编码）。若 transport 交回的是已缓冲的响应，则把 `response.content` 当作单块转发。
  - 请求体一经读完就挂起一个 `receive()` 作为断开监听，贯穿整个上游往返：`_await_upstream` 让「等上游响应头」与「客户端断开」赛跑，`_pump` 让「等下一个数据块」与它赛跑。两条路径上客户端离开都会取消上游请求并关闭上游流。
  - 客户端响应先收尾（发出结束帧），然后才 `record()`。
  - 上游在响应开始后断流：记录落盘后**不再发送结束帧直接返回**，由 ASGI 服务器断开与客户端的连接。不以抛异常的方式中止，否则服务器会为每次断流打印完整 traceback。
  - 请求开始时向 recorder 领取记录号，结束时构造 `CaptureRecord` 交给 `recorder.write()`。
  - ASGI 类型用模块内 `Scope` / `Message` / `Receive` / `Send` 别名表达，不引入 asgiref。
- **`proxy/recorder.py`**：`CaptureRecorder(path, *, live=False, session=None)`。`next_record_id()` 顺序发号（`cap-<时间戳>-<随机后缀>-<序号>`）；`write(record)` 在 `asyncio.Lock` 内写入并 flush；`live` 时用 `session.feed(record)` 的诊断结果打印一行到 stderr。`write()` 不向调用方抛任何异常：落盘与诊断各自包在 `try` 里，失败只打印 `cannot capture/diagnose <记录号> (<异常类型>)`。
- **`proxy/cli.py`**：退出码 `0` 正常退出，`2` 配置错误（缺依赖、非法 `--upstream` / `--listen`、非法方言名）。启动时向 stderr 打印监听地址、上游与落盘路径，随后 `uvicorn.run(app, host, port, log_level="warning")`；退出时关闭 recorder。
- **测试基建**：`httpx.MockTransport` 的异步 handler 可返回 `content=<async generator>` 的流式响应，并在生成器中途抛 `httpx.ReadError` 模拟断流；`httpx.ASGITransport(app=app)` 驱动代理，全程不开端口。异步测试用 `asyncio.run` 包在同步测试函数里，不引入 `pytest-asyncio`。
- **`cli.py` 的 `DiagnosisSession`**：先做完一切可能因异常数据而抛错的提取，再把记录登记进血缘解析器，并立刻存入该轮的派生状态；用量提取抛错时转成 `INVALID` 观测。这样一轮出问题既不会让流水线崩溃，也不会留下半登记的记录连累后续各轮。
- **真服务器集成测试**：ASGITransport 无法表达「连接被中止」，所以另有两条测试在回环网卡的临时端口上起真实 `uvicorn.Server` 和一个原始 HTTP/1.1 假上游，分别验证「上游断流 → 客户端连接被断开」与「客户端断开 → 上游流被关闭」。

## 3. 对既定计划的取舍

- 计划把 `--live` 写成「终端实时摘要渲染」，未规定实现方式。实现用 `DiagnosisSession` 增量诊断：若每次落盘都对全部记录重跑 `analyze_records`，开销随会话长度平方增长，且发生在代理的事件循环里，会拖慢流式转发。
- 计划未提客户端断开与 `accept-encoding`。两者都是真实 Agent 流量下必然遇到的路径（用户中断生成；上游压缩 SSE 会让「转发的字节」与「解析事件的字节」不是同一份），纳入本期。
