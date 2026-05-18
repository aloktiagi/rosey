"""Agent tool registry.

Centralizes the list of tools the agent gets per turn so forks can
extend or replace it without editing `agent.handle_message` directly.

Two ways to extend:

  1. Edit `default_tools()` here.
  2. Pass a custom `tools` argument to `agent.handle_message(...)`
     (not yet wired — would require a small signature change in
     `agent.py`; planned for the next refactor pass).

The list is a mix of:
  - The local `memory` tool (a FileMemoryTool instance, rendered to dict).
  - Anthropic-hosted server-side tools, declared as plain dicts. The API
    handles their execution; we don't need to dispatch them locally.

For Anthropic's full tool catalog see:
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
"""

from __future__ import annotations

from typing import List


def default_tools(memory) -> List[dict]:
    """Return the tools available on every agent turn.

    `memory` is a `FileMemoryTool` (or compatible) instance. Its `.to_dict()`
    is what the API expects.

    `max_uses` on the server-side tools caps how many times Claude can call
    each within a single API turn. Without these caps, "find a plumber" can
    snowball into 4–5 searches and several fetches, each adding 5–10s of
    latency and a few KB of result tokens to the conversation. Two of each
    is more than enough for the typical household question.
    """
    return [
        memory.to_dict(),
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 2},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 2},
    ]
