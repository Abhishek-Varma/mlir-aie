# This file is licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.

# Common CMake configuration for programming examples
# This file provides common setup for test_utils library linking

# -----------------------------------------------------------------------------
# Guard: project() must be called before this file is included, because
# find_package(XRT) below loads xrt-targets.cmake which calls
# add_library(... SHARED IMPORTED). Without a prior project() call, CMake
# has not initialised platform shared-library support and the call either
# fails ("does not support dynamic linking") or silently downgrades to
# STATIC. See Xilinx/mlir-aie#3048.
# -----------------------------------------------------------------------------
if(NOT PROJECT_NAME)
  message(FATAL_ERROR
    "common.cmake must be included after project(). "
    "Call mlir_aie_init_example() (or your own project() call) first. "
    "See https://github.com/Xilinx/mlir-aie/issues/3048")
endif()

# -----------------------------------------------------------------------------
# Resolve MLIR-AIE root directory
# -----------------------------------------------------------------------------
# In WSL, CMake runs on Windows via `powershell.exe cmake`. Therefore, we must
# prefer deterministic repo-root detection. Fall back to Python only if needed.

get_filename_component(_mlir_aie_repo_root "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
if(EXISTS "${_mlir_aie_repo_root}/runtime_lib/test_lib/xrt_test_wrapper.h")
  set(MLIR_AIE_DIR "${_mlir_aie_repo_root}")
else()
  find_package(Python3 COMPONENTS Interpreter QUIET)
  if(Python3_Interpreter_FOUND)
    execute_process(
      COMMAND "${Python3_EXECUTABLE}" -c "from aie.utils.config import root_path; print(root_path())"
      OUTPUT_VARIABLE MLIR_AIE_DIR
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET
    )
  endif()
endif()

if(NOT MLIR_AIE_DIR)
  message(FATAL_ERROR "Unable to determine MLIR_AIE_DIR (repo root not found and Python probe unavailable).")
endif()

# -----------------------------------------------------------------------------
# HRX backend selection (RUNTIME=hrx in makefile-common -> -DUSE_HRX=ON)
# -----------------------------------------------------------------------------
# When building the HRX host backend, examples dispatch through libhrx instead
# of XRT. We don't need the XRT SDK headers at all, but the per-example
# CMakeLists still does `target_link_libraries(... xrt_coreutil)` and
# `target_include_directories(... ${XRT_INC_DIR})`. To keep those a no-op
# without editing ~50 example files, define a dummy INTERFACE target named
# `xrt_coreutil` (so the link resolves to nothing instead of `-lxrt_coreutil`)
# and leave the XRT include/lib dir variables empty.
option(USE_HRX "Build programming-example host code against HRX instead of XRT" OFF)

if(USE_HRX)
  if(NOT TARGET xrt_coreutil)
    add_library(xrt_coreutil INTERFACE IMPORTED)
  endif()
  if(NOT DEFINED XRT_INC_DIR)
    set(XRT_INC_DIR "" CACHE STRING "Path to XRT headers (unused for HRX)")
  endif()
  if(NOT DEFINED XRT_LIB_DIR)
    set(XRT_LIB_DIR "" CACHE STRING "Path to XRT libraries (unused for HRX)")
  endif()
endif()

# -----------------------------------------------------------------------------
# XRT auto-detection (supports both Ubuntu packages and legacy /opt/xilinx/xrt)
# -----------------------------------------------------------------------------
if(NOT USE_HRX)
if(NOT DEFINED XRT_INC_DIR OR NOT DEFINED XRT_LIB_DIR)
    find_package(XRT QUIET)
    if(XRT_FOUND)
        # find_package(XRT) may resolve via the project's FindXRT.cmake (which
        # sets XRT_INCLUDE_DIR / XRT_LIB_DIR, singular) or via XRT's own
        # xrt-config.cmake (which sets XRT_INCLUDE_DIRS / XRT_LINK_DIRS,
        # plural).  Accept whichever set is available.
        if(NOT DEFINED XRT_INC_DIR)
            if(XRT_INCLUDE_DIRS)
                set(XRT_INC_DIR "${XRT_INCLUDE_DIRS}" CACHE STRING "Path to XRT headers")
            elseif(XRT_INCLUDE_DIR)
                set(XRT_INC_DIR "${XRT_INCLUDE_DIR}" CACHE STRING "Path to XRT headers")
            endif()
        endif()
        if(NOT DEFINED XRT_LIB_DIR)
            if(XRT_LINK_DIRS)
                set(XRT_LIB_DIR "${XRT_LINK_DIRS}" CACHE STRING "Path to XRT libraries")
            endif()
        endif()
    endif()

    # Fall back to legacy/default paths if still unset
    if(NOT DEFINED XRT_INC_DIR OR NOT DEFINED XRT_LIB_DIR)
        find_program(WSL NAMES powershell.exe)
        if(NOT WSL)
            if(NOT DEFINED XRT_INC_DIR)
                set(XRT_INC_DIR /opt/xilinx/xrt/include CACHE STRING "Path to XRT headers")
            endif()
            if(NOT DEFINED XRT_LIB_DIR)
                set(XRT_LIB_DIR /opt/xilinx/xrt/lib CACHE STRING "Path to XRT libraries")
            endif()
        else()
            if(NOT DEFINED XRT_INC_DIR)
                set(XRT_INC_DIR C:/Technical/XRT/src/runtime_src/core/include CACHE STRING "Path to XRT headers")
            endif()
            if(NOT DEFINED XRT_LIB_DIR)
                set(XRT_LIB_DIR C:/Technical/xrtNPUfromDLL CACHE STRING "Path to XRT libraries")
            endif()
        endif()
    endif()
endif()
endif() # NOT USE_HRX

# -----------------------------------------------------------------------------
# test_utils discovery
# -----------------------------------------------------------------------------
# Preferred: installed layout (from cmake --install). Fallback: build from source.
set(TEST_UTILS_INST_LIB_DIR "${MLIR_AIE_DIR}/runtime_lib/x86_64/test_lib/lib")
set(TEST_UTILS_INST_INC_DIR "${MLIR_AIE_DIR}/runtime_lib/x86_64/test_lib/include")
set(TEST_UTILS_SRC_DIR     "${MLIR_AIE_DIR}/runtime_lib/test_lib")
set(TEST_UTILS_RUNTIME_LIB_DIR "${MLIR_AIE_DIR}/runtime_lib")

function(target_link_test_utils target_name)
  target_include_directories(${target_name} PUBLIC "${TEST_UTILS_RUNTIME_LIB_DIR}")

  # 0) HRX backend: dispatch via libhrx, no XRT SDK needed. test_utils is built
  #    WITHOUT TEST_UTILS_USE_XRT (its XRT block is #ifdef'd out and unused by
  #    the HRX wrapper), and the example target gets TEST_UTILS_USE_HRX so
  #    xrt_test_wrapper.h pulls in hrx_test_wrapper.h.
  if(USE_HRX)
    if(NOT EXISTS "${TEST_UTILS_SRC_DIR}/hrx_test_wrapper.h")
      message(FATAL_ERROR "HRX wrapper not found at: ${TEST_UTILS_SRC_DIR}")
    endif()

    # HRX paths (from activate_env.sh): HRX_DIR (headers), LIBHRX_DIR (libhrx.so).
    set(_hrx_dir "$ENV{HRX_DIR}")
    set(_libhrx_dir "$ENV{LIBHRX_DIR}")
    if(NOT _hrx_dir OR NOT EXISTS "${_hrx_dir}/libhrx/include/hrx_runtime.h")
      message(FATAL_ERROR "USE_HRX set but HRX_DIR/libhrx/include/hrx_runtime.h "
        "not found (HRX_DIR='${_hrx_dir}'). Source activate_env.sh.")
    endif()
    if(NOT _libhrx_dir OR NOT EXISTS "${_libhrx_dir}/libhrx.so")
      message(FATAL_ERROR "USE_HRX set but ${_libhrx_dir}/libhrx.so not found. "
        "Source activate_env.sh (sets LIBHRX_DIR).")
    endif()

    # XADX builder helper: env override, else the in-tree prebuilt .so.
    set(_xadx_so "$ENV{IRON_HRX_XADX}")
    if(NOT _xadx_so)
      set(_xadx_so "${MLIR_AIE_DIR}/python/utils/hostruntime/hrxruntime/libironhrx_xadx.so")
    endif()
    if(NOT EXISTS "${_xadx_so}")
      message(FATAL_ERROR "libironhrx_xadx.so not found at '${_xadx_so}'. "
        "Build it with python/utils/hostruntime/hrxruntime/build_xadx_helper.sh "
        "or set IRON_HRX_XADX.")
    endif()

    target_include_directories(${target_name} PUBLIC
        "${TEST_UTILS_SRC_DIR}" "${_hrx_dir}/libhrx/include")
    target_compile_definitions(${target_name} PRIVATE TEST_UTILS_USE_HRX)

    if(NOT TARGET test_utils)
      add_library(test_utils STATIC "${TEST_UTILS_SRC_DIR}/test_utils.cpp")
      target_include_directories(test_utils PUBLIC
          "${TEST_UTILS_SRC_DIR}" "${TEST_UTILS_RUNTIME_LIB_DIR}")
    endif()

    target_link_libraries(${target_name} PUBLIC
        test_utils "${_libhrx_dir}/libhrx.so" "${_xadx_so}")
    return()
  endif()

  # 1) Use installed/prebuilt if present
  if(EXISTS "${TEST_UTILS_INST_INC_DIR}/xrt_test_wrapper.h" AND EXISTS "${TEST_UTILS_INST_LIB_DIR}")
    target_include_directories(${target_name} PUBLIC "${TEST_UTILS_INST_INC_DIR}")
    target_link_directories(${target_name} PUBLIC "${TEST_UTILS_INST_LIB_DIR}")
    target_link_libraries(${target_name} PUBLIC test_utils)
    return()
  endif()

  # 2) Otherwise build test_utils from source
  if(NOT EXISTS "${TEST_UTILS_SRC_DIR}/test_utils.cpp")
    message(FATAL_ERROR "test_utils source not found at: ${TEST_UTILS_SRC_DIR}")
  endif()

  target_include_directories(${target_name} PUBLIC "${TEST_UTILS_SRC_DIR}")

  if(NOT TARGET test_utils)
    add_library(test_utils STATIC "${TEST_UTILS_SRC_DIR}/test_utils.cpp")
    target_include_directories(test_utils PUBLIC "${TEST_UTILS_SRC_DIR}" "${TEST_UTILS_RUNTIME_LIB_DIR}")

    # Enable XRT helpers if an XRT include dir is available
    if(DEFINED XRT_INC_DIR AND XRT_INC_DIR)
      target_include_directories(test_utils PUBLIC "${XRT_INC_DIR}")
      target_compile_definitions(test_utils PRIVATE TEST_UTILS_USE_XRT)
    elseif(DEFINED XRT_INCLUDE_DIRS AND XRT_INCLUDE_DIRS)
      target_include_directories(test_utils PUBLIC "${XRT_INCLUDE_DIRS}")
      target_compile_definitions(test_utils PRIVATE TEST_UTILS_USE_XRT)
    elseif(DEFINED XRT_INCLUDE_DIR AND XRT_INCLUDE_DIR)
      target_include_directories(test_utils PUBLIC "${XRT_INCLUDE_DIR}")
      target_compile_definitions(test_utils PRIVATE TEST_UTILS_USE_XRT)
    endif()
  endif()

  target_link_libraries(${target_name} PUBLIC test_utils)
endfunction()

