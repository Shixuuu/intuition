"""The Steward's default route: the host session's own model.

A fresh install configures no model. The Steward asks the host it runs inside
for a completion, and only falls back to a command or an endpoint when no host
answers (a cron tick, say).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from intuition import config, inbox, llm
from intuition.adapters.hermes import IntuitionProvider


@pytest.fixture(autouse=True)
def no_stray_host_model():
    """A transport installed by one test never leaks into the next."""
    llm.set_host_transport(None)
    yield
    llm.set_host_transport(None)


class StubHostModel:
    """Stands in for the Pi extension or the Hermes ``ctx.llm`` facade."""

    def __init__(self, reply: str = '{"ops": [{"op": "noop"}], "summary": "s"}',
                 error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[list[dict]] = []

    def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.reply, provider="stub", model="stub-1")


# -- routing --------------------------------------------------------------------------

def test_outside_a_host_nothing_answers_and_the_pass_is_deterministic(store):
    assert store.cfg["steward"]["llm_mode"] == "host"
    assert llm.route(store) == "none"
    assert llm.llm_configured(store) is False
    assert llm.call_json(store, "light", "system", "user") is None


def test_host_transport_is_the_default_route(store):
    seen = {}

    def transport(system: str, user: str) -> str:
        seen["call"] = (system, user)
        return 'prose {"ops": [{"op": "noop"}], "summary": "s"} trailing'

    llm.set_host_transport(transport, label="test/model")
    assert llm.route(store) == "host"
    assert llm.llm_configured(store) is True
    assert llm.call_json(store, "light", "SYSTEM", "USER") == {
        "ops": [{"op": "noop"}], "summary": "s"}
    assert seen["call"] == ("SYSTEM", "USER")
    assert llm.LAST_ROUTE == "host"
    assert "test/model" in llm.describe_route(store)


def test_a_host_failure_never_passes_as_a_deterministic_plan(store):
    def boom(system: str, user: str) -> str:
        raise llm.LLMError("the session has no model")

    llm.set_host_transport(boom, label="test/model")
    with pytest.raises(llm.LLMError):
        llm.call_json(store, "light", "system", "user")


def test_command_is_the_fallback_when_no_host_answers(store):
    config.set_value(store, "steward.llm_command_light", """printf '{"ops": []}'""")
    assert llm.route(store) == "command"
    assert llm.call_json(store, "light", "system", "user") == {"ops": []}
    assert llm.LAST_ROUTE == "command"


def test_an_installed_host_outranks_the_configured_command(store):
    config.set_value(store, "steward.llm_command_light", """printf '{"ops": []}'""")
    llm.set_host_transport(lambda system, user: '{"ops": [{"op": "noop"}]}',
                           label="test/model")
    assert llm.route(store) == "host"
    assert llm.call_json(store, "light", "s", "u") == {"ops": [{"op": "noop"}]}


def test_mode_command_uses_only_the_command(store):
    config.set_value(store, "steward.llm_mode", "command")
    config.set_value(store, "steward.llm_http.url", "http://127.0.0.1:9/v1/chat")
    llm.set_host_transport(lambda system, user: "{}", label="test/model")
    assert llm.route(store) == "none", "http is not the command route's business"
    config.set_value(store, "steward.llm_command_light", """printf '{"ops": []}'""")
    assert llm.route(store) == "command"


def test_mode_http_uses_only_the_endpoint(store):
    config.set_value(store, "steward.llm_mode", "http")
    config.set_value(store, "steward.llm_command_light", """printf '{"ops": []}'""")
    assert llm.route(store) == "none"
    config.set_value(store, "steward.llm_http.url", "http://127.0.0.1:8099/v1/chat")
    assert llm.route(store) == "http"
    # a configured route that fails fails the pass: it never reads as deterministic
    with pytest.raises(llm.LLMError):
        llm.call_json(store, "light", "s", "u")


def test_mode_none_keeps_every_pass_rule_derived(store):
    config.set_value(store, "steward.llm_mode", "none")
    config.set_value(store, "steward.llm_command_light", """printf '{"ops": []}'""")
    llm.set_host_transport(lambda system, user: "{}", label="test/model")
    assert llm.route(store) == "none"
    assert llm.llm_configured(store) is False


def test_mode_must_be_one_of_the_routes(store):
    with pytest.raises(config.ConfigError):
        config.set_value(store, "steward.llm_mode", "telepathy")
    assert config.set_value(store, "steward.llm_mode", "HOST") == "host"
    assert store.cfg["steward"]["llm_mode"] == "host"


# -- the pass reports what planned it ---------------------------------------------------

def test_a_host_planned_pass_says_so(store, index):
    from intuition.steward.tick import tick

    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")
    llm.set_host_transport(lambda system, user: '{"ops": [{"op": "noop"}], "summary": "s"}',
                           label="test/model")
    result = tick(store, index, session_end=True)
    assert result["pass"] == "light"
    assert result["model"] == "host"


def test_a_rule_planned_pass_says_deterministic(store, index):
    from intuition.steward.tick import tick

    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")
    result = tick(store, index, session_end=True)
    assert result["pass"] == "light"
    assert result["model"] == "deterministic"


def test_a_pass_with_nothing_to_do_reports_no_model(store, index):
    from intuition.steward.tick import tick

    config.set_value(store, "steward.deep_time", "23:59")   # keep the nightly pass out
    result = tick(store, index, session_end=True)
    assert result["pass"] == "none"
    assert "model" not in result


def test_the_store_remembers_what_planned_the_last_pass(store, index):
    from intuition.steward.tick import tick
    from intuition.tools import memory_status

    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")
    llm.set_host_transport(lambda system, user: '{"ops": [{"op": "noop"}]}',
                           label="test/model")
    tick(store, index, session_end=True)
    assert memory_status(store, index, {})["last_pass_model"] == "host"


# -- Hermes ------------------------------------------------------------------------

def test_hermes_provider_plans_on_the_active_hermes_model(store, index):
    host = StubHostModel('{"ops": [{"op": "noop"}], "summary": "s"}')
    provider = IntuitionProvider(host_llm=host)
    provider._store, provider._index = store, index
    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")

    provider._session_end_tick()

    assert host.calls, "the Steward should have asked the host for a plan"
    assert host.calls[0][0]["role"] == "system"
    assert llm.host_label() == "", "the transport is released after the pass"


def test_hermes_provider_without_a_host_model_still_plans(store, index):
    """An older host with no ctx.llm keeps working, rule-derived."""
    provider = IntuitionProvider()
    provider._store, provider._index = store, index
    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")

    provider._session_end_tick()

    assert llm.route(store) == "none"


def test_hermes_provider_carries_a_failing_host_without_raising(store, index):
    host = StubHostModel(error=RuntimeError("auth expired"))
    provider = IntuitionProvider(host_llm=host)
    provider._store, provider._index = store, index
    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")

    provider._session_end_tick()               # must not break the host's turn

    assert host.calls
    assert llm.host_label() == ""
    assert store.read_jsonl("inbox/pending.jsonl"), "the batch stays for the next pass"


class CollectorContext:
    """Hermes' memory-provider context: it captures register_memory_provider, builds
    a real plugin context on demand for the rest, and raises on anything else."""

    def __init__(self, llm=None):
        self.provider = None
        self._llm = llm

    def register_memory_provider(self, provider):
        self.provider = provider

    def _plugin_context(self):
        return SimpleNamespace(llm=self._llm)

    def __getattr__(self, name):
        if not name.startswith("register_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


def test_hermes_register_finds_the_model_behind_the_collector(store, index):
    """Registering is how the host activates us; the model has to come out of it."""
    from intuition.adapters.hermes import register

    host = StubHostModel('{"ops": [{"op": "noop"}], "summary": "s"}')
    ctx = CollectorContext(llm=host)
    register(ctx)

    provider = ctx.provider
    assert provider is not None, "the host's registry must receive the provider"
    assert provider._host_model() is host, "the collector's plugin context has the facade"
    provider._store, provider._index = store, index
    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")

    provider._session_end_tick()

    assert host.calls, "the Steward should plan on the host's model"


def test_hermes_register_takes_the_facade_a_plain_context_offers(store):
    from intuition.adapters.hermes import register

    host = StubHostModel()
    ctx = SimpleNamespace(llm=host, register_memory_provider=lambda p: setattr(ctx, "provider", p))
    register(ctx)

    assert ctx.provider._host_model() is host


def test_hermes_register_survives_a_context_with_no_model(store, index):
    """A host that offers no model keeps the configured route, not an error."""
    from intuition.adapters.hermes import register

    ctx = CollectorContext(llm=None)
    register(ctx)

    provider = ctx.provider
    assert provider._host_model() is None
    provider._store, provider._index = store, index
    inbox.append(store, kind="fact", text="june wants the agenda early",
                 source="user", evidence="june wants the agenda early")

    provider._session_end_tick()

    assert llm.route(store) == "none"
