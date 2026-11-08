"""Execute a voice command against Home Assistant via its MCP server.

Gemma (via the OpenAI-compatible LLM server) gets HA's MCP tools as OpenAI-style function tools, picks
calls, we execute them through MCP, feed results back, repeat until it answers.

Env:
  HA_MCP_URL         default: http://100.78.133.58:8123/api/mcp
  HA_TOKEN           HA long-lived access token (Profile -> Security)
  HA_DRY_RUN         '1' = print the tool calls gemma wants, but don't execute them
  HA_MAX_STEPS       max LLM<->tool round trips (default: 5)
"""

import json
import re
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from app.classify import LLM_MODEL, LLM_REASONING_EFFORT, LLM_TIMEOUT_S, LLM_URL

HA_MCP_URL = os.environ.get("HA_MCP_URL", "http://100.78.133.58:8123/api/mcp")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_DRY_RUN = os.environ.get("HA_DRY_RUN", "") == "1"
HA_MAX_STEPS = int(os.environ.get("HA_MAX_STEPS", "5"))

# HA's MCP endpoint also exposes admin tools (call_action = any service, config reloads,
# templates, logs). A voice transcript must only reach the voice-control intents.
# Tool names are namespaced: "<namespace>__<tool>".
ALLOWED_TOOL_PREFIXES = ("intent__", "light__")
ALLOWED_TOOLS = {"homeassistant__GetLiveContext", "llm__GetDateTime"}
# Read-only tools: safe to execute even in dry run.
READ_ONLY_TOOLS = {"homeassistant__GetLiveContext", "llm__GetDateTime"}


def _allowed(name: str) -> bool:
    return name in ALLOWED_TOOLS or name.startswith(ALLOWED_TOOL_PREFIXES)


SYSTEM_PROMPT = (
    "You control a smart home through Home Assistant tools. The user's request is an automatic "
    "speech-to-text transcript and may contain recognition errors (e.g. a missing first word).\n"
    "Steps:\n"
    "1. If unsure of exact device or area names, call GetLiveContext first.\n"
    "2. Call the tool needed. Pass only the arguments required. To act on all devices of a kind "
    "in a room, use 'area' plus 'domain' (e.g. area='Bedroom', domain=['light']) instead of 'name'. "
    "Only use 'device_class' with one of the exact values allowed by the tool schema; "
    "lights have no device_class.\n"
    "If GetLiveContext lists a device whose name equals the room name (e.g. a light named "
    "'Bedroom' in area Bedroom), it is a group of that room's devices: make ONE call with that "
    "name instead of one call per member device.\n"
    "Requests about lights only target devices with domain 'light'. Devices with domain "
    "'scene' are presets: only activate one (HassTurnOn) when the user names it, e.g. "
    "'set the bedroom to relax'. Scenes cannot be turned off.\n"
    "3. If a tool returns an error, fix the arguments and retry once.\n"
    "Never ask follow-up questions: if the request is ambiguous or unsafe, do nothing and say why.\n"
    "Finish with one short sentence saying what happened. If the action failed, say it failed."
)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: str | None = None  # None in dry run
    is_error: bool = False


@dataclass
class HomeResult:
    calls: list[ToolCall] = field(default_factory=list)
    reply: str = ""
    dry_run: bool = False


