# Reproducible shader toolchain: Debian bookworm's clang 14 + lld 14 (wasm32 target + wasm-ld).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends clang lld ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && clang --version && wasm-ld --version
