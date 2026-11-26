# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-27

### Added

- `cacherift probe`: sends small, byte-exact probes to an upstream and reports the cache behavior it actually sees. It is a dry-run unless `--execute` is passed, never sends a request that would take it past its caps on request count and cost (12 requests and $0.10 by default), runs every configuration check before the first request, and supports `--prefix-tokens` to adjust the shared prefix target.
- `cacherift proxy`: a local, read-only proxy that forwards request bodies and responses byte for byte and appends each model request and its response to a JSONL capture. Credential headers are stored redacted and the URL query string is never stored.
- `cacherift check`: turns a capture into an exit code by rules on the shape of the prefix and on the usage report, with no hit-rate threshold. The policy is read from `cacherift.toml` or `[tool.cacherift.check]`, and `--format github` prints workflow commands for GitHub Actions.
- A pytest plugin with the `cacherift_session` fixture and `assert_prefix_stable()`, to check a prompt builder without a network connection.
- `cacherift repair`: rewrites the four kinds of prefix defect its whitelist rules cover, and nothing else. It is a dry-run unless `--apply --output` is passed, never rewrites free text, and can write a hash audit of every change with `--audit-out`.
- Usage from streamed responses for the Chat Completions, DeepSeek and Gemini dialects. Usage is taken from the last frame that carries it and never summed across frames; a stream that did not end cleanly is reported as missing, never as zero.
- Pricing rules transcribed from the vendors' public pages on 2026-11-26: DeepSeek (`deepseek-flash`, `deepseek-v4-pro` and two retired names still served by Flash), Gemini 2.5 (Pro, Flash, Flash-Lite) and OpenAI chat models (`gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`). Each rule uses an exact model name, so a model the page prices differently stays unpriced instead of borrowing a neighbour's rate.
- A sanitized real third-party relay capture fixture (`tests/fixtures/captured/openai_responses_relay_gpt61_hit.jsonl`) exercising OpenAI Responses cache hits (`4096/4259`) and `WIRE_RAW` diff spans.
- A GitHub Actions workflow for CI: lint and type check, a Python 3.12 to 3.14 test matrix, and a core-only isolation gate that installs the package without its optional dependencies.
- A GitHub Actions workflow that builds, verifies and publishes a release to PyPI through a trusted publisher, with no stored API token. A manual run publishes to TestPyPI.
- `scripts/verify_install.py`, which runs the installation checks CI runs, and `docs/integrations.md`.

### Fixed

- The JSON report of `cacherift analyze` could carry the model's output text in `usage.raw_fields` when usage was missing. The field now holds only the usage object.
- Usage a provider sent in a shape that cannot be parsed ended the whole diagnosis with a traceback. It is now reported as invalid and the remaining turns are still diagnosed.
- A malformed capture file made `cacherift analyze` end with a traceback. The problem is now reported in one line.
- A turn whose only change was an edit to its final conversation unit was reported as a broken prefix and got no `unexplained_low_hit` cause, although everything before that unit was byte-identical. The edit is still reported, and a zero cache read on such a turn is now flagged as unexplained as well.
- The build requirement allowed `setuptools>=68`, but the license declaration in `pyproject.toml` needs setuptools 77. A build that resolved to an older setuptools failed with `invalid pyproject.toml config: project.license`. The requirement is now `setuptools>=77`.
- Replaced pre-check OpenAI (`gpt-6.1*`, `gpt-6.2*`) and Anthropic (`claude-*-4*`) pricing wildcards in `default_pricing.toml` with exact vendor-verified model rules (`gpt-6.1-sol`, `gpt-6.2-sol`, `gpt-6.2-astra`, `claude-sonnet-4-*`, `claude-opus-4-6`/`4-5` at $5/$25, `claude-opus-4-1`/`4-20250514`, and `claude-haiku-4-5` at $1/$5) so `gpt-6.2-astra` cannot borrow Sol's rate under `probe` budget bounds.
- Zero-token `cache_write_tokens` (`0`) reported by OpenAI Responses upstreams no longer cause `estimate_cost()` to return `None` when a pricing rule omits write tiers: `openai_responses` omits zero-token write buckets (negative counts become `UsageState.INVALID`), and `estimate_cost()` skips zero-token write buckets while keeping `None` for non-zero unpriced write tiers.
- Raised the default `cacherift probe` shared prefix target to at least 2,200 tokens (`max(2200, ceil(min_cache_tokens * 1.1))` sized at 5 UTF-8 bytes per token) and raised the default `--max-cost-usd` cap to `$0.10`; the previous `1200 * 4` byte sizing yielded only ~1,080 real tokens on English prose and produced false zero-hit reports on a healthy `gpt-6.1-sol` upstream.
- Multi-root comparison scenarios in `cacherift probe` (`gpt62_instructions_placement`) now isolate each baseline chain into its own `client_session_id`, preventing `dev_round1` from being falsely parented onto `top_round2` during diagnosis and `cacherift check`.
- `cacherift probe` now warns explicitly (on `stderr` and in JSON `warnings`) when an upstream exceeds `max_output_tokens`, noting that the per-request output envelope did not hold.
- Updated Anthropic capability rules in `default_capabilities.toml` so `claude-opus-4-6*`, `claude-opus-4-5*`, and `claude-haiku-4-5*` declare their documented `4096`-token minimum cacheable prefix.
- OpenAI Responses parent/child turns whose responses echo different `prompt_cache_key` values while the request omitted or kept the same key now emit `PROVIDER_REPORTED` `response_cache_key_diverged` (with 16-char SHA-256 fingerprints only, no raw key text) instead of `unexplained_low_hit`, and `cacherift check` includes `response_cache_key_diverged` as a default warning.
- `cacherift probe`'s `baseline_identical_pair` scenario now attaches a deterministic per-run `prompt_cache_key` (`<nonce>:baseline_identical_pair`) on `openai_responses` so independent single-turn baseline calls route to the same cache bucket on healthy upstreams.

## [0.1.0] - 2026-11-26

The state of the initial commit. It was never tagged or published, and the version number stayed at `0.1.0` while everything under Unreleased was added, so a `v0.1.0` tag would release that work under this name.

### Added

- The offline diagnostic kernel: a wire-level versus structural diff between turns, conversation lineage resolution, and an evidence-graded diagnosis that keeps observations, provider reports and inferences apart.
- `cacherift analyze`, which diagnoses a JSONL capture. Its output holds JSON paths, lengths, hashes and byte spans; prompt text appears only with `--include-snippets`.
- Full support for the OpenAI Responses and Anthropic Messages dialects, and usage-only support for OpenAI Chat Completions, DeepSeek and Gemini `generateContent`.

[Unreleased]: https://github.com/1an-y/CacheRift/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/1an-y/CacheRift/compare/3017d19a5a25b04503af9a4e45cf3a7df6f64839...v0.2.0
[0.1.0]: https://github.com/1an-y/CacheRift/commit/3017d19a5a25b04503af9a4e45cf3a7df6f64839
