"""The four whitelisted repair rules: pure functions over request bodies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from cacherift.core.freeze import canonical_project_bytes, order_sensitive_equal, sha256_hex
from cacherift.dialects.base import strip_cache_control
from cacherift.repair.models import RepairEdit

ALL_RULES: tuple[str, ...] = (
    "stabilize_tool_array_order",
    "canonicalize_tool_key_order",
    "migrate_gpt62_top_level_instructions",
    "normalize_anthropic_breakpoints",
)


@dataclass(frozen=True, slots=True)
class ParentTurn:
    """What a rule may know about the turn this one is compared with.

    ``body`` is the parent's effective request body (repaired in this run, when a repair was
    applied to it). ``top_level_instructions`` is the parent's original top-level text.
    """

    body: dict[str, Any] | None
    top_level_instructions: str | None


# A new request body plus the edits that produced it, or None when the rule has nothing to do.
RuleResult = tuple[dict[str, Any], tuple[RepairEdit, ...]]


def _edit(rule: str, json_path: str, detail: str, before: Any, after: Any) -> RepairEdit:
    def fingerprint(value: Any) -> tuple[str | None, int | None]:
        if value is None:
            return None, None
        data = canonical_project_bytes(value)
        return sha256_hex(data), len(data)

    before_sha256, before_len = fingerprint(before)
    after_sha256, after_len = fingerprint(after)
    return RepairEdit(rule, json_path, detail, before_sha256, after_sha256, before_len, after_len)


def _tools(body: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The request's tools when they are a list of objects; the objects themselves, not copies."""
    tools = body.get("tools") if body is not None else None
    if isinstance(tools, list) and all(isinstance(tool, Mapping) for tool in tools):
        return [cast(dict[str, Any], tool) for tool in tools]
    return None


def _same_modulo_key_order(a: Any, b: Any) -> bool:
    """Same structure and values, allowing any key order: the same test the diagnoser uses."""
    return canonical_project_bytes(a, sort_keys=True) == canonical_project_bytes(b, sort_keys=True)


def _tool_name(tool: Mapping[str, Any]) -> str | None:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        function = tool.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
    return name if isinstance(name, str) and name else None


def _labels(tools: list[dict[str, Any]]) -> str:
    return ", ".join(_tool_name(tool) or f"tools[{index}]" for index, tool in enumerate(tools))


def stabilize_tool_array_order(body: dict[str, Any], parent: ParentTurn) -> RuleResult | None:
    """Put the tools the parent already had back in the parent's order; new tools go last."""
    tools, parent_tools = _tools(body), _tools(parent.body)
    if not tools or not parent_tools:
        return None

    # Pass 1 pairs tools with the same content (key order aside); pass 2 pairs by tool name.
    pairing: dict[int, int] = {}  # parent index -> current index
    taken: set[int] = set()
    for parent_index, before in enumerate(parent_tools):
        for current_index, current in enumerate(tools):
            if current_index not in taken and _same_modulo_key_order(before, current):
                pairing[parent_index] = current_index
                taken.add(current_index)
                break
    for parent_index, before in enumerate(parent_tools):
        if parent_index in pairing:
            continue
        name = _tool_name(before)
        for current_index, current in enumerate(tools):
            if current_index not in taken and name is not None and _tool_name(current) == name:
                pairing[parent_index] = current_index
                taken.add(current_index)
                break

    order = [pairing[index] for index in sorted(pairing)]
    appended = [index for index in range(len(tools)) if index not in taken]
    if order + appended == list(range(len(tools))):
        return None
    new_tools = [tools[index] for index in order + appended]
    existing = new_tools[: len(order)]
    detail = f"existing tools restored to the parent turn's order: [{_labels(existing)}]"
    if appended:
        detail += f"; new tools moved to the end: [{_labels([tools[i] for i in appended])}]"
    edit = _edit("stabilize_tool_array_order", "$.tools", detail, _tools(body), new_tools)
    return {**body, "tools": new_tools}, (edit,)


