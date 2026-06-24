#!/usr/bin/env bash
# Build libironhrx_xadx.so — the small C ABI helper that serializes the HRX
# amdxdna XADX executable flatbuffer, loaded by the hrxruntime Python package via
# ctypes. Depends only on the HRX tree (schema + flatcc); no FastFlowLM.
#
# Requires (env vars or auto-detected):
#   HRX_DIR    path to the hrx source checkout (libhrx/include/hrx_runtime.h)
#   HRX_BUILD  path to the hrx build dir (libflatcc_runtime.a, generated builder)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/libironhrx_xadx.so"

HRX_DIR="${HRX_DIR:-}"
HRX_BUILD="${HRX_BUILD:-}"

if [[ -z "$HRX_DIR" ]]; then
  for c in "$HOME/avarma_repro/hrx" "$HOME/hrx" "../hrx"; do
    [[ -f "$c/libhrx/include/hrx_runtime.h" ]] && HRX_DIR="$(cd "$c" && pwd)" && break
  done
fi
[[ -n "$HRX_DIR" && -f "$HRX_DIR/libhrx/include/hrx_runtime.h" ]] || {
  echo "ERROR: set HRX_DIR (hrx checkout with libhrx/include/hrx_runtime.h)"; exit 1; }

if [[ -z "$HRX_BUILD" ]]; then
  for c in "$HRX_DIR/build/cmake" "$HRX_DIR/build-amdxdna-native" "$HRX_DIR/build"; do
    [[ -f "$c/libflatcc_runtime.a" ]] && HRX_BUILD="$c" && break
  done
fi
[[ -n "$HRX_BUILD" && -f "$HRX_BUILD/libflatcc_runtime.a" ]] || {
  echo "ERROR: set HRX_BUILD (hrx build dir with libflatcc_runtime.a)"; exit 1; }

FLATCC_INC="$HRX_BUILD/_deps/flatcc-src/include"
FLATCC_LIB="$HRX_BUILD/libflatcc_runtime.a"

echo "HRX_DIR   = $HRX_DIR"
echo "HRX_BUILD = $HRX_BUILD"

g++ -std=c++20 -O2 -fPIC -shared -o "$OUT" \
  "$HERE/iron_hrx_xadx.cpp" \
  -I "$HRX_DIR/runtime/src" \
  -I "$HRX_BUILD/runtime/src" \
  -I "$FLATCC_INC" \
  "$FLATCC_LIB"

echo "built: $OUT"
