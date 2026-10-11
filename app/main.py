"""omr2-server — a thin HTTP wrapper around the homr OMR pipeline.

Runs the stock `homr` CLI (pinned upstream SHA, see HOMR_SHA) as a subprocess
per request: JPEG/PNG in, MusicXML out. Stateless by design — the uploaded
image lives in a per-request temp directory that is deleted before the
response is sent, and only metadata (sizes, timings, counts) is logged.

This wrapper imports nothing from homr; the AGPL-3.0 boundary is the CLI.
The full source of this service is offered at GET /source (AGPL §13).

Environment:
    OMR2_TOKEN          shared secret; when set, POST endpoints require the
                        X-OMR2-Token header to match (constant-time compare).
                        Unset = auth disabled (local development).
    HOMR_CMD            homr executable (default: "homr" on PATH).
    HOMR_TIMEOUT_S      per-page pipeline timeout (default: 280).
    RATE_LIMIT_PER_HOUR per-IP transcribe limit (default: 30, in-memory,
                        per-instance — a deterrent, not a guarantee).
    MAX_UPLOAD_MB       upload size cap (default: 25; Cloud Run caps at 32).
    HOMR_SHA            upstream SHA baked in by the Dockerfile, reported by
                        /health and /source.
"""

import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("omr2-server")

HOMR_CMD = os.environ.get("HOMR_CMD", "homr")
HOMR_TIMEOUT_S = int(os.environ.get("HOMR_TIMEOUT_S", "280"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024
RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "30"))
HOMR_SHA = os.environ.get("HOMR_SHA", "unknown")
SERVER_VERSION = "0.1.0"

UPSTREAM_URL = "https://github.com/liebharc/homr"
WRAPPER_URL = "https://github.com/elipack/omr2-server"

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png"}

app = FastAPI(title="omr2-server", version=SERVER_VERSION, docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# Auth + rate limiting
# ---------------------------------------------------------------------------

_rate: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Cloud Run terminates TLS and sets X-Forwarded-For: <client>, <lb>.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_auth(request: Request) -> None:
    token = os.environ.get("OMR2_TOKEN")
    if not token:
        return  # auth disabled (local dev)
    supplied = request.headers.get("x-omr2-token", "")
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="bad or missing X-OMR2-Token")


def _check_rate(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    window = _rate[ip]
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="rate limit exceeded, try later")
    window.append(now)


# ---------------------------------------------------------------------------
# MusicXML helpers
# ---------------------------------------------------------------------------

MUSICXML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
    '"http://www.musicxml.org/dtds/partwise.dtd">\n'
)


def _summarize(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    notes = [n for n in root.iter("note") if n.find("rest") is None]
    measures = root.findall(".//part[1]/measure")
    title_el = root.find(".//work/work-title")
    if title_el is None:
        title_el = root.find(".//movement-title")
    title = (title_el.text or "").strip() if title_el is not None else ""
    return {"notes": len(notes), "measures": len(measures), "title": title}


def _stitch(pages: list[str]) -> tuple[str, list[str]]:
    """Concatenate the measures of subsequent pages onto page 1, per part.

    Parts are matched by index. Restated <attributes> on page boundaries are
    kept — restating clef/key/time mid-score is valid MusicXML and renders
    fine. Measures are renumbered sequentially per part.
    """
    warnings: list[str] = []
    roots = [ET.fromstring(p) for p in pages]
    base = roots[0]
    base_parts = base.findall("part")
    for page_idx, other in enumerate(roots[1:], start=2):
        other_parts = other.findall("part")
        if len(other_parts) != len(base_parts):
            warnings.append(
                f"page {page_idx} has {len(other_parts)} parts, "
                f"page 1 has {len(base_parts)}; stitching by index over the overlap"
            )
        for part, other_part in zip(base_parts, other_parts):
            for measure in other_part.findall("measure"):
                part.append(measure)
    for part in base_parts:
        for i, measure in enumerate(part.findall("measure"), start=1):
            measure.set("number", str(i))
    body = ET.tostring(base, encoding="unicode")
    return MUSICXML_HEADER + body, warnings


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


# Named "/health", not "/healthz" — Cloud Run's GFE edge intercepts the
# literal path "/healthz" and returns its own 404 before the request ever
# reaches this container.
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "server": SERVER_VERSION, "homr_sha": HOMR_SHA}


