# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line entry point: ``nicotine-mcp`` or ``python -m nicotine_mcp``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

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

    commands = parser.add_subparsers(dest="command", metavar="[command]")
    command = commands.add_parser(
        "export", help="save jobs, the download list and shared settings to a file, for another user with the same "
                       "drive. The Soulseek login is not included."
    )
    command.add_argument("file", nargs="?", help="file to write (default: next to the download folder)")
    command = commands.add_parser(
        "import", help="continue from an export. What you have stays, and what is replaced is backed up first."
    )
    command.add_argument("file", help="file written by export")
    command.add_argument("--map-path", metavar="OLD=NEW", action="append", default=[],
                         help="the drive or a folder has another path here, e.g. "
                              "'/Volumes/T7 Shield=/Volumes/Music' (can be repeated)")
    command.add_argument("--keep-settings", action="store_true",
                         help="keep your download folder, shares and speed limits")

    return parser


def export_state(path):
    """With a server running it has to do this, as what it has in memory is newer than the files."""

    from pynicotine.config import config

    from nicotine_mcp import portable
    from nicotine_mcp.web import REQUEST_HEADER
    from nicotine_mcp.web import REQUEST_HEADER_VALUE

    config.load_config()
    server = portable.get_running_server(config.data_folder_path)

    if server is not None:
        if not server.get("dashboard_url"):
            raise ValueError("The running server has no dashboard, use its export_state tool instead.")

        request = urllib.request.Request(
            server["dashboard_url"] + "api/export", data=json.dumps({"path": path}).encode("utf-8"),
            headers={"Content-Type": "application/json", REQUEST_HEADER: REQUEST_HEADER_VALUE}
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)

        except urllib.error.HTTPError as error:
            raise ValueError(json.load(error).get("error", str(error))) from error

    settings = config.sections["transfers"]
    data = portable.build_export(config.data_folder_path, settings)
    path = path or portable.default_export_path(os.path.expandvars(settings["downloaddir"]))
    portable.write_export(data, path)
    return {"path": path, **portable.summarize(data)}


def import_state(path, path_mappings, keep_settings):

    from pynicotine.config import config

    from nicotine_mcp import portable

    if portable.get_running_server(config.data_folder_path) is not None:
        raise ValueError("A server is running, and would overwrite the import when it stops. Stop it first.")

    data = portable.read_export(path)
    portable.map_paths(data, [portable.parse_path_mapping(mapping) for mapping in path_mappings])
    unusable = portable.find_unusable_folders(data)

    if unusable:
        raise ValueError("These folders can't be used here: " + ", ".join(unusable) + ". Is the drive connected? "
                         "If it has another path here, use --map-path OLD=NEW.")

    config.create_data_folder()
    backup_folder = portable.back_up(config.data_folder_path, config.config_file_path)
    config.load_config()
    summary = portable.merge_into(data, config.data_folder_path)

    if not keep_settings:
        config.sections["transfers"].update(portable.get_import_settings(data))
        config.write_configuration()

    return {**summary, "backup_folder": backup_folder, "needs_login": config.need_config(),
            "shared_folders": [share[1] for share in config.sections["transfers"]["shared"]]}


def run_command(args):

    try:
        if args.command == "export":
            result = export_state(args.file)
            print(f"Saved to {result['path']}: {result['num_jobs']} jobs {result['jobs_by_status']}, "
                  f"{result['num_transfers']} downloads. The Soulseek login is not in it.")
            print(f'The other user runs: nicotine-mcp import "{result["path"]}"')

            if result.get("warning"):
                print(result["warning"])

            return 0

        result = import_state(args.file, args.map_path, args.keep_settings)

    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f"nicotine-mcp {args.command}: {error}", file=sys.stderr)
        return 1

    print(f"Imported {result['num_jobs']} jobs {result['jobs_by_status']} ({result['num_jobs_replaced']} replaced) "
          f"and {result['num_transfers']} downloads. Backup of what was here: {result['backup_folder']}")
    print("Jobs continue when the server starts. Make sure the other server has stopped, "
          "or both will download the same albums.")

    if result["shared_folders"]:
        print("You now share these with everybody on Soulseek: " + ", ".join(result["shared_folders"]))

    if result["needs_login"]:
        print("No Soulseek account is set up yet. Log in once with Nicotine+, or set NICOTINE_USERNAME "
              "and NICOTINE_PASSWORD. Use your own account: only one client can be logged into one at a time.")

    return 0


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

    if args.command:
        return run_command(args)

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
