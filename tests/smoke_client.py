# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Minimal MCP stdio client to exercise nicotine-mcp end to end (manual checks, not a unit test).

Usage: smoke_client.py <scenario> [args] -- <server command...>
Scenarios: isolated (no credentials), live-search <track query> <album query>,
           live-download <query>, live-album-probe <query>, live-album <query>

Set NICOTINE_MCP_UI=http://127.0.0.1:PORT/ to also fetch the dashboard while the server runs.
"""
import asyncio
import json
import os
import sys
import urllib.request

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def show(label, result, limit=1800):
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", None)
    if structured is not None:
        text = json.dumps(structured, indent=1)
    else:
        text = " | ".join(getattr(c, "text", str(c)) for c in result.content)
        if text.startswith("{"):
            try:
                structured = json.loads(text)
            except ValueError:
                pass
    print(f"\n### {label} (error={is_error})\n{text[:limit]}{'…' if len(text) > limit else ''}")
    return structured


def fetch_dashboard(label):
    base = os.environ.get("NICOTINE_MCP_UI")
    if not base:
        return
    try:
        with urllib.request.urlopen(base, timeout=10) as response:
            html = response.read().decode("utf-8")
        with urllib.request.urlopen(base.rstrip("/") + "/api/state", timeout=10) as response:
            state = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        print(f"\n### dashboard {label}: FAILED {error}")
        return
    status = state["status"]
    print(f"\n### dashboard {label}: html {len(html)} bytes, title ok={'<title>Nicotine+ MCP</title>' in html}")
    print(f"  status: connected={status['connected']} user={status['username']} counts={status['downloads']} "
          f"speed={state['download_speed']} searches={len(state['searches'])} log_lines={len(state['recent_log'])}")
    for item in state["downloads"][:6]:
        print(f"  {item['state']:9} {item['progress_percent']:5.1f}% {item['speed'] or '':>12} {item['basename']}")


async def main():
    scenario = sys.argv[1]
    command, *args = sys.argv[sys.argv.index("--") + 1:]
    params = StdioServerParameters(command=command, args=args)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.server_info if hasattr(init, "server_info") else init.serverInfo)
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            show("get_status", await session.call_tool("get_status", {}))
            fetch_dashboard("at start")

            if scenario == "isolated":
                show("connect", await session.call_tool("connect", {"timeout_seconds": 2}))
                show("search_tracks", await session.call_tool("search_tracks", {"query": "test", "wait_seconds": 1}))
                show("get_downloads", await session.call_tool("get_downloads", {}))
                return

            status = show("connect", await session.call_tool("connect", {"timeout_seconds": 30}))

            if scenario == "live-search":
                show("search_tracks", await session.call_tool(
                    "search_tracks", {"query": sys.argv[2], "wait_seconds": 8, "limit": 5}), limit=6000)
                show("search_albums", await session.call_tool(
                    "search_albums", {"query": sys.argv[3], "wait_seconds": 8, "limit": 2}), limit=6000)

            elif scenario == "live-download":
                result = show("download_best_track", await session.call_tool(
                    "download_best_track", {"query": sys.argv[2], "wait_seconds": 8, "max_attempts": 3,
                                            "wait_for_start_seconds": 20}), limit=5000)
                if result and result.get("success"):
                    dl = result["download"]
                    show("wait_for_downloads", await session.call_tool("wait_for_downloads", {
                        "username": dl["username"], "virtual_paths": [dl["virtual_path"]],
                        "timeout_seconds": 300}), limit=3000)
                    # Clean up: remove leftover unfinished copies of the same track from earlier runs
                    listing = show("get_downloads", await session.call_tool("get_downloads", {"limit": 500}))
                    for item in (listing or {}).get("downloads", []):
                        if item["state"] != "finished" and "Around the World" in item["basename"]:
                            show("cancel_download", await session.call_tool("cancel_download", {
                                "username": item["username"], "virtual_path": item["virtual_path"]}))

            elif scenario == "live-album-probe":
                # Verify the album flow (folder listing + enqueue + first transfer start), then cancel
                result = show("download_best_album", await session.call_tool(
                    "download_best_album", {"query": sys.argv[2], "wait_seconds": 10, "max_attempts": 2,
                                            "wait_for_start_seconds": 30}), limit=12000)
                if result and result.get("success"):
                    album = result["album"]
                    await asyncio.sleep(3)
                    fetch_dashboard("while transferring")
                    show("cancel_album", await session.call_tool("cancel_album", {
                        "username": album["username"], "folder_path": album["folder_path"]}))
                    show("get_downloads", await session.call_tool("get_downloads", {
                        "username": album["username"], "folder_path": album["folder_path"]}))

            elif scenario == "live-album":
                result = show("download_best_album", await session.call_tool(
                    "download_best_album", {"query": sys.argv[2], "wait_seconds": 10, "max_attempts": 2,
                                            "wait_for_start_seconds": 30, "audio_only": False}), limit=6000)
                if result and result.get("success"):
                    show("wait_for_downloads", await session.call_tool("wait_for_downloads", {
                        "username": result["album"]["username"], "folder_path": result["album"]["folder_path"],
                        "timeout_seconds": 400}), limit=4000)


asyncio.run(main())
