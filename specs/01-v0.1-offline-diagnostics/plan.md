# Implementation Plan: CacheRift v0.1 (`v0.1-rev2`)

## 1. 模块边界

```text
cacherift/
├── AGENTS.md
├── pyproject.toml
├── specs/
│   ├── 00-constitution.md
│   └── 01-v0.1-offline-diagnostics/
│       ├── spec.md
│       ├── plan.md
│       └── tasks.md
├── src/cacherift/
│   ├── __init__.py
│   ├── core/                       # 纯 stdlib 诊断内核（零 I/O）
│   │   ├── models.py               # 捕获记录、证据分级、不可变快照、多候选诊断模型
│   │   ├── freeze.py               # 递归深冻结、保序投影字节序列化、order_sensitive_equal
│   │   ├── span.py                 # 原始 UTF-8 JSON 字节起止偏移扫描器
│   │   ├── lineage.py              # 4 级优先级血缘归组 + 前缀 Trie 索引
│   │   ├── diff.py                 # Wire Diff vs 结构 Diff
│   │   ├── diagnoser.py            # 多证据聚合器
│   │   └── pricing.py              # 独立版本化价表估算引擎
│   ├── dialects/                   # 协议方言适配器
│   │   ├── base.py
│   │   ├── openai_responses.py     # [完整]
│   │   ├── anthropic_messages.py   # [完整]
│   │   ├── openai_chat.py          # [部分 usage_only]
│   │   ├── deepseek_chat.py        # [部分 usage_only]
│   │   └── gemini_generate.py      # [部分 usage_only]
│   ├── capabilities/
│   │   ├── registry.py             # TOML 能力与价表注册表，未登记返回 unknown
│   │   └── tables/
│   │       ├── default_capabilities.toml
│   │       └── default_pricing.toml
│   ├── io/
│   │   └── capture.py              # JSONL 捕获读写器
│   └── cli.py                      # `cacherift analyze`
└── tests/
```

## 2. 关键契约与实现细节

- **`core/freeze.py`**：提供 `freeze_json`、`thaw_json`、`canonical_project_bytes(value, *, sort_keys=False)`、`sha256_hex`、`order_sensitive_equal`。`order_sensitive_equal` 强制校验类型一致（`True != 1`、`1 != 1.0`）与键插入顺序一致。
- **`core/span.py`**：递归下降扫描 UTF-8 JSON 字节流，处理转义字符（`\"`、`\\`、`\uXXXX`）及多字节 UTF-8 字符，精确给出 `ByteSpan(space, start, end)`。默认 `space=OffsetSpace.WIRE_RAW`；离线重构投影时可传 `OffsetSpace.PROJECTED`。
- **`dialects/openai_responses.py`**：
  - 默认顺序：`tools` -> `instructions` -> `input`。
  - 当能力表或模型代际声明 `instructions_after_input`（如 GPT-6.2）时，投影顺序切为 `tools` -> `input` -> `instructions`，使对话历史追加与静态/不可缓存顶层 `instructions` 的前缀边界清晰可查。
  - 提取 `prompt_cache_diagnostics`、`previous_response_id`、`prompt_cache_options.comparison_response_id`。
- **`dialects/anthropic_messages.py`**：
  - 顺序：`tools` -> `system` -> `messages`。
  - 区分根级 `$.cache_control` 与块级 `$.messages[i].content[j].cache_control` 写入 `CacheConfigFingerprint.cache_params`，同时在单元文本投影时剥离随轮次滑动的 `cache_control` 字段，避免断点移动被误判为文本内容改写。
  - 提取 `diagnostics.previous_message_id` 与 `diagnostics`（附带“结构比较独立于缓存命中”的 `caveat`）。
  - 分母严格为 `uncached_input + cache_read + sum(cache_writes)`，若 `cache_creation` 分档明细与 `cache_creation_input_tokens` 矛盾则标记 `UsageState.INVALID`。
- **`core/lineage.py`**：4 级优先级依次为 `explicit_metadata` -> `response_id_chain` -> `history_continuation` -> `heuristic_prefix`。忽略 `prompt_cache_key` 作为会话标识。多项平票时返回 `LineageStatus.AMBIGUOUS` 且 `parent_record_id=None`。
- **`core/diff.py`**：在均有 `captured_body_bytes` 且结构键序等价但原始字节不同时仅输出 `WIRE_ONLY_DIFF`；`tools` 末尾新增且后方仍有单元时输出 `INSERTED_BEFORE_TAIL`；正常对话末尾追加输出 `NORMAL_APPEND`；默认 `before_snippet=None`、`after_snippet=None`。
- **`core/diagnoser.py`**：多证据并行输出；不设固定 70% 命中红线或硬编码 2400 token 阈值；`C8` 输出 `unexplained_low_hit` 与 `unresolved`；`C9` 同时输出结构变化与 TTL 超期候选。
