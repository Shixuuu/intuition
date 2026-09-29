"""Every user-facing setting, in one table.

A setting is a dotted key with a type, a default, and a sentence of help. The
file written by ``intuition init`` is generated from this table, ``Store.section``
reads through it, and the CLI and the Pi ``/intuition`` command both write through
``set_value``. One table means a key cannot mean one thing in the file and another
in the code, and a command surface can list exactly the knobs that do something.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "intuition.toml"


class ConfigError(ValueError):
    """A key that does not exist, or a value the key cannot take."""


@dataclass(frozen=True)
class Setting:
    kind: type
    default: object
    help: str
    choices: tuple[str, ...] = ()


def _setting(kind, default, help_text, choices=()) -> Setting:
    return Setting(kind, default, help_text, choices)


SETTINGS: dict[str, Setting] = {
    # Read path
    "search.max_records": _setting(
        int, 4, "records injected per search turn"),
    "search.max_chars": _setting(
        int, 2400, "character budget for one search turn"),
    "search.hop_decay": _setting(
        float, 0.3, "score multiplier for a record reached through a link"),
    "search.pending_days": _setting(
        int, 2, "days an unconfirmed proposal stays searchable"),
    # Prompt blocks
    "profile.pinned_max_chars": _setting(
        int, 4000, "cap on the hand-written PROFILE.md block"),
    "profile.generated_max_chars": _setting(
        int, 12000, "cap on the generated ONEPAGER.md block"),
    "profile.index_lines": _setting(
        int, 40, "index lines appended to the stable prefix"),
    "observe.prefix_max_chars": _setting(
        int, 16000, "cap on the observation tail in the prefix"),
    # Steward planning and triggers
    "steward.tick_minutes": _setting(
        int, 15, "minutes between scheduled ticks"),
    "steward.light_inbox_items": _setting(
        int, 10, "pending proposals that force a light pass"),
    "steward.light_raw_chars": _setting(
        int, 80000, "unobserved raw characters that force a light pass"),
    "steward.pending_age_minutes": _setting(
        int, 20, "age of the oldest proposal that forces a light pass"),
    "steward.idle_minutes": _setting(
        int, 30, "quiet minutes that force a light pass"),
    "steward.deep_time": _setting(
        str, "03:00", "local time after which the nightly deep pass runs"),
    "steward.max_plan_ops": _setting(
        int, 60, "operations one plan may carry"),
    "steward.llm_command_light": _setting(
        str, "", "shell command for the light planner, {prompt} substituted"),
    "steward.llm_command_deep": _setting(
        str, "", "shell command for the deep planner"),
    "steward.llm_http.url": _setting(
        str, "", "HTTP endpoint for the planner when no command is set"),
    "steward.llm_http.model": _setting(
        str, "", "model name sent to that endpoint"),
    "steward.llm_http.key_env": _setting(
        str, "", "environment variable holding the endpoint's key"),
    # Observation and retention
    "observe.observer_raw_chars": _setting(
        int, 80000, "unread raw characters that trigger the observer"),
    "observe.reflector_log_chars": _setting(
        int, 48000, "observation log size that triggers condensing"),
    "retention.raw_days": _setting(
        int, 30, "days raw capture is kept once the observer read it"),
    "retention.tasks_days": _setting(
        int, 14, "days a finished task directory is kept"),
    "retention.archive_months": _setting(
        int, 12, "months of archived inbox items to keep"),
    # Safety
    "safety.secure_enabled": _setting(
        bool, False, "expose the secure scope to the main assistant"),
    "safety.profile_min_confidence": _setting(
        float, 0.8, "confidence an inferred fact needs to enter the ONEPAGER"),
}

SECTIONS = tuple(dict.fromkeys(key.split(".")[0] for key in SETTINGS))


def default_of(key: str):
    _require(key)
    return SETTINGS[key].default


def _require(key: str) -> Setting:
    if key not in SETTINGS:
        known = ", ".join(sorted(SETTINGS))
        raise ConfigError(f"unknown setting {key!r}; known settings: {known}")
    return SETTINGS[key]


def coerce(key: str, value):
    """The stored form of *value* for *key*, or raise ``ConfigError``."""
    setting = _require(key)
    if setting.kind is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().casefold()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
        raise ConfigError(f"{key} takes true or false, got {value!r}")
    if setting.kind is int:
        if isinstance(value, bool):
            raise ConfigError(f"{key} takes a whole number")
        try:
            return int(str(value).strip())
        except ValueError as exc:
            raise ConfigError(f"{key} takes a whole number, got {value!r}") from exc
    if setting.kind is float:
        if isinstance(value, bool):
            raise ConfigError(f"{key} takes a number")
        try:
            return float(str(value).strip())
        except ValueError as exc:
            raise ConfigError(f"{key} takes a number, got {value!r}") from exc
    text = str(value)
    if setting.choices and text.strip().casefold() not in setting.choices:
        raise ConfigError(f"{key} takes one of {', '.join(setting.choices)}")
    return text


def _walk(cfg: dict, key: str) -> tuple[dict, str] | None:
    """The parent table and leaf name for *key*, or None when it is not set."""
    parts = key.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return None
    if parts[-1] not in node:
        return None
    return node, parts[-1]


def get_value(cfg: dict, key: str):
    """The configured value of *key*, or the schema default when unset."""
    found = _walk(cfg, key)
    return found[0][found[1]] if found else default_of(key)


def is_set(cfg: dict, key: str) -> bool:
    return _walk(cfg, key) is not None


def read(path: str | Path) -> dict:
    """Parse a config file. A missing or unreadable file reads as empty."""
    p = Path(path)
    if not p.exists():
        return {}
    loaded = tomllib.loads(p.read_text())
    return loaded if isinstance(loaded, dict) else {}


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def dump(cfg: dict) -> str:
    """Render a config table as TOML text.

    Handles the shape this project uses: sections of scalars, with nested tables
    one level deep (``[steward.llm_http]``). Unknown keys are rendered as they
    were read, so writing one setting never drops another.
    """
    blocks: list[str] = []
    for section, values in cfg.items():
        if not isinstance(values, dict):
            continue
        scalars = {k: v for k, v in values.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in values.items() if isinstance(v, dict)}
        if scalars:
            blocks.append("\n".join(
                [f"[{section}]"] + [f"{k} = {_literal(v)}" for k, v in scalars.items()]))
        for name, sub in nested.items():
            blocks.append("\n".join(
                [f"[{section}.{name}]"] + [f"{k} = {_literal(v)}" for k, v in sub.items()]))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def write(path: str | Path, cfg: dict) -> Path:
    """Write the config atomically (temp file, fsync, rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dump(cfg))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def set_value(store, key: str, value):
    """Validate, persist, and apply a setting to *store*. Returns the value.

    The file is re-read first, so a change made while another process held the
    store cannot drop that process's edits, and the store's in-memory config is
    refreshed after, so a running session sees the value without a restart.
    """
    coerced = coerce(key, value)
    merged = read(store.dir(CONFIG_NAME))
    parts = key.split(".")
    node = merged
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = coerced
    write(store.dir(CONFIG_NAME), merged)
    store.cfg = read(store.dir(CONFIG_NAME))
    return coerced


def describe(store) -> list[dict]:
    """Every setting with its effective value, so a command surface can list it."""
    rows = []
    for key, setting in SETTINGS.items():
        value = get_value(store.cfg, key)
        rows.append({"key": key, "value": value, "default": setting.default,
                     "set": is_set(store.cfg, key), "overridden": value != setting.default,
                     "help": setting.help, "kind": setting.kind.__name__})
    return rows


def default_config_text() -> str:
    """The file ``intuition init`` writes, generated from the schema."""
    sections: dict[str, dict] = {}
    for key, setting in SETTINGS.items():
        parts = key.split(".")
        node = sections
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = setting.default
    return dump(sections)


def settings_help() -> str:
    """One line per setting, grouped by section, for `config show` and /intuition."""
    lines: list[str] = []
    for section in SECTIONS:
        lines.append(f"[{section}]")
        for key, setting in SETTINGS.items():
            if key.split(".")[0] != section:
                continue
            name = key[len(section) + 1:]
            lines.append(f"  {name} = {_literal(setting.default)}   # {setting.help}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
