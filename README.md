# CacheRift

**Prompt Cache Reliability for AI Agents**

**Find the break. Restore the cache.**

`cacherift` is an open-source Python toolkit for detecting, diagnosing, and repairing silent prompt cache failures in multi-turn AI agents.

## Status

Five commands around one pure, offline diagnostic kernel:

| Command | What it does | Touches the disk |
|---|---|---|
| `analyze` | Diagnoses a JSONL capture turn by turn | Never |
| `probe` | Sends small, controlled probes to an upstream and captures what really comes back | Only with `--execute` |
| `proxy` | Local read-only proxy that captures real agent traffic | Appends to one capture file |
| `check` | Turns diagnoses into pass / fail; also a pytest plugin | Never |
| `repair` | Rewrites the few prefix defects that are provably equivalent for the model | Only with `--apply --output` |

### What the kernel detects

- **Exact wire vs. structural diff**: Distinguishes JSON formatting/escaping differences (`WIRE_ONLY_DIFF`), normal multi-turn conversation growth (`NORMAL_APPEND`), upstream tool insertions (`INSERTED_BEFORE_TAIL`), object/array reordering (`REORDERED_EXISTING`), edits (`MUTATED_EXISTING`), deletions (`DELETED_EXISTING`), compaction (`COMPACTED_OR_TRUNCATED`), and parameter changes (`CONFIG_CHANGED`).
- **Two complete dialect pipelines**: OpenAI Responses (including `prompt_cache_diagnostics` and generation-specific ordering such as GPT-6.2 top-level `instructions`) and Anthropic Messages (including `diagnostics.previous_message_id`, tiered 5m/1h write buckets, and a non-overlapping `total_input` denominator).
- **Partial usage-only adapters**: OpenAI Chat Completions, DeepSeek Chat, and Gemini `generateContent`.
- **Evidence-graded diagnosis**: Local observations, provider-native diagnostics, lineage resolution (`NO_BASELINE` / `AMBIGUOUS` / `MATCHED`), capability metadata, and optional versioned pricing estimates, kept distinct. No cross-provider hit-rate thresholds.
- **Zero-snippet redaction by default**: Output carries `json_path`, byte spans, lengths, and SHA-256 fingerprints. Prompt text appears only with `--include-snippets`. Credential headers are replaced with `[REDACTED]` wherever a capture is stored.

## Quick Start

Offline diagnosis of a capture:

```bash
cacherift analyze path/to/captures.jsonl
```

```bash
cacherift analyze path/to/captures.jsonl --format json --pricing-table default-2026-11
```

### `probe`: measure what an upstream really does

Prints the plan and its worst-case cost; nothing is sent:

```bash
cacherift probe --dialect openai_responses --base-url https://api.openai.com/v1 --model gpt-6.1
```

Send it, with the API key read from `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`), under a hard cap of 12 requests and $0.05:

```bash
cacherift probe --dialect openai_responses --base-url https://api.openai.com/v1 --model gpt-6.1 --execute --output probe.jsonl
```

Scenarios are admitted as whole groups: a scenario whose worst-case cost does not fit the remaining budget is skipped and reported, never cut in half. A model with no pricing rule cannot be probed, because its cost cannot be bounded.

Everything that can be refused is refused before the first request goes out, including an output path that already exists or cannot be written. The capture file is written as each response arrives, so an interrupted run keeps what it paid for.

### `proxy`: capture a real agent

```bash
cacherift proxy --upstream https://api.openai.com --output session.jsonl --live
```

Point the agent's base URL at `http://127.0.0.1:8787/v1`. Request bodies pass through byte for byte and are recorded as received. An optional `x-cacherift-session-id` header groups requests into a session; without it, lineage is resolved from the traffic itself.

Streamed responses (`text/event-stream`) are parsed for all five dialects. Chat Completions, DeepSeek and Gemini usage is taken from the last frame that carries it and never summed across frames; Anthropic usage is the final snapshot (`message_start` merged with the last `message_delta`); OpenAI Responses usage comes from the `response.completed` frame. Two shapes are not parsed: a Gemini `streamGenerateContent` call made without `alt=sse` answers with a JSON array, and a Gemini stream that closes early cannot be told from one that ended. A Chat Completions stream only contains usage when the client sets `stream_options.include_usage`; without it the turn is reported as missing usage, not as zero. `--live` keeps diagnosis state in memory, which grows with the traffic captured; for a long-running proxy, leave it off and run `cacherift analyze` on the capture file afterwards.

### `check`: gate a build

```bash
cacherift check session.jsonl --format github
```

Exit `0` when no error-level rule is hit, `1` when one is, `2` when the capture or the policy cannot be read. Rules look at the shape of the prefix and at the usage report, never at the hit rate. Settings live in a `[check]` table of `cacherift.toml` or in `[tool.cacherift.check]` of `pyproject.toml`, and only override the rules they name:

```toml
[check]
ignore = ["non_cacheable_top_level_instructions"]
ignore_paths = ["$.messages[*].content[*].cache_control"]
```

In tests, check a prompt builder without any network:

```python
def test_prompt_builder_keeps_its_prefix(cacherift_session):
    for turn in range(1, 4):
        cacherift_session.add(build_request(turn))
    cacherift_session.assert_stable()
```

### `repair`: fix what is provably safe

```bash
cacherift repair session.jsonl
```

```bash
cacherift repair session.jsonl --apply --output repaired.jsonl --audit-out audit.json
```

Four rules only: tool key order aligned to the parent turn, tool array order restored with new tools last, static top-level `instructions` moved into a developer message, and stale Anthropic breakpoints removed. Free text is never rewritten. Records with ambiguous lineage, or on targets the capability table does not fully support, are refused and listed.

## Integrating CacheRift

Pointing an agent at the proxy from the OpenAI SDK, the Anthropic SDK, Claude Code or LangChain, gating a GitHub Actions build with `check`, and using the pytest fixture are described in [docs/integrations.md](docs/integrations.md).

## Installation

```bash
pip install cacherift
```

`analyze`, `check` and the pytest plugin need nothing beyond the core install. The two commands that talk to the network are optional extras:

```bash
pip install 'cacherift[probe]'
```

```bash
pip install 'cacherift[proxy]'
```

### Core-only isolation

`analyze`, `check` and `repair` must keep working in an installation that has neither `httpx` nor `uvicorn`. CI checks this on every pull request and on every push to `main`. To run the same check on your machine, create a fresh virtual environment with Python 3.12 or newer, install only the core package and pytest into it, and run the verification script, all from the repository root:

```bash
python3 -m venv /tmp/cacherift-core-only
```

```bash
/tmp/cacherift-core-only/bin/python -m pip install --upgrade pip
```

```bash
/tmp/cacherift-core-only/bin/python -m pip install . pytest
```

```bash
/tmp/cacherift-core-only/bin/python scripts/verify_install.py core-only
```

The script checks that neither `httpx` nor `uvicorn` can be imported, that the help of every command works, that `probe` and `proxy` exit with a one-line install hint, that `analyze`, `check` and `repair` run on the captured fixtures, and that the tests pass without the two packages.

## Changelog

Changes between versions are listed in [CHANGELOG.md](CHANGELOG.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
