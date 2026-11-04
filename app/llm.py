"""Provider-agnostic chat completions over plain HTTP (OpenAI-compatible APIs)."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger("slipstream.llm")

PRESETS = {
    "vultr": {
        "label": "Vultr Serverless Inference",
        "short": "Vultr inference",
        "base": "https://api.vultrinference.com/v1",
        "key_env": "VULTR_INFERENCE_API_KEY",
        "model_env": "VULTR_INFERENCE_MODEL",
        "default_model": "deepseek-v4.1-flash-1017",
        "token_field": "max_tokens",
        "user_agent": None,
    },
    "groq": {
        "label": "Groq",
        "short": "Groq",
        "base": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model_env": None,
        "default_model": "openai/gpt-oss-120b",
        "token_field": "max_completion_tokens",
        # Groq's edge rejects urllib's default User-Agent (Cloudflare error 1010).
        "user_agent": "slipstream/0.1",
    },
}
DEFAULT_EFFORT = {"plan": "low", "code": "medium"}


class LLMError(Exception):
    def __init__(
        self, message: str, *, finish_reason: str | None = None, status: int | None = None
    ):
        super().__init__(message)
        self.finish_reason = finish_reason
        self.status = status


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    finish_reason: str | None
    usage: dict


def settings() -> dict | None:
    """Resolve provider and model from the environment; None when nothing is configured."""
    require_vultr = os.getenv("REQUIRE_VULTR", "").strip().lower() == "true"
    name = os.getenv("LLM_PROVIDER", "").strip().lower()

    if require_vultr:
        if name not in ("", "vultr"):
            raise LLMError(
                f"REQUIRE_VULTR=true refuses LLM provider {name!r}; only vultr is allowed."
            )
        if not os.getenv("VULTR_INFERENCE_API_KEY"):
            raise LLMError("REQUIRE_VULTR=true but VULTR_INFERENCE_API_KEY is not set.")
        name = "vultr"
    elif not name:
        if os.getenv("VULTR_INFERENCE_API_KEY"):
            name = "vultr"
        elif os.getenv("GROQ_API_KEY"):
            name = "groq"
    if not name:
        return None
    if name not in PRESETS:
        raise LLMError(f"Unknown LLM_PROVIDER {name!r}; use vultr or groq.")

    preset = PRESETS[name]
    model = (
        os.getenv("LLM_MODEL", "").strip()
        or (os.getenv(preset["model_env"], "").strip() if preset["model_env"] else "")
        or preset["default_model"]
    )
    return preset | {
        "provider": name,
        "model": model,
        "configured": bool(os.getenv(preset["key_env"])),
        "require_vultr": require_vultr,
    }


def status() -> dict:
    """Non-raising view of the configuration for health checks. Never includes key values."""
    require_vultr = os.getenv("REQUIRE_VULTR", "").strip().lower() == "true"
    try:
        cfg = settings()
    except LLMError as exc:
        return {
            "provider": None,
            "model": None,
            "configured": False,
            "require_vultr": require_vultr,
            "error": str(exc),
        }
    return {
        "provider": cfg and cfg["provider"],
        "model": cfg and cfg["model"],
        "configured": bool(cfg and cfg["configured"]),
        "require_vultr": require_vultr,
        "error": None,
    }


def chat(
    messages, *, purpose, max_tokens=None, temperature=None, timeout=None, reasoning_effort=None
) -> LLMResult:
    """Send one chat completion. Only choices[0].message.content is used; reasoning is ignored."""
    cfg = settings()
    if cfg is None:
        raise LLMError("No LLM provider is configured.")
    key = os.getenv(cfg["key_env"])
    if not key:
        raise LLMError(f"{cfg['key_env']} is not set for LLM provider {cfg['provider']}.")

    payload = {"model": cfg["model"]}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload[cfg["token_field"]] = max_tokens
    if cfg["provider"] == "groq" and cfg["model"].startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = (
            reasoning_effort
            or os.getenv("LLM_REASONING_EFFORT", "").strip()
            or DEFAULT_EFFORT.get(purpose, "low")
        )
    payload["messages"] = messages

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if cfg["user_agent"]:
        headers["User-Agent"] = cfg["user_agent"]
    request = urllib.request.Request(
        f"{cfg['base']}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=timeout or 12) as resp:
                body = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            # Do not log request headers or the API key.
            if attempt == 2 or not (exc.code == 429 or exc.code >= 500):
                raise LLMError(f"HTTP {exc.code} {exc.reason}", status=exc.code) from None
            delay = _retry_delay(exc.headers)
            logger.warning(
                "%s returned HTTP %s; retrying once in %.1fs.", cfg["label"], exc.code, delay
            )
            time.sleep(delay)
        except (OSError, ValueError) as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}"[:250]) from None

    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices:
        raise LLMError(f"{cfg['label']} response contained no choices.")
    finish_reason = choices[0].get("finish_reason")
    text = (choices[0].get("message") or {}).get("content")

    if not text:
        if finish_reason == "length":
            raise LLMError(
                "Model returned no content: token budget exhausted by reasoning "
                "(finish_reason=length).",
                finish_reason=finish_reason,
            )
        raise LLMError(
            f"Model returned empty content (finish_reason={finish_reason}).",
            finish_reason=finish_reason,
        )
    if not isinstance(text, str):
        raise LLMError(f"Unexpected content type: {type(text).__name__}")

    usage = body.get("usage") or {}
    usage = {
        k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in usage
    }
    logger.info(
        "%s chat succeeded | purpose=%s | model=%s | finish_reason=%s | usage=%s",
        cfg["label"],
        purpose,
        cfg["model"],
        finish_reason,
        usage,
    )
    return LLMResult(text, cfg["provider"], cfg["model"], finish_reason, usage)


def _retry_delay(headers) -> float:
    try:
        return max(0.0, min(5.0, float(headers.get("Retry-After"))))
    except (AttributeError, TypeError, ValueError):
        return 1.0
