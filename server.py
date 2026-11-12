"""Kube Investigator MCP application entry point."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from tools.github import (
    get_commit,
    list_repo_commits,
    register as register_github_tools,
)
from tools.kube_namespace import (
    inspect_namespace,
    register as register_kubernetes_tools,
)

PORT = int(os.getenv("PORT", "8000"))

mcp = FastMCP(
    "Kube Investigator",
    host="0.0.0.0",
    port=PORT,
    streamable_http_path="/mcp",
)

register_kubernetes_tools(mcp)
register_github_tools(mcp)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
