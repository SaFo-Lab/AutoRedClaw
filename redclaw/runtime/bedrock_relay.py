#!/usr/bin/env python3
"""
Bedrock relay proxy.

Auto-detects whether the incoming request uses OpenAI format (LiteLLM/seclaw)
or Anthropic format (SDK-based adapters). Handles both:

OpenAI path (openclaw / seclaw / zeroclaw):
  request  → OpenAI  → normalize to Anthropic → Bedrock
  response ← OpenAI  ← convert from Anthropic ← Bedrock

Anthropic path (nanobot):
  request  → Anthropic (model name fixed) → Bedrock
  response ← Anthropic (pass-through)     ← Bedrock

Discovers AWS model capabilities and inference profiles on the first request.
Preference: Runtime InvokeModel, Runtime Converse, then Mantle Messages.
Catalogs and routes are pinned for the relay process. Discovery failures fall
back to the previous routing rules; inference failures never change routes.

Usage (inside Docker container):
    BEDROCK_RELAY_BASE=https://bedrock-mantle.us-east-1.api.aws \
    AWS_BEARER_TOKEN_BEDROCK=<token> \
    python3 /opt/bedrock_relay.py 7891 &
"""
import http.server
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = os.environ.get("BEDROCK_RELAY_BASE", "")
TOKEN = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
# If set, always use this model regardless of what the client sends.
FORCE_MODEL = os.environ.get("BEDROCK_RELAY_MODEL", "")
# HTTP listener bind address (default: loopback only; set to 0.0.0.0 to accept all).
HTTP_BIND = os.environ.get("HTTP_BIND", "127.0.0.1")
CLIENT_TOKEN = os.environ.get("BEDROCK_RELAY_CLIENT_TOKEN", "")
SERVICE_ID = os.environ.get("BEDROCK_RELAY_SERVICE_ID", "")
_CATALOG_CACHE: dict = {}
_ROUTE_CACHE: dict = {}
_DISCOVERY_LOCK = threading.RLock()

INFERENCE_PROFILE_PREFIXES = ("us", "eu", "au", "jp", "apac", "global")
# Only historical naming exceptions need aliases. Starting with Sonnet 4.6,
# Bedrock uses the dateless model name directly (Opus 4.6 still needs -v1).
# https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions
RUNTIME_MODEL_ALIASES = {
    "anthropic.claude-sonnet-4-5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-sonnet-4-5-20250929": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    "anthropic.claude-opus-4-5-20251101": "anthropic.claude-opus-4-5-20251101-v1:0",
    "anthropic.claude-opus-4-6": "anthropic.claude-opus-4-6-v1",
}
CLAUDE_MODEL_PATTERN = re.compile(
    rf"(?:(?P<profile>{'|'.join(INFERENCE_PROFILE_PREFIXES)})\.)?"
    r"anthropic\.claude-(?P<family>sonnet|opus)-(?P<major>\d+)"
    r"(?:[-.](?P<minor>\d{1,3})(?=-|$))?"
    r"(?P<suffix>(?:-[A-Za-z0-9][A-Za-z0-9:.-]*)?)"
)


def _normalize_model_id(model: str) -> str:
    """Accept adapter model aliases and explicit Bedrock inference profiles."""
    while "/" in model:
        model = model.split("/", 1)[1]
    # OpenClaw's existing FORCE_MODEL wiring prepends anthropic. even when
    # an operator supplies a fully qualified inference profile.
    if model.startswith("anthropic.") and model.removeprefix("anthropic.").startswith(
        tuple(f"{p}.anthropic." for p in INFERENCE_PROFILE_PREFIXES)
    ):
        model = model.removeprefix("anthropic.")
    if model and not model.startswith(
        ("anthropic.", *(f"{p}.anthropic." for p in INFERENCE_PROFILE_PREFIXES))
    ):
        model = f"anthropic.{model}"
    return model


def _runtime_claude_model(model: str) -> re.Match | None:
    """Select the Runtime route by numeric version, not an allowlist."""
    match = CLAUDE_MODEL_PATTERN.fullmatch(model)
    if match and (int(match["major"]), int(match["minor"] or 0)) >= (4, 5):
        return match
    return None


def _runtime_profile_id(match: re.Match, region: str) -> str:
    base = f"anthropic.claude-{match['family']}-{match['major']}"
    if match["minor"] is not None:
        base += f"-{match['minor']}"
    base += match["suffix"]
    base = RUNTIME_MODEL_ALIASES.get(base, base)
    prefix = match["profile"]
    if not prefix:
        version = (int(match["major"]), int(match["minor"] or 0))
        # Prefer geography profiles where supported. Explicit caller profiles
        # always win; do not silently fall back after a permission/quota error.
        if region.startswith("us-") or region == "ca-central-1":
            prefix = "us"
        elif region == "ca-west-1" and version >= (4, 6):
            prefix = "us"
        elif region.startswith("eu-"):
            prefix = "eu"
        elif region in {"ap-southeast-2", "ap-southeast-4", "ap-southeast-6"} and (
            match["family"] == "sonnet" or version >= (4, 6)
        ):
            prefix = "au"
        elif region in {"ap-northeast-1", "ap-northeast-3"} and (
            match["family"] == "sonnet" and version <= (4, 6)
        ):
            prefix = "jp"
        else:
            prefix = "global"
    return f"{prefix}.{base}"


