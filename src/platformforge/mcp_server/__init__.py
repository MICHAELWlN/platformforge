"""Read-only MCP server package. Importing it does not start a server."""

from platformforge.mcp_server.server import build_server

__all__ = ["build_server"]
