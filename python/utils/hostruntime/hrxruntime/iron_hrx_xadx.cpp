// SPDX-FileCopyrightText: Copyright (C) 2026 Advanced Micro Devices, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// iron_hrx_xadx.cpp — tiny C ABI helper that serializes an HRX amdxdna "XADX"
// executable flatbuffer (xclbin + TXN control code + buffer patch table).
//
// ctypes cannot assemble a flatbuffer directly, so this ~1-function library does
// it in C and exposes a flat C entry point the hrxruntime Python package binds
// with ctypes. It depends ONLY on HRX's own schema/flatcc headers (no external
// runtimes): the XADX schema and its generated builder live in the HRX tree
//   - iree/schemas/amdxdna_xclbin_executable_def.fbs
//   - iree/hal/drivers/amdxdna/executable_xclbin_test.cc (MakeXadxExecutable,
//     the verified reference producer this mirrors)
//
// Build (see build_xadx_helper.sh next to this file):
//   g++ -std=c++20 -O2 -fPIC -shared -o libironhrx_xadx.so iron_hrx_xadx.cpp \
//       -I<HRX>/runtime/src -I<HRX_BUILD>/runtime/src \
//       -I<HRX_BUILD>/_deps/flatcc-src/include \
//       <HRX_BUILD>/libflatcc_runtime.a
//
// The XADX produced here is a single xclbin, one entry point ("MLIR_AIE" by
// default) with one RunDef whose control_code is the mlir-aie insts.bin TXN
// stream (uint32 words) and whose patch_table is the (offset, arg_idx, addend)
// triples extracted from the aiebu control ELF's .rela.dyn. HRX host-patches
// each I/O buffer's device address into the control code at those offsets
// (binding[arg_idx]) on the npu4 COMMAND_CHAIN path; without it the addresses
// stay 0 and the NPU writes nothing (all-zero output) — so the patch table is
// always passed, never treated as optional.

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>

// flatcc builder + the generated XADX schema builder, both from the HRX tree.
#include "iree/base/internal/flatcc/building.h"
#include "iree/schemas/amdxdna_xclbin_executable_def_builder.h"

