from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from pood_thai.brain import AgentSdkBrain, build_options
from pood_thai.models import BrainError


def _result(text="ok", is_error=False, subtype="success"):
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="s", result=text,
    )


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options, results=None, fail_connect=False):
        self.options = options
        self.results = list(results or [])
        self.fail_connect = fail_connect
        self.queries: list[str] = []
        self.connected = False
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self):
        if self.fail_connect:
            raise RuntimeError("not logged in")
        self.connected = True

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        yield "ignored non-result message"
        yield self.results.pop(0)

    async def disconnect(self):
        self.disconnected = True


def factory(results=None, fail_connect=False):
    def make(options):
        return FakeClient(options, results=results, fail_connect=fail_connect)
    return make


def test_build_options_isolates_from_user_settings(tmp_path):
    opts = build_options("SYS", "sonnet", tmp_path)
    assert opts.system_prompt == "SYS"
    assert opts.model == "sonnet"
    assert opts.tools == []
    assert opts.setting_sources == []
    assert opts.skills == []
    assert opts.max_turns == 1
    assert opts.effort == "low"
    assert opts.thinking == {"type": "disabled"}
    assert opts.cwd == str(tmp_path)
    # claude.ai connectors (Gmail, Drive...) must not reach the persona, and nothing is saved to ~/.claude.
    assert opts.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert opts.strict_mcp_config is True
    assert "no-session-persistence" in opts.extra_args


async def test_chat_send_returns_result_text(tmp_path):
    brain = AgentSdkBrain("sonnet", tmp_path, client_factory=factory([_result("A"), _result("B")]))
    chat = await brain.open_chat("SYS")
    assert await chat.send("hi") == "A"
    assert await chat.send("again") == "B"
    client = FakeClient.instances[-1]
    assert client.connected and client.queries == ["hi", "again"]
    await chat.close()
    assert client.disconnected


async def test_error_result_raises_brain_error(tmp_path):
    brain = AgentSdkBrain("sonnet", tmp_path, client_factory=factory([_result("limit", is_error=True)]))
    chat = await brain.open_chat("SYS")
    with pytest.raises(BrainError, match="limit"):
        await chat.send("hi")


async def test_connect_failure_raises_brain_error(tmp_path):
    brain = AgentSdkBrain("sonnet", tmp_path, client_factory=factory(fail_connect=True))
    with pytest.raises(BrainError, match="not logged in"):
        await brain.open_chat("SYS")


async def test_complete_opens_sends_and_closes(tmp_path):
    brain = AgentSdkBrain("sonnet", tmp_path, client_factory=factory([_result('{"facts": []}')]))
    assert await brain.complete("MEM", "transcript") == '{"facts": []}'
    client = FakeClient.instances[-1]
    assert client.options.system_prompt == "MEM"
    assert client.disconnected