def _legacy_route(model: str, region: str) -> dict:
    """Preserve the previous routing rules when discovery is unavailable."""
    runtime_model = _runtime_claude_model(model)
    if runtime_model:
        model = _runtime_profile_id(runtime_model, region)
    return {"api": "invoke" if runtime_model else "mantle", "model_id": model,
            "region": region, "source": "legacy-fallback"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _aws_region() -> str:
    endpoint = urllib.parse.urlsplit(BASE_URL)
    match = re.fullmatch(
        r"bedrock-(?:mantle|runtime)\.([a-z0-9-]+)\.(?:api\.aws|amazonaws\.com)",
        endpoint.hostname or "",
    )
    if endpoint.scheme != "https" or not match:
        raise ValueError("Discovery requires an AWS Bedrock HTTPS endpoint with a region")
    return match[1]


def _discovery_json(url: str) -> dict:
    # Use raw JSON: some SDK service schemas omit inferenceAPIsSupported.
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": "application/json",
    })
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Invalid AWS discovery response")
    return result


def _catalog_pages(url: str, field: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    while True:
        page = _discovery_json(url)
        entries = page.get(field)
        if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
            raise ValueError("Invalid AWS model catalog")
        rows.extend(entries)
        cursor = page.get("nextToken")
        cursor_key = "nextToken"
        if not cursor and page.get("has_more"):
            cursor = page.get("last_id") or (entries[-1].get("id") if entries else None)
            cursor_key = "after"
            if not cursor:
                raise ValueError("AWS catalog pagination is incomplete")
        if not cursor:
            return rows
        if cursor in seen:
            raise ValueError("AWS catalog pagination repeated a cursor")
        seen.add(cursor)
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        query[cursor_key] = cursor
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _fetch_catalog(region: str) -> dict:
    endpoints = {
        "runtime": (f"https://bedrock.{region}.amazonaws.com/foundation-models?byProvider=anthropic", "modelSummaries"),
        "profiles": (f"https://bedrock.{region}.amazonaws.com/inference-profiles?type=SYSTEM_DEFINED&maxResults=1000", "inferenceProfileSummaries"),
        "mantle": (f"https://bedrock-mantle.{region}.api.aws/v1/models", "data"),
    }
    def fetch(item):
        name, (url, field) = item
        try:
            return name, _catalog_pages(url, field)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            # Do not print response bodies, which can contain account details.
            reason = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            print(f"[bedrock-relay] {name} discovery unavailable ({reason})", file=sys.stderr, flush=True)
            return name, None  # unavailable is distinct from an empty catalog
    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(fetch, endpoints.items()))


def _split_profile(model: str) -> tuple[str | None, str]:
    for prefix in INFERENCE_PROFILE_PREFIXES:
        if model.startswith(f"{prefix}.anthropic."):
            return prefix, model[len(prefix) + 1:]
    return None, model


def _catalog_model_id(requested: str, available: list[str]) -> str | None:
    requested = re.sub(r"(claude-[a-z]+-\d+)\.(\d+)(?=-|$)", r"\1-\2", requested)
    ids = set(available)
    if requested in ids:
        return requested
    # Resolve only omitted snapshot/revision suffixes, never another model version.
    matches = sorted(model for model in ids if model.startswith(requested) and re.fullmatch(
        r"(?:-\d{8})?(?:-v\d+(?::\d+)?)?", model[len(requested):],
    ))
    if len(matches) > 1:
        raise ValueError(f"Ambiguous model alias {requested}; use an exact AWS model ID: {', '.join(matches)}")
    return matches[0] if matches else None


def _profile_target(model: dict, profiles: list[dict], explicit: str | None, region: str) -> str | None:
    base = model["modelId"]
    candidates = []
    for profile in profiles:
        if profile.get("status") != "ACTIVE":
            continue
        if not any(entry.get("modelArn", "").rsplit("/", 1)[-1] == base
                   for entry in profile.get("models", [])):
            continue
        profile_id = profile.get("inferenceProfileId", "")
        prefix, _ = _split_profile(profile_id)
        if prefix and (not explicit or prefix == explicit):
            candidates.append(profile_id)
    if explicit:
        return sorted(candidates)[0] if candidates else None
    if region.startswith(("us-", "ca-")):
        local = ["us"]
    elif region.startswith("eu-"):
        local = ["eu"]
    elif region in {"ap-southeast-2", "ap-southeast-4", "ap-southeast-6"}:
        local = ["au", "apac"]
    elif region in {"ap-northeast-1", "ap-northeast-3"}:
        local = ["jp", "apac"]
    elif region.startswith("ap-"):
        local = ["apac"]
    else:
        local = []
    for prefix in [*local, "global"]:
        matches = sorted(item for item in candidates if item.startswith(prefix + "."))
        if matches:
            return matches[0]
    if "ON_DEMAND" in model.get("inferenceTypesSupported", []):
        return base
    return None


