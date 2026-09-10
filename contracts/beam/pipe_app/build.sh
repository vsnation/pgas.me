#!/usr/bin/env bash
# Build the index-keyed pipe app reproducibly: pinned upstream commit + pipe_app.indexed.diff, compiled
# in a docker container built from ./Dockerfile (no host toolchain involved).
#
#   ./build.sh              # clone/pin, patch, build ./pipe_app.wasm, verify SHA256SUMS
#   WORK=/some/dir ./build.sh
#
# Exit 0 only when the built file's sha256 equals PATCHED_SHA256 in upstream.txt and SHA256SUMS.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/upstream.txt"
WORK="${WORK:-$HERE/build}"
IMAGE="${IMAGE:-pgasme-beam-shader:bookworm-clang14}"
SRC="$WORK/beam-bridge-pipe"

mkdir -p "$WORK"
if [ ! -d "$SRC/.git" ]; then
  git clone -q --no-checkout "$UPSTREAM_REPO" "$SRC"
fi
git -C "$SRC" fetch -q origin "$UPSTREAM_COMMIT" || true
git -C "$SRC" checkout -q --force "$UPSTREAM_COMMIT"
git -C "$SRC" submodule update --init --depth 1 beam
[ "$(git -C "$SRC/beam" rev-parse HEAD)" = "$BEAM_COMMIT" ] || { echo "REFUSED: beam submodule is not at $BEAM_COMMIT"; exit 1; }

# 1. the upstream file must be the one the diff was made against
git -C "$SRC" checkout -q -- shaders/pipe_app.cpp
echo "$UPSTREAM_PIPE_APP_WASM_SHA256  $SRC/shaders/pipe_app.wasm" | shasum -a 256 -c - >/dev/null \
  || { echo "REFUSED: upstream shaders/pipe_app.wasm is not the pinned one"; exit 1; }

# 2. apply the patch; the full patched source shipped next to it must be exactly upstream + diff
git -C "$SRC" apply --check "$HERE/pipe_app.indexed.diff"
git -C "$SRC" apply "$HERE/pipe_app.indexed.diff"
cmp -s "$SRC/shaders/pipe_app.cpp" "$HERE/pipe_app.cpp" || { echo "REFUSED: pipe_app.cpp != upstream + pipe_app.indexed.diff"; exit 1; }

# 3. build in the container (same flags as beam's cmake/AddShader.cmake)
docker build -q -t "$IMAGE" "$HERE" >/dev/null
docker run --rm --user "$(id -u):$(id -g)" -v "$SRC":/src -w /src/shaders "$IMAGE" \
  clang --target=wasm32 -I /src/beam/bvm -O3 -std=c++17 -fno-rtti -nostdlib \
        -Wl,--export-dynamic,--no-entry,--allow-undefined pipe_app.cpp --output pipe_app.indexed.wasm
cp "$SRC/shaders/pipe_app.indexed.wasm" "$HERE/pipe_app.wasm"

# 4. reproducibility gate
(cd "$HERE" && shasum -a 256 -c SHA256SUMS)
echo "$PATCHED_SHA256  $HERE/pipe_app.wasm" | shasum -a 256 -c - >/dev/null || { echo "REFUSED: built wasm differs from PATCHED_SHA256"; exit 1; }
echo "OK: $HERE/pipe_app.wasm ($(wc -c < "$HERE/pipe_app.wasm" | tr -d ' ') bytes) reproduced"
