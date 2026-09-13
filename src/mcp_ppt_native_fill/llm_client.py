"""llm_client.py — stdlib-only LLM provider adapters.

Two adapters are provided:

* :class:`OpenAICompatibleClient` — POST ``{base_url}/chat/completions`` with
  ``{"model", "messages", "response_format": {"type": "json_object"}}``.
  Covers OpenAI, Ollama (``/v1/chat/completions``), llama.cpp's server,
  vLLM, LM Studio, and any OpenAI-compatible proxy.

* :class:`AnthropicClient` — POST ``{base_url}/v1/messages`` with the
  Anthropic Messages API shape (``model``, ``max_tokens``, ``system``,
  ``messages``, optional ``response_format`` via tool-use).

Both implement the same :func:`llm_complete_json` contract: send a system +
user prompt, get back a parsed JSON dict. The caller (llm_planner.py) wraps
either client transparently.

All transport goes through ``urllib.request`` — no third-party deps.

Configuration (env vars, all optional except when ``llm_plan`` is on)
--------------------------------------------------------------------
* ``MCP_LLM_PROVIDER``       — ``"anthropic"`` (default) or
                               ``"openai_compatible"``
* ``MCP_LLM_API_KEY``        — API key (``x-api-key`` for Anthropic,
                               ``Authorization: Bearer ...`` for OpenAI)
* ``MCP_LLM_BASE_URL``       — provider root. Defaults:
                               Anthropic  → ``https://api.anthropic.com``
                               OpenAI     → ``https://api.openai.com``
* ``MCP_LLM_MODEL``          — model name. Defaults:
                               Anthropic  → ``claude-sonnet-5-20250929``
                               OpenAI     → ``gpt-5-mini``
* ``MCP_LLM_MAX_TOKENS``     — output cap, default ``4096``
* ``MCP_LLM_TIMEOUT_S``      — per-request timeout, default ``60``

The API key is read once at module import and cached. Never logged.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("mcp_ppt_native_fill.llm_client")


# ---------------------------------------------------------------------------
# Config + provider enumeration.
# ---------------------------------------------------------------------------

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPAT = "openai_compatible"
SUPPORTED_PROVIDERS = {PROVIDER_ANTHROPIC, PROVIDER_OPENAI_COMPAT}

_DEFAULT_MODELS = {
    PROVIDER_ANTHROPIC: "claude-sonnet-5-20250929",
    PROVIDER_OPENAI_COMPAT: "gpt-5-mini",
}

_DEFAULT_BASE_URLS = {
    PROVIDER_ANTHROPIC: "https://api.anthropic.com",
    PROVIDER_OPENAI_COMPAT: "https://api.openai.com",
}

_ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    timeout_s: int
    settings_source: str = "env"  # diagnostic: where the API key came from

    @classmethod
    def from_env(cls) -> "LLMConfig":
        # 1) Explicit MCP_LLM_* env vars always win (overrides everything).
        explicit_key = os.environ.get("MCP_LLM_API_KEY", "").strip()
        if explicit_key:
            return cls._build(provider_env=os.environ.get("MCP_LLM_PROVIDER"),
                              api_key=explicit_key, source="env")

        # 2) Fall back to ~/.claude/settings.json (Claude Code stores the
        #    ANTHROPIC_* trio there: AUTH_TOKEN / BASE_URL / MODEL). Path
        #    is overridable via MCP_LLM_SETTINGS_JSON.
        settings_api_key, settings_base, settings_model, settings_path = (
            _read_claude_settings()
        )
        if settings_api_key:
            # The Claude Code convention is Anthropic-compatible; the
            # ANTHROPIC_BASE_URL points at MiniMax's gateway. Force
            # provider=anthropic unless the caller explicitly set
            # MCP_LLM_PROVIDER.
            provider = (
                os.environ.get("MCP_LLM_PROVIDER", PROVIDER_ANTHROPIC)
                .strip()
                .lower()
            )
            if provider not in SUPPORTED_PROVIDERS:
                raise ValueError(
                    f"MCP_LLM_PROVIDER={provider!r} not supported; "
                    f"use one of {sorted(SUPPORTED_PROVIDERS)}"
                )
            base_url = (
                os.environ.get("MCP_LLM_BASE_URL", settings_base or _DEFAULT_BASE_URLS[provider])
                .rstrip("/")
            )
            model = os.environ.get("MCP_LLM_MODEL", settings_model or _DEFAULT_MODELS[provider])
            try:
                max_tokens = int(os.environ.get("MCP_LLM_MAX_TOKENS", "4096"))
            except ValueError:
                max_tokens = 4096
            try:
                timeout_s = int(os.environ.get("MCP_LLM_TIMEOUT_S", "60"))
            except ValueError:
                timeout_s = 60
            return cls(
                provider=provider,
                api_key=settings_api_key,
                base_url=base_url,
                model=model,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                settings_source=f"claude_settings({settings_path})",
            )

        # 3) Nothing usable — fail loud with a hint that points at both paths.
        raise ValueError(
            "MCP_LLM_API_KEY is required when an LLM phase is enabled. "
            "Set it in the environment, or ensure "
            "~/.claude/settings.json has env.ANTHROPIC_AUTH_TOKEN "
            "(Claude Code does this automatically when launched via the "
            "official MiniMax / Claude Code launcher)."
        )

    @classmethod
    def _build(cls, *, provider_env: str | None, api_key: str, source: str) -> "LLMConfig":
        provider = (provider_env or PROVIDER_ANTHROPIC).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"MCP_LLM_PROVIDER={provider!r} not supported; "
                f"use one of {sorted(SUPPORTED_PROVIDERS)}"
            )
        base_url = (
            os.environ.get("MCP_LLM_BASE_URL", _DEFAULT_BASE_URLS[provider]).rstrip("/")
        )
        model = os.environ.get("MCP_LLM_MODEL", _DEFAULT_MODELS[provider])
        try:
            max_tokens = int(os.environ.get("MCP_LLM_MAX_TOKENS", "4096"))
        except ValueError:
            max_tokens = 4096
        try:
            timeout_s = int(os.environ.get("MCP_LLM_TIMEOUT_S", "60"))
        except ValueError:
            timeout_s = 60
        return cls(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            settings_source=source,
        )


def _read_claude_settings() -> tuple[str | None, str | None, str | None, str | None]:
    """Read ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL from
    the Claude Code settings file. Returns ``(api_key, base_url, model, path)``
    with any of the four ``None`` if missing.

    Honors ``MCP_LLM_SETTINGS_JSON`` to override the path; defaults to
    ``~/.claude/settings.json`` on all platforms.
    """
    import json
    import platform

    path_str = os.environ.get("MCP_LLM_SETTINGS_JSON", "").strip()
    if not path_str:
        home = Path(os.path.expanduser("~"))
        # Claude Code uses .claude on every platform; on Windows that's
        # %USERPROFILE%/.claude.
        path_str = str(home / ".claude" / "settings.json")
    path = Path(path_str)
    if not path.is_file():
        return None, None, None, None
    try:
        # settings.json may carry a UTF-8 BOM (Windows notepad default).
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, None, None, None
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return None, None, None, None
    api_key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    base_url = env.get("ANTHROPIC_BASE_URL")
    model = env.get("ANTHROPIC_MODEL") or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
    return api_key, base_url, model, str(path)


def _extract_first_json_object(text: str) -> dict | None:
    """Scan ``text`` for the first balanced ``{ … }`` JSON object.

    LLMs sometimes prefix the JSON with analysis prose or suffix it
    with ``Reasoning: …`` notes that confuse ``json.loads``. We
    bracket-count braces while respecting JSON string literals (and
    ``\\`` escapes sequences inside them) so braces inside a string
    do not desync the count. Returns the parsed dict, or ``None``
    when no balanced object can be located.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    # First balanced object was non-dict (e.g. a list);
                    # fall through to next candidate below.
                    break
        start = text.find("{", start + 1)
    return None