def _sync_supported(apis: dict, field: str) -> bool | None:
    capability = apis.get(field)
    return capability if isinstance(capability, bool) else (
        capability.get("sync") if isinstance(capability, dict) else None
    )


def _select_route(requested: str, region: str, catalog: dict) -> dict | None:
    explicit, base = _split_profile(requested)
    runtime = catalog.get("runtime")
    profiles = catalog.get("profiles")
    mantle = catalog.get("mantle")
    incomplete = runtime is None
    if runtime is not None:
        rows = {row["modelId"]: row for row in runtime if row.get("modelId", "").startswith("anthropic.claude-")
                and row.get("modelLifecycle", {}).get("status") not in {"EOL", "RETIRED", "DEPRECATED"}}
        model_id = _catalog_model_id(base, list(rows))
        if model_id:
            row = rows[model_id]
            target = _profile_target(row, profiles or [], explicit, region)
            apis = row.get("inferenceAPIsSupported") or {}
            if explicit and profiles is not None and not target:
                raise ValueError(f"Requested profile {explicit} for {model_id} is unavailable in {region}")
            for api, field in (("invoke", "invokeModel"), ("converse", "converse")):
                if target and _sync_supported(apis, field) is True:
                    return {"api": api, "model_id": target, "region": region, "source": "aws-discovery"}
            # Older service schemas lack capability metadata. This remains an
            # explicit compatibility fallback, never an override of false.
            if target and _sync_supported(apis, "invokeModel") is None:
                return {"api": "invoke", "model_id": target, "region": region, "source": "catalog-legacy-api"}
            incomplete |= profiles is None and _sync_supported(apis, "invokeModel") is not False
    if mantle is not None and not explicit:
        ids = [row["id"] for row in mantle if row.get("id", "").startswith("anthropic.claude-")
               and str(row.get("status", "active")).lower() in {"active", "available"}]
        model_id = _catalog_model_id(base, ids)
        if model_id:
            return {"api": "mantle", "model_id": model_id, "region": region, "source": "aws-discovery"}
    if incomplete:
        return None
    raise ValueError(f"No supported AWS route/profile found for {requested} in {region}")


def _record_route(requested: str, route: dict) -> None:
    entry = {"requested_model": requested, **route}
    print(f"[bedrock-relay] route {json.dumps(entry)}", file=sys.stderr, flush=True)
    try:
        with open("/tmp/bedrock_relay_routes.jsonl", "a", encoding="utf-8") as output:
            output.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _resolve_route(model: str) -> dict:
    region = _aws_region()
    scope = (region, hashlib.sha256(TOKEN.encode()).hexdigest())
    key = (*scope, model)
    with _DISCOVERY_LOCK:
        if key not in _ROUTE_CACHE:
            if scope not in _CATALOG_CACHE:
                _CATALOG_CACHE[scope] = _fetch_catalog(region)
            route = _select_route(model, region, _CATALOG_CACHE[scope])
            if route is None:
                route = _legacy_route(model, region)
            _ROUTE_CACHE[key] = route
            _record_route(model, route)
        return _ROUTE_CACHE[key]


def _sampling_parameters(payload: dict, model: str) -> None:
    # https://platform.claude.com/docs/en/docs/about-claude/model-deprecations
    match = _runtime_claude_model(model)
    if match:
        version = (int(match["major"]), int(match["minor"] or 0))
        if version >= (4, 7):
            removed = [key for key in ("temperature", "top_p", "top_k") if key in payload]
            for key in removed:
                payload.pop(key)
            if removed:
                print(f"[bedrock-relay] {model}: omitted unsupported sampling parameters: "
                      f"{', '.join(removed)}", file=sys.stderr, flush=True)
    if "temperature" in payload and "top_p" in payload and (
        match or "claude-haiku-4-5" in model
    ):
        payload.pop("top_p")


def _upstream_request(body: dict, *, anthropic_version: str) -> urllib.request.Request:
    route = _resolve_route(body["model"])
    model, region, api = route["model_id"], route["region"], route["api"]
    payload = dict(body)
    _sampling_parameters(payload, model)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    if api == "mantle":
        target = f"https://bedrock-mantle.{region}.api.aws/anthropic/v1/messages"
        payload["model"] = model
        headers["anthropic-version"] = anthropic_version
    else:
        target = f"https://bedrock-runtime.{region}.amazonaws.com/model/{urllib.parse.quote(model, safe='')}/{api}"
        payload.pop("model")
        if api == "converse":
            payload = _to_converse(payload)
        else:
            payload["anthropic_version"] = "bedrock-2023-05-31"
    return urllib.request.Request(target, data=json.dumps(payload).encode(), headers=headers, method="POST")


