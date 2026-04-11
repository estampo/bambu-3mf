# bridge/Dockerfile — Multi-stage build for the bambu-bridge daemon.
#
# The daemon provides an HTTP API (axum) for Bambu Lab printer communication
# via libbambu_networking.so FFI.
#
# Usage:
#   docker build -t bambox/bridge bridge/
#   docker run --rm -v /path/to/credentials.toml:/config/credentials.toml:ro \
#     -p 8765:8765 bambox/bridge
#
# Note: x86_64/amd64 only (libbambu_networking.so is an x86_64 binary).
# On Apple Silicon Macs, Docker runs this via Rosetta emulation.

# ---------------------------------------------------------------------------
# Build stage: compile the Rust binary with C++ shim
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 rust:1.88-bookworm AS builder

# g++ is required by the cc crate to compile the C++ shim (shim/shim.cpp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy dependency manifests first for layer caching
COPY Cargo.toml Cargo.lock ./

# Create a dummy main.rs so cargo can resolve and cache dependencies
RUN mkdir src && echo "fn main() {}" > src/main.rs \
    && mkdir shim && touch shim/shim.cpp \
    && cargo build --release 2>/dev/null || true \
    && rm -rf src shim

# Copy actual source and build
COPY src/ src/
COPY shim/ shim/
COPY build.rs ./

RUN cargo build --release

# ---------------------------------------------------------------------------
# Fetch stage: download libbambu_networking.so and slicer cert
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 debian:bookworm-slim AS fetcher

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl python3 unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fetch libbambu_networking.so — try Bambu's API first, fall back to private cache.
ARG BAMBU_SLICER_VERSION=02.05.00.00
ARG BNL_TOKEN=""
RUN mkdir -p /out/lib /out/cert \
    && (PLUGIN_URL=$(curl -fsSL \
        -H "User-Agent: BambuStudio/${BAMBU_SLICER_VERSION}" \
        -H "X-BBL-OS-Type: linux" \
        "https://api.bambulab.com/v1/iot-service/api/slicer/resource?slicer/plugins/cloud=${BAMBU_SLICER_VERSION}" \
        | python3 -c "import sys,json; r=json.load(sys.stdin)['resources']; print([x for x in r if 'plugins' in x['type']][0]['url'])") \
    && curl -fsSL -o /tmp/plugins.zip "$PLUGIN_URL" \
    && unzip -j /tmp/plugins.zip libbambu_networking.so -d /out/lib/ \
    && echo "Fetched libbambu_networking.so from Bambu API") \
    || (echo "Bambu API failed, falling back to private cache..." \
    && curl -fsSL \
        -H "Authorization: token ${BNL_TOKEN}" \
        -H "Accept: application/octet-stream" \
        -o /out/lib/libbambu_networking.so \
        "https://api.github.com/repos/estampo/bnl/releases/assets/$(curl -fsSL \
            -H "Authorization: token ${BNL_TOKEN}" \
            "https://api.github.com/repos/estampo/bnl/releases/tags/v${BAMBU_SLICER_VERSION}" \
            | python3 -c "import sys,json; print([a for a in json.load(sys.stdin)['assets'] if a['name']=='libbambu_networking.so'][0]['id'])")" \
    && echo "Fetched libbambu_networking.so from private cache")

# Download slicer TLS certificate (for MQTT to *.bambulab.com)
RUN curl -fSL -o /out/cert/slicer_base64.cer \
       "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/cert/slicer_base64.cer"

# ---------------------------------------------------------------------------
# Runtime stage: binary + .so + cert on distroless (glibc + libssl, no shell)
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 gcr.io/distroless/cc-debian12

LABEL org.opencontainers.image.description="bambox bridge daemon — Bambu Lab printer communication via HTTP API"

COPY --from=fetcher /out/lib/libbambu_networking.so /usr/lib/libbambu_networking.so
COPY --from=fetcher /out/cert/slicer_base64.cer /tmp/bambu_agent/cert/slicer_base64.cer
COPY --from=fetcher /lib/x86_64-linux-gnu/liblzma.so.5 /usr/lib/x86_64-linux-gnu/liblzma.so.5
COPY --from=builder /build/target/release/bambox-bridge /usr/local/bin/bambox-bridge

ENV BAMBU_LIB_PATH=/usr/lib/libbambu_networking.so

EXPOSE 8765

ENTRYPOINT ["bambox-bridge"]
CMD ["daemon", "--bind", "0.0.0.0", "--port", "8765", "--credentials", "/config/credentials.toml"]