@asynccontextmanager
async def ha_session() -> AsyncIterator[ClientSession]:
    if not HA_TOKEN:
        raise RuntimeError("HA_TOKEN is not set (HA: Profile -> Security -> Long-lived access tokens)")
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {HA_TOKEN}"})
    async with http_client:
        # mcp 1.x (nixpkgs) yields (read, write, get_session_id); requirements.txt pins <2 to match.
        async with streamable_http_client(HA_MCP_URL, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _to_openai_tools(mcp_tools: list[Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Expose tools to the LLM under short names ("HassTurnOn", not "intent__HassTurnOn"):
    gemma mangles the double-underscore names and the server then fails to parse the call.
    Returns (openai_tools, short_name -> full MCP name)."""
    shorts = [t.name.split("__", 1)[-1] for t in mcp_tools]
    name_map: dict[str, str] = {}
    tools = []
    for t, short in zip(mcp_tools, shorts):
        llm_name = short if shorts.count(short) == 1 else t.name  # keep full name on collision
        name_map[llm_name] = t.name
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": llm_name,
                    "description": t.description or "",
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                },
            }
        )
    return tools, name_map


def _result_text(result: Any) -> tuple[str, bool]:
    parts = [getattr(c, "text", None) or str(c) for c in getattr(result, "content", None) or []]
    return "\n".join(parts), bool(result.isError)


# --- Local guardrails -------------------------------------------------------------
# A 2B model doesn't reliably follow prompt rules, so check each call in code and send a
# precise, corrective error back instead. The model's retry loop then does the fixing.


@dataclass(frozen=True)
class Device:
    name: str
    domain: str
    areas: tuple[str, ...]


def _parse_live_context(raw: str) -> list[Device]:
    """Parse GetLiveContext's YAML-ish listing ('- names: X\\n  domain: Y\\n  areas: Z')."""
    try:
        raw = json.loads(raw).get("result", raw)
    except (json.JSONDecodeError, AttributeError):
        pass
    devices = []
    for block in re.split(r"\n- ", "\n" + raw)[1:]:
        names = re.search(r"^names: (.+)$", block, re.M)
        domain = re.search(r"^\s*domain: (\S+)", block, re.M)
        areas = re.search(r"^\s*areas: (.+)$", block, re.M)
        if not (names and domain):
            continue
        area_list = tuple(a.strip() for a in areas.group(1).split(",")) if areas else ()
        for n in names.group(1).split(","):  # aliases are comma-separated
            devices.append(Device(n.strip().strip("'\""), domain.group(1), area_list))
    return devices


def _format_devices(devices: list[Device]) -> str:
    by_domain: dict[str, list[str]] = {}
    for d in devices:
        group = " [whole-room group]" if any(d.name.casefold() == a.casefold() for a in d.areas) else ""
        where = f" (area: {', '.join(d.areas)})" if d.areas else ""
        by_domain.setdefault(d.domain, []).append(f"- {d.name}{where}{group}")
    return "\n".join(f"{dom} devices:\n" + "\n".join(lines) for dom, lines in sorted(by_domain.items()))


def _sanitize_args(args: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """Drop unknown keys and enum values the schema doesn't allow (e.g. device_class=['light'])."""
    props = (schema or {}).get("properties") or {}
    if not props:
        return args
    clean: dict[str, Any] = {}
    for k, v in args.items():
        if k not in props:
            continue
        enum = (props[k].get("items") or {}).get("enum") or props[k].get("enum")
        if enum is not None:
            v = [x for x in v if x in enum] if isinstance(v, list) else (v if v in enum else None)
            if not v:
                continue
        clean[k] = v
    return clean


def _check_target(args: dict[str, Any], devices: list[Device]) -> str | None:
    """Return a corrective error message if the call targets something that doesn't exist."""
    domains = args.get("domain") or []
    domains = [domains] if isinstance(domains, str) else domains

    def names_for(doms: list[str]) -> str:
        pool = [d.name for d in devices if not doms or d.domain in doms]
        return ", ".join(sorted(set(pool))) or "(none)"

    if name := args.get("name"):
        matches = [d for d in devices if d.name.casefold() == str(name).casefold()]
        if not matches:
            return f"No device named '{name}'. Valid {'/'.join(domains) or 'device'} names: {names_for(domains)}."
        if domains and not any(m.domain in domains for m in matches):
            return (
                f"'{name}' is a {matches[0].domain}, not a {'/'.join(domains)}. "
                f"Valid {'/'.join(domains)} names: {names_for(domains)}."
            )
    if area := args.get("area"):
        known = {a.casefold() for d in devices for a in d.areas}
        if str(area).casefold() not in known:
            areas = sorted({a for d in devices for a in d.areas})
            return f"No area named '{area}'. Valid areas: {', '.join(areas)}."
    return None


async def _chat(client: httpx.AsyncClient, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "tools": tools,
        "temperature": 0,
        "max_tokens": 1024,
    }
    if LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = LLM_REASONING_EFFORT
    r = await client.post(f"{LLM_URL}/chat/completions", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"LLM server {r.status_code}: {r.text[:500]}")
    return r.json()["choices"][0]["message"]


async def run_home_command(text: str) -> HomeResult:
    # The MCP transport runs in an anyio TaskGroup, so failures arrive wrapped in
    # ExceptionGroups. Unwrap to the first real error for a readable message.
    try:
        return await _run_home_command(text)
    except BaseExceptionGroup as eg:
        leaf: BaseException = eg
        while isinstance(leaf, BaseExceptionGroup):
            leaf = leaf.exceptions[0]
        raise RuntimeError(f"Home Assistant MCP: {type(leaf).__name__}: {leaf} (check HA_TOKEN / HA_MCP_URL)") from leaf


async def _run_home_command(text: str) -> HomeResult:
    out = HomeResult(dry_run=HA_DRY_RUN)
    async with ha_session() as session, httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as llm:
        mcp_tools = [t for t in (await session.list_tools()).tools if _allowed(t.name)]
        tools, name_map = _to_openai_tools(mcp_tools)
        schemas = {t.name: t.inputSchema for t in mcp_tools}

        # Prefetch device names/areas: a small model often skips GetLiveContext and guesses
        # names like "bedroom lights" that HA can't match. Read-only, so fine in dry run too.
        live_context, _ = _result_text(await session.call_tool("homeassistant__GetLiveContext", {}))
        devices = _parse_live_context(live_context)
        system = (
            f"{SYSTEM_PROMPT}\n\nCurrent devices (use these exact names and areas):\n"
            f"{_format_devices(devices) if devices else live_context}"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]
        for _ in range(HA_MAX_STEPS):
            msg = await _chat(llm, messages, tools)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                out.reply = (msg.get("content") or "").strip()
                return out

            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
            for tc in tool_calls:
                llm_name = tc["function"]["name"]
                name = name_map.get(llm_name, llm_name)  # unknown names fail _allowed() below
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name not in READ_ONLY_TOOLS:
                    args = _sanitize_args(args, schemas.get(name))
                call = ToolCall(name, args)
                out.calls.append(call)

                problem = None if name in READ_ONLY_TOOLS or not devices else _check_target(args, devices)
                if not _allowed(name):
                    # Model hallucinated or reused a name we never offered: refuse, don't forward.
                    call.is_error = True
                    content = call.result = f"Tool '{name}' is not available."
                elif problem:
                    # Caught locally (works in dry run too): tell the model exactly what to fix.
                    call.is_error = True
                    content = call.result = problem
                elif HA_DRY_RUN and name not in READ_ONLY_TOOLS:
                    content = "DRY RUN: not executed. Assume it succeeded."
                else:
                    result = await session.call_tool(name, args)
                    content, call.is_error = _result_text(result)
                    call.result = content
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})

        out.reply = f"(stopped after {HA_MAX_STEPS} tool rounds)"
        return out


def format_home_result(r: HomeResult) -> str:
    lines = [f"home assistant{' (DRY RUN)' if r.dry_run else ''}:"]
    for c in r.calls:
        short = c.name.split("__", 1)[-1]
        if c.name in READ_ONLY_TOOLS:
            lines.append(f"  -> {short}({json.dumps(c.arguments)})")
            continue
        status = "not executed" if c.result is None else ("ERROR " + c.result[:200] if c.is_error else "ok")
        lines.append(f"  -> {short}({json.dumps(c.arguments)})  [{status}]")

    # Outcome from tool results, not from the model's reply (which can gloss over errors).
    actions = [c for c in r.calls if c.name not in READ_ONLY_TOOLS]
    if not actions:
        outcome = "no action taken"
    elif r.dry_run:
        outcome = "dry run"
    elif actions[-1].is_error:
        outcome = "FAILED"
    else:
        outcome = "success"
    lines.append(f"  outcome: {outcome}")
    lines.append(f"  reply: {r.reply}")
    return "\n".join(lines)
