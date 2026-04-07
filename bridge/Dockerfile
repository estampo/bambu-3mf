# bridge/Dockerfile — Multi-stage build for the bambu-bridge daemon.
#
# The daemon provides an HTTP API (axum) for Bambu Lab printer communication
# via libbambu_networking.so FFI.

# ---------------------------------------------------------------------------
# Build stage: compile the Rust binary with C++ shim
# ---------------------------------------------------------------------------
FROM rust:1.83-bookworm AS builder

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
# Runtime stage: minimal image with just the binary
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# libbambu_networking.so is proprietary and cannot be bundled in this image.
# It must be provided at runtime, either by:
#   - Volume mount: docker run -v /path/to/libbambu_networking.so:/usr/lib/libbambu_networking.so ...
#   - Baking into a derived image: COPY libbambu_networking.so /usr/lib/
#
# The daemon loads it via dlopen at the path specified by --lib-path
# (default: /tmp/bambu_plugin/libbambu_networking.so, override via BAMBU_LIB_PATH env).

COPY --from=builder /build/target/release/bambu-bridge /usr/local/bin/bambu-bridge

EXPOSE 8765

ENTRYPOINT ["bambu-bridge"]
CMD ["daemon", "--bind", "0.0.0.0", "--port", "8765", "--credentials", "/config/credentials.toml"]
