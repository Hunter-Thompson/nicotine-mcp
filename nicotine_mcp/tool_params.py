# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Annotations for the parameters of MCP tools, shared between the modules defining tools."""

from __future__ import annotations

from typing import Annotated
from typing import Literal

from pydantic import BaseModel
from pydantic import Field


def described(annotation, description):
    """Shorthand for a tool parameter annotation with a description."""
    return Annotated[annotation, Field(description=description)]


Formats = described(
    list[str] | None,
    "Allowed audio formats (file extensions) ordered by preference, e.g. ['flac', 'mp3']. "
    "Also accepts 'lossless', 'lossy' or 'any'. Defaults to ['flac', 'mp3']."
)
Prefer = described(
    Literal["balanced", "quality", "availability"],
    "What to optimize for: 'balanced', 'quality' (lossless/high bitrate first) or "
    "'availability' (free slot and fast uploader first)."
)
MinBitrate = described(int, "Minimum bitrate in kbps for lossy files (0 = no minimum). Lossless always passes.")
RequireFreeSlot = described(bool, "Only consider uploaders that currently have a free upload slot.")
MaxQueue = described(int | None, "Ignore uploaders with more than this many files queued (None = no limit).")
MinLength = described(int, "Minimum track length in seconds when known (0 = no minimum).")
MaxLength = described(int, "Maximum track length in seconds when known (0 = no maximum).")
WaitSeconds = described(float, "How long to wait for search results before ranking (1-60 seconds).")
SearchMode = described(
    Literal["global", "user", "buddies", "rooms"],
    "Where to search: the whole network (global), a single user's shares (user), buddies or joined rooms."
)
SearchUser = described(str | None, "Username whose shares to search, required when mode is 'user'.")
DownloadFolder = described(str | None, "Local folder to save into. Defaults to the Nicotine+ download folder.")
AudioOnly = described(bool, "Skip non-audio files (cover art, cue/log files) when downloading a folder.")
TrackQuery = described(str, "Search terms, e.g. 'artist title'. Prefix a word with - to exclude it.")
AlbumQuery = described(str, "Search terms, e.g. 'artist album'. Prefix a word with - to exclude it.")
GroupBy = described(Literal["track", "album"], "Return ranked tracks or ranked folders.")
Username = described(str, "Uploader username from a search result.")
VirtualPath = described(str, "Virtual path of the file from a search result (uses backslashes).")
FolderPath = described(str, "Virtual folder path from a search result (uses backslashes).")
MaxAttempts = described(int, "How many candidates to try before giving up.")
WaitForTrackStart = described(int, "How long to wait for a transfer to start before trying the next candidate.")
WaitForAlbumStart = described(
    int, "How long to wait for the first file to start transferring before trying the next folder."
)
StateFilter = described(
    Literal["all", "queued", "active", "paused", "finished", "failed"], "Filter by download state."
)
OptionalUsername = described(str | None, "Only downloads from this user.")
OptionalFolderPath = described(str | None, "Only downloads inside this remote folder.")
Limit = described(int, "Maximum number of entries to return.")
RemoveEntry = described(bool, "Remove the entry from the download list instead of pausing it.")
MaxPerUser = described(int, "Maximum number of results per uploader in the ranked list (0 = unlimited).")
LabelId = described(str, "Label ID from find_label, e.g. 'musicbrainz:5f14969e-...' or 'discogs:178535'.")
AlsoLabelId = described(
    str | None,
    "The same label's ID in the other catalog. Pass it whenever find_label returns the label in both: "
    "each catalog lacks releases the other has, and the lists are combined."
)
ArtistId = described(str, "Artist ID from find_artist, e.g. 'musicbrainz:44ac33f1-...' or 'discogs:3621627'.")
AlsoArtistId = described(
    str | None,
    "The same artist's ID in the other catalog. Pass it whenever find_artist returns the artist in both: "
    "each catalog lacks releases the other has, and the lists are combined."
)
IncludeAppearances = described(
    bool, "Also include releases credited to somebody else that have a track by this artist or a remix of "
          "theirs, like compilations. These can be large downloads for a single track."
)
ReleaseId = described(str, "Release ID from a catalog, e.g. 'musicbrainz:118f2d45-...' or 'discogs:772763'.")


class ReleaseRef(BaseModel):
    """A release to collect, as a catalog listed it."""

    release_id: str = Field(description="Release ID from a catalog, e.g. 'musicbrainz:118f2d45-...' or "
                                        "'discogs:772763'.")
    artist: str = Field("", description="Artist of the release, shown while the job runs. Taken from the catalog "
                                        "when left out.")
    title: str = Field("", description="Title of the release, shown while the job runs. Taken from the catalog "
                                       "when left out.")


ReleaseRefs = described(
    list[ReleaseRef],
    "Releases to collect, as find_album, find_track_albums or a catalog listing returned them. "
    "One listed twice is collected once."
)
JobName = described(str, "Name of the job, used for the download subfolder, e.g. the name of the playlist.")
JobId = described(str, "Job ID from start_label_job, start_artist_job or list_jobs.")
JobMode = described(
    Literal["preview", "download"],
    "'preview' finds out what is available without downloading anything. 'download' also downloads it."
)
ReleaseInJob = described(str, "Release ID of a release in the job, as get_job shows it.")
MaxReleases = described(int | None, "Only work on this many releases (None = all of them).")
Sample = described(
    bool, "With max_releases, spread the releases over the whole catalog, not only the newest. "
          "Gives a quick estimate of how much is available."
)
JobDownloadFolder = described(
    str | None, "Folder to collect the releases in. Defaults to a subfolder named after the "
                "label or artist in the Nicotine+ download folder."
)
MaxActiveReleases = described(int, "How many releases to download at the same time.")
MaxPerUploader = described(int, "How many releases to download from one uploader at the same time.")
StartTimeoutSeconds = described(int, "Try another uploader when a release has made no progress for this long.")
MinSpeedKbps = described(
    int, "Move a release's remaining tracks to another uploader when it stays below this speed "
         "in KB/s for a minute (0 = never)."
)
RetryMissingHours = described(float, "Search again for missing releases after this long (0 = never).")
SearchIntervalSeconds = described(float, "Minimum time between searches.")
Kinds = described(
    list[str] | None,
    "Only these kinds of release: album, ep, single, compilation, live, mix, other. Defaults to all of them."
)
ExcludeKinds = described(list[str] | None, "Leave out these kinds of release, e.g. ['mix', 'compilation'].")
YearFrom = described(int | None, "Only releases from this year onwards.")
YearTo = described(int | None, "Only releases up to this year.")
ArtistFilter = described(str | None, "Only releases by this artist.")
CatalogPrefix = described(
    str | None,
    "Only releases whose catalog number starts with this, e.g. 'o-ton'. Useful to clean up Discogs listings."
)
SkipOwned = described(bool, "Don't download releases found in the download folder, the shares or --library folders.")
FlacOnly = described(
    bool,
    "Strictly FLAC: nothing else is downloaded, even from a folder holding other formats too, and a copy "
    "in another format doesn't count as already owned. Overrides formats. Use it whenever FLAC is asked for."
)
AcceptPartial = described(bool, "Also accept folders that lack some of the release's tracks.")