def _converse_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    result = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            result.append({"text": block["text"]})
        elif kind in {"image", "document"} and block.get("source", {}).get("type") == "base64":
            source = block["source"]
            item = {"format": source["media_type"].split("/")[-1], "source": {"bytes": source["data"]}}
            if kind == "document":
                item["name"] = block.get("title", "document")
            result.append({kind: item})
        elif kind == "tool_use":
            result.append({"toolUse": {"toolUseId": block["id"], "name": block["name"], "input": block["input"]}})
        elif kind == "tool_result":
            result.append({"toolResult": {"toolUseId": block["tool_use_id"],
                           "content": _converse_blocks(block.get("content", "")),
                           "status": "error" if block.get("is_error") else "success"}})
        elif kind == "thinking":
            result.append({"reasoningContent": {"reasoningText": {
                "text": block["thinking"], "signature": block["signature"],
            }}})
        elif kind == "redacted_thinking":
            result.append({"reasoningContent": {"redactedContent": block["data"]}})
        else:
            raise ValueError(f"Converse conversion does not support content type {kind}")
    return result


def _to_converse(body: dict) -> dict:
    payload = {"messages": [{"role": message["role"], "content": _converse_blocks(message["content"])}
                            for message in body["messages"]]}
    if body.get("system"):
        payload["system"] = _converse_blocks(body["system"])
    names = {"max_tokens": "maxTokens", "temperature": "temperature", "top_p": "topP", "stop_sequences": "stopSequences"}
    payload["inferenceConfig"] = {target: body[source] for source, target in names.items() if source in body}
    if body.get("tools"):
        config = {"tools": [{"toolSpec": {"name": tool["name"], "description": tool.get("description", ""),
                                         "inputSchema": {"json": tool["input_schema"]}}} for tool in body["tools"]]}
        choice = body.get("tool_choice")
        if choice:
            kind = choice["type"]
            if kind not in {"auto", "any", "tool"}:
                raise ValueError(f"Converse conversion does not support tool_choice {kind}")
            if choice.get("disable_parallel_tool_use"):
                raise ValueError("Converse conversion does not support disable_parallel_tool_use")
            config["toolChoice"] = {kind: {"name": choice["name"]} if kind == "tool" else {}}
        payload["toolConfig"] = config
    consumed = {*names, "messages", "system", "tools", "tool_choice", "anthropic_version"}
    extras = {key: value for key, value in body.items() if key not in consumed}
    if extras:
        payload["additionalModelRequestFields"] = extras
    return payload


def _from_converse(response: dict, model: str) -> dict:
    content = []
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            content.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            tool = block["toolUse"]
            content.append({"type": "tool_use", "id": tool["toolUseId"], "name": tool["name"], "input": tool["input"]})
        elif "reasoningContent" in block:
            reasoning = block["reasoningContent"]
            if "reasoningText" in reasoning:
                thought = reasoning["reasoningText"]
                content.append({"type": "thinking", "thinking": thought["text"], "signature": thought.get("signature", "")})
            elif "redactedContent" in reasoning:
                content.append({"type": "redacted_thinking", "data": reasoning["redactedContent"]})
        else:
            raise ValueError(f"Unsupported Converse response content: {', '.join(block)}")
    usage = response.get("usage", {})
    return {"id": response.get("requestId", "msg_relay"), "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": response.get("stopReason", "end_turn"),
            "stop_sequence": response.get("additionalModelResponseFields", {}).get("stop_sequence"),
            "usage": {"input_tokens": usage.get("inputTokens", 0), "output_tokens": usage.get("outputTokens", 0),
                      "cache_read_input_tokens": usage.get("cacheReadInputTokens", 0),
                      "cache_creation_input_tokens": usage.get("cacheWriteInputTokens", 0)}}


# ---------------------------------------------------------------------------
# Request normalization helpers
# ---------------------------------------------------------------------------

