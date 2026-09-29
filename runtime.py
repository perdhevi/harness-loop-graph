"""Shared setup: config and prompt loading, and building the tool registry.

Moved out of main.py in Stage 5 so both the question mode and the build
mode use the same wiring.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from tools.local import LocalToolSource
from tools.mcp_client import McpClient, McpToolSource, load_servers
from tools.registry import ToolRegistry

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
PROMPTS_PATH = ROOT / "prompts.json"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def prompt_text(prompts: dict, key: str) -> str:
    value = prompts[key]
    return "\n".join(value) if isinstance(value, list) else value


def build_registry(config: dict, workspace: Path, stack: contextlib.ExitStack) -> ToolRegistry:
    """Local tools + one source per enabled MCP server. Servers close when `stack` closes."""
    registry = ToolRegistry()
    tools_cfg = config.get("tools", {})
    if tools_cfg.get("local"):
        registry.add_source(LocalToolSource(ROOT / tools_cfg["local"]))
    if tools_cfg.get("mcp"):
        for name, srv in load_servers(ROOT / tools_cfg["mcp"], workspace).items():
            client = stack.enter_context(McpClient(
                name, srv["command"], srv["args"], env=srv["env"], cwd=ROOT, timeout_s=srv["timeout_s"]))
            client.initialize()
            registry.add_source(McpToolSource(client))
    return registry
