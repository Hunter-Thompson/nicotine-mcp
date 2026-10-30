# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line entry point: ``nicotine-mcp`` or ``python -m nicotine_mcp``."""

from __future__ import annotations

import argparse
import sys

from nicotine_mcp import DEFAULT_HTTP_PORT
from nicotine_mcp import DEFAULT_UI_PORT
from nicotine_mcp import __version__


def build_parser():

    parser = argparse.ArgumentParser(
        prog="nicotine-mcp",
        description="MCP server that lets AI agents search and download music from Soulseek through Nicotine+. "
                    "Uses the Nicotine+ configuration, account and download folder of the current user."
    )
    parser.add_argument("-v", "--version", action="version", version=f"nicotine-mcp {__version__}")

    group = parser.add_argument_group("Nicotine+ profile")
    group.add_argument("-c", "--config", metavar="FILE", help="use a non-default Nicotine+ configuration file")
    group.add_argument("-u", "--user-data", metavar="DIR", help="alternative folder for Nicotine+ user data")
    group.add_argument("-l", "--listen-port", metavar="PORT", type=int,
                       help="listen on the given port for peer connections (overrides the configured port range)")
    group.add_argument("-b", "--bindip", metavar="ADDRESS", help="bind connections to the given IP address")
    group.add_argument("--isolated", action="store_true",
                       help="disable features that need external applications (e.g. inside containers)")

    group = parser.add_argument_group("Library")
    group.add_argument("--library", metavar="DIR", action="append", default=[],
                       help="folder with music you already have, not downloaded again by label jobs and album "
                            "downloads (can be repeated; the download folder and shares are always included)")

    group = parser.add_argument_group("MCP transport")
    group.add_argument("--http", metavar="PORT", type=int, nargs="?", const=DEFAULT_HTTP_PORT,
                       help=f"serve MCP over streamable HTTP on this port instead of stdio "
                            f"(default port {DEFAULT_HTTP_PORT})")
    group.add_argument("--http-host", metavar="HOST", default="127.0.0.1",
                       help="host to bind the MCP HTTP transport to (default: 127.0.0.1)")

    group = parser.add_argument_group("Dashboard")
    group.add_argument("--ui-port", metavar="PORT", type=int, default=DEFAULT_UI_PORT,
                       help=f"port for the local web dashboard (default: {DEFAULT_UI_PORT}, 0 picks a free port)")
    group.add_argument("--ui-host", metavar="HOST", default="127.0.0.1",
                       help="host to bind the dashboard to (default: 127.0.0.1)")
    group.add_argument("--no-ui", action="store_true", help="do not start the web dashboard")
    group.add_argument("--open-ui", action="store_true", help="open the dashboard in the default browser")

    return parser


def main(argv=None):

    args = build_parser().parse_args(argv)

    try:
        from pynicotine.config import config
        from pynicotine.core import core

    except ImportError:
        print("nicotine-mcp needs the Nicotine+ core (the 'pynicotine' package, version 3.4 or newer). "
              "Install it without its GTK dependencies, e.g.:\n"
              "  pip install --no-deps \"nicotine-plus @ git+https://github.com/nicotine-plus/nicotine-plus@master\"",
              file=sys.stderr)
        return 1

    if args.config:
        config.set_config_file(args.config)

    if args.user_data:
        config.set_data_folder(args.user_data)

    core.cli_interface_address = args.bindip
    core.cli_listen_port = args.listen_port

    from nicotine_mcp.server import MCPApplication

    return MCPApplication(
        isolated_mode=args.isolated,
        http_port=args.http,
        http_host=args.http_host,
        ui_port=None if args.no_ui else args.ui_port,
        ui_host=args.ui_host,
        open_ui=args.open_ui,
        library_folders=args.library
    ).run()
