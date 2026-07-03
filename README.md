# omr2-server

A thin, stateless HTTP wrapper around [homr](https://github.com/liebharc/homr) —
an optical music recognition (OMR) pipeline that converts a photo of printed
sheet music into MusicXML. Upload a JPEG, get MusicXML back.

This service exists as the backend for the OMR2 iOS app. It runs **stock,
unmodified homr** (pinned to `8b5dcf7d`, tag `v0.7.0`) as a subprocess; the
wrapper is ~250 lines of FastAPI.

## License

**AGPL-3.0**, inherited from homr. The complete corresponding source of the
running service is this repository plus upstream homr at the pinned SHA. A
running instance offers the same information at `GET /source`.

You are free to self-host: everything needed is in the Dockerfile.

## API

| Endpoint | Description |
|---|---|
| `POST /transcribe` | multipart field `image` (JPEG/PNG, ≤25 MB) → `{musicxml, title, staffs, notes, measures, elapsed_s}`. One page per request; expect 30–90 s. |
| `POST /stitch` | `{"pages": ["<musicxml>", ...]}` → `{musicxml, notes, measures, warnings}` — concatenates multi-page transcriptions into one document. |
| `GET /healthz` | liveness + versions |
| `GET /source` | AGPL §13 corresponding-source offer |

Errors: `401` bad token · `413` upload too large · `422 no_staves_detected` ·
`429` rate-limited · `504` pipeline timeout.

When the `OMR2_TOKEN` env var is set, `POST` requests must carry a matching
`X-OMR2-Token` header.

## Run locally

With Docker:

```sh
docker build -t omr2-server .
docker run -p 8080:8080 omr2-server
curl -F "image=@sheet.jpg" http://127.0.0.1:8080/transcribe
```

Without Docker (needs a Python 3.11 env with homr installed, e.g. a checkout's
poetry venv):

```sh
pip install fastapi 'uvicorn[standard]' python-multipart
HOMR_CMD=/path/to/homr-venv/bin/homr uvicorn app.main:app --port 8080
```

## Deploy (Google Cloud Run)

See `deploy/deploy.sh` — one-time GCP setup steps are in its header comment.
Summary: scale-to-zero, 2 vCPU / 2 GiB, concurrency 1, max 3 instances,
300 s timeout. At low volume this fits inside Cloud Run's always-free tier;
set a budget alert anyway.

## Privacy

Stateless by construction: uploaded images are written to a per-request temp
directory, processed, and deleted before the response returns. No object
storage, no database, no image bytes in logs — only sizes, timings, and
result counts.

## Tests

Golden parity tests (require the OMR2 reference photos and a running server):

```sh
SERVER_URL=http://127.0.0.1:8080 OMR2_FIXTURES=/path/to/OMR2 \
  python -m pytest tests/ -v
```

The counts asserted (86 notes / 6 staffs on the reference page; 13 measures /
44 notes stitched) match the OMR2 iOS app's on-device regression baseline,
so server and app provably transcribe identically.