def canonicalize_tool_key_order(body: dict[str, Any], parent: ParentTurn) -> RuleResult | None:
    """Where tools[i] differs from the parent's only by key order, take the parent's object."""
    tools, parent_tools = _tools(body), _tools(parent.body)
    if not tools or not parent_tools:
        return None

    new_tools = list(tools)
    edits: list[RepairEdit] = []
    for index in range(min(len(tools), len(parent_tools))):
        before, current = parent_tools[index], tools[index]
        if order_sensitive_equal(before, current) or not _same_modulo_key_order(before, current):
            continue
        new_tools[index] = before
        edits.append(
            _edit(
                "canonicalize_tool_key_order",
                f"$.tools[{index}]",
                "key order aligned with the parent turn's tool at the same position",
                current,
                before,
            )
        )
    if not edits:
        return None
    return {**body, "tools": new_tools}, tuple(edits)


def migrate_gpt62_top_level_instructions(
    body: dict[str, Any], parent: ParentTurn
) -> RuleResult | None:
    """Move static top-level instructions into a leading developer message.

    Only the root of a lineage, or a turn whose parent carried the same text and was itself
    migrated, may move. Anywhere else the developer message would be a new unit in front of a
    prefix the parent already established: the very break this command exists to remove.
    """
    instructions = body.get("instructions")
    if not isinstance(instructions, str) or not instructions:
        return None
    if "previous_response_id" in body:
        return None  # server-side chaining carries the context; there is no local prefix to align
    if parent.body is not None:
        same_text = parent.top_level_instructions == instructions
        parent_moved = "instructions" not in parent.body
        if not (same_text and parent_moved):
            return None
    inputs = body.get("input", [])
    if not isinstance(inputs, list):
        return None

    developer = {"role": "developer", "content": instructions}
    new_body = {key: value for key, value in body.items() if key != "instructions"}
    new_body["input"] = [developer, *inputs]
    return new_body, (
        _edit(
            "migrate_gpt62_top_level_instructions",
            "$.instructions",
            "top-level field removed",
            instructions,
            None,
        ),
        _edit(
            "migrate_gpt62_top_level_instructions",
            "$.input[0]",
            "the same text re-added as a leading developer message",
            None,
            developer,
        ),
    )


def _has_cache_control(node: Any) -> bool:
    if isinstance(node, Mapping):
        return "cache_control" in node or any(_has_cache_control(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_cache_control(item) for item in node)
    return False


def normalize_anthropic_breakpoints(body: dict[str, Any], parent: ParentTurn) -> RuleResult | None:
    """Keep the breakpoint on the last message that has one; drop the stale earlier ones."""
    if parent.body is None:
        return None
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    marked = [index for index, message in enumerate(messages) if _has_cache_control(message)]
    if len(marked) < 2:
        return None

    new_messages = list(messages)
    edits: list[RepairEdit] = []
    for index in marked[:-1]:
        cleaned, removed = strip_cache_control(messages[index], f"$.messages[{index}]")
        new_messages[index] = cleaned
        edits.append(
            _edit(
                "normalize_anthropic_breakpoints",
                f"$.messages[{index}]",
                "stale breakpoints removed at " + ", ".join(path for path, _ in removed),
                messages[index],
                cleaned,
            )
        )
    return {**body, "messages": new_messages}, tuple(edits)


Rule = Callable[[dict[str, Any], ParentTurn], "RuleResult | None"]

RULES: dict[str, Rule] = {
    "stabilize_tool_array_order": stabilize_tool_array_order,
    "canonicalize_tool_key_order": canonicalize_tool_key_order,
    "migrate_gpt62_top_level_instructions": migrate_gpt62_top_level_instructions,
    "normalize_anthropic_breakpoints": normalize_anthropic_breakpoints,
}