def _strip_cache_control(obj: object) -> None:
    """Recursively remove cache_control — bedrock-mantle rejects it."""
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for v in obj.values():
            _strip_cache_control(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_cache_control(item)


def _normalize_tools(tools: list) -> list:
    """Convert OpenAI function-tool format to Anthropic tool format.

    Filters out built-in Responses API tools (web_search_preview, code_interpreter,
    etc.) that have no function body — Bedrock rejects tools without a name.
    """
    result = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", t)
            name = fn.get("name", "")
            if not name:
                continue  # skip nameless entries
            result.append({
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        else:
            entry = dict(t)
            entry.pop("type", None)
            if "parameters" in entry and "input_schema" not in entry:
                entry["input_schema"] = entry.pop("parameters")
            if not entry.get("name"):
                continue  # skip built-in tools (web_search_preview, etc.) with no name
            # Strip any non-Anthropic fields (e.g. "custom" sub-objects) that
            # would cause bedrock to reject the request ("tools.N.custom.name: Field required").
            result.append({
                k: entry[k] for k in ("name", "description", "input_schema")
                if k in entry
            })
    return result


def _normalize_messages(messages: list) -> tuple[list, str | None]:
    """Normalize OpenAI-format messages to Anthropic format.

    Handles:
    - system messages → extracted as separate system string
    - role: "tool" → wrapped as tool_result in user turn
    - assistant tool_calls → tool_use content blocks
    """
    system: str | None = None
    result: list = []
    pending_tool_results: list = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str):
                system = content
            elif isinstance(content, list):
                system = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            continue

        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content if isinstance(content, str) else json.dumps(content),
            })
            continue

        if pending_tool_results:
            result.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results = []

        if role == "assistant" and msg.get("tool_calls"):
            blocks: list = []
            if content:
                blocks.append({"type": "text", "text": content if isinstance(content, str) else str(content)})
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    parsed = {"_raw": raw_args}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": parsed,
                })
            result.append({"role": "assistant", "content": blocks})
            continue

        if isinstance(content, str):
            result.append({"role": role, "content": content})
        elif isinstance(content, list):
            result.append({"role": role, "content": content})
        else:
            result.append(msg)

    if pending_tool_results:
        result.append({"role": "user", "content": list(pending_tool_results)})

    return result, system


def _is_openai_format(body: dict) -> bool:
    """Detect if the request is OpenAI format (needs response conversion)."""
    if "input" in body and "messages" not in body:
        return True  # OpenAI Responses API
    tools = body.get("tools", [])
    if any(t.get("type") == "function" for t in tools):
        return True
    messages = body.get("messages", [])
    for msg in messages:
        if msg.get("role") == "tool":
            return True
        if msg.get("tool_calls"):
            return True
    return False


def _responses_api_to_anthropic(body: dict) -> dict:
    """Convert OpenAI Responses API request to Anthropic Messages format."""
    # Extract system instruction
    system: str | None = body.get("instructions")

    # Convert input → messages
    raw_input = body.get("input", [])
    if isinstance(raw_input, str):
        raw_input = [{"role": "user", "content": raw_input}]

    messages: list = []
    pending_tool_results: list = []

    for item in raw_input:
        item_type = item.get("type", item.get("role", ""))
        if item_type == "system":
            system = item.get("content", system)
        elif item_type == "function_call":
            # Convert to assistant tool_use block
            if pending_tool_results:
                messages.append({"role": "user", "content": list(pending_tool_results)})
                pending_tool_results = []
            try:
                parsed = json.loads(item.get("arguments", "{}"))
            except Exception:
                parsed = {}
            messages.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": item.get("call_id", ""), "name": item.get("name", ""), "input": parsed}],
            })
        elif item_type == "function_call_output":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": item.get("call_id", ""),
                "content": str(item.get("output", "")),
            })
        else:
            if pending_tool_results:
                messages.append({"role": "user", "content": list(pending_tool_results)})
                pending_tool_results = []
            role = item.get("role", "user")
            content = item.get("content", "")
            # Normalize Responses API content block types to Anthropic types.
            # e.g. "input_text" → "text", "output_text" → "text"
            if isinstance(content, list):
                normalized = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype in ("input_text", "output_text"):
                            block = {"type": "text", "text": block.get("text", "")}
                    normalized.append(block)
                content = normalized
            messages.append({"role": role, "content": content if isinstance(content, (str, list)) else str(content)})

    if pending_tool_results:
        messages.append({"role": "user", "content": pending_tool_results})

    result: dict = {"messages": messages}
    if system:
        result["system"] = system

    # Convert tools
    if body.get("tools"):
        result["tools"] = _normalize_tools(body["tools"])

    return result