@app.get("/source")
def source() -> dict:
    """AGPL §13 corresponding-source offer."""
    return {
        "license": "AGPL-3.0",
        "service": {"name": "omr2-server", "version": SERVER_VERSION, "source": WRAPPER_URL},
        "upstream": {"name": "homr", "sha": HOMR_SHA, "source": UPSTREAM_URL},
        "notice": "This service runs the homr OMR pipeline. You may obtain, "
        "modify, and self-host the complete corresponding source at the URLs above.",
    }


@app.get("/")
def index() -> dict:
    return source()


@app.post("/transcribe")
async def transcribe(request: Request, image: UploadFile) -> JSONResponse:
    _check_auth(request)
    _check_rate(request)

    suffix = Path(image.filename or "page.jpg").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        suffix = ".jpg"

    data = await image.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"image exceeds {MAX_UPLOAD_BYTES} bytes")
    if not data:
        raise HTTPException(status_code=422, detail="empty upload")

    started = time.monotonic()
    workdir = tempfile.mkdtemp(prefix="omr2-")
    try:
        img_path = os.path.join(workdir, f"page{suffix}")
        with open(img_path, "wb") as f:
            f.write(data)

        try:
            proc = subprocess.run(
                [HOMR_CMD, img_path],
                capture_output=True,
                text=True,
                timeout=HOMR_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="pipeline timed out")

        stderr_tail = proc.stderr[-2000:] if proc.stderr else ""
        xml_path = os.path.join(workdir, "page.musicxml")
        if proc.returncode != 0 or not os.path.exists(xml_path):
            if "No staffs found" in stderr_tail or "No noteheads found" in stderr_tail:
                raise HTTPException(status_code=422, detail="no_staves_detected")
            log.error("homr failed rc=%d stderr=%s", proc.returncode, stderr_tail)
            raise HTTPException(status_code=500, detail="pipeline_failed")

        xml_text = Path(xml_path).read_text(encoding="utf-8")
        summary = _summarize(xml_text)

        # "Found 3 connected staffs (after merging grand staffs, multiple
        # voices):  [1, 1, 1]" — the count is the number of systems (grand
        # staves) on the page. The per-staff total isn't exposed on stderr in
        # homr v0.7.0, so we report systems and let notes/measures carry the
        # accuracy signal.
        systems = 0
        m = re.search(r"Found (\d+) connected staffs", proc.stderr or "")
        if m:
            systems = int(m.group(1))

        elapsed = time.monotonic() - started
        log.info(
            "transcribe ok: %d bytes in, %d notes, %d measures, %d systems, %.1fs",
            len(data), summary["notes"], summary["measures"], systems, elapsed,
        )
        return JSONResponse(
            {
                "musicxml": xml_text,
                "title": summary["title"],
                "systems": systems,
                "notes": summary["notes"],
                "measures": summary["measures"],
                "elapsed_s": round(elapsed, 2),
            }
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/stitch")
async def stitch(request: Request) -> JSONResponse:
    _check_auth(request)
    body = await request.json()
    pages = body.get("pages")
    if not isinstance(pages, list) or not pages or not all(isinstance(p, str) for p in pages):
        raise HTTPException(status_code=422, detail="body must be {\"pages\": [musicxml, ...]}")
    if len(pages) == 1:
        stitched, warnings = pages[0], []
    else:
        try:
            stitched, warnings = _stitch(pages)
        except ET.ParseError as e:
            raise HTTPException(status_code=422, detail=f"invalid MusicXML: {e}")
    summary = _summarize(stitched)
    log.info("stitch ok: %d pages -> %d measures, %d notes", len(pages),
             summary["measures"], summary["notes"])
    return JSONResponse(
        {
            "musicxml": stitched,
            "notes": summary["notes"],
            "measures": summary["measures"],
            "warnings": warnings,
        }
    )