namespace {

flatbuffers_uint32_vec_ref_t create_u32_vec(flatbuffers_builder_t *b,
                                            const uint32_t *data, size_t n) {
  return flatbuffers_uint32_vec_create(b, n ? data : nullptr, n);
}

// Serialize a single-xclbin, single-entry-point, single-run XADX flatbuffer.
// Returns true on success and fills `out`.
bool build_xadx(const uint8_t *xclbin, size_t xclbin_n, const uint32_t *cc,
                size_t cc_n, const uint32_t *patch, size_t patch_n,
                const char *entry_name, std::vector<uint8_t> *out) {
  flatbuffers_builder_t builder;
  if (flatcc_builder_init(&builder) != 0) return false;

  bool ok = true;
  auto guard = [&](bool cond) { ok = ok && cond; };

  guard(!flatbuffers_failed(
      iree_hal_amdxdna_xclbin_ExecutableDef_start_as_root(&builder)));

  // xclbins: one container holding the raw final.xclbin bytes.
  iree_hal_amdxdna_xclbin_XclbinDef_vec_ref_t xclbins_ref = 0;
  if (ok) {
    flatbuffers_string_ref_t xclbin_str = flatbuffers_string_create(
        &builder, reinterpret_cast<const char *>(xclbin), xclbin_n);
    iree_hal_amdxdna_xclbin_XclbinDef_ref_t xclbin_ref =
        xclbin_str ? iree_hal_amdxdna_xclbin_XclbinDef_create(&builder,
                                                              xclbin_str)
                   : 0;
    guard(xclbin_ref != 0);
    if (ok) {
      iree_hal_amdxdna_xclbin_XclbinDef_ref_t arr[] = {xclbin_ref};
      xclbins_ref =
          iree_hal_amdxdna_xclbin_XclbinDef_vec_create(&builder, arr, 1);
      guard(xclbins_ref != 0);
    }
  }

  // entry point: one RunDef (control_code + empty data_payload + patch_table).
  iree_hal_amdxdna_xclbin_EntryPointDef_vec_ref_t entries_ref = 0;
  if (ok) {
    iree_hal_amdxdna_xclbin_RunDef_ref_t run_ref =
        iree_hal_amdxdna_xclbin_RunDef_create(
            &builder, create_u32_vec(&builder, cc, cc_n),
            create_u32_vec(&builder, nullptr, 0),
            create_u32_vec(&builder, patch, patch_n));
    guard(run_ref != 0);

    iree_hal_amdxdna_xclbin_RunDef_ref_t run_arr[] = {run_ref};
    iree_hal_amdxdna_xclbin_RunDef_vec_ref_t runs_ref =
        ok ? iree_hal_amdxdna_xclbin_RunDef_vec_create(&builder, run_arr, 1) : 0;
    guard(runs_ref != 0);

    flatbuffers_string_ref_t name_ref =
        ok ? flatbuffers_string_create_str(
                 &builder, entry_name ? entry_name : "MLIR_AIE")
           : 0;
    guard(name_ref != 0);

    iree_hal_amdxdna_xclbin_EntryPointDef_ref_t ep_ref = 0;
    if (ok &&
        !flatbuffers_failed(
            iree_hal_amdxdna_xclbin_EntryPointDef_start(&builder)) &&
        !flatbuffers_failed(iree_hal_amdxdna_xclbin_EntryPointDef_name_add(
            &builder, name_ref)) &&
        !flatbuffers_failed(iree_hal_amdxdna_xclbin_EntryPointDef_pdi_index_add(
            &builder, 0)) &&
        !flatbuffers_failed(
            iree_hal_amdxdna_xclbin_EntryPointDef_xclbin_index_add(&builder,
                                                                   0)) &&
        !flatbuffers_failed(iree_hal_amdxdna_xclbin_EntryPointDef_runs_add(
            &builder, runs_ref))) {
      ep_ref = iree_hal_amdxdna_xclbin_EntryPointDef_end(&builder);
    }
    guard(ep_ref != 0);
    if (ok) {
      iree_hal_amdxdna_xclbin_EntryPointDef_ref_t ep_arr[] = {ep_ref};
      entries_ref = iree_hal_amdxdna_xclbin_EntryPointDef_vec_create(
          &builder, ep_arr, 1);
      guard(entries_ref != 0);
    }
  }

  guard(ok && !flatbuffers_failed(
                  iree_hal_amdxdna_xclbin_ExecutableDef_xclbins_add(
                      &builder, xclbins_ref)));
  guard(ok && !flatbuffers_failed(
                  iree_hal_amdxdna_xclbin_ExecutableDef_entry_points_add(
                      &builder, entries_ref)));
  guard(ok &&
        iree_hal_amdxdna_xclbin_ExecutableDef_end_as_root(&builder) != 0);

  if (!ok) {
    flatcc_builder_clear(&builder);
    return false;
  }

  size_t size = 0;
  void *buf = flatcc_builder_finalize_buffer(&builder, &size);
  if (buf) {
    out->assign(static_cast<uint8_t *>(buf),
                static_cast<uint8_t *>(buf) + size);
    free(buf);
  }
  flatcc_builder_clear(&builder);
  return buf != nullptr;
}

}  // namespace

extern "C" {

// Build a XADX flatbuffer.
//   xclbin / xclbin_n : raw final.xclbin bytes
//   cc / cc_n         : insts.bin as uint32 words (the TXN control stream)
//   patch / patch_n   : flat (offset, arg_idx, addend) uint32 triples from the
//                       control ELF's .rela.dyn (patch_n is the element count,
//                       i.e. 3 * number_of_relocations); may be NULL/0.
//   entry_name        : export name (e.g. "MLIR_AIE"); NULL -> "MLIR_AIE"
//   out / out_n       : receive a malloc'd buffer (free with iron_free_xadx)
// Returns 0 on success, non-zero on failure.
int iron_build_xadx(const uint8_t *xclbin, size_t xclbin_n, const uint32_t *cc,
                    size_t cc_n, const uint32_t *patch, size_t patch_n,
                    const char *entry_name, uint8_t **out, size_t *out_n) {
  if (!xclbin || xclbin_n == 0 || !cc || cc_n == 0 || !out || !out_n) {
    return 1;
  }
  try {
    std::vector<uint8_t> blob;
    if (!build_xadx(xclbin, xclbin_n, cc, cc_n, patch, patch_n, entry_name,
                    &blob) ||
        blob.empty()) {
      return 3;
    }
    uint8_t *buf = static_cast<uint8_t *>(std::malloc(blob.size()));
    if (!buf) return 2;
    std::memcpy(buf, blob.data(), blob.size());
    *out = buf;
    *out_n = blob.size();
    return 0;
  } catch (...) {
    return 3;
  }
}

void iron_free_xadx(uint8_t *p) { std::free(p); }

}  // extern "C"
