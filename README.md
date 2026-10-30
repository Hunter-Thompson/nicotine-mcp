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

It can also work from what music catalogs know: "get the album this song is
on", or "collect everything on the label Ostgut Ton". See
[Albums and labels](#albums-and-labels).

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
| `--library DIR`        | A folder with music you already have, can be repeated. The download folder and shares are always checked |

Set the `DISCOGS_TOKEN` environment variable to a [Discogs personal access
token](https://www.discogs.com/settings/developers) for a higher Discogs rate
limit (60 instead of 25 requests per minute). MusicBrainz needs no account.

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

- the current download speed with its recent history, connection status and
  the download folder
- each label being collected as a shelf of record spines, one per release, with
  thickness following its number of tracks: dim when not searched yet, gray when
  found, amber filling up while downloading and bright once it has arrived. Click
  a label or a spine for the releases, and pause, resume, cancel or start
  downloading from the page
- downloads grouped by album, with real speed and progress, which open up to
  the individual files; pause, retry or remove an album or a file
- the fastest uploaders by measured speed
- the agent's searches; click one to see its ranked tracks or albums
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
| `find_track_albums`        | Which releases a song appears on, according to MusicBrainz |
| `download_album_for_track` | Download the album a song is on, checked against its tracklist |
| `download_release`         | Download one specific catalog release, checked against its tracklist |
| `find_label`               | Find record labels by name in MusicBrainz and Discogs |
| `get_label_releases`       | What a label released, with editions merged and what you already own marked |
| `start_label_job`          | Start a background job that previews or downloads a label's releases |
| `find_artist`              | Find artists by name in MusicBrainz and Discogs |
| `get_artist_releases`      | An artist's discography, with editions merged and what you already own marked |
| `start_artist_job`         | Start a background job that previews or downloads an artist's discography |
| `get_job`, `list_jobs`     | Progress of jobs, and what they found for each release |
| `start_job_downloads`      | Start downloading what a preview found |
| `pause_job`, `resume_job`  | Stop and continue searching and starting downloads |
| `retry_job_missing`        | Search right away for the releases a job did not find |
| `cancel_job`               | Stop a job and remove its unfinished downloads, finished files stay |
| `remove_job`               | Forget a job that is over, its files stay |

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
- **Speed** (up to 20 points): how fast the uploader was when measured, on a
  logarithmic scale from 50 KB/s to 5 MB/s. See [Speed](#speed).
- **Quality** (up to 35 points): lossless formats score highest (small bonus
  for hi-res), lossy files score by bitrate relative to 320 kbps.

A **relevance** term (up to 10 points) rewards files whose own name contains
the query words, and an 8 point penalty applies to names with variant markers
(remix, live, edit, instrumental, …) the query did not ask for. Small bonuses
go to preferred formats and to files shared by several users with identical
name and size. Suspiciously small audio files are penalized. Folders (albums)
additionally score on the number of matching audio files and format
consistency.



## FLAC only

Every download tool takes `flac_only`, and the dashboard has a button for it on a preview.
It's strict, all the way through:

- A folder holding both FLAC and MP3 gives you only the FLACs. Cover art and such still come along.
- Folders are checked against the tracklist using their FLACs alone, so an album
  with 3 of 10 tracks in FLAC counts as incomplete, whatever else is in there.
- An MP3 you own doesn't count as already having it, so it doesn't get in the way of a FLAC.
- Moving to another uploader stays within FLAC.

Starting a preview's downloads as FLAC only keeps what it found in pure FLAC folders, and searches
again for the rest.


## Speed

The speed an uploader reports says little about what they deliver. In a measurement of
8 uploaders of the same track, the reported speeds and real speeds were if anything
inversely related: one reporting 8.3 MB/s delivered 96 KB/s, and one reporting 1.9 MB/s
delivered 1.7 MB/s. So nicotine-mcp measures instead:

- **Races:** the download tools start the best 4 uploaders at once, on a single file
  each. About 14 seconds after the first one starts, the fastest is kept and the others
  are removed along with their partial files. The first 4 seconds of each transfer don't
  count, as they are a burst from network buffers filling up. For albums, the rest of the
  folder then follows from the winner. If nobody starts in time, the next 4 get their turn.
  Speed is all that decides a race, so only candidates with the quality of the best one take
  part, and uploaders known to be slow only do when there is nobody else.
- **Memory:** real speeds are remembered in `nicotine_mcp_speeds.json` in the Nicotine+
  data folder, and they replace reported speeds in the ranking. Somebody unknown
  ranks above somebody known to be slow, but below somebody known to be fast. A reported
  speed only counts as a sign of recent activity, for up to 5 points.
- **Switching:** jobs and albums checked against a tracklist stay watched. When a release stays below `min_speed_kbps`
  (default 300) for a minute, or tracks fail, the missing tracks move to another uploader
  with a verified copy who isn't known to be slower. Tracks that arrived are kept, and new
  ones go to the same folder, which can mean a mix of formats within an album. Not done
  past 80% progress, when it's not worth it.

None of this can help when everybody who has a release is slow. Rare releases have few
uploaders, and a track-by-track download from several of them at once is not implemented.
Your own connection and whether your listening port is reachable matter too. Peers can't
connect to you directly without an open port, which rules out some transfers.


## Albums and labels

Labels are rarely part of the paths shared on Soulseek: searching for "bite"
finds anything with that word in its path. So the list of what to get comes from
a catalog ([MusicBrainz](https://musicbrainz.org), or
[Discogs](https://www.discogs.com) as a fallback), and Soulseek only supplies the
files:

```
catalog → releases → search Soulseek → check folders against tracklist → download
```

**The check is what makes this work.** A folder's full listing is requested from
the uploader and compared with the release: the tracklist and track lengths, the
catalog number, title and artist. Each folder gets a verdict:

| Verdict    | Meaning | Downloaded? |
|------------|---------|-------------|
| `complete` | Every track is there, and the folder is identifiably this release | Yes |
| `probable` | The catalog has no tracklist, but the folder is named after the release | Yes |
| `partial`  | Tracks are missing, or buried among many other files | Only with `accept_partial` |
| `mismatch` | Something else | No |

**From a song to its album:** `download_album_for_track` picks the most canonical
release (exact title before remixes; albums, then EPs, singles and compilations;
oldest first). If the catalog doesn't know the song, or nobody shares the album, it
falls back to the folder the song was found in, but only if that looks like one
album: numbered tracks, one format, no generic name like `Misc`. The `method` in the
result says which way it went, since a `source_folder` download was not checked
against a tracklist.

**A whole label:** `find_label`, then `get_label_releases`, then `start_label_job`. Neither
catalog is complete, so give these the label's ID in both (`label_id` and `also_label_id`) and
the lists are combined. Jobs run in the background and survive restarts, with their state saved in
`nicotine_mcp_jobs.json` in the Nicotine+ data folder:

- Jobs start as a **preview**, which downloads nothing and reports what is available.
  Use `max_releases` with `sample` for a quick estimate on a large label. Then
  `start_job_downloads` reuses what the preview found.
- Releases you already have are skipped, and releases go to a subfolder named after
  the label.
- By default, 3 releases download at a time and 1 per uploader. There are at least 10
  seconds between searches, so a job takes roughly 10-30 seconds per release.
- A release with no progress for 15 minutes moves on to the next uploader.
- Missing releases are searched for again every 24 hours, up to 7 times.
- If the download folder's disk is unplugged, the job waits instead of writing to the
  system disk.

**A whole discography:** `find_artist`, then `get_artist_releases`, then `start_artist_job`. These
work like the label tools, with the artist's ID in both catalogs as `artist_id` and
`also_artist_id`, and releases go to a subfolder named after the artist. Releases credited to the
artist count, including collaborations. Compilations they only have a track on and remixes they
did for others are left out unless you set `include_appearances`, since those are whole albums for
a single track. Aliases and side projects are artists of their own.

Known limitations:

- Discogs doesn't say what kind most of an artist's releases are. They count as `other`, so a
  filter like `kinds=["album"]` leaves out those only Discogs knows about.
- Albums split into subfolders (`CD1`, `CD2`) count as `partial`, since each
  folder only holds part of the tracklist.
- Sublabels aren't included, they count as labels of their own.
- Discogs label listings include releases a label was only credited on. They are
  removed once a job fetches their tracklists (state `not_on_label`).
- Only file names and lengths are compared, audio files are not fingerprinted.


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
- `nicotine_mcp/matching.py`: pure comparison of shared folders with releases
- `nicotine_mcp/catalog.py`: MusicBrainz and Discogs clients, no Nicotine+ dependency
- `nicotine_mcp/library.py`: finds out which releases are already on disk
- `nicotine_mcp/automation.py`: search collection, folder requests and download
  management on top of the Nicotine+ core
- `nicotine_mcp/runner.py`: runs the core in a background thread
- `nicotine_mcp/sourcing.py`: finds the shared folders that really contain a release
- `nicotine_mcp/speed.py`: measures uploaders, and races them to find the fastest
- `nicotine_mcp/jobs.py`, `job_models.py`: background jobs with saved state
- `nicotine_mcp/server.py`, `collection_tools.py`, `tool_params.py`: the MCP tools
- `nicotine_mcp/web.py` and `static/index.html`: the dashboard
- `nicotine_mcp/cli.py`: command line entry point


## License

GPL-3.0-or-later, like Nicotine+ itself. See [LICENSE](LICENSE).
