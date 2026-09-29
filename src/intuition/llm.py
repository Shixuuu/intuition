"""Model calls for the Steward: command or HTTP, JSON-checked, one retry (plan §6.5).

Config (intuition.toml [steward]):
    llm_command_light = "pi -p --no-extensions '{prompt}'"
    llm_command_deep  = "hermes chat -q '{prompt}'"
or an HTTP endpoint:
    [steward.llm_http]
    url = "https://api.example.com/v1/chat/completions"
    model = "…"
    key_env = "MY_API_KEY"

If neither is configured the Steward runs in deterministic mode (no model):
only rule-derived ops are planned, which keeps the daily driver working.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request


class LLMError(RuntimeError):
    pass


def _extract_json(text: str):
    """First balanced JSON object/array found anywhere in the reply."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
            return obj
        except json.JSONDecodeError:
            continue
    raise LLMError("no JSON object in model reply")


def call_command(spec: str, system: str, user: str) -> str:
    """Run a CLI in print mode. '{prompt}' is replaced; else prompt goes to stdin."""
    prompt = f"{system}\n\n{user}"
    if "{prompt}" in spec:
        cmd = spec.replace("{prompt}", prompt)
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
    else:
        r = subprocess.run(spec, shell=True, input=prompt,
                           capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise LLMError(f"llm command failed ({r.returncode}): {r.stderr.strip()[:400]}")
    return r.stdout


def call_http(spec: dict, system: str, user: str) -> str:
    url = spec["url"]
    key = os.environ.get(spec.get("key_env", ""), "")
    payload = {
        "model": spec.get("model", ""),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    return body["choices"][0]["message"]["content"]


def llm_configured(store) -> bool:
    """True when the Steward has a model to plan with.

    An empty ``[steward.llm_http]`` table does not count, so writing that table at
    init cannot silently switch a fresh store into model mode.
    """
    cfg = store.cfg.get("steward", {})
    return bool(cfg.get("llm_command_light") or cfg.get("llm_command_deep")
                or (cfg.get("llm_http") or {}).get("url"))


def call_json(store, which: str, system: str, user: str):
    """Call the configured model for 'light' or 'deep'; return parsed JSON.
    One retry on invalid JSON; then the caller keeps the batch unprocessed."""
    cfg = store.cfg.get("steward", {})
    cmd = cfg.get(f"llm_command_{which}") or ""
    http = cfg.get("llm_http") or {}
    if not cmd and not (http.get("url")):
        return None                      # deterministic mode
    last_err: Exception | None = None
    for _ in range(2):                   # one retry (plan §6.5)
        try:
            if cmd:
                raw = call_command(cmd, system, user)
            else:
                raw = call_http(http, system, user)
            return _extract_json(raw)
        except (LLMError, KeyError, subprocess.SubprocessError,
                urllib.error.URLError, json.JSONDecodeError) as e:
            last_err = e
    raise LLMError(f"model call failed twice: {last_err}")
