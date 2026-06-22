"""LLM client (stdlib only — no extra deps).

Speaks the Anthropic Messages API natively (the product default) and falls back
to the OpenAI-compatible chat/completions shape when SF_LLM_BASE points at a
gateway. Same call_llm / call_llm_fast / parse_json_from_model interface the
rest of the harness expects.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from . import config


def _is_anthropic(base: str) -> bool:
    return "anthropic.com" in base


def _http_post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def call_llm(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    json_mode: bool = True,
    timeout: float | None = None,
    retries: int = 3,
) -> tuple[str, dict]:
    model = model or config.DISTILL_MODEL
    max_tokens = max_tokens or config.DISTILL_MAX_TOKENS
    timeout = timeout or config.LLM_TIMEOUT
    base = config.LLM_BASE.rstrip("/")

    if _is_anthropic(base):
        url = f"{base}/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": config.LLM_KEY,
            "anthropic-version": "2023-06-01",
        }
        # No temperature/thinking — Opus 4.x rejects sampling params, and we want
        # clean JSON out (parse_json_from_model handles any wrapping).
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }

        def _extract(d: dict) -> tuple[str, dict]:
            text = "".join(
                b.get("text", "") for b in d.get("content", []) if b.get("type") == "text"
            )
            return text.strip(), {"model": model, "usage": d.get("usage", {})}
    else:
        url = f"{base}/v1/chat/completions"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {config.LLM_KEY}",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        def _extract(d: dict) -> tuple[str, dict]:
            content = d["choices"][0]["message"]["content"]
            return content.strip(), {"model": model, "usage": d.get("usage", {})}

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            data = _http_post(url, headers, body, timeout)
            if isinstance(data, dict) and data.get("type") == "error":
                raise RuntimeError(f"LLM error: {data.get('error')}")
            return _extract(data)
        except urllib.error.HTTPError as e:  # surface API error body
            detail = e.read().decode("utf-8", "replace")[:300] if hasattr(e, "read") else str(e)
            last_err = RuntimeError(f"HTTP {e.code}: {detail}")
        except Exception as e:  # noqa: BLE001
            last_err = e
        wait = min(2**attempt, 8)
        print(f"[llm] attempt {attempt + 1}/{retries} failed: {last_err}; retry in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"LLM call failed after {retries} retries: {last_err}")


def call_llm_fast(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
) -> tuple[str, dict]:
    return call_llm(
        prompt,
        model=model or config.CLASSIFY_MODEL,
        max_tokens=max_tokens or config.CLASSIFY_MAX_TOKENS,
        json_mode=True,
        timeout=90,
        retries=3,
    )


def _escape_invalid_json_backslashes(text: str) -> str:
    valid_escapes = set('"\\/bfnrtu')
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt not in valid_escapes:
                out.append("\\\\")
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_json_from_model(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"```$", "", cleaned).strip()

    for s in (cleaned, _escape_invalid_json_backslashes(cleaned)):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        body = cleaned[start : end + 1]
        for s in (body, _escape_invalid_json_backslashes(body)):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Could not parse model response as JSON: {text[:200]}...")
