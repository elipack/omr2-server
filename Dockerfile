# omr2-server — homr OMR pipeline behind a FastAPI wrapper.
# Build for Cloud Run (linux/amd64):
#   docker build --platform linux/amd64 -t omr2-server .
# or let Cloud Build do it: gcloud run deploy --source . (see deploy/deploy.sh)

FROM python:3.11-slim

# git: pip install from GitHub. libglib2.0-0 + libgomp1: runtime deps of
# opencv-python-headless / onnxruntime wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1

# Pin the exact upstream SHA the OMR2 project validates against (tag v0.7.0).
ARG HOMR_SHA=8b5dcf7d7bdd1a47911dc0c661c573b957271eab
ENV HOMR_SHA=${HOMR_SHA}

RUN pip install "homr @ git+https://github.com/liebharc/homr.git@${HOMR_SHA}"

# Bake the ONNX models (~122 MB) + OCR weights into the image at build time.
# homr downloads into its installed package directory; doing this here means
# cold starts never touch the network. In a container build there is no GPU,
# so --init fetches the fp32 CPU models — exactly what runtime will use.
RUN homr --init

RUN pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" python-multipart

WORKDIR /srv
COPY app /srv/app

# Cloud Run injects PORT; default to 8080 for local runs.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
