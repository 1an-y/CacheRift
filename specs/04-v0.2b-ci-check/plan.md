# Implementation Plan: v0.2b CI Check (`v0.2b`)

## 1. 模块边界

```text
src/cacherift/check/
├── __init__.py
├── policy.py                  # CheckPolicy、规则 ID 命名空间、TOML 加载与校验、PolicyError
├── evaluator.py               # Finding / CheckReport、evaluate_diagnoses（纯函数）、路径 glob
├── pytest_plugin.py           # cacherift_session fixture、PrefixSession、assert_prefix_stable
└── cli.py                     # `cacherift check`
pyproject.toml                 # [project.entry-points."pytest11"] cacherift = "cacherift.check.pytest_plugin"
tests/
├── conftest.py                # pytest_configure：插件未经入口加载时才补注册
└── test_check.py              # K1–K5
```

依赖方向：`check/*` → `cli.analyze_records`、`core`；`core` 不感知 `check`。`pytest_plugin.py` 是唯一导入 `pytest` 的模块。

## 2. 关键契约与实现细节

- **`check/policy.py`**：
  - `CHANGE_RULES`（8 个 `ChangeKind` 值）、`CAUSE_RULES`（诊断器会产出的非回声原因码）、`SPECIAL_RULES`（`usage_invalid`、`usage_missing`、`usage_incomplete`、`no_baseline`）、`ECHO_CAUSE_TO_CHANGE`（回声码 → 变更类型）。`ALL_RULES` 为前三者之并。
  - `normalize_rule(rule_id)` 把回声码折到对应的变更类型。
  - `CheckPolicy(error: frozenset[str], warning: frozenset[str], ignore: frozenset[str], ignore_paths: tuple[str, ...])`，不可变；`severity(rule_id) -> "error" | "warning" | "ignore" | None`。
  - `CheckPolicy.default()`、`CheckPolicy.from_mapping(mapping)`（点名的规则以配置为准，其余沿用默认）、`load_policy(config_path: Path | None, *, start: Path | None = None)`。加载时逐个 ID 与键名校验，未知的 ID 或键抛 `PolicyError(ValueError)`。
- **`check/evaluator.py`**：
  - `Finding(record_id, line, rule, severity, source, json_path, message)`；`CheckReport(findings)`，带 `errors` / `warnings` / `ignored` 三个只读视图与 `exit_code`。
  - `path_matches(json_path, pattern)`：把模式编译成正则——`[*]` → `\[\d+\]`，`*` → `[^.]*`，其余字符按字面转义。
  - `evaluate_diagnoses`：对每条诊断依次评估 `observed_changes`（先过 `ignore_paths`）、非回声 `candidate_causes`、`usage.state`、`lineage.status`。
- **`check/pytest_plugin.py`**：`PrefixSession` 负责把请求体变成带显式会话的 `CaptureRecord`（记录号 `turn-0001` 起，时间戳单调递增）。fixture 只负责创建并交出会话。
- **`check/cli.py`**：`check <capture_path> [--config path] [--format table|json|github]`。捕获文件不存在或无法解析、配置错误 → 单行 stderr + `2`。

## 3. 对既定计划的取舍

- 计划把 `CheckPolicy` 写成「从 `cacherift.toml` 或 `[tool.cacherift.check]` 读取」，未规定配置片段在 `cacherift.toml` 里的位置。实现取顶层 `[check]`：专用文件再套一层 `[tool.cacherift.…]` 没有意义。
- 计划未提配置里的未知规则 ID。实现选择报错退出 `2`：门禁配置里一个拼错的规则如果被静默忽略，CI 会在没有任何保护的情况下显示通过。
- 计划未提「回声原因码」。诊断器对每个变更同时产出一条同名含义的原因码，若两者都算 finding，同一处变更会在报告里出现两次；实现只按变更类型计一次，回声码作为别名接受。
- 项目自身的测试在 `tests/conftest.py` 的 `pytest_configure` 里检查插件是否已加载，没有才 `import_plugin`。这样无论 venv 里的安装元数据是否已刷新（入口是否生效），套件都能跑。不能用 `pytest_plugins`：pytest 9 会把「同一模块以不同名字再注册一次」当作错误。
