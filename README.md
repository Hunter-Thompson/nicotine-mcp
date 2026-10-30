<!--
  SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
  SPDX-License-Identifier: GPL-3.0-or-later
-->

# nicotine-mcp

An [MCP](https://modelcontextprotocol.io/) server that lets AI agents search
the Soulseek network and download music through [Nicotine+](https://nicotine-plus.org),
plus a small local dashboard so you can watch what the agent is doing.

Ask your agent for "the best FLAC of Artist – Title" or "the album Artist –
Album", and it will search, rank the results (free upload slot and fast
uploader first, then audio quality, canonical versions over remixes),
download the best copy, and fall back to the next candidate if the transfer
does not start.

nicotine-mcp runs the Nicotine+ core without its GTK user interface. It uses
the Nicotine+ configuration, account, shares and download folder of the
current user.


## Requirements

- Python 3.10 or newer
- The Nicotine+ core, version 3.4 or newer (currently the `master` branch,
  the 3.3.x releases lack APIs this project needs)
- A Soulseek account configured in Nicotine+ (start the GUI once and log in),
  or the `NICOTINE_USERNAME` and `NICOTINE_PASSWORD` environment variables

GTK and PyGObject are **not** needed. Building Nicotine+ from source needs
`msgfmt` from gettext (`brew install gettext` on macOS,
`apt install gettext` on Debian/Ubuntu).


## Installation

```sh
git clone https://github.com/Hunter-Thompson/nicotine-mcp
cd nicotine-mcp
python3 -m venv venv
. venv/bin/activate
pip install --no-deps "nicotine-plus @ git+https://github.com/nicotine-plus/nicotine-plus@master"
pip install -e .
```

`--no-deps` skips PyGObject, which the headless core does not use.


## Running

```sh
nicotine-mcp
```

speaks MCP over stdio and starts the dashboard on <http://127.0.0.1:8585/>.
Nicotine+ log messages go to stderr. Useful options:

| Option                 | Effect |
|------------------------|--------|
| `--http [PORT]`        | Serve MCP over streamable HTTP (default port 8766) instead of stdio |
| `--ui-port PORT`       | Dashboard port (default 8585, `0` picks a free port) |
| `--no-ui`              | Do not start the dashboard |
| `--open-ui`            | Open the dashboard in your browser at startup |
| `-c FILE`, `-u DIR`    | Use another Nicotine+ config file / data folder (a separate profile) |
| `-l PORT`              | Listen port for peer connections (if the configured one is taken) |

Only one client can be logged into a Soulseek account at a time. If the
Nicotine+ GUI is running with the same account, close it first, or give the
agent its own account and profile (`-c`/`-u`). If a Nicotine+ process still
holds the listen port, the log shows `Cannot listen on port 2234`; pass
`-l 2235` or stop that process.

### Claude Code

```sh
claude mcp add nicotine -- /path/to/venv/bin/nicotine-mcp
```

### Claude Desktop and other MCP hosts

```json
{
  "mcpServers": {
    "nicotine": {
      "command": "/path/to/venv/bin/nicotine-mcp",
      "args": [],
      "env": {
        "NICOTINE_USERNAME": "your-username",
        "NICOTINE_PASSWORD": "your-password"
      }
    }
  }
}
```

The `env` block is only needed if no account is configured in Nicotine+ yet.


## Dashboard

The dashboard at <http://127.0.0.1:8585/> refreshes every two seconds and
shows:

- connection status and the download folder
- every download with state, progress, speed, ETA or queue position, and
  buttons to pause, retry or remove it (plus "Clear finished")
- the agent's searches, with the number of responding users and files;
  click one to see its top ranked tracks or albums and why they scored as
  they did
- the most recent Nicotine+ log messages

It binds to localhost only, rejects requests with a non-local `Host` or
`Origin` header, and requires a custom header for state changing requests,
so other websites cannot drive it from your browser. The `get_status` tool
reports the dashboard URL to the agent, so it can point you there.


## Tools

| Tool                       | Description |
|----------------------------|-------------|
| `get_status`               | Connection status, download folders, counts, dashboard URL, recent log |
| `connect`                  | Connect to the Soulseek server and wait for the login |
| `search_tracks`            | Search for individual files, returns ranked candidates |
| `search_albums`            | Search for shared folders (albums), returns ranked folders |
| `get_search_results`       | Re-rank the results of an earlier search, with other filters |
| `download_file`            | Download one file from a user |
| `download_best_track`      | Search, download the best copy, fall back to the next one if it does not start |
| `download_album`           | Download a whole folder from a user (fetches the full listing first) |
| `download_best_album`      | Search, download the best folder, fall back to the next one if it does not start |
| `get_downloads`            | List downloads with state, progress, speed and local path when finished |
| `wait_for_downloads`       | Block until downloads finish or fail (with a timeout) |
| `cancel_download`          | Remove or pause a download |
| `cancel_album`             | Remove or pause every download inside a user's folder |
| `retry_download`           | Retry a failed or paused download |
| `clear_finished_downloads` | Remove finished downloads from the list |

Search tools accept these filters and preferences:

- `formats`: allowed file extensions ordered by preference, default
  `["flac", "mp3"]`; `"lossless"`, `"lossy"` and `"any"` work too
- `prefer`: `balanced` (default), `quality` (lossless and high bitrates
  first) or `availability` (free upload slots and fast uploaders first)
- `min_bitrate`, `require_free_slot`, `max_queue`, `min_length`,
  `max_length`: hard filters
- `wait_seconds`: how long to collect results before ranking; results keep
  arriving for a while, `get_search_results` with the returned token shows
  the ones that came in later
- `max_per_user`: cap per uploader in ranked lists (3 for tracks, 2 for
  albums), so one large collection does not hide alternatives


## Ranking

Every file is scored on three axes, and the scores are returned with each
result so the agent (and you, in the dashboard) can see why it was picked:

- **Availability** (up to 30 points): a free upload slot scores full points,
  otherwise the score drops with the length of the uploader's queue.
- **Speed** (up to 20 points): the uploader's reported upload speed, on a
  logarithmic scale from 50 KB/s to 50 MB/s.
- **Quality** (up to 35 points): lossless formats score highest (small bonus
  for hi-res), lossy files score by bitrate relative to 320 kbps.

A **relevance** term (up to 10 points) rewards files whose own name contains
the query words, and an 8 point penalty applies to names with variant markers
(remix, live, edit, instrumental, …) the query did not ask for. Small bonuses
go to preferred formats and to files shared by several users with identical
name and size. Suspiciously small audio files are penalized. Folders (albums)
additionally score on the number of matching audio files and format
consistency.

`download_best_track` and `download_best_album` try the best candidate first
and wait for the transfer to start. If it stays queued or fails, the download
is cancelled and the next candidate is tried.


## Tips for agents

- Queries work best as plain words: `artist title` or `artist album`.
  Prefix a word with `-` to exclude it, e.g. `-live -remix`.
- Virtual paths use backslashes and belong to a specific user; pass them
  back exactly as returned.
- Downloads continue in the background after a tool returns. Use
  `wait_for_downloads` (optionally with the `folder_path` of an album) to
  block until they are done, and read `local_path` from finished entries.
- Failed downloads marked `will_retry` are retried automatically by
  Nicotine+ after a few minutes (connection problems, user temporarily
  offline).


## Development

```sh
pip install -e ".[dev]"
python -m pycodestyle nicotine_mcp tests
python -m pylint nicotine_mcp tests
python -m unittest discover -s tests -t . -v
```

The unit tests run the real Nicotine+ core offline with a temporary profile;
no network access or account is needed. `tests/smoke_client.py` is a small
MCP client for manual end-to-end checks against the running server.

Layout:

- `nicotine_mcp/ranking.py`: pure scoring and ranking, no Nicotine+ dependency
- `nicotine_mcp/automation.py`: search collection, folder requests and download
  management on top of the Nicotine+ core
- `nicotine_mcp/runner.py`: runs the core in a background thread
- `nicotine_mcp/server.py`: the MCP tools
- `nicotine_mcp/web.py` and `static/index.html`: the dashboard
- `nicotine_mcp/cli.py`: command line entry point


## License

GPL-3.0-or-later, like Nicotine+ itself. See [LICENSE](LICENSE).
