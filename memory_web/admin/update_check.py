"""Once-a-day check for a newer feature release (second version number), never blocking a page."""

import logging
import os
import re
import threading
import time

import httpx

from ..version import VERSION

LOG = logging.getLogger("memory_web.update_check")
REPOSITORY = "lixiaomeng-eng/amber-heart-memory"
TAGS_URL = f"https://api.github.com/repos/{REPOSITORY}/tags?per_page=100"
RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/v{{version}}"
NOTES_LIMIT = 4000
CHANGELOG_URL = f"https://github.com/{REPOSITORY}/blob/main/CHANGELOG.md"
INTERVAL_SECONDS = 24 * 3600
RETRY_SECONDS = 3600
TAG = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def chinese_notes(body):
    """The Chinese half of a bilingual Release note: everything above the first `---` line."""
    lines = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.strip() == "---":
            break
        lines.append(line)
    return "\n".join(lines).strip()[:NOTES_LIMIT] or None


def parse(text):
    match = TAG.fullmatch(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


class UpdateChecker:
    def __init__(self, enabled=None, fetch=None, fetch_notes=None, clock=time.monotonic):
        self.enabled = (os.environ.get("MEMORY_UPDATE_CHECK", "true").lower() != "false") if enabled is None else enabled
        self.current = parse(VERSION)
        self.fetch = fetch or self._fetch
        self.fetch_notes = fetch_notes or self._fetch_notes
        self.clock = clock
        self.latest = None
        self.notes = None
        self.next_check = 0.0
        self.lock = threading.Lock()
        self.running = False

    @staticmethod
    def _fetch():
        response = httpx.get(TAGS_URL, timeout=10, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        return [item.get("name") for item in response.json() if isinstance(item, dict)]

    @staticmethod
    def _fetch_notes(version):
        response = httpx.get(RELEASE_URL.format(version=version), timeout=10, headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:  # Tag pushed, Release not written (yet).
            return None
        response.raise_for_status()
        return response.json().get("body")

    def _refresh(self):
        try:
            versions = [v for v in (parse(name) for name in self.fetch()) if v]
            latest = max(versions) if versions else None
            notes = None
            if latest and self.current and latest[:2] > self.current[:2]:
                try:
                    notes = chinese_notes(self.fetch_notes("%d.%d.%d" % latest))
                except Exception as exc:
                    LOG.info("update_notes_failed type=%s", type(exc).__name__)
            with self.lock:
                self.latest = latest
                self.notes = notes
                self.next_check = self.clock() + INTERVAL_SECONDS
        except Exception as exc:  # Unreachable GitHub (common on mainland servers) is not an error page.
            LOG.info("update_check_failed type=%s", type(exc).__name__)
            with self.lock:
                self.next_check = self.clock() + RETRY_SECONDS
        finally:
            with self.lock:
                self.running = False

    def status(self):
        """Cached result; starts a background refresh when stale."""
        if not self.enabled or not self.current:
            return None
        with self.lock:
            if not self.running and self.clock() >= self.next_check:
                self.running = True
                threading.Thread(target=self._refresh, daemon=True).start()
            latest, notes = self.latest, self.notes
        if not latest or latest[:2] <= self.current[:2]:
            return None
        return dict(current=VERSION, latest="%d.%d.%d" % latest, notes=notes, changelog=CHANGELOG_URL)
