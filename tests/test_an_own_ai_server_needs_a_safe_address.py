"""A model on the company's own server is reached at an address that is safe.

SUPPLECO uses only a model on its own server (Qwen served by Ollama).
LLM_PROVIDER=openai_compatible reads LLM_BASE_URL and LLM_MODEL for that.
Every prompt that carries a recipient's data goes to that address, so it must
stay on this machine or the private network: every address its host resolves
to must be loopback or private (10/8, 172.16/12, 192.168/16, fc00::/7, ::1).
A public, link-local, multicast or unspecified address, an address with a
password in it, or one the server would not read as the API's root, stops
start-up with a plain message that never quotes the address. A name that
cannot be looked up does not stop start-up (the agent must still read
opt-outs), but doctor and every AI call refuse it until it resolves. None of
this is read when LLM_PROVIDER is blank: the AI is off.
"""

import ipaddress

import pytest
from pydantic import ValidationError

import config

# What the stand-in resolver answers: no test asks real DNS.
NAMES = {
    "ai-server": ["192.168.1.20"],
    "ai.internal": ["10.1.2.3", "fd12:3456::7"],
    "gpu-box": ["172.20.0.4"],
    "example.com": ["93.184.215.14"],
    "half-inside": ["10.0.0.9", "93.184.215.14"],
    "metadata.cloud": ["169.254.169.254"],
}


@pytest.fixture(autouse=True)
def _resolver(monkeypatch):
    def resolve(host):
        if host not in NAMES:
            raise OSError(f"no such name: {host}")
        return [ipaddress.ip_address(a) for a in NAMES[host]]

    monkeypatch.setattr(config, "resolve_host", resolve)


