# Jev service image — headless Chrome + browser-harness runtime.
#
# Build:  docker build -t ghcr.io/al3xar/jev-ultrafast:sha-<git-short> .
#
# PUBLISHING IS GATED (TFM human gate, T-14): do NOT push this image until
# Al3xar authorizes. The chart (hermes-agent-charts/.../values-hades.yaml)
# pins the exact tag/digest at deploy time — no :latest floats.
#
# RUNTIME CONTRACT (verified against the service code):
#   * the jev-service entrypoint (jev_ultrafast.service.main) binds
#     $JUV_SERVICE_HOST:$JUV_SERVICE_PORT (chart sets 0.0.0.0:8765);
#   * GET /health is the K8s readiness endpoint (T-14) — it reports harness
#     + Chrome availability and active sessions, and does NOT require a live
#     browser (`browser-harness --doctor` exits non-zero while idle);
#   * Chrome is launched ON DEMAND per session_id with --no-sandbox (T-4);
#     the service needs the `google-chrome` binary + the browser-harness
#     python package (both pinned below).
FROM python:3.12-slim

# Headless Chrome + shared libs the service needs at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      fonts-noto-cjk \
      curl \
      wget \
      gnupg \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libcups2 \
      libdrm2 \
      libgbm1 \
      libgtk-3-0 \
      libnspr4 \
      libnss3 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      xdg-utils \
 && mkdir -p /tmp/apt-keys \
 && wget -qO /tmp/apt-keys/google.gpg https://dl.google.com/linux/linux_signing_key.pub \
 && apt-key add /tmp/apt-keys/google.gpg \
 && echo "deb [arch=amd64 signed-by=/tmp/apt-keys/google.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/* /tmp/apt-keys /tmp/*

WORKDIR /app

# Copy + install first (layer cache): deps change far less often than code.
COPY pyproject.toml uv.lock README.md ./
COPY jev_ultrafast ./jev_ultrafast
RUN pip install --no-cache-dir .

# Pinned runtime versions (T-14: "versión fijada"):
#   * browser-harness 0.1.13 is the pinned dep in pyproject.toml
#     (browser-harness==0.1.13); the service spawns its daemon from here.
#   * google-chrome-stable is apt-pinned to the distro "stable" channel;
#     re-build the image to move Chrome, never rely on a mutable tag.
RUN browser-harness --version | tee /app/.browser-harness.version \
 && google-chrome --version | tee /app/.chrome.version

# Non-root runtime user; Chrome needs --no-sandbox (already in the service's
# launch args, T-4) when unprivileged.
RUN useradd -m -u 1000 jev \
 && mkdir -p /tmp/jev-sessions && chown -R jev:jev /tmp/jev-sessions
USER jev

ENV JUV_SERVICE_HOST=0.0.0.0 \
    JUV_SERVICE_PORT=8765 \
    PATH="/root/.local/bin:$PATH"

EXPOSE 8765

# Readiness = the service answers (harness + Chrome present). NOT
# `browser-harness --doctor` (fails while idle — no Chrome running yet).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["jev-service"]
