# CPU-only TapScribe — hobby-grade transcription with faster-whisper.
# No GPU support: the Recorder + WhisperLiveKit + faster-whisper backend
# all run fine on CPU for small models (tiny.en / small.en); larger models
# are slow on CPU but still work. For GPU users, run from source on the host.
#
# Build:  docker build -t tapscribe .
# Run:    docker run --rm -it -p 8001:8001 -v "$PWD/data:/data" tapscribe
#
# Port: 8001 = FastAPI dashboard + /tap. WhisperLiveKit's 8000 is internal
# only — the Recorder relays /tap bytes to localhost:8000 inside the
# container; nothing outside ever needs to reach it.
#
# Data volume: bind-mount any host directory to /data. Recordings, config,
# auth password, /tap token, and TLS files all land there and persist
# across container runs. The host directory needs to be writable by UID
# 1000 (matching the `tapscribe` user created below).
FROM python:3.12-slim

# ffmpeg is pulled in for any future WAV transcoding paths (silero-VAD's
# `read_audio` falls back to it on non-WAV input). libsndfile1 is required
# by soundfile when fixture conversion runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user to own /data and the cache directories. /data and its
# expected subdirs are pre-created with the right owner so a bind mount
# from a host dir owned by UID 1000 works without any in-container chown.
RUN useradd --create-home --uid 1000 tapscribe \
    && mkdir -p /data /data/recordings /data/config \
    && chown -R tapscribe:tapscribe /data

WORKDIR /app
COPY --chown=tapscribe:tapscribe pyproject.toml README.md LICENSE ./
COPY --chown=tapscribe:tapscribe tapscribe/ ./tapscribe/

# Install runtime + the `whisper` extra (faster-whisper + whisperlivekit).
# Other extras (mlx, voxtral, vad) are intentionally omitted — they pull
# in PyTorch / CUDA and balloon the image past 5 GB without buying the
# CPU-only target anything.
#
# whisperlivekit transitively depends on torch with no version pin, so a
# naive install resolves to the default torch wheel which ships with the
# CUDA runtime (cu13 + nvidia-* libs, ~7 GB). We pre-install the CPU-only
# torch wheel from PyTorch's CPU index first; the later [whisper] install
# sees torch as already satisfied and doesn't pull the GPU build. Saves
# ~7 GB of image weight on a CPU-only target that can't use any of it.
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch torchaudio \
    && python -m pip install --no-cache-dir ".[whisper]"

USER tapscribe
WORKDIR /data

# Override BASE_DIR so recordings/, config/, .auth-password, .tap-token,
# and TLS files all resolve under /data instead of inside the installed
# package's site-packages location. See tapscribe/config.py.
ENV TAPSCRIBE_BASE_DIR=/data

EXPOSE 8001

# `--host 0.0.0.0` so the container is reachable from the host network
# bridge; flip to `--lan` on the host side if you want explicit LAN
# exposure semantics instead of any-interface bind.
CMD ["python", "-m", "tapscribe", "--host", "0.0.0.0"]
