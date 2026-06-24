# This file is licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# Copyright (C) 2026 Advanced Micro Devices, Inc.
#
# FindHRX.cmake - locate the HRX (libhrx amdxdna) runtime for the IRON host stack.
#
# This mirrors the role find_package(XRT) plays for the XRT backend: it probes
# standard install locations (plus a few env-var hints) so callers don't have to
# hard-code paths or rely on activate_env.sh. Environment variables, when set,
# are treated only as *hints* with the highest priority -- detection still works
# without them.
#
# Result variables:
#   HRX_FOUND                    TRUE if a usable HRX tree was located.
#   HRX_DIR                      Root of the hrx source checkout.
#   HRX_INCLUDE_DIR              Dir containing hrx_runtime.h (libhrx/include).
#   HRX_LIBHRX                   Full path to libhrx.so.
#   HRX_LIBHRX_DIR               Dir containing libhrx.so.
#
# Additional variables for building the XADX flatbuffer helper from source
# (set only when the matching build artifacts are present):
#   HRX_BUILD_DIR                HRX build dir (contains libflatcc_runtime.a).
#   HRX_RUNTIME_SRC_DIR          <HRX_DIR>/runtime/src (schema sources).
#   HRX_BUILD_RUNTIME_SRC_DIR    <HRX_BUILD_DIR>/runtime/src (generated builder).
#   HRX_FLATCC_INCLUDE_DIR       flatcc public headers.
#   HRX_FLATCC_LIB               libflatcc_runtime.a.
#   HRX_XADX_BUILDABLE           TRUE if all of the above were found.

include(FindPackageHandleStandardArgs)

# --- candidate roots (env hints first, then standard locations) --------------
set(_hrx_root_hints
  "$ENV{HRX_DIR}"
  "$HOME/hrx"
  "$ENV{HOME}/hrx"
  "$ENV{HOME}/avarma_repro/hrx"
  "/opt/hrx"
  "/usr/local/hrx"
  "${CMAKE_CURRENT_LIST_DIR}/../../../hrx"
  "${CMAKE_CURRENT_LIST_DIR}/../../../../hrx"
)

# --- 1) the public header (anchors the source root) --------------------------
find_path(HRX_INCLUDE_DIR
  NAMES hrx_runtime.h
  HINTS ${_hrx_root_hints}
  PATH_SUFFIXES libhrx/include
  DOC "Directory containing hrx_runtime.h"
)

if(HRX_INCLUDE_DIR)
  # <root>/libhrx/include/hrx_runtime.h -> <root>
  get_filename_component(HRX_DIR "${HRX_INCLUDE_DIR}/../.." ABSOLUTE)
endif()

# --- 2) libhrx.so ------------------------------------------------------------
# Build the candidate build-dir list from env hints and the derived source root.
set(_hrx_build_hints
  "$ENV{HRX_BUILD}"
  "${HRX_DIR}/build/cmake"
  "${HRX_DIR}/build-amdxdna-native"
  "${HRX_DIR}/build"
)

find_library(HRX_LIBHRX
  NAMES hrx libhrx
  HINTS
    "$ENV{LIBHRX_DIR}"
    "$ENV{HRX_BUILD}/libhrx/src/libhrx"
    "${HRX_DIR}/build/cmake/libhrx/src/libhrx"
    "${HRX_DIR}/build-amdxdna-native/libhrx/src/libhrx"
  PATHS /usr/lib /usr/local/lib
  DOC "Path to libhrx.so"
)

if(HRX_LIBHRX)
  get_filename_component(HRX_LIBHRX_DIR "${HRX_LIBHRX}" DIRECTORY)
endif()

# --- 3) build artifacts needed to compile the XADX helper from source --------
find_library(HRX_FLATCC_LIB
  NAMES flatcc_runtime libflatcc_runtime
  HINTS ${_hrx_build_hints}
  DOC "Path to libflatcc_runtime.a"
)

if(HRX_FLATCC_LIB)
  get_filename_component(HRX_BUILD_DIR "${HRX_FLATCC_LIB}" DIRECTORY)
endif()

find_path(HRX_FLATCC_INCLUDE_DIR
  NAMES flatcc/flatcc_builder.h
  HINTS
    "${HRX_BUILD_DIR}/_deps/flatcc-src/include"
    "$ENV{HRX_BUILD}/_deps/flatcc-src/include"
  DOC "flatcc public include dir"
)

# Generated XADX schema builder header lives under the build tree.
find_path(HRX_BUILD_RUNTIME_SRC_DIR
  NAMES iree/schemas/amdxdna_xclbin_executable_def_builder.h
  HINTS
    "${HRX_BUILD_DIR}/runtime/src"
    "$ENV{HRX_BUILD}/runtime/src"
  DOC "HRX build runtime/src (generated flatbuffer builder)"
)

if(HRX_DIR AND EXISTS "${HRX_DIR}/runtime/src")
  set(HRX_RUNTIME_SRC_DIR "${HRX_DIR}/runtime/src")
endif()

set(HRX_XADX_BUILDABLE FALSE)
if(HRX_FLATCC_LIB AND HRX_FLATCC_INCLUDE_DIR AND HRX_BUILD_RUNTIME_SRC_DIR
    AND HRX_RUNTIME_SRC_DIR)
  set(HRX_XADX_BUILDABLE TRUE)
endif()

find_package_handle_standard_args(HRX
  REQUIRED_VARS HRX_INCLUDE_DIR HRX_LIBHRX
  FAIL_MESSAGE
    "Could not find HRX. Set HRX_DIR (source checkout with libhrx/include/hrx_runtime.h) and/or LIBHRX_DIR (dir with libhrx.so), or install HRX to a standard location."
)

mark_as_advanced(
  HRX_INCLUDE_DIR HRX_LIBHRX HRX_FLATCC_LIB HRX_FLATCC_INCLUDE_DIR
  HRX_BUILD_RUNTIME_SRC_DIR
)
