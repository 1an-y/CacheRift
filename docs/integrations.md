# Integrating CacheRift

CacheRift has three integration points. Pick by how much of your stack you want it to see.

| You want to | Use |
|---|---|
| see what your agent really sends to the model | the [capture proxy](#point-your-agent-at-the-capture-proxy) |
| fail a build when a change breaks the prompt prefix | the [check gate](#gate-a-build-with-cacherift-check) |
| keep a prompt builder honest in unit tests | the [pytest fixture](#keep-a-prompt-builder-honest-in-unit-tests) |

The proxy writes a JSONL capture and the gate reads one. None of the three changes a request on its way to the model.

## Point your agent at the capture proxy

The proxy listens on `127.0.0.1:8787` and forwards every request to one upstream. Request bodies and responses pass through byte for byte. The headers are handled the way a proxy must: connection-specific headers are dropped, `Host` and `Content-Length` are set for the upstream leg, `x-cacherift-*` headers stay local, and `accept-encoding` is sent as `identity`, so the bytes forwarded to the client are the bytes the proxy parses. Your client keeps using its own API key: the proxy forwards the key and stores only a redacted copy.

```bash
cacherift proxy --upstream https://api.openai.com --output session.jsonl --live
```

One proxy serves one upstream. To watch an OpenAI client and an Anthropic client at the same time, run two and give the second its own port and capture file.

```bash
cacherift proxy --upstream https://api.anthropic.com --listen 127.0.0.1:8788 --output claude-session.jsonl
```

Paths are passed through unchanged, so keep the part of the path your upstream expects. The OpenAI SDK is configured down to `/v1`; the Anthropic SDK is not.

| Client | Point it at | Proxy started with |
|---|---|---|
| OpenAI SDK | `OPENAI_BASE_URL=http://127.0.0.1:8787/v1` | `--upstream https://api.openai.com` |
| Anthropic SDK | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` | `--upstream https://api.anthropic.com` |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` | `--upstream https://api.anthropic.com` |
| LangChain, LangGraph | `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL`, or `base_url=` on the model | either |

### OpenAI SDK

The SDK reads `OPENAI_BASE_URL` when you do not pass `base_url`.

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

Or set it in code:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1")
```

A streamed Chat Completions answer only carries usage when you ask for it. Without it the turn is reported as *usage missing*, which is what it is, rather than as zero:

```python
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=messages,
    stream=True,
    stream_options={"include_usage": True},
)
```

### Anthropic SDK

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
```

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:8787")
```

### Claude Code

Claude Code reads `ANTHROPIC_BASE_URL` as well. Set a credential next to it: with a base URL in place, Claude Code expects a credential in the environment (`ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` for a gateway).

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
```

```bash
export ANTHROPIC_API_KEY=your-anthropic-key
```

```bash
claude
```

### LangChain and LangGraph

`ChatOpenAI` and `ChatAnthropic` read the same environment variables as the SDKs underneath when `base_url` is not passed, so the two `export` lines from the sections above are enough:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
```

`ChatOpenAI` reads `OPENAI_API_BASE` first and `OPENAI_BASE_URL` second, and `ChatAnthropic` reads `ANTHROPIC_API_URL` first and `ANTHROPIC_BASE_URL` second. Leave the first variable of a pair unset, or point it at the proxy as well.

Or set it in code. `init_chat_model`, `ChatOpenAI` and `ChatAnthropic` all take `base_url`:

```python
from langchain.chat_models import init_chat_model

model = init_chat_model(model="gpt-4o", model_provider="openai", base_url="http://127.0.0.1:8787/v1")
```

```python
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

openai_model = ChatOpenAI(model="gpt-4o", base_url="http://127.0.0.1:8787/v1")
anthropic_model = ChatAnthropic(model="claude-sonnet-4-5", base_url="http://127.0.0.1:8787")
```

### Read the result

With `--live`, the proxy prints one line to stderr after each request it records. For the full diagnosis, run `analyze` on the capture:

```bash
cacherift analyze session.jsonl
```

### Group requests into a session

Requests are linked into conversations by what they contain. To make the grouping explicit, send a session header; it is recorded and not forwarded.

```python
client = OpenAI(base_url="http://127.0.0.1:8787/v1", default_headers={"x-cacherift-session-id": "run-42"})
```

```python
client = anthropic.Anthropic(base_url="http://127.0.0.1:8787", default_headers={"x-cacherift-session-id": "run-42"})
```

### What the proxy does not parse

- A Gemini `streamGenerateContent` call made without `alt=sse` answers with a JSON array, not an event stream. It is forwarded and recorded as incomplete.
- A non-JSON body, such as a multipart upload, is forwarded but not recorded.
- A request whose path is not a known model endpoint is forwarded but not recorded. Pass `--dialect` to record everything with one dialect.

## Gate a build with `cacherift check`

`check` turns a capture into an exit code: `0` when no error-level rule is hit, `1` when at least one is, `2` when the capture or the policy cannot be read. The rules look at the shape of the prefix and at whether the usage a turn reports can be trusted. None of them is a hit-rate threshold, because a low hit rate has many causes and a broken prefix has one.

In a GitHub Actions workflow:

```yaml
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - run: pip install cacherift
      - run: cacherift check captures.jsonl --format github
```

`pip install cacherift` needs the package on PyPI; until the first release, install it from a checkout with `pip install .`.

`--format github` prints workflow commands only, so each finding appears as an annotation on the job, and the step fails when any error-level rule is hit.

`captures.jsonl` holds prompts, so commit a capture only after checking what is in it. Rules you expect to hit can be demoted or switched off in `cacherift.toml`, or in the `[tool.cacherift.check]` table of `pyproject.toml`, looked up in that order in the current directory. A setting only changes the rules it names:

```toml
[check]
warning = ["mutated_existing"]
ignore_paths = ["$.messages[*].content[*].cache_control"]
```

## Keep a prompt builder honest in unit tests

The package ships a pytest plugin, registered when the package is installed where the tests run. It needs no API key and opens no connection.

```python
def test_prompt_builder_keeps_its_prefix(cacherift_session):
    for turn in (1, 2, 3):
        cacherift_session.add(build_request(turn))
    cacherift_session.assert_stable()
```

- `add()` takes a request body as a dict, or the exact bytes your builder would put on the wire.
- Everything added to a session belongs to one conversation; each request is compared with the one before it.
- `assert_stable()` raises `AssertionError` naming the turn, the rule and the JSON path of every problem. The message never contains prompt text.
- For records you already hold, `assert_prefix_stable(records)` does the same, with an optional `policy=`.

The fixture defaults to the OpenAI Responses dialect. For another dialect, build the session yourself:

```python
from cacherift.check.pytest_plugin import PrefixSession

session = PrefixSession(dialect="anthropic_messages", model="claude-sonnet-4-5", provider="anthropic")
```

## Where these names come from

Checked against the vendors' own pages on 2026-11-26:

- OpenAI SDK: the README of [openai/openai-python](https://github.com/openai/openai-python), sections on configuration and custom HTTP clients.
- Anthropic SDK: `_client.py` in [anthropics/anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python), which documents the precedence kwarg, then `ANTHROPIC_BASE_URL`, then profile config.
- Claude Code: [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect).
- LangChain and LangGraph: [Models](https://docs.langchain.com/oss/python/langchain/models) on `docs.langchain.com`, and the `base_url` fields of `ChatOpenAI` and `ChatAnthropic` in the [langchain-ai/langchain](https://github.com/langchain-ai/langchain) sources.
