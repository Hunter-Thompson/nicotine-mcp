# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# pylint: disable=too-many-lines

"""Long running jobs that collect many releases, such as a whole record label.

A label can have hundreds of releases, far more than fits into a single tool
call. A job works through them in a background thread at a pace that is fair
to the network and to uploaders, saves its state after every change so it
survives restarts, and keeps a record of what it found and what is missing.

A job starts in one of two modes:

- ``preview``: finds out which releases are available and who has them, without
  downloading anything
- ``download``: also downloads them, a few at a time

Releases that cannot be found are searched for again after a while, since
which users are online changes all the time.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

from dataclasses import asdict
from threading import Event
from threading import RLock
from threading import Thread

from nicotine_mcp.catalog import CatalogError
from nicotine_mcp.job_models import ItemState
from nicotine_mcp.job_models import Job
from nicotine_mcp.job_models import JobItem
from nicotine_mcp.job_models import JobStatus
from nicotine_mcp.job_models import PauseMode
from nicotine_mcp.job_models import clean_folder_name
from nicotine_mcp.job_models import describe_item
from nicotine_mcp.job_models import describe_spine
from nicotine_mcp.job_models import is_destination_available
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.mcp_compat import anyio
from nicotine_mcp.matching import Release
from nicotine_mcp.matching import select_remaining_files
from nicotine_mcp.ranking import get_extension
from nicotine_mcp.ranking import is_audio_extension
from nicotine_mcp.ranking import is_file_wanted
from nicotine_mcp.ranking import normalize_formats

JOBS_FILE_NAME = "nicotine_mcp_jobs.json"


class JobManager:
    """Runs jobs in a background thread, and answers questions about them from any thread."""

    TICK_SECONDS = 1.0
    MAX_READY_BACKLOG_FACTOR = 2
    MAX_SEARCH_ROUNDS = 2
    QUEUE_TIMEOUT = 6 * 60 * 60  # Uploaders with a long queue can take hours to get to us
    FINISHED_JOBS_KEPT = 20
    SPEED_CHECK_SECONDS = 60
    SWITCH_FASTER_FACTOR = 1.5
    SWITCH_MAX_PROGRESS = 80

    def __init__(self, runner, sourcer, library=None, store_path=None, clock=time.time, is_destination_ok=None):

        self.runner = runner
        self.sourcer = sourcer
        self.library = library
        self.store_path = store_path
        self.jobs = {}
        self._clock = clock
        self._is_destination_ok = is_destination_ok or is_destination_available
        self._lock = RLock()
        self._stop_event = Event()
        self._thread = None
        self._is_dirty = False
        self._last_search_at = 0.0
        self._load()

    # Persistence #

    def _load(self):

        if not self.store_path or not os.path.exists(self.store_path):
            return

        try:
            with open(self.store_path, encoding="utf-8") as file_handle:
                data = json.load(file_handle)

            for job_data in data.get("jobs", []):
                items = [JobItem(**item) for item in job_data.pop("items", [])]
                job = Job(**job_data, items=items)
                self.jobs[job.job_id] = job

                # Time spent not running says nothing about whether a transfer is stalled
                for item in items:
                    if item.state == ItemState.DOWNLOADING:
                        item.last_progress_at = self._clock()

        except (OSError, ValueError, TypeError) as error:
            # Keep the unreadable file around, it might be possible to recover it
            os.replace(self.store_path, self.store_path + ".corrupt")
            print(f"Could not load jobs, moved them to {self.store_path}.corrupt: {error}", file=sys.stderr, flush=True)

    def _save(self):

        with self._lock:
            if not self._is_dirty or not self.store_path:
                return

            content = json.dumps({"jobs": [asdict(job) for job in self.jobs.values()]}, ensure_ascii=False)
            temporary_path = self.store_path + ".tmp"

            with open(temporary_path, "w", encoding="utf-8") as file_handle:
                file_handle.write(content)

            os.replace(temporary_path, self.store_path)
            self._is_dirty = False

    def _mark_dirty(self):
        self._is_dirty = True

    def flush(self):
        """Saves right now, for those who are about to read the file."""

        with self._lock:
            self._mark_dirty()
            self._save()

    # Thread #

    def start(self):

        self._thread = Thread(target=self._run, name="NicotineMCPJobs", daemon=True)
        self._thread.start()

    def stop(self, timeout=10):

        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout)

        self._save()

    def _run(self):
        anyio.run(self._main)

    async def _main(self):

        while not self._stop_event.is_set():
            try:
                await self.step()

            except Exception as error:
                self.runner.recent_log.append(f"Job error: {error}")

            await anyio.sleep(self.TICK_SECONDS)

    # Requests from tools #

    def create_job(self, name, releases, label_id=None, artist_id=None, mode="preview", filters=None, **options):
        """Creates a job for a list of catalog releases."""

        if mode not in {"preview", "download"}:
            raise ValueError("mode must be 'preview' or 'download'")

        if not releases:
            raise ValueError("No releases to work on, the filters may be too narrow")

        with self._lock:
            for job in self.jobs.values():
                if label_id and job.label_id == label_id and job.status in {JobStatus.RUNNING, JobStatus.PAUSED}:
                    raise ValueError(f"Job {job.job_id} is already working on this label. Cancel it first, "
                                     f"or use start_job_downloads.")

                if artist_id and job.artist_id == artist_id and job.status in {JobStatus.RUNNING, JobStatus.PAUSED}:
                    raise ValueError(f"Job {job.job_id} is already working on this artist. Cancel it first, "
                                     f"or use start_job_downloads.")

            job = Job(
                job_id=uuid.uuid4().hex[:8], name=name, label_id=label_id, artist_id=artist_id, mode=mode,
                created_at=self._clock(), filters=filters or {},
                items=[JobItem(release=release.to_dict()) for release in releases],
                **options
            )
            self.jobs[job.job_id] = job
            self._prune_finished_jobs()
            self._mark_dirty()

        self._save()
        return self.describe_job(job.job_id)

    def watch_download(self, name, release, source, sources, enqueued, **options):
        """Keeps an eye on a single release that has just started downloading, as if it were part of
        a job, so that it moves to somebody else if its uploader turns out to be slow."""

        now = self._clock()
        download_folder = enqueued["download_folder"]
        username = source["username"]
        folder_path = source["folder_path"]
        item = JobItem(
            release=release.to_dict(), state=ItemState.DOWNLOADING, sources=sources,
            tried_sources=[[username, folder_path]], current_source=source, download_folder=download_folder,
            started_at=now, last_progress_at=now, last_update_at=now, speed_check_at=now,
            num_files=enqueued.get("num_files", 0), search_rounds=self.MAX_SEARCH_ROUNDS
        )

        with self._lock:
            job = Job(
                job_id=uuid.uuid4().hex[:8], name=name, label_id=None, mode="download", created_at=now,
                download_folder=os.path.dirname(download_folder), retry_missing_hours=0, items=[item], **options
            )
            self.jobs[job.job_id] = job
            self._prune_finished_jobs()
            self._mark_dirty()

        self._save()
        return job.job_id

    def _prune_finished_jobs(self):

        finished = [job for job in self.jobs.values() if job.status in {JobStatus.FINISHED, JobStatus.CANCELLED}]

        for job in finished[:-self.FINISHED_JOBS_KEPT]:
            del self.jobs[job.job_id]

    def _get_job(self, job_id):

        job = self.jobs.get(job_id)

        if job is None:
            raise KeyError(f"Unknown job '{job_id}'. Use list_jobs to see the jobs.")

        return job

    def pause_job(self, job_id, mode=PauseMode.AFTER_ALBUM):
        """Stops searching and starting downloads. Albums that have started finish, unless
        ``mode`` says to stop them now. Those only waiting in a queue go back in line on resuming."""

        if mode not in PauseMode.ALL:
            raise ValueError(f"Unknown way to pause '{mode}', expected one of {', '.join(PauseMode.ALL)}")

        with self._lock:
            job = self._get_job(job_id)

            if job.status in {JobStatus.FINISHED, JobStatus.CANCELLED}:
                raise ValueError(f"Job {job_id} is already {job.status}")

            job.status = JobStatus.PAUSED
            job.pause_mode = mode
            job.paused_by_all = False
            self._mark_dirty()

        self._save()
        return self.describe_job(job_id)

    def resume_job(self, job_id):

        with self._lock:
            job = self._get_job(job_id)

            if job.status == JobStatus.CANCELLED:
                raise ValueError(f"Job {job_id} was cancelled")

            job.status = JobStatus.RUNNING
            job.pause_mode = None
            job.finished_at = None
            job.paused_by_all = False
            self._mark_dirty()

        self._save()
        return self.describe_job(job_id)

    def pause_all(self, mode=PauseMode.NOW):
        """Pauses every job that is running or waiting to retry, and remembers which ones,
        so that resume_all() continues only those. Jobs paused one by one stay paused."""

        if mode not in PauseMode.ALL:
            raise ValueError(f"Unknown way to pause '{mode}', expected one of {', '.join(PauseMode.ALL)}")

        with self._lock:
            paused = []

            for job in self.jobs.values():
                if job.status not in JobStatus.ACTIVE:
                    continue

                job.status = JobStatus.PAUSED
                job.pause_mode = mode
                job.paused_by_all = True
                paused.append(job.job_id)

            if paused:
                self._mark_dirty()

        self._save()
        return paused

    def resume_all(self):
        """Continues the jobs that pause_all() paused."""

        with self._lock:
            resumed = []

            for job in self.jobs.values():
                if job.status != JobStatus.PAUSED or not job.paused_by_all:
                    continue

                job.status = JobStatus.RUNNING
                job.pause_mode = None
                job.finished_at = None
                job.paused_by_all = False
                resumed.append(job.job_id)

            if resumed:
                self._mark_dirty()

        self._save()
        return resumed

    def _get_item(self, job, release_id):

        for item in job.items:
            if item.release.get("release_id") == release_id:
                return item

        raise KeyError(f"Job {job.job_id} has no release '{release_id}'")

    async def skip_release(self, job_id, release_id):
        """Leaves a release out of a job, and removes its unfinished downloads. Finished files stay on disk."""

        with self._lock:
            job = self._get_job(job_id)
            item = self._get_item(job, release_id)

            if item.state in {ItemState.FINISHED, ItemState.OWNED}:
                raise ValueError(f"'{item.release.get('title')}' is already there")

            source = item.current_source if item.state == ItemState.DOWNLOADING else None
            item.state = ItemState.CANCELLED
            item.is_queued = False
            item.current_source = None
            item.note = "skipped"
            self._mark_dirty()

        if source is not None:
            await self._cancel_unfinished(source["username"], source["folder_path"])

        self._save()
        return self.describe_job(job_id)

    def prioritize_release(self, job_id, release_id):
        """Makes a release the next one to be worked on. That also brings back one that
        was skipped or not found, which is then searched for again."""

        with self._lock:
            job = self._get_job(job_id)
            item = self._get_item(job, release_id)

            if job.status == JobStatus.CANCELLED:
                raise ValueError(f"Job {job_id} was cancelled")

            if item.state in {ItemState.FINISHED, ItemState.OWNED, ItemState.DOWNLOADING}:
                raise ValueError(f"'{item.release.get('title')}' is already {item.state}")

            if item.state not in {ItemState.PENDING, ItemState.SOURCED}:
                item.state = ItemState.PENDING
                item.tried_sources = []
                item.search_rounds = 0
                item.note = None

            job.items.remove(item)
            job.items.insert(0, item)

            if job.status in {JobStatus.FINISHED, JobStatus.WAITING_RETRY, JobStatus.PREVIEWED}:
                job.status = JobStatus.RUNNING
                job.finished_at = None
                job.next_retry_at = None

            self._mark_dirty()

        self._save()
        return self.describe_job(job_id)

    @staticmethod
    def _restrict_to_flac(job):
        """Makes a preview that accepted any format FLAC only. What it found in pure FLAC folders still
        holds, the rest needs another look, since it stopped searching once it had found enough."""

        job.formats = ["flac"]

        for item in job.items:
            if item.state not in {ItemState.SOURCED, ItemState.PARTIAL, ItemState.OWNED}:
                continue

            item.sources = [source for source in item.sources if source.get("formats") == ["flac"]]
            item.partial_sources = [source for source in item.partial_sources if source.get("formats") == ["flac"]]
            item.tried_sources = []

            if item.state == ItemState.SOURCED and item.sources:
                continue

            # Owning it in another format doesn't count anymore, which the search step checks again
            item.state = ItemState.PENDING
            item.local_path = None
            item.note = None

    def start_downloads(self, job_id, accept_partial=False, flac_only=False):
        """Turns a preview into a download job. What the preview found is reused,
        and uploaders that have gone away in the meantime are replaced."""

        with self._lock:
            job = self._get_job(job_id)

            if job.status == JobStatus.CANCELLED:
                raise ValueError(f"Job {job_id} was cancelled")

            job.mode = "download"
            job.accept_partial = accept_partial or job.accept_partial
            job.status = JobStatus.RUNNING
            job.finished_at = None

            if flac_only:
                self._restrict_to_flac(job)

            if job.accept_partial:
                for item in job.items:
                    if item.state == ItemState.PARTIAL and item.partial_sources:
                        item.sources = [*item.sources, *item.partial_sources]
                        item.state = ItemState.SOURCED

            self._mark_dirty()

        self._save()
        return self.describe_job(job_id)

    def retry_missing(self, job_id):
        """Searches again right now for the releases that were not found."""

        with self._lock:
            job = self._get_job(job_id)

            if job.status == JobStatus.CANCELLED:
                raise ValueError(f"Job {job_id} was cancelled")

            num_retried = self._reset_missing(job)
            job.status = JobStatus.RUNNING
            job.finished_at = None
            job.next_retry_at = None
            self._mark_dirty()

        self._save()
        result = self.describe_job(job_id)
        result["num_retried"] = num_retried
        return result

    async def cancel_job(self, job_id):
        """Stops a job, and removes its unfinished downloads. Finished files stay on disk."""

        with self._lock:
            job = self._get_job(job_id)
            job.status = JobStatus.CANCELLED
            job.finished_at = self._clock()
            downloading = [item for item in job.items if item.state == ItemState.DOWNLOADING]

            for item in job.items:
                if item.state not in ItemState.DONE:
                    item.state = ItemState.CANCELLED

            self._mark_dirty()

        for item in downloading:
            source = item.current_source
            await self._cancel_unfinished(source["username"], source["folder_path"])

        self._save()
        return self.describe_job(job_id)

    def remove_job(self, job_id):
        """Forgets a job that is over. Its files stay on disk."""

        with self._lock:
            job = self._get_job(job_id)

            if job.status not in {JobStatus.FINISHED, JobStatus.CANCELLED, JobStatus.PREVIEWED}:
                raise ValueError(f"Job {job_id} is {job.status}. Cancel it first.")

            del self.jobs[job_id]
            self._mark_dirty()

        self._save()
        return {"job_id": job_id, "removed": True}

    # Queries #

    def _describe_job_locked(self, job):

        counts = {}

        for item in job.items:
            counts[item.state] = counts.get(item.state, 0) + 1

        return {
            "job_id": job.job_id,
            "name": job.name,
            "label_id": job.label_id,
            "artist_id": job.artist_id,
            "pause_mode": job.pause_mode,
            "paused_by_all": job.paused_by_all,
            "mode": job.mode,
            "status": job.status,
            "blocked_reason": job.blocked_reason,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
            "next_retry_at": job.next_retry_at,
            "retry_rounds": job.retry_rounds,
            "download_folder": job.download_folder,
            "num_releases": len(job.items),
            "num_in_catalog": job.num_in_catalog or len(job.items),
            "counts": counts,
            "num_available": sum(counts.get(state, 0) for state in (
                ItemState.SOURCED, ItemState.DOWNLOADING, ItemState.FINISHED
            )),
            "num_available_flac": sum(
                1 for item in job.items if item.state == ItemState.SOURCED
                and any(source.get("formats") == ["flac"] for source in item.sources)
            ),
            "formats": job.formats,
            "num_remaining_to_search": counts.get(ItemState.PENDING, 0)
        }

    def describe_job(self, job_id):

        with self._lock:
            return self._describe_job_locked(self._get_job(job_id))

    def list_jobs(self, include_shelf=False):
        """``include_shelf`` adds what the dashboard needs to draw every release of a job."""

        with self._lock:
            jobs = []

            for job in reversed(list(self.jobs.values())):
                data = self._describe_job_locked(job)

                if include_shelf:
                    data["shelf"] = [describe_spine(item) for item in job.items]

                jobs.append(data)

            return jobs

    def get_job(self, job_id, state=None, limit=100, include_sources=False):

        if state is not None and state not in ItemState.ALL:
            raise ValueError(f"Unknown state '{state}', expected one of {', '.join(ItemState.ALL)}")

        with self._lock:
            job = self._get_job(job_id)
            result = self._describe_job_locked(job)
            items = [item for item in job.items if state is None or item.state == state]
            result["num_listed"] = len(items)
            result["releases"] = [describe_item(item, include_sources) for item in items[:max(1, int(limit))]]
            return result

    # Worker #

    async def step(self):
        """One round of work on all jobs. A round includes at most one search per job,
        so uploads of other releases are checked on regularly."""

        with self._lock:
            jobs = [job for job in self.jobs.values() if job.status in JobStatus.ACTIVE
                    or (job.status == JobStatus.PAUSED and self._has_downloads(job))]

        for job in jobs:
            await self._apply_pause(job)
            await self._update_downloads(job)

            if job.status == JobStatus.PAUSED:
                continue

            if job.status == JobStatus.WAITING_RETRY:
                self._retry_when_due(job)

            if job.status == JobStatus.RUNNING:
                await self._work(job)
                self._update_job_status(job)

        self._save()

    async def _apply_pause(self, job):
        """Does what pausing and resuming take, since the core is out of reach where those are asked for."""

        downloading = [item for item in job.items if item.state == ItemState.DOWNLOADING]

        if job.status == JobStatus.PAUSED and job.pause_mode == PauseMode.NOW and not job.transfers_paused:
            for item in downloading:
                source = item.current_source
                await self.runner.call_async(
                    self.runner.automation.cancel_folder_downloads, source["username"], source["folder_path"], False
                )

            job.transfers_paused = True
            self._mark_dirty()

        elif job.status == JobStatus.PAUSED and job.pause_mode == PauseMode.AFTER_ALBUM:
            for item in downloading:
                if item.is_queued:
                    await self._leave_queue(item)

        elif job.status != JobStatus.PAUSED and job.transfers_paused:
            for item in downloading:
                await self._resume_transfers(item)

            job.transfers_paused = False
            self._mark_dirty()

    async def _leave_queue(self, item):
        """A paused job shouldn't have an album start out of the blue when the uploader gets to it."""

        source = item.current_source
        await self._cancel_unfinished(source["username"], source["folder_path"])

        with self._lock:
            entry = [source["username"], source["folder_path"]]

            if entry in item.tried_sources:
                item.tried_sources.remove(entry)

            item.state = ItemState.SOURCED
            item.is_queued = False
            item.current_source = None
            item.note = None
            self._mark_dirty()

    async def _resume_transfers(self, item):

        source = item.current_source
        automation = self.runner.automation
        listing = await self.runner.call_async(
            automation.get_downloads, "all", source["username"], source["folder_path"], 10000
        )

        for download in listing["downloads"]:
            if download["state"] == "paused":
                await self.runner.call_async(automation.retry_download, source["username"], download["virtual_path"])

        with self._lock:
            now = self._clock()
            item.last_progress_at = now
            item.last_update_at = now
            item.speed_check_at = now
            item.speed_check_bytes = sum(download["bytes_transferred"] for download in listing["downloads"])

    @staticmethod
    def _has_downloads(job):
        return any(item.state == ItemState.DOWNLOADING for item in job.items)

    @staticmethod
    def _is_retryable(item):
        """Missing releases may turn up, and uploaders that delivered nothing may come back."""
        return item.state in ItemState.MISSING or (item.state == ItemState.FAILED and not item.num_files_finished)

    def _reset_missing(self, job):

        num_retried = 0

        for item in job.items:
            if self._is_retryable(item):
                item.state = ItemState.PENDING
                item.tried_sources = []
                item.search_rounds = 0
                item.note = None
                num_retried += 1

        return num_retried

    def _retry_when_due(self, job):

        if job.next_retry_at is not None and self._clock() >= job.next_retry_at:
            with self._lock:
                job.retry_rounds += 1
                job.next_retry_at = None
                self._reset_missing(job)
                job.status = JobStatus.RUNNING
                self._mark_dirty()

    async def _work(self, job):

        if not await self._is_connected():
            self._set_blocked(job, "not connected to the Soulseek server")
            return

        root_folder = await self._get_root_folder(job)

        if job.mode == "download" and not self._is_destination_ok(root_folder):
            self._set_blocked(job, f"download folder {root_folder} is not available, is the disk connected?")
            return

        self._set_blocked(job, None)

        if job.mode == "download":
            await self._start_downloads(job)

        await self._source_next(job)

    def _set_blocked(self, job, reason):

        if job.blocked_reason != reason:
            job.blocked_reason = reason
            self._mark_dirty()

    async def _is_connected(self):
        return await self.runner.call_async(self.runner.automation.is_connected)

    async def _get_root_folder(self, job):
        """Keeps a label's releases together in a subfolder of the download folder."""

        if job.download_folder:
            return job.download_folder

        default_folder = (await self.runner.call_async(self.runner.automation.get_status))["download_folder"]
        job.download_folder = os.path.join(default_folder, clean_folder_name(job.name))
        self._mark_dirty()
        return job.download_folder

    async def _before_search(self):
        """Spaces out searches across all jobs, a long burst of them looks like abuse to the server."""

        while True:
            with self._lock:
                interval = max(
                    (job.search_interval for job in self.jobs.values() if job.status == JobStatus.RUNNING), default=0
                )
                remaining = self._last_search_at + interval - self._clock()

                if remaining <= 0 or self._stop_event.is_set():
                    self._last_search_at = self._clock()
                    return

            await anyio.sleep(min(remaining, 1.0))

    async def _load_tracklist(self, item, release):
        """Returns the release with its tracklist, or None when there is nothing to search for."""

        try:
            return await self.sourcer.load_tracklist(release)

        except CatalogError as error:
            if not release.title:
                # Only its ID was known, so there is nothing to search for. retry_missing asks again
                self._finish_item(item, ItemState.NOT_FOUND, release, f"the catalog could not be asked: {error}")
                return None

            # Worth a try anyway, the folder names may be enough to recognize it
            item.note = f"no tracklist: {error}"
            return release

    def _next_pending_item(self, job):

        for item in job.items:
            if item.state == ItemState.PENDING:
                return item

        return None

    def _has_enough_ready(self, job):
        """Don't search far ahead, what was found would be out of date by the time it is its turn.
        Releases waiting for an uploader who is busy may take hours, they don't stop the search."""

        num_ready = sum(1 for other in job.items
                        if other.state == ItemState.SOURCED and self._eligible_sources(job, other)[0])

        return num_ready >= job.max_active_releases * self.MAX_READY_BACKLOG_FACTOR

    async def _source_next(self, job):

        item = self._next_pending_item(job)

        if item is None:
            return

        if job.mode == "download" and self._has_enough_ready(job):
            return

        release = await self._load_tracklist(item, Release.from_dict(item.release))

        if release is None:
            return

        if not self.sourcer.catalog.is_on_label(release, job.label_id):
            self._finish_item(item, ItemState.NOT_ON_LABEL, release, "the catalog credits this release to other labels")
            return

        if job.skip_owned and self.library is not None:
            local_path = await anyio.to_thread.run_sync(self.library.find_release, release, job.formats)

            if local_path:
                item.local_path = local_path
                self._finish_item(item, ItemState.OWNED, release, "already on disk")
                return

        formats = job.formats

        try:
            result = await self.sourcer.find_sources(
                release, formats=formats, prefer=job.prefer, min_bitrate=job.min_bitrate,
                wait_seconds=job.wait_seconds, before_search=self._before_search
            )

        except ToolError as error:
            # Probably disconnected. Stays pending, and the job waits for the connection to return
            self._set_blocked(job, str(error))
            item.release = release.to_dict()
            self._mark_dirty()
            return

        if job.formats != formats:
            # FLAC only was chosen during the search, what it found may be anything. Stays pending
            item.release = release.to_dict()
            self._mark_dirty()
            return

        item.search_rounds += 1
        item.sources = result["sources"]
        item.partial_sources = result["partial"]
        queries = ", ".join(f"'{search['query']}'" for search in result["searches"])

        if item.sources:
            self._finish_item(item, ItemState.SOURCED, release)

        elif item.partial_sources and job.accept_partial:
            item.sources = item.partial_sources
            self._finish_item(item, ItemState.SOURCED, release, "only an incomplete copy was found")

        elif item.partial_sources:
            self._finish_item(item, ItemState.PARTIAL, release, f"only incomplete copies found with {queries}")

        else:
            self._finish_item(item, ItemState.NOT_FOUND, release, f"nothing found with {queries}")

    def _finish_item(self, item, state, release, note=None):

        with self._lock:
            item.state = state
            item.release = release.to_dict()
            item.note = note or item.note
            self._mark_dirty()

    def _active_downloads_by_user(self):

        counts = {}

        for job in self.jobs.values():
            for item in job.items:
                if item.state == ItemState.DOWNLOADING and item.current_source:
                    username = item.current_source["username"]
                    counts[username] = counts.get(username, 0) + 1

        return counts

    async def _start_downloads(self, job):

        for item in job.items:
            if job.status != JobStatus.RUNNING:
                return

            if item.state != ItemState.SOURCED:
                continue

            # Waiting in somebody's queue takes no bandwidth, it shouldn't hold up other releases
            num_active = sum(1 for other in job.items if other.state == ItemState.DOWNLOADING and not other.is_queued)

            if num_active >= job.max_active_releases:
                return

            await self._start_item(job, item)

    @staticmethod
    def _has_wanted_format(job, source):
        """Sources found before a job became FLAC only, or that were enqueued file by file in a race."""

        probe_file = source.get("probe_file")

        if probe_file and not is_file_wanted(probe_file["basename"], job.formats):
            return False

        wanted_formats = normalize_formats(job.formats)
        source_formats = source.get("formats")

        return not wanted_formats or not source_formats or bool(set(wanted_formats) & set(source_formats))

    @staticmethod
    def _count_wanted_tracks(job, basenames):
        return sum(1 for basename in basenames
                   if is_audio_extension(get_extension(basename)) and is_file_wanted(basename, job.formats))

    def _has_enough_tracks(self, job, item, downloads):
        """A folder can finish without holding the album: cover art and text files count as files too,
        and the formats asked for may rule out most of what is there. Partial copies are fine if asked for."""

        if job.accept_partial:
            return True

        basenames = [*item.done_files, *(download["virtual_path"].rpartition("\\")[-1] for download in downloads)]
        num_expected = len(item.release.get("tracks") or []) or 1

        return self._count_wanted_tracks(job, basenames) >= num_expected

    def _eligible_sources(self, job, item):
        """Returns the sources not tried yet, one per uploader, and whether others
        are only waiting until their uploader has finished another release."""

        active_by_user = self._active_downloads_by_user()
        sources = []
        usernames = set()
        is_waiting = False

        for source in item.sources:
            username = source["username"]

            if [username, source["folder_path"]] in item.tried_sources or not self._has_wanted_format(job, source):
                continue

            if active_by_user.get(username, 0) >= job.max_per_uploader:
                is_waiting = True
                continue

            if username not in usernames:
                usernames.add(username)
                sources.append(source)

        return sources, is_waiting

    async def _start_item(self, job, item):

        queued_sources = []

        while True:
            sources, is_waiting = self._eligible_sources(job, item)

            if not sources:
                # Nobody is free. Queues are normal on Soulseek, so get in line rather than give up
                for source in queued_sources:
                    if await self._enqueue_folder(job, item, source, is_queued=True):
                        return

                if not is_waiting:
                    self._sources_exhausted(item)

                return

            sources = await self.sourcer.pick_sources(sources)
            winner, results = await self.sourcer.race_sources(sources, job.download_folder)
            failed_usernames = {result["username"] for result in results if result["state"] in {"failed", "waiting"}}
            waiting_usernames = {result["username"] for result in results if result["state"] == "waiting"}

            with self._lock:
                item.last_race = results

                # Those who lost a race are fine as a fallback, those who didn't even start are not
                for source in sources:
                    if source is winner or source["username"] in failed_usernames:
                        item.tried_sources.append([source["username"], source["folder_path"]])

                self._mark_dirty()

            if job.status != JobStatus.RUNNING:
                await self._abandon_race(item, sources, winner)
                return

            queued_sources.extend(source for source in sources if source["username"] in waiting_usernames)

            if winner is None:
                item.note = "none of the uploaders started in time"
                continue

            if await self._enqueue_folder(job, item, winner):
                return

    async def _abandon_race(self, item, sources, winner):
        """Paused while a race was on, so whoever won doesn't get to start. All of them get another chance."""

        if winner is not None:
            await self._cancel_unfinished(winner["username"], winner["folder_path"])

        with self._lock:
            usernames = {source["username"] for source in sources}
            item.tried_sources = [entry for entry in item.tried_sources if entry[0] not in usernames]
            self._mark_dirty()

    async def _enqueue_folder(self, job, item, source, is_queued=False):
        """Requests a whole folder for a release. Returns False if that didn't work."""

        try:
            result = await self.runner.request_folder_download(
                source["username"], source["folder_path"], job.download_folder, job.audio_only, job.formats
            )

        except (ToolError, TimeoutError, RuntimeError) as error:
            item.note = f"{source['username']}: {error}"
            self._mark_dirty()
            return False

        with self._lock:
            now = self._clock()
            item.state = ItemState.DOWNLOADING
            item.is_queued = is_queued
            item.current_source = source
            item.download_folder = result.get("download_folder")
            item.started_at = now
            item.last_progress_at = now
            item.last_update_at = now
            item.speed_check_at = now
            item.speed_check_bytes = 0
            item.last_bytes = 0
            item.current_speed = 0
            item.done_files = []
            item.num_files = result.get("num_files", 0)
            item.num_files_finished = 0
            item.progress_percent = 0.0
            item.note = result.get("warning") or (f"waiting in {source['username']}'s queue" if is_queued else None)
            self._mark_dirty()

        return True

    async def _switch_uploader(self, job, item, finished_basenames, current_speed):
        """Moves the tracks still missing to a faster uploader. What has arrived stays, and the new
        files go to the same folder. Returns False if nobody better has them."""

        automation = self.runner.automation
        release = Release.from_dict(item.release)
        sources, _is_waiting = self._eligible_sources(job, item)
        ranked_sources = []

        for source in sources:
            estimate = await self.runner.call_async(automation.get_uploader_speed, source["username"])

            # Not measured yet is worth a try, known to be no better isn't
            if not estimate or estimate >= current_speed * self.SWITCH_FASTER_FACTOR:
                ranked_sources.append((estimate, source))

        ranked_sources.sort(key=lambda entry: -entry[0])
        done_basenames = [*item.done_files, *finished_basenames]

        for _estimate, source in ranked_sources:
            username = source["username"]
            folder_path = source["folder_path"]
            item.tried_sources.append([username, folder_path])

            try:
                listing = await self.runner.request_folder_listing(username, folder_path)

                if "error" in listing:
                    continue

                wanted = set(select_remaining_files(
                    release, done_basenames, [file_data["basename"] for file_data in listing["files"]
                                              if is_file_wanted(file_data["basename"], job.formats)]
                ))
                files = [file_data for file_data in listing["files"] if file_data["basename"] in wanted]
                num_missing = len(release.tracks or []) - len(done_basenames)

                # This copy has to have everything that is missing, otherwise it is no improvement
                if not files or len(files) < num_missing:
                    continue

                previous_source = item.current_source
                await self.runner.call_async(
                    automation.enqueue_files, username, folder_path, files, item.download_folder
                )

            except (ToolError, TimeoutError, RuntimeError):
                continue

            await self._cancel_unfinished(previous_source["username"], previous_source["folder_path"])

            with self._lock:
                now = self._clock()
                item.note = f"switched from {previous_source['username']} to {username} for the remaining tracks"
                item.current_source = source
                item.done_files = done_basenames
                item.last_progress_at = now
                item.last_update_at = now
                item.speed_check_at = now
                item.speed_check_bytes = 0
                item.last_bytes = 0
                item.current_speed = 0
                self._mark_dirty()

            return True

        return False

    def _sources_exhausted(self, item):
        """Every known source failed. They may have been found long ago, so look again once."""

        with self._lock:
            item.current_source = None

            if item.search_rounds < self.MAX_SEARCH_ROUNDS:
                item.state = ItemState.PENDING
                item.sources = []
            else:
                item.state = ItemState.FAILED
                item.note = item.note or "no uploader delivered this release"

            self._mark_dirty()

    async def _cancel_unfinished(self, username, folder_path):

        automation = self.runner.automation
        listing = await self.runner.call_async(automation.get_downloads, "all", username, folder_path, 10000)

        for download in listing["downloads"]:
            if download["state"] != "finished":
                await self.runner.call_async(automation.discard_download, username, download["virtual_path"])

    async def _update_downloads(self, job):

        items = [item for item in job.items if item.state == ItemState.DOWNLOADING]

        if not items:
            return

        if not await self._is_connected():
            # Nothing can transfer while offline, that doesn't make an uploader unreliable
            with self._lock:
                for item in items:
                    item.last_progress_at = self._clock()

            return

        for item in items:
            await self._update_item(job, item)

    async def _update_item(self, job, item):

        source = item.current_source
        listing = await self.runner.call_async(
            self.runner.automation.get_downloads, "all", source["username"], source["folder_path"], 10000
        )
        downloads = listing["downloads"]
        now = self._clock()
        action = None

        with self._lock:
            if item.state != ItemState.DOWNLOADING:
                return

            if not downloads:
                item.note = "the downloads were removed"
                item.state = ItemState.CANCELLED
                item.current_source = None
                self._mark_dirty()
                return

            states = [download["state"] for download in downloads]
            total_bytes = sum(download["bytes_transferred"] for download in downloads)
            total_size = sum(download["size_bytes"] for download in downloads)
            num_finished = states.count("finished")
            num_earlier = len(item.done_files)
            num_files = num_earlier + len(states)
            fraction = 1.0 if num_finished == len(states) else (total_bytes / total_size if total_size else 0.0)
            progress = round((num_earlier + fraction * len(states)) / num_files * 100, 1)

            if (num_earlier + num_finished, progress, num_files) != (item.num_files_finished, item.progress_percent,
                                                                     item.num_files):
                item.num_files_finished = num_earlier + num_finished
                item.progress_percent = progress
                item.num_files = num_files
                self._mark_dirty()

            if item.last_update_at is not None and now > item.last_update_at:
                item.current_speed = round(max(0, total_bytes - item.last_bytes) / (now - item.last_update_at))

            item.last_update_at = now

            if item.is_queued and (total_bytes or "active" in states or "finished" in states):
                item.is_queued = False
                item.note = None
                item.last_progress_at = now
                item.speed_check_at = now
                item.speed_check_bytes = total_bytes
                self._mark_dirty()

            if total_bytes != item.last_bytes:
                item.last_bytes = total_bytes
                item.last_progress_at = now
                self._mark_dirty()

            has_finished = num_finished == len(states)

            if has_finished and self._has_enough_tracks(job, item, downloads):
                item.state = ItemState.FINISHED
                item.local_path = item.download_folder
                item.current_speed = 0
                item.note = None
                self._mark_dirty()

                if self.library is not None:
                    self.library.invalidate()

                return

            # Failures the core retries by itself are not final yet
            is_retrying = any(download["will_retry"] for download in downloads if download["state"] == "failed")
            is_settled = all(state in {"finished", "failed"} for state in states) and not is_retrying
            is_paused_by_user = "paused" in states and "active" not in states
            timeout = self.QUEUE_TIMEOUT if item.is_queued else job.start_timeout
            is_stalled = not is_paused_by_user and now - item.last_progress_at >= timeout

            if has_finished:
                action = "too_few_tracks"

            elif is_settled or is_stalled:
                # What has arrived is worth keeping, starting over would only be needed with nothing
                action = "keep_finished" if num_finished or item.done_files else "start_over"

            elif "active" in states and self._is_too_slow(job, item, total_bytes, now):
                action = "too_slow"

            finished_basenames = [download["virtual_path"].rpartition("\\")[-1] for download in downloads
                                  if download["state"] == "finished"]

        if action is not None:
            await self._act_on_problem(job, item, action, finished_basenames, len(states) - num_finished, len(states))

    async def _act_on_problem(self, job, item, action, finished_basenames, num_unfinished, num_files):

        source = item.current_source

        if action == "too_slow":
            await self._switch_uploader(job, item, finished_basenames, item.current_speed)

        elif action == "keep_finished":
            if not await self._switch_uploader(job, item, finished_basenames, 0):
                with self._lock:
                    item.state = ItemState.FAILED
                    item.note = f"{num_unfinished} of {num_files} files did not arrive from {source['username']}"
                    self._mark_dirty()

                await self._cancel_unfinished(source["username"], source["folder_path"])

        elif action == "too_few_tracks":
            with self._lock:
                item.note = f"{source['username']}: the folder didn't hold the whole release, trying another uploader"
                item.state = ItemState.SOURCED
                item.is_queued = False
                item.current_source = None
                item.num_files_finished = 0
                item.progress_percent = 0.0
                self._mark_dirty()

        elif action == "start_over":
            with self._lock:
                item.note = f"{source['username']}: transfers stalled or failed, trying another uploader"
                item.state = ItemState.SOURCED
                item.is_queued = False
                item.current_source = None
                self._mark_dirty()

            await self.runner.call_async(self.runner.automation.record_speed_failure, source["username"])
            await self._cancel_unfinished(source["username"], source["folder_path"])

    def _is_too_slow(self, job, item, total_bytes, now):
        """Looks at a whole minute, a slow moment is normal. Near the end, switching isn't worth it."""

        if item.speed_check_at is None:
            item.speed_check_at = now
            item.speed_check_bytes = total_bytes
            return False

        elapsed = now - item.speed_check_at

        if elapsed < self.SPEED_CHECK_SECONDS:
            return False

        speed = (total_bytes - item.speed_check_bytes) / elapsed
        item.speed_check_at = now
        item.speed_check_bytes = total_bytes
        item.current_speed = round(max(0, speed))

        return speed < job.min_speed and item.progress_percent < self.SWITCH_MAX_PROGRESS

    def _update_job_status(self, job):

        with self._lock:
            states = {item.state for item in job.items}

            if job.mode == "preview":
                if ItemState.PENDING not in states:
                    job.status = JobStatus.PREVIEWED
                    job.finished_at = self._clock()
                    self._mark_dirty()

                return

            if not states <= ItemState.DONE:
                return

            has_missing = any(self._is_retryable(item) for item in job.items)

            if has_missing and job.retry_missing_hours > 0 and job.retry_rounds < job.max_retry_rounds:
                job.status = JobStatus.WAITING_RETRY
                job.next_retry_at = self._clock() + job.retry_missing_hours * 3600
            else:
                job.status = JobStatus.FINISHED
                job.finished_at = self._clock()

            self._mark_dirty()
