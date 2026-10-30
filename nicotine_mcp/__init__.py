# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""nicotine-mcp: a Model Context Protocol server for the Soulseek network.

Runs the Nicotine+ core without its GTK interface and exposes search and
download functionality as MCP tools, so AI agents can find tracks or
albums and download the best available copy. A small web dashboard lets
the human in the loop watch what is being searched and downloaded.

Layers:

- ``ranking``: pure scoring/ranking of search results, no Nicotine+ dependency
- ``automation``: glue around the Nicotine+ core (search collection, folder
  requests, download management), runs in the core's main loop thread
- ``runner``: runs the Nicotine+ core in a background thread and marshals calls
- ``server``: the MCP tools
- ``web``: the local dashboard
- ``cli``: command line entry point
"""

__version__ = "0.1.0"

DEFAULT_UI_PORT = 8585
DEFAULT_HTTP_PORT = 8766
