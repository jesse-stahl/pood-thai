"""Claude, via the Claude Agent SDK, on the user's Claude Code login."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from pood_thai.models import BrainError

logger = logging.getLogger(__name__)


def build_options(system_prompt: str, model: str, cwd: Path) -> ClaudeAgentOptions:
    # setting_sources=[] and skills=[] keep the user's CLAUDE.md, settings and skills out of the persona;
    # the env flag and strict_mcp_config keep claude.ai connectors (Gmail, Drive...) out as well.
    # no-session-persistence stops the CLI saving transcripts under ~/.claude, so undo/forget stay honest.
    return ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        setting_sources=[],
        skills=[],
        max_turns=1,
        effort="low",
        thinking={"type": "disabled"},
        cwd=str(cwd),
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        strict_mcp_config=True,
        extra_args={"no-session-persistence": None},
    )


class AgentSdkChat:
    def __init__(self, client: Any):
        self._client = client
        self._lock = asyncio.Lock()

    async def send(self, message: str) -> str:
        async with self._lock:
            try:
                await self._client.query(message)
                async for item in self._client.receive_response():
                    if isinstance(item, ResultMessage):
                        if item.is_error:
                            detail = "; ".join(item.errors or []) or item.result or item.subtype
                            raise BrainError(f"Claude error ({item.subtype}): {detail}")
                        return item.result or ""
            except BrainError:
                raise
            except Exception as exc:
                raise BrainError(f"Claude request failed: {exc}") from exc
            raise BrainError("Claude returned no result")

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except Exception:
            logger.warning("Error while disconnecting Claude client", exc_info=True)


class AgentSdkBrain:
    def __init__(self, model: str, cwd: Path, client_factory: Callable[..., Any] = ClaudeSDKClient):
        self._model = model
        self._cwd = cwd
        self._client_factory = client_factory

    async def open_chat(self, system_prompt: str) -> AgentSdkChat:
        client = self._client_factory(options=build_options(system_prompt, self._model, self._cwd))
        try:
            await client.connect()
        except Exception as exc:
            raise BrainError(f"Could not start Claude: {exc}") from exc
        return AgentSdkChat(client)

    async def complete(self, system_prompt: str, message: str) -> str:
        chat = await self.open_chat(system_prompt)
        try:
            return await chat.send(message)
        finally:
            await chat.close()