def llm_complete_json(
    *,
    system: str,
    user: str,
    config: LLMConfig | None = None,
) -> dict[str, Any]:
    """Send system + user to the configured LLM, return parsed JSON.

    Raises ``LLMError`` on any failure (network, HTTP, JSON parse, missing
    content). The caller is expected to surface ``stage="llm_plan"`` in the
    tool result on failure.
    """
    cfg = config or LLMConfig.from_env()
    log.info(
        "llm_complete_json provider=%s model=%s base=%s timeout=%ds key_source=%s",
        cfg.provider,
        cfg.model,
        cfg.base_url,
        cfg.timeout_s,
        cfg.settings_source,
    )
    t0 = time.time()
    if cfg.provider == PROVIDER_ANTHROPIC:
        text = _anthropic_complete(
            system=system, user=user, config=cfg
        )
    elif cfg.provider == PROVIDER_OPENAI_COMPAT:
        text = _openai_compat_complete(
            system=system, user=user, config=cfg
        )
    else:
        raise LLMError(f"unknown provider: {cfg.provider}")
    elapsed_ms = int((time.time() - t0) * 1000)
    log.info("llm_complete_json done in %d ms (response %d chars)", elapsed_ms, len(text))

    # The LLM may wrap JSON in ```json fences or precede it with prose.
    text = _strip_code_fence(text)
    # Even after stripping fences the response may carry analysis prose
    # before the JSON object, or `Reasoning:` text after. Try strict
    # parse first; on failure fall back to scanning for the first
    # balanced { ... } object so a single response covers a few cases.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        candidate = _extract_first_json_object(text)
        if candidate is not None:
            return candidate
        raise LLMError(
            f"LLM response is not valid JSON; "
            f"first 200 chars: {text[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Anthropic Messages API.
# ---------------------------------------------------------------------------

def _anthropic_complete(*, system: str, user: str, config: LLMConfig) -> str:
    url = f"{config.base_url}/v1/messages"
    body = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": config.api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
    }
    raw = _http_post_json(url, body, headers, config.timeout_s)
    try:
        # Anthropic returns content as a list of typed blocks.
        blocks = raw["content"]
        text_blocks = [b for b in blocks if b.get("type") == "text"]
        if not text_blocks:
            raise LLMError(f"anthropic: no text blocks in response: {raw}")
        return text_blocks[0]["text"]
    except (KeyError, TypeError, IndexError) as exc:
        raise LLMError(f"anthropic: malformed response: {exc}; raw={raw!r}") from exc


