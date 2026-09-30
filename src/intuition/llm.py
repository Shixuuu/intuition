"""Model calls for the Steward.

By default (``steward.llm_mode = "host"``) the Steward asks the harness it runs
inside to plan on the session's active model, so a new install needs no model
configuration at all. The Pi extension answers those requests on the RPC channel
with the model the session is using; the Hermes memory provider answers them with
``ctx.llm``. Both hosts fall back to the routes below when they have no model to
offer, and a cron tick that runs outside a host uses them directly:

    [steward]
    llm_mode = "command"
    llm_command_light = "pi -p --no-extensions '{prompt}'"
    llm_command_deep  = "hermes chat -q '{prompt}'"
or an HTTP endpoint:
    [steward.llm_http]
    url = "https://api.example.com/v1/chat/completions"
    model = "…"
    key_env = "MY_API_KEY"

``llm_mode = "none"`` keeps every pass rule-derived, which is what happens on its
own when no route answers.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable

from . import config


class LLMError(RuntimeError):
    pass


# Per-context, so a pass in one host session never answers through another
# session's model when the two share a process (two Hermes profiles, say).
_HOST_TRANSPORT: contextvars.ContextVar[Callable[[str, str], str] | None] = \
    contextvars.ContextVar("intuition_host_transport", default=None)
_HOST_LABEL = ""

# The route that produced the most recent plan, for the pass report.
LAST_ROUTE = ""


def set_host_transport(call: Callable[[str, str], str] | None, *, label: str = "") -> None:
    """Install the harness's model call for this context: ``call(system, user)``
    returns the reply text or raises ``LLMError``. *label* names it in reports."""
    global _HOST_LABEL
    _HOST_TRANSPORT.set(call)
    _HOST_LABEL = label


def clear_host_transport() -> None:
    set_host_transport(None)


def host_transport() -> Callable[[str, str], str] | None:
    return _HOST_TRANSPORT.get()


def host_label() -> str:
    return _HOST_LABEL


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


def _mode(store) -> str:
    return str(config.get_value(store.cfg, "steward.llm_mode")).strip().casefold()


def _command(store, which: str) -> str:
    return str(store.cfg.get("steward", {}).get(f"llm_command_{which}") or "")


def _http(store) -> dict:
    spec = store.cfg.get("steward", {}).get("llm_http") or {}
    return spec if spec.get("url") else {}


def route(store, which: str = "light") -> str:
    """The route a plan takes right now: host, command, http, or none."""
    mode = _mode(store)
    if mode == "none":
        return "none"
    if mode == "command":
        return "command" if _command(store, which) else "none"
    if mode == "http":
        return "http" if _http(store) else "none"
    if host_transport() is not None:
        return "host"
    if _command(store, which):
        return "command"
    return "http" if _http(store) else "none"


def describe_route(store, which: str = "light") -> str:
    """Where the planner's model comes from, in one line, for doctor and /intuition."""
    kind = route(store, which)
    if kind == "host":
        return f"inherited from the host session ({_HOST_LABEL})" if _HOST_LABEL \
            else "inherited from the host session"
    if kind == "command":
        return f"shell command: {_command(store, which)}"
    if kind == "http":
        return f"HTTP endpoint: {_http(store).get('url')}"
    return "none → deterministic mode (no model)"


def llm_configured(store) -> bool:
    """True when the Steward has a model to plan with, by any route."""
    return route(store) != "none"


def call_json(store, which: str, system: str, user: str):
    """Call the routed model for 'light' or 'deep'; return parsed JSON.
    One retry on invalid JSON; then the caller keeps the batch unprocessed."""
    global LAST_ROUTE
    kind = route(store, which)
    if kind == "none":
        return None                      # deterministic mode
    last_err: Exception | None = None
    for _ in range(2):                   # one retry
        try:
            if kind == "host":
                raw = host_transport()(system, user)
            elif kind == "command":
                raw = call_command(_command(store, which), system, user)
            else:
                raw = call_http(_http(store), system, user)
            LAST_ROUTE = kind
            return _extract_json(raw)
        except (LLMError, KeyError, subprocess.SubprocessError,
                urllib.error.URLError, json.JSONDecodeError) as e:
            last_err = e
    raise LLMError(f"model call failed twice: {last_err}")