def _own(settings_factory, **overrides):
    overrides.setdefault("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    overrides.setdefault("LLM_MODEL", "qwen2.5:14b")
    return settings_factory(llm_provider="openai_compatible", **overrides)


@pytest.mark.parametrize(
    "address",
    [
        "http://127.0.0.1:11434/v1",
        "http://127.8.9.10:11434/v1",
        "http://192.168.1.20:11434/v1",
        "http://10.0.0.5/v1",
        "http://172.16.0.1/v1",
        "http://172.31.255.254/v1",
        "http://[::1]:11434/v1",
        "http://[fd00::1]:11434/v1",
        "http://[::ffff:10.0.0.1]/v1",
        "http://ai-server/v1",
        "http://gpu-box:11434/v1",
        "https://ai.internal/v1",
        "http://2130706433:11434/v1",
        "http://0xc0a80114/v1",
        "http://127.1/v1",
    ],
)
def test_an_address_on_this_machine_or_the_private_network_is_accepted(
    settings_factory, address
):
    assert _own(settings_factory, LLM_BASE_URL=address).llm_base_url == address


def test_localhost_is_accepted_as_the_system_resolves_it(settings_factory, monkeypatch):
    monkeypatch.undo()
    assert _own(settings_factory, LLM_BASE_URL="http://localhost:11434/v1")


@pytest.mark.parametrize(
    "address,reason",
    [
        ("http://example.com/v1", "resolves to a public address"),
        ("https://example.com/v1", "resolves to a public address"),
        ("http://8.8.8.8/v1", "resolves to a public address"),
        ("https://8.8.8.8/v1", "resolves to a public address"),
        ("http://172.32.0.1/v1", "resolves to a public address"),
        ("http://100.64.0.1/v1", "resolves to a public address"),
        # Ranges ipaddress counts as private that are not SUPPLECO's network.
        ("http://192.0.2.1/v1", "resolves to a public address"),
        ("http://198.18.0.1/v1", "resolves to a public address"),
        ("http://240.0.0.1/v1", "resolves to a public address"),
        ("http://[2001:4860:4860::8888]/v1", "resolves to a public address"),
        # Every address counts: one outside is enough to refuse.
        ("http://half-inside/v1", "resolves to a public address"),
        ("http://169.254.169.254/v1", "a link-local address"),
        ("http://metadata.cloud/v1", "a link-local address"),
        ("http://[fe80::1]/v1", "a link-local address"),
        ("http://224.0.0.1/v1", "a multicast address"),
        ("http://[ff02::1]/v1", "a multicast address"),
        ("http://0.0.0.0:11434/v1", "an unspecified address"),
        ("http://[::]:11434/v1", "an unspecified address"),
        # 8.8.8.8 written as one number, in hex, in octal and shortened: the
        # resolver reads each as that IP, with no DNS asked.
        ("http://134744072:11434/v1", "resolves to a public address"),
        ("http://0x08080808:11434/v1", "resolves to a public address"),
        ("http://01002004010/v1", "resolves to a public address"),
        ("http://8.526344/v1", "resolves to a public address"),
        ("http://99999999999/v1", "no address fits"),
        ("ftp://x/v1", "http:// or https://"),
        ("127.0.0.1:11434/v1", "http:// or https://"),
        ("http://user:pw@127.0.0.1/v1", "user name or password"),
        ("https://user@ai.internal/v1", "user name or password"),
        ("http://127.0.0.1:11434/v1?key=1", "query"),
        ("http://127.0.0.1:11434/v1#top", "fragment"),
        ("http:///v1", "names no host"),
        # The whole endpoint pasted: every call went to .../chat/completions
        # twice over, answered 404, and fell back without a word.
        (
            "http://127.0.0.1:11434/v1/chat/completions",
            "cut /chat/completions off",
        ),
        ("http://127.0.0.1:11434/v1/Chat/Completions/", "cut /chat/completions off"),
        ("http://127.0.0.1:port/v1", "port"),
        ("", "is required"),
        ("   ", "is required"),
    ],
)
def test_an_unsafe_or_unusable_address_stops_start_up(
    settings_factory, address, reason
):
    with pytest.raises(ValidationError, match="LLM_BASE_URL") as refused:
        _own(settings_factory, LLM_BASE_URL=address)
    assert reason in str(refused.value)


def test_a_name_that_cannot_be_looked_up_does_not_stop_start_up(settings_factory):
    """The AI machine is switched off, or the name server is slow after a
    restart: LLM_BASE_URL's name does not resolve. Before, the settings
    refused to load, so the agent did not start and no opt-out or bounce was
    read until someone restarted it, all for an optional AI. Now it starts."""
    address = "http://no-such-host:11434/v1"
    assert _own(settings_factory, LLM_BASE_URL=address).llm_base_url == address


def test_a_name_that_cannot_be_looked_up_is_still_refused_everywhere_else():
    """Doctor and every AI call ask without the allowance: until the name
    resolves to this machine or the private network, nothing is sent."""
    assert "does not resolve" in config.own_server_host_problem("no-such-host")
    assert "does not resolve" in config.own_server_address_problem(
        "http://no-such-host:11434/v1"
    )


def test_the_allowance_covers_only_a_name_that_cannot_be_looked_up():
    assert "a public address" in config.own_server_host_problem(
        "example.com", unresolved_ok=True
    )
    assert "a public address" in config.own_server_host_problem(
        "half-inside", unresolved_ok=True
    )


@pytest.mark.parametrize(
    "host", ["example.com", "8.8.8.8", "134744072", "169.254.169.254", "half-inside"]
)
def test_the_refusal_of_an_outside_address_does_not_quote_it(settings_factory, host):
    with pytest.raises(ValidationError) as refused:
        _own(settings_factory, LLM_BASE_URL=f"http://{host}:11434/v1")
    assert host not in str(refused.value)
    assert host not in config.config_error_text(refused.value)


@pytest.mark.parametrize(
    "address",
    [
        "http://admin:hunter2pw@192.168.1.5:11434/v1",
        "https://hunter2pw@example.com/v1",
    ],
)
def test_the_refusal_of_a_password_does_not_print_it(settings_factory, address):
    """The refusal is printed at start-up, into the Docker log or the task's
    output, and pydantic quoted the address in it, password and all."""
    with pytest.raises(ValidationError, match="user name or password") as refused:
        _own(settings_factory, LLM_BASE_URL=address)
    assert "hunter2pw" not in str(refused.value)
    assert "hunter2pw" not in repr(refused.value.errors())


@pytest.mark.parametrize("slashes", ["/", "//", "///"])
def test_trailing_slashes_are_dropped(settings_factory, slashes):
    """All of them, as the generator does: dropping one left doctor asking
    /v1//models, a false 404 for a setup the agent used without trouble."""
    s = _own(settings_factory, LLM_BASE_URL=f"http://127.0.0.1:11434/v1{slashes}")
    assert s.llm_base_url == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize("model", ["", "   "])
def test_a_blank_model_stops_start_up(settings_factory, model):
    with pytest.raises(ValidationError, match="LLM_MODEL is required"):
        _own(settings_factory, LLM_MODEL=model)


@pytest.mark.parametrize("seconds", [5, 30, 300])
def test_a_timeout_from_5_to_300_seconds_is_accepted(settings_factory, seconds):
    assert _own(settings_factory, LLM_TIMEOUT_SECONDS=seconds).llm_timeout_seconds == (
        seconds
    )


@pytest.mark.parametrize("seconds", [4, 301, 0])
def test_a_timeout_outside_5_to_300_seconds_stops_start_up(settings_factory, seconds):
    with pytest.raises(ValidationError, match="LLM_TIMEOUT_SECONDS"):
        _own(settings_factory, LLM_TIMEOUT_SECONDS=seconds)


def test_the_timeout_defaults_to_the_30_seconds_every_call_waited(settings_factory):
    assert _own(settings_factory).llm_timeout_seconds == 30


def test_the_reasoning_effort_defaults_to_none(settings_factory):
    """With reasoning on, a one-word question to Qwen 3.5 on Ollama got no
    answer in three minutes; with none, it took a second."""
    assert _own(settings_factory).llm_reasoning_effort == "none"


@pytest.mark.parametrize(
    "effort,kept",
    [("none", "none"), ("low", "low"), ("Medium", "medium"), (" HIGH ", "high")]
    + [("", ""), ("  ", "")],
)
def test_a_known_reasoning_effort_or_blank_is_accepted(settings_factory, effort, kept):
    s = _own(settings_factory, LLM_REASONING_EFFORT=effort)
    assert s.llm_reasoning_effort == kept


@pytest.mark.parametrize("effort", ["off", "minimal", "false", "0"])
def test_any_other_reasoning_effort_stops_start_up_naming_the_values(
    settings_factory, effort
):
    with pytest.raises(ValidationError) as refused:
        _own(settings_factory, LLM_REASONING_EFFORT=effort)
    assert (
        "LLM_REASONING_EFFORT must be none, low, medium, high or blank"
    ) in str(refused.value)
    assert repr(effort) not in str(refused.value)


def test_the_reasoning_effort_is_not_checked_without_the_ai(settings_factory):
    s = settings_factory(llm_provider="", LLM_REASONING_EFFORT="off")
    assert s.llm_provider == ""


@pytest.mark.parametrize("provider", ["openai", "ollama", "local", "gemini", "groq"])
def test_an_unknown_provider_names_the_one_it_could_be(settings_factory, provider):
    with pytest.raises(
        ValidationError,
        match="LLM_PROVIDER must be openai_compatible",
    ):
        settings_factory(llm_provider=provider)


def test_without_the_ai_start_up_ignores_the_own_server_settings(settings_factory):
    s = settings_factory(
        llm_provider="",
        LLM_BASE_URL="http://user:pw@example.com/v1?x=1",
        LLM_MODEL="",
        LLM_TIMEOUT_SECONDS=1,
    )
    assert s.llm_provider == ""


def test_the_key_is_kept_out_of_the_settings_repr(settings_factory):
    s = _own(settings_factory, LLM_API_KEY="sk-own-server-secret")
    assert "sk-own-server-secret" not in repr(s)
