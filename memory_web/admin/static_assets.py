"""Admin page files: content-derived version, gzip and long browser caching.

Files are read once at startup. Every `?v=` reference inside the HTML, JS, CSS
and manifest is rewritten to one version derived from all file contents, so a
changed file always gets a new URL and versioned URLs can be cached for a year.
The page itself is never cached, so it always points at the current version.
"""

import gzip
import hashlib
import mimetypes
from pathlib import Path
import re

from fastapi.responses import Response

REFERENCE = re.compile(rb"(\.(?:js|css|png|svg|webmanifest))\?v=[\w.-]+")
TEXT = {".html", ".js", ".css", ".svg", ".webmanifest"}
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}
YEAR = "public, max-age=31536000, immutable"


class StaticAssets:
    def __init__(self, directory: Path):
        files = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file() and not path.name.startswith(".")
        }
        digest = hashlib.sha256()
        for name, body in files.items():
            digest.update(name.encode() + b"\0" + body + b"\0")
        self.version = digest.hexdigest()[:12]
        self.files = {}
        for name, body in files.items():
            suffix = Path(name).suffix
            if suffix in TEXT:
                body = REFERENCE.sub(rb"\1?v=" + self.version.encode(), body)
            packed = gzip.compress(body, 9, mtime=0) if suffix in TEXT and len(body) > 1024 else None
            media = TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.files[name] = (body, packed, media, hashlib.sha256(body).hexdigest()[:16])

    def _send(self, request, name, cache):
        entry = self.files.get(name)
        if entry is None:
            return Response(status_code=404)
        body, packed, media, tag = entry
        accepted = request.headers.get("accept-encoding", "")
        compressed = packed is not None and "gzip" in accepted
        etag = f'"{tag}{"-gz" if compressed else ""}"'
        headers = {"Cache-Control": cache, "ETag": etag, "Vary": "Accept-Encoding"}
        if etag in [value.strip() for value in request.headers.get("if-none-match", "").split(",")]:
            return Response(status_code=304, headers=headers)
        if compressed:
            headers["Content-Encoding"] = "gzip"
        return Response(packed if compressed else body, media_type=media, headers=headers)

    def static(self, request, name):
        current = request.query_params.get("v") == self.version
        return self._send(request, name, YEAR if current else "no-cache")

    def page(self, request):
        return self._send(request, "index.html", "no-store")