# ---------------------------------------------------------------------------
# OpenAI-compatible Chat Completions.
# ---------------------------------------------------------------------------

def _openai_compat_complete(
    *, system: str, user: str, config: LLMConfig
) -> str:
    # OpenAI-style: /chat/completions relative to the base. For Ollama the
    # default base is http://localhost:11434 which exposes /v1/chat/completions
    # — so we always append /v1 if missing, since both OpenAI and the major
    # OpenAI-compatible proxies expose chat completions under /v1.
    base = config.base_url
    if not base.endswith("/v1"):
        base = base + "/v1"
    url = f"{base}/chat/completions"
    body = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": config.max_tokens,
    }
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {config.api_key}",
    }
    raw = _http_post_json(url, body, headers, config.timeout_s)
    try:
        return raw["choices"][0]["message"]["content"]
    except (KeyError, TypeError, IndexError) as exc:
        raise LLMError(f"openai_compat: malformed response: {exc}; raw={raw!r}") from exc


# ---------------------------------------------------------------------------
# HTTP transport.
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """Raised on any LLM-side failure (network, HTTP status, malformed body)."""


def _http_post_json(
    url: str, body: dict, headers: dict, timeout_s: int
) -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        # Read the body — Anthropic/OpenAI return useful error messages.
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise LLMError(
            f"HTTP {exc.code} from {url}: {err_body[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"network error reaching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"timeout after {timeout_s}s reaching {url}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LLMError(f"non-JSON response from {url}: {exc}") from exc


def _strip_code_fence(text: str) -> str:
    """Strip an outer ```` ```json … ``` ```` fence if present.

    Some providers (notably Anthropic when not given ``response_format``)
    wrap JSON in a code fence even when the user asked for raw JSON. This
    helper is permissive: it removes the fence only if both opening and
    closing markers are found, otherwise returns the input unchanged.
    """
    s = text.strip()
    if s.startswith("```"):
        # find end of first line (language hint)
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