def _anthropic_to_responses_api(resp: dict) -> dict:
    """Convert Anthropic Messages response to OpenAI Responses API format."""
    output: list = []
    content_blocks = resp.get("content", [])
    text_parts: list[str] = []

    for block in content_blocks:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            if text_parts:
                output.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "".join(text_parts)}],
                })
                text_parts = []
            output.append({
                "type": "function_call",
                "call_id": block.get("id", "call_0"),
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {})),
            })

    if text_parts:
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(text_parts)}],
        })

    usage = resp.get("usage", {})
    return {
        "id": resp.get("id", "resp_relay"),
        "object": "response",
        "model": resp.get("model", ""),
        "output": output,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Response conversion: Anthropic → OpenAI
# ---------------------------------------------------------------------------

def _to_chat_completion_sse(full_resp: dict) -> bytes:
    """Convert a non-streaming chat.completion response to SSE stream bytes."""
    choice = full_resp["choices"][0]
    msg = choice["message"]
    model = full_resp.get("model", "")
    resp_id = full_resp.get("id", "chatcmpl-relay")
    finish = choice.get("finish_reason", "stop")

    chunks: list[dict] = []
    chunks.append({
        "id": resp_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    })
    if msg.get("content"):
        chunks.append({
            "id": resp_id, "object": "chat.completion.chunk", "model": model,
            "choices": [{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}],
        })
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        chunks.append({
            "id": resp_id, "object": "chat.completion.chunk", "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": i, "id": tc["id"], "type": "function",
                "function": {"name": fn["name"], "arguments": ""},
            }]}, "finish_reason": None}],
        })
        if fn.get("arguments"):
            chunks.append({
                "id": resp_id, "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "function": {"arguments": fn["arguments"]}}]}, "finish_reason": None}],
            })
    chunks.append({
        "id": resp_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
    })
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return sse.encode("utf-8")


