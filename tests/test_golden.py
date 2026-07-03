"""Golden parity tests against a running omr2-server instance.

These hit a live server (local `uvicorn` or the deployed Cloud Run URL) with
the OMR2 project's reference photos and assert the same counts the Swift
regression suite locks in (OMR2 PipelineTests):

  Twinkle twinkle photo.jpeg          -> 86 notes / 12 measures / 3 systems
  Twinkle v3 1of2.jpeg + 2of2.jpeg    -> stitched 13 measures / 44 notes

Usage:
  SERVER_URL=http://127.0.0.1:8080 \
  OMR2_FIXTURES="/Users/elipack/Documents/Code/OMR2" \
  python -m pytest tests/test_golden.py -v

Env:
  SERVER_URL     base URL (default http://127.0.0.1:8080)
  OMR2_FIXTURES  directory containing the reference JPEGs (required)
  OMR2_TOKEN     sent as X-OMR2-Token when set
"""

import os
from pathlib import Path

import pytest
import requests

SERVER = os.environ.get("SERVER_URL", "http://127.0.0.1:8080")
FIXTURES = os.environ.get("OMR2_FIXTURES", "")
TIMEOUT = 400  # cold model load + full page on a small cloud CPU


def _headers() -> dict:
    token = os.environ.get("OMR2_TOKEN")
    return {"X-OMR2-Token": token} if token else {}


def _transcribe(image_path: Path) -> dict:
    with open(image_path, "rb") as f:
        r = requests.post(
            f"{SERVER}/transcribe",
            files={"image": (image_path.name, f, "image/jpeg")},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:500]}"
    return r.json()


def _fixture(name: str) -> Path:
    if not FIXTURES:
        pytest.skip("OMR2_FIXTURES not set")
    p = Path(FIXTURES) / name
    if not p.exists():
        pytest.skip(f"missing fixture {p}")
    return p


def test_healthz() -> None:
    r = requests.get(f"{SERVER}/healthz", timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_source_offer() -> None:
    r = requests.get(f"{SERVER}/source", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["license"] == "AGPL-3.0"
    assert "github.com" in body["upstream"]["source"]


def test_twinkle_golden() -> None:
    result = _transcribe(_fixture("Twinkle twinkle photo.jpeg"))
    assert result["notes"] == 86, result
    assert result["measures"] == 12, result
    assert result["systems"] == 3, result
    assert result["musicxml"].lstrip().startswith("<?xml")


def test_multipage_stitch_golden() -> None:
    page1 = _transcribe(_fixture("Twinkle v3 1of2.jpeg"))
    page2 = _transcribe(_fixture("Twinkle v3 2of2.jpeg"))
    r = requests.post(
        f"{SERVER}/stitch",
        json={"pages": [page1["musicxml"], page2["musicxml"]]},
        headers=_headers(),
        timeout=60,
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:500]}"
    stitched = r.json()
    assert stitched["measures"] == 13, stitched
    assert stitched["notes"] == 44, stitched
