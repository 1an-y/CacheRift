# Implementation Plan: v0.2.1 Streaming Usage (`v0.2.1`)

## 1. 模块边界

```text
src/cacherift/dialects/
├── base.py                  # [增补] 用量对象的查找：非流式取顶层字段，流式取末帧
├── openai_chat.py           # [修改] extract_usage 改用共用查找
├── deepseek_chat.py         # [修改] 同上
└── gemini_generate.py       # [修改] 同上
src/cacherift/capabilities/tables/
└── default_pricing.toml     # [核对] 只补入能从官方页面核实的条目
specs/03-v0.2a-capture-proxy/spec.md   # [修改] 第 5 条「适用范围」改为交叉引用本迭代
README.md                                # [修改] proxy 一节的流式用量说明
tests/
├── stream_fixtures.py      # [新增] 三家方言的合成 SSE 流，适配器测试与代理测试共用
├── test_dialects.py         # S1–S4（接缝 A、C）
├── test_proxy.py            # S5（接缝 B）
└── test_usage_and_pricing.py # [修改] 新增价表规则的核对
AGENTS.md                   # [修改] 迭代目录索引加入 06
```

依赖方向不变：适配器 → `core`、`dialects.base`；`core/` 不感知任何外围包。

## 2. 关键契约与实现细节

- **`dialects/base.py`** 新增两个纯函数：
  - `is_streamed(record)`：`response_body` 是否带有 `events` 列表。
  - `find_usage(record, key, *aliases)`：响应里的用量对象。非流式取顶层字段；流式在 `events` 里反向查找第一个让 `key`（或任一别名）为对象的帧。值为 `None`、非对象、或终止哨兵（`{"data": "[DONE]"}`）的帧一律跳过。找不到返回 `None`。
- **三个适配器**的 `extract_usage()` 开头统一成同一段：先判 `response_complete`，再用 `find_usage` 取对象，取不到时按是否流式给出 `missing_usage_in_stream` 或原有的 `missing_usage` 注记。其后的字段校验与分桶逻辑不动。
- **零正文**：`missing_usage` 只把 `usage` 对象放进 `raw_fields`，流式响应下该对象不存在，所以 `raw_fields` 为空；`INCOMPLETE` / `INVALID` 的 `raw_fields` 是用量对象本身。适配器不新增任何会带回响应正文的路径。
- **测试数据**：三家方言的帧由 `tests/stream_fixtures.py` 构造，适配器测试与代理测试共用一份。接缝 A 的记录由真实 `SSEAccumulator` 把 SSE 字节解析成 `events` 得到，`response_complete` 取它的 `complete`，不手写事件列表，也不由测试设标志，保证测到的是代理实际写入的形态。断流就是去掉 `[DONE]`，错误帧是 Chat Completions 形态的 `{"error": {...}}`。Gemini 没有哨兵，它的「不完整」只能以错误帧表达。测试里的帧是合成契约样例，不放进 `tests/fixtures/captured/`。
- **价表核对流程**：
  1. 逐项访问服务商官方定价页，读页面的原始表格而不是二手转述，记录输入、缓存读取、输出单价与核对日期。
  2. 只有页面上明确可读的条目才写入，并在条目上方用 TOML 注释写明来源 URL、核对日期与适用条件。
  3. 价表按「服务商 + 模型名」匹配，与方言无关。
  4. **新增条目用精确型号名**。同一页面上常有共享前缀但价格不同的产品（`gpt-4o-mini-transcribe`、`gemini-2.5-flash-image`），前缀模式会把邻居的费率借给它们；宁可让没登记的名字保持未定价，也不让它算错。唯一的例外是页面明确说明按同一价格计费的别名。
  5. **单费率表达不了的分档取页面的基础档**：标准服务档、文本输入、先列出的提示词长度档、未打折的时段档（DeepSeek 的 PEAK）。其余各档的数字与适用条件写进注释，并在交付汇报里列为局限。
  6. **既有条目修正（维护者已确认）**：原规则保留的 `gpt-6.1*`、`gpt-6.2*` 与 `claude-*-4*` 通配条目已按 2026-11-26 官方定价页拆改并校准为精确型号规则（`gpt-6.1-sol`、`gpt-6.2-sol`、`gpt-6.2-astra` 及 Claude 4.x 各子代），受影响的预算单测字面量同步更新。

## 3. 对既定要求的取舍

- 要求里 S1 写的是「端到端经 proxy 落盘后 analyze 识别为 COMPLETE」，S5 又要求三家各一条经 proxy 的集成测试。实现把 S1 的端到端部分并入 S5（按方言各一条），`tests/test_dialects.py` 里的 S1 只做到接缝 A。原因是 `tests/test_dialects.py` 必须留在「不装 `httpx` / `uvicorn` 也能跑」的测试集里，那是纯核心隔离验证要跑的集合。
- 要求里「带非空 `Mapping` 类型 `usage` 的帧」按「值为对象」实现，空对象 `{}` 也算找到。理由：服务商发出了用量对象但里面没有计数，如实结果是 `INCOMPLETE`，不是「流里没有用量」。
- 要求里核对两张表。价表按上面的流程核对并扩充；能力表只核对了型号模式是否仍覆盖各定价页上的型号，缓存行为的声明没有重新核实，也没有改动，理由与范围写在 `spec.md` 的「能力表核对」里。
- S2 的「中途断流」与「出现 `error` 帧」都由真实帧构造，并以 `SSEAccumulator.complete` 判定，不以测试标志硬设。