def _to_anthropic_sse(full_resp: dict) -> bytes:
    """Convert non-streaming Anthropic Messages response to Anthropic SSE stream bytes."""
    model = full_resp.get("model", "")
    resp_id = full_resp.get("id", "msg_relay")
    usage = full_resp.get("usage", {})
    content_blocks = full_resp.get("content", [])
    stop_reason = full_resp.get("stop_reason", "end_turn")

    lines: list[str] = []
    msg_start = {
        "type": "message_start",
        "message": {
            "id": resp_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0},
        },
    }
    lines.append(f"event: message_start\ndata: {json.dumps(msg_start)}\n\n")

    for i, block in enumerate(content_blocks):
        btype = block.get("type", "text")
        if btype == "text":
            lines.append(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': i, 'content_block': {'type': 'text', 'text': ''}})}\n\n")
            lines.append(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'text_delta', 'text': block.get('text', '')}})}\n\n")
            lines.append(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n")
        elif btype == "tool_use":
            lines.append(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': i, 'content_block': {'type': 'tool_use', 'id': block.get('id', ''), 'name': block.get('name', ''), 'input': {}}})}\n\n")
            lines.append(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block.get('input', {}))}})}\n\n")
            lines.append(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n")

    lines.append(f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': usage.get('output_tokens', 0)}})}\n\n")
    lines.append(f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n")
    return "".join(lines).encode("utf-8")


def _to_responses_api_sse(full_resp: dict) -> bytes:
    """Emit a full Responses API SSE stream with per-item events.

    The OpenAI SDK counts payloads via response.output_item.added events —
    a minimal response.created + response.completed stream yields payloads=0.
    We emit the full event sequence so the SDK sees each output item correctly.
    """
    output = full_resp.get("output", [])
    resp_in_progress = {**full_resp, "status": "in_progress"}
    resp_completed = {**full_resp, "status": "completed"}

    lines: list[str] = []

    # 1. response.created
    lines.append(f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': resp_in_progress})}\n\n")

    # 2. Per-item events
    for idx, item in enumerate(output):
        item_type = item.get("type", "")

        if item_type == "function_call":
            item_id = item.get("id") or item.get("call_id") or f"fc_{idx}"
            call_id = item.get("call_id", item_id)
            name = item.get("name", "")
            arguments = item.get("arguments", "")
            skeleton = {"type": "function_call", "id": item_id, "call_id": call_id, "name": name, "arguments": ""}
            lines.append(f"event: response.output_item.added\ndata: {json.dumps({'type': 'response.output_item.added', 'output_index': idx, 'item': skeleton})}\n\n")
            if arguments:
                lines.append(f"event: response.function_call_arguments.delta\ndata: {json.dumps({'type': 'response.function_call_arguments.delta', 'item_id': item_id, 'output_index': idx, 'delta': arguments})}\n\n")
            lines.append(f"event: response.function_call_arguments.done\ndata: {json.dumps({'type': 'response.function_call_arguments.done', 'item_id': item_id, 'output_index': idx, 'arguments': arguments})}\n\n")
            lines.append(f"event: response.output_item.done\ndata: {json.dumps({'type': 'response.output_item.done', 'output_index': idx, 'item': {**skeleton, 'arguments': arguments}})}\n\n")

        elif item_type == "message":
            item_id = item.get("id", f"msg_{idx}")
            content_blocks = item.get("content", [])
            skeleton = {"type": "message", "id": item_id, "role": item.get("role", "assistant"), "content": []}
            lines.append(f"event: response.output_item.added\ndata: {json.dumps({'type': 'response.output_item.added', 'output_index': idx, 'item': skeleton})}\n\n")
            for c_idx, block in enumerate(content_blocks):
                btype = block.get("type", "output_text")
                text = block.get("text", "")
                lines.append(f"event: response.content_part.added\ndata: {json.dumps({'type': 'response.content_part.added', 'item_id': item_id, 'output_index': idx, 'content_index': c_idx, 'part': {'type': btype, 'text': ''}})}\n\n")
                if text:
                    lines.append(f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': idx, 'content_index': c_idx, 'delta': text})}\n\n")
                lines.append(f"event: response.output_text.done\ndata: {json.dumps({'type': 'response.output_text.done', 'item_id': item_id, 'output_index': idx, 'content_index': c_idx, 'text': text})}\n\n")
                lines.append(f"event: response.content_part.done\ndata: {json.dumps({'type': 'response.content_part.done', 'item_id': item_id, 'output_index': idx, 'content_index': c_idx, 'part': {'type': btype, 'text': text}})}\n\n")
            lines.append(f"event: response.output_item.done\ndata: {json.dumps({'type': 'response.output_item.done', 'output_index': idx, 'item': {**skeleton, 'content': content_blocks}})}\n\n")

        else:
            # Generic item — just emit added/done
            lines.append(f"event: response.output_item.added\ndata: {json.dumps({'type': 'response.output_item.added', 'output_index': idx, 'item': item})}\n\n")
            lines.append(f"event: response.output_item.done\ndata: {json.dumps({'type': 'response.output_item.done', 'output_index': idx, 'item': item})}\n\n")

    # 3. response.completed
    lines.append(f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': resp_completed})}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _anthropic_to_openai(resp: dict) -> dict:
    """Convert an Anthropic Messages API response to OpenAI chat.completion."""
    content_blocks = resp.get("content", [])
    text = ""
    tool_calls = []

    for block in content_blocks:
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", "call_0"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    stop_reason = resp.get("stop_reason", "end_turn")
    finish_reason = "tool_calls" if tool_calls else ("stop" if stop_reason == "end_turn" else stop_reason)

    message: dict = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = resp.get("usage", {})
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)

    return {
        "id": resp.get("id", "chatcmpl-relay"),
        "object": "chat.completion",
        "model": resp.get("model", ""),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out},
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        if not CLIENT_TOKEN or hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {CLIENT_TOKEN}"
        ):
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"Invalid relay credential"}}')
        return False

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path == "/health":
            data = {"status": "ok", "service_id": SERVICE_ID}
        elif path == "/v1/models":
            data = {"object": "list", "data": [
                {"id": FORCE_MODEL, "object": "model", "owned_by": "aws-bedrock"}
            ] if FORCE_MODEL else []}
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self) -> None:
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", 0))
        body: dict = json.loads(self.rfile.read(length)) if length else {}

        # Detect format before any mutation
        is_responses_api = "input" in body and "messages" not in body
        openai_mode = (
            self.path.rstrip("/").endswith("/chat/completions")
            or _is_openai_format(body)
        )
        was_streaming = bool(body.pop("stream", False))


        # For Responses API, merge converted fields into body
        if is_responses_api:
            converted = _responses_api_to_anthropic(body)
            body.update(converted)
            # Remove Responses API-specific fields not accepted by Anthropic
            for _key in ("input", "instructions", "store", "metadata",
                         "previous_response_id", "response_format",
                         "service_tier", "user", "truncation", "text",
                         "include", "parallel_tool_calls", "reasoning",
                         "context_management", "prompt_cache_key"):
                body.pop(_key, None)
            # Responses API uses max_output_tokens; Anthropic uses max_tokens.
            # Cap at 32768 — gpt-5.5 allows 128K but bedrock Haiku allows only 64K;
            # 32768 is a safe universal cap that avoids per-model bookkeeping.
            if "max_output_tokens" in body:
                body.setdefault("max_tokens", min(body.pop("max_output_tokens"), 32768))
            else:
                body.pop("max_output_tokens", None)

        # 1. Translate model ID
        body["model"] = _normalize_model_id(FORCE_MODEL or body.get("model", ""))

        # 2. Normalize tool definitions (OpenAI → Anthropic)
        if body.get("tools"):
            body["tools"] = _normalize_tools(body["tools"])

        # 3. Normalize tool_choice
        tc = body.get("tool_choice")
        if tc == "auto":
            body["tool_choice"] = {"type": "auto"}
        elif tc in ("any", "required"):
            body["tool_choice"] = {"type": "any"}
        elif tc == "none":
            body.pop("tool_choice", None)
        elif isinstance(tc, dict) and tc.get("type") == "function":
            body["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}

        # 4. Normalize messages; extract system prompt
        if body.get("messages"):
            msgs, sys_prompt = _normalize_messages(body["messages"])
            body["messages"] = msgs
            if sys_prompt and "system" not in body:
                body["system"] = sys_prompt

        # 5. Strip cache_control
        _strip_cache_control(body)

        # 5b. Map/strip OpenAI-only params bedrock-mantle rejects. openclaw's Chat
        # Completions requests carry max_completion_tokens (+ other OpenAI-only fields);
        # Anthropic uses max_tokens/stop_sequences and validates strictly ("Extra
        # inputs are not permitted"), so unknown params must be removed, not forwarded.
        _mct = body.pop("max_completion_tokens", None)
        if _mct is not None and "max_tokens" not in body:
            body["max_tokens"] = _mct
        if "max_tokens" in body:
            body["max_tokens"] = min(body["max_tokens"], 32768)
        if "stop" in body:
            _stop = body.pop("stop")
            if _stop is not None:
                body["stop_sequences"] = _stop if isinstance(_stop, list) else [_stop]
        for _key in ("frequency_penalty", "presence_penalty", "logprobs",
                     "top_logprobs", "n", "seed", "logit_bias", "response_format",
                     "stream_options", "parallel_tool_calls", "store", "user",
                     "service_tier", "metadata", "prompt_cache_key", "modalities",
                     "audio", "prediction", "reasoning_effort", "verbosity",
                     "web_search_options"):
            body.pop(_key, None)

        # 6. Both Bedrock routes require max_tokens
        if "max_tokens" not in body:
            body["max_tokens"] = 4096

        try:
            req = _upstream_request(
                body,
                anthropic_version=self.headers.get("anthropic-version", "2023-06-01"),
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read()
            anthropic_resp = json.loads(raw)
            if urllib.parse.urlsplit(req.full_url).path.endswith("/converse"):
                model_id = urllib.parse.unquote(urllib.parse.urlsplit(req.full_url).path.split("/")[-2])
                anthropic_resp = _from_converse(anthropic_resp, model_id)
                raw = json.dumps(anthropic_resp).encode()
            if is_responses_api:
                try:
                    import sys as _sys
                    _entry = {
                        "RELAY_DEBUG": True,
                        "req_messages_len": len(body.get("messages", [])),
                        "req_tools_len": len(body.get("tools", [])),
                        "resp_content_len": len(anthropic_resp.get("content", [])),
                        "resp_content_types": [b.get("type") for b in anthropic_resp.get("content", [])],
                        "resp_stop_reason": anthropic_resp.get("stop_reason"),
                        "resp_text_preview": next((b.get("text","")[:200] for b in anthropic_resp.get("content",[]) if b.get("type")=="text"), None),
                        "resp_tool_names": [b.get("name") for b in anthropic_resp.get("content",[]) if b.get("type")=="tool_use"],
                    }
                    print(json.dumps(_entry), file=_sys.stderr, flush=True)
                    try:
                        with open("/tmp/bedrock_relay_resp.json", "a") as _f:
                            _f.write(json.dumps({**_entry, "resp_full": anthropic_resp}) + "\n")
                    except Exception:
                        pass
                except Exception:
                    pass
            if is_responses_api:
                converted = _anthropic_to_responses_api(anthropic_resp)
                if was_streaming:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(_to_responses_api_sse(converted))
                    return
                out = json.dumps(converted).encode("utf-8")
            elif openai_mode:
                openai_resp = _anthropic_to_openai(anthropic_resp)
                if was_streaming:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(_to_chat_completion_sse(openai_resp))
                    return
                out = json.dumps(openai_resp).encode("utf-8")
            else:
                if was_streaming:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(_to_anthropic_sse(anthropic_resp))
                    return
                out = raw
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)
        except urllib.error.HTTPError as exc:
            err = exc.read()
            if exc.code == 400:
                try:
                    with open("/tmp/bedrock_relay_400.json", "a") as _f:
                        _f.write(json.dumps({"request_keys": list(body.keys()), "error": err.decode("utf-8", "replace")}) + "\n")
                except Exception:
                    pass
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err)
        except urllib.error.URLError as exc:
            msg = json.dumps({"error": {"message": str(exc.reason), "type": "relay_error"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)
        except ValueError as exc:
            msg = json.dumps({"error": {"message": str(exc), "type": "relay_error"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, *_: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7891
    server = http.server.ThreadingHTTPServer((HTTP_BIND, port), _Handler)

    # Optional TLS listener (for HTTPS MITM, e.g. redirecting api.openai.com via /etc/hosts)
    tls_cert = os.environ.get("TLS_CERT", "")
    tls_key = os.environ.get("TLS_KEY", tls_cert)
    tls_port = int(os.environ.get("TLS_PORT", "443"))
    tls_bind = os.environ.get("TLS_BIND", "127.0.0.1")
    if tls_cert:
        tls_server = http.server.ThreadingHTTPServer((tls_bind, tls_port), _Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        tls_server.socket = ctx.wrap_socket(tls_server.socket, server_side=True)
        threading.Thread(target=tls_server.serve_forever, daemon=True).start()

    server.serve_forever()
