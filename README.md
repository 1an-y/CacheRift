# CacheRift

**Prompt Cache Reliability for AI Agents**

**Find the break. Restore the cache.**

`cacherift` is an open-source Python toolkit for detecting, diagnosing, and repairing silent prompt cache failures in multi-turn AI agents.

## Status (v0.1)

v0.1 implements the offline diagnostic engine (`cacherift analyze`):

- **Exact wire vs. structural diff**: Distinguishes JSON formatting/escaping differences (`WIRE_ONLY_DIFF`), normal multi-turn conversation growth (`NORMAL_APPEND`), upstream tool insertions (`INSERTED_BEFORE_TAIL`), object/array reordering (`REORDERED_EXISTING`), edits (`MUTATED_EXISTING`), deletions (`DELETED_EXISTING`), compaction (`COMPACTED_OR_TRUNCATED`), and parameter changes (`CONFIG_CHANGED`).
- **Two complete dialect pipelines**: OpenAI Responses (including `prompt_cache_diagnostics` and generation-specific ordering notes such as GPT-6.2 top-level `instructions`) and Anthropic Messages (including `diagnostics.previous_message_id`, tiered 5m/1h write buckets, and non-overlapping `total_input` denominator).
- **Partial usage-only adapters**: OpenAI Chat Completions, DeepSeek Chat, and Gemini `generateContent`.
- **Evidence-graded diagnosis**: Combines local observations, provider-native diagnostics, lineage resolution (`NO_BASELINE` / `AMBIGUOUS` / `MATCHED`), capability metadata, and optional versioned pricing estimates without hardcoding cross-provider magic thresholds.
- **Zero-snippet redaction by default**: CLI and JSON outputs emit `json_path`, byte spans, lengths, and SHA-256 fingerprints unless `--include-snippets` is explicitly enabled.

## Quick Start

```bash
cacherift analyze path/to/captures.jsonl
```

```bash
cacherift analyze path/to/captures.jsonl --format json --pricing-table default-2026-11
```

## CLI Roadmap

- `cacherift analyze` — Offline multi-turn capture diagnosis (v0.1)
- `cacherift probe` — Controlled active cache probing (v0.1.5)
- `cacherift proxy` — Local traffic capture proxy (v0.2)
- `cacherift repair` — Audited prefix repair rules (v0.2)
- `cacherift check` — CI / pytest cache stability assertions (v0.2)

## License

Apache-2.0. See [LICENSE](LICENSE).
