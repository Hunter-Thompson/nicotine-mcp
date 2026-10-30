# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Imports from the ``mcp`` package that differ between its major versions.

``MCPServerClass`` is ``MCPServer`` on mcp 2.x and ``FastMCP`` on mcp 1.x.
Everything is None when the package is not installed, so the rest of the
code can be imported (and tested) without it.
"""

__all__ = ["MCPServerClass", "ToolError", "anyio"]

try:
    from mcp.server.mcpserver import MCPServer as MCPServerClass  # mcp >= 2.0
except ImportError:  # pragma: no cover
    try:
        from mcp.server.fastmcp import FastMCP as MCPServerClass  # mcp 1.x
    except ImportError:
        MCPServerClass = None

try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:
        class ToolError(RuntimeError):
            """Raised by tools to report an error to the agent."""

try:
    import anyio
except ImportError:  # pragma: no cover
    anyio = None
