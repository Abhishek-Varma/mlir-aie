//===- hrx_test_wrapper.h ---------------------------------------*- C++ -*-===//
//
// This file is licensed under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
// Copyright (C) 2026 Advanced Micro Devices, Inc.
//
//===----------------------------------------------------------------------===//
//
// HRX-based drop-in replacement for xrt_test_wrapper.h.
//
// It exposes the *same* `args`, `parse_args()`, and templated
// `setup_and_run_aie<...>()` surface the example `test.cpp` files already use,
// but dispatches through HRX (libhrx + the amdxdna HAL) instead of XRT. A
// test.cpp keeps `#include "xrt_test_wrapper.h"`; defining `TEST_UTILS_USE_HRX`
// makes that header pull this one in instead, so no example source changes.
//
// It consumes the identical artifacts as the XRT path (`final.xclbin` +
// `insts.bin`): the insts.bin TXN stream's words become the XADX control code
// and its embedded DDR_PATCH ops are parsed directly into the (offset, arg_idx,
// addend) patch table (no aiebu-asm; see parse_txn()). An aiebu control-ELF
// round-trip remains as a fallback for ELF input / unknown TXN versions. The
// two are wrapped into an HRX "amdxdna-xclbin-fb" executable by the shared
// `libironhrx_xadx.so` helper. This is the C++ analogue of the Python
// `HRXHostRuntime` (python/utils/hostruntime/hrxruntime/).
//
// Buffer order matches the XRT wrapper exactly (faithful drop-in): the bindings
// list is [in1, in2, out] (2-input form) or [in1, out] (1-input form), so
// patch-table arg indices (XRT kernel arg index - 3) line up with binding slots
// the same way they do under XRT.
//
//===----------------------------------------------------------------------===//

#ifndef HRX_TEST_WRAPPER_H
#define HRX_TEST_WRAPPER_H

#include "cxxopts.hpp"
#include "test_utils.h"

#include "hrx_runtime.h"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

// ---------------------------------------------------------------------------
// XADX builder helper (libironhrx_xadx.so) — same entry points the Python
// hrxruntime binds via ctypes.
// ---------------------------------------------------------------------------
extern "C" {
int iron_build_xadx(const uint8_t *xclbin, size_t xclbin_n, const uint32_t *cc,
                    size_t cc_n, const uint32_t *patch, size_t patch_n,
                    const char *entry_name, uint8_t **out, size_t *out_n);
void iron_free_xadx(uint8_t *p);
}

namespace hrx_test {

// HAL executable format string for the amdxdna xclbin package build_xadx emits.
static constexpr const char *kHrxAmdxdnaFormat = "amdxdna-xclbin-fb";

// ---------------------------------------------------------------------------
// Status check
// ---------------------------------------------------------------------------
inline void hrx_check(hrx_status_t status, const char *what) {
  if (hrx_status_is_ok(status))
    return;
  char *msg = nullptr;
  size_t len = 0;
  hrx_status_t s2 = hrx_status_to_string(status, &msg, &len);
  int code = hrx_status_code(status);
  std::string text = msg ? std::string(msg, len) : std::string("?");
  if (msg)
    hrx_status_free_message(msg);
  hrx_status_ignore(s2);
  hrx_status_ignore(status);
  throw std::runtime_error(std::string(what) + " failed (hrx status code " +
                           std::to_string(code) + "): " + text);
}

// ---------------------------------------------------------------------------
// Process-wide HRX context (device + dispatch stream), created once.
// Mirrors the Python HRXContext singleton.
// ---------------------------------------------------------------------------
class Context {
public:
  static Context &get() {
    static Context instance;
    return instance;
  }

  hrx_device_t device = nullptr;
  hrx_stream_t stream = nullptr;

private:
  Context() {
    hrx_check(hrx_gpu_initialize(0), "hrx_gpu_initialize");
    hrx_check(hrx_gpu_device_get(0, &device), "hrx_gpu_device_get");
    hrx_check(hrx_stream_create(device, 0, &stream), "hrx_stream_create");
  }
};

// ---------------------------------------------------------------------------
// A persistent, host-mapped, device-visible HRX buffer.
// Coherence is maintained explicitly via flush_range / invalidate_range.
// ---------------------------------------------------------------------------
class Buffer {
public:
  Buffer(hrx_stream_t stream, size_t nbytes) : nbytes_(nbytes) {
    size_t alloc = nbytes ? nbytes : 1; // HRX rejects 0-size allocations
    hrx_check(hrx_buffer_allocate(
                  stream, alloc,
                  HRX_MEMORY_TYPE_HOST_LOCAL | HRX_MEMORY_TYPE_DEVICE_VISIBLE,
                  HRX_BUFFER_USAGE_DEFAULT | HRX_BUFFER_USAGE_MAPPING_PERSISTENT,
                  &handle_),
              "hrx_buffer_allocate");
    hrx_check(hrx_buffer_map_persistent(handle_, HRX_MAP_READ | HRX_MAP_WRITE,
                                        &host_ptr_),
              "hrx_buffer_map_persistent");
  }

  ~Buffer() {
    if (handle_)
      hrx_buffer_release(handle_);
  }

  Buffer(const Buffer &) = delete;
  Buffer &operator=(const Buffer &) = delete;

  void flush() {
    if (nbytes_)
      hrx_check(hrx_buffer_flush_range(handle_, 0, nbytes_),
                "hrx_buffer_flush_range");
  }
  void invalidate() {
    if (nbytes_)
      hrx_check(hrx_buffer_invalidate_range(handle_, 0, nbytes_),
                "hrx_buffer_invalidate_range");
  }

  hrx_buffer_t handle() const { return handle_; }
  void *host_ptr() const { return host_ptr_; }
  size_t nbytes() const { return nbytes_; }

private:
  hrx_buffer_t handle_ = nullptr;
  void *host_ptr_ = nullptr;
  size_t nbytes_ = 0;
};

// ---------------------------------------------------------------------------
// insts.bin (raw TXN stream) -> aiebu control ELF bytes.
// Prefers a fresh sibling "<instr>.ctrl.elf"; otherwise runs aiebu-asm.
// ---------------------------------------------------------------------------
inline std::vector<uint8_t> insts_to_control_elf(const std::string &insts_path) {
  namespace fs = std::filesystem;

  // If the caller already handed us an ELF (xrt::elf flow / --elf-path), use it.
  {
    std::ifstream f(insts_path, std::ios::binary);
    char magic[4] = {0};
    if (f.read(magic, 4) && magic[0] == 0x7f && magic[1] == 'E' &&
        magic[2] == 'L' && magic[3] == 'F') {
      f.seekg(0, std::ios::end);
      std::streamsize n = f.tellg();
      f.seekg(0, std::ios::beg);
      std::vector<uint8_t> data(static_cast<size_t>(n));
      f.read(reinterpret_cast<char *>(data.data()), n);
      return data;
    }
  }

  std::string elf_path = insts_path + ".ctrl.elf";
  bool reuse = fs::exists(elf_path);
  if (reuse) {
    std::error_code ec;
    auto ie = fs::last_write_time(insts_path, ec);
    auto ee = fs::last_write_time(elf_path, ec);
    reuse = !ec && ee >= ie;
  }
  if (!reuse) {
    std::string aiebu;
    if (const char *e = std::getenv("AIEBU_ASM"))
      aiebu = e;
    else if (fs::exists("/opt/xilinx/xrt/bin/aiebu-asm"))
      aiebu = "/opt/xilinx/xrt/bin/aiebu-asm";
    else
      aiebu = "aiebu-asm"; // hope it's on PATH
    std::string cmd = "'" + aiebu + "' -t aie2txn -c '" + insts_path +
                      "' -o '" + elf_path + "'";
    int rc = std::system(cmd.c_str());
    if (rc != 0)
      throw std::runtime_error(
          "aiebu-asm failed converting insts.bin to a control ELF (cmd: " +
          cmd + "). Set AIEBU_ASM or add /opt/xilinx/xrt/bin to PATH.");
  }
  std::ifstream f(elf_path, std::ios::binary);
  if (!f)
    throw std::runtime_error("could not read control ELF: " + elf_path);
  f.seekg(0, std::ios::end);
  std::streamsize n = f.tellg();
  f.seekg(0, std::ios::beg);
  std::vector<uint8_t> data(static_cast<size_t>(n));
  if (!f.read(reinterpret_cast<char *>(data.data()), n))
    throw std::runtime_error("short read on control ELF: " + elf_path);
  return data;
}

// ---------------------------------------------------------------------------
// Parse an aiebu control ELF (ELF32, little-endian) into the TXN control code
// and the (offset, arg_idx, addend) patch table. C++ port of
// parse_control_elf() in python/utils/hostruntime/hrxruntime/__init__.py.
// ---------------------------------------------------------------------------
struct ControlProgram {
  std::vector<uint32_t> control_code;
  std::vector<uint32_t> patch_table; // flat (offset, arg_idx, addend) triples
};

inline ControlProgram parse_control_elf(const std::vector<uint8_t> &d,
                                        int scalar_args = 3) {
  auto rd32 = [&](size_t off) -> uint32_t {
    uint32_t v;
    std::memcpy(&v, d.data() + off, 4);
    return v;
  };
  auto rd16 = [&](size_t off) -> uint16_t {
    uint16_t v;
    std::memcpy(&v, d.data() + off, 2);
    return v;
  };
  auto rdi32 = [&](size_t off) -> int32_t {
    int32_t v;
    std::memcpy(&v, d.data() + off, 4);
    return v;
  };

  if (d.size() < 52 || d[0] != 0x7f || d[1] != 'E' || d[2] != 'L' ||
      d[3] != 'F')
    throw std::runtime_error("control ELF is not a valid ELF32 file");

  uint32_t e_shoff = rd32(0x20);
  uint16_t e_shentsize = rd16(0x2E);
  uint16_t e_shnum = rd16(0x30);
  uint16_t e_shstrndx = rd16(0x32);

  auto sh = [&](uint32_t i, uint32_t f) -> size_t {
    return e_shoff + static_cast<size_t>(i) * e_shentsize + f;
  };
  uint32_t shstr_off = rd32(sh(e_shstrndx, 0x10));

  auto cstr = [&](size_t off) -> std::string {
    size_t end = off;
    while (end < d.size() && d[end] != 0)
      ++end;
    return std::string(reinterpret_cast<const char *>(d.data() + off),
                       end - off);
  };
  auto sname = [&](uint32_t i) -> std::string {
    uint32_t nm = rd32(sh(i, 0));
    return cstr(shstr_off + nm);
  };

  int ctrl = -1, rela = -1, dynsym = -1, dynstr = -1;
  for (uint32_t i = 0; i < e_shnum; ++i) {
    std::string n = sname(i);
    if (n == ".ctrltext")
      ctrl = i;
    else if (n == ".rela.dyn")
      rela = i;
    else if (n == ".dynsym")
      dynsym = i;
    else if (n == ".dynstr")
      dynstr = i;
  }
  if (ctrl < 0)
    throw std::runtime_error("control ELF has no .ctrltext section");

  ControlProgram out;
  uint32_t coff = rd32(sh(ctrl, 0x10));
  uint32_t csize = rd32(sh(ctrl, 0x14));
  out.control_code.resize(csize / 4);
  std::memcpy(out.control_code.data(), d.data() + coff, csize);

  if (rela >= 0 && dynsym >= 0 && dynstr >= 0) {
    uint32_t roff = rd32(sh(rela, 0x10));
    uint32_t rsize = rd32(sh(rela, 0x14));
    uint32_t symoff = rd32(sh(dynsym, 0x10));
    uint32_t symentsz = rd32(sh(dynsym, 0x24));
    if (symentsz == 0)
      symentsz = 16;
    uint32_t strtab = rd32(sh(dynstr, 0x10));
    for (uint32_t o = 0; o + 12 <= rsize; o += 12) {
      uint32_t r_offset = rd32(roff + o + 0);
      uint32_t r_info = rd32(roff + o + 4);
      int32_t r_addend = rdi32(roff + o + 8);
      uint32_t symidx = r_info >> 8;
      uint32_t st_name = rd32(symoff + symidx * symentsz);
      std::string name = cstr(strtab + st_name);
      char *endp = nullptr;
      long val = std::strtol(name.c_str(), &endp, 10);
      if (endp == name.c_str() || *endp != '\0')
        continue; // not a decimal arg-index symbol
      long arg_idx = val - scalar_args;
      if (arg_idx >= 0) {
        out.patch_table.push_back(r_offset);
        out.patch_table.push_back(static_cast<uint32_t>(arg_idx));
        out.patch_table.push_back(static_cast<uint32_t>(r_addend));
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Direct TXN -> ControlProgram (no aiebu dependency). C++ port of parse_txn()
// in python/utils/hostruntime/hrxruntime/__init__.py. Walks the raw insts.bin
// op stream and derives the same (offset, arg_idx, addend) patch table aiebu
// would emit; validated byte-for-byte against `aiebu-asm -t aie2txn`.
//
// Op layout from lib/Targets/AIETargetNPU.cpp + AIEToConfiguration.cpp:
//   - 16-byte header: major=byte[0], minor=byte[1], num_ops@8, txn_size@12.
//   - BLOCKWRITE (0x01): base@+8, opSize@+12, payload words from +16; payload
//     word j programs absolute address base + 4*j.
//   - DDR_PATCH (0x81): opSize@+4, addr@+24, argIdx@+32, argPlus@+40. argIdx is
//     already the 0-based binding index. The reloc lands on the low DDR-address
//     word, i.e. the TXN word at absolute address (addr - 4).
// The .ctrltext aiebu emits is the raw TXN verbatim, so control_code = words.
// ---------------------------------------------------------------------------
struct TxnUnsupported : std::runtime_error {
  using std::runtime_error::runtime_error;
};

inline ControlProgram parse_txn(const std::vector<uint8_t> &d) {
  constexpr uint8_t OP_WRITE = 0x00, OP_BLOCKWRITE = 0x01, OP_MASKWRITE = 0x03,
                    OP_PREEMPT = 0x06, OP_TCT = 0x80, OP_DDR_PATCH = 0x81;
  constexpr size_t HEADER = 16;

  if (d.size() < HEADER)
    throw TxnUnsupported("insts.bin too small for a TXN header");
  auto r32 = [&](size_t o) -> uint32_t {
    if (o + 4 > d.size())
      throw TxnUnsupported("TXN truncated");
    uint32_t v;
    std::memcpy(&v, d.data() + o, 4);
    return v;
  };
  auto ri32 = [&](size_t o) -> int32_t {
    return static_cast<int32_t>(r32(o));
  };

  if (d[0] != 0 || d[1] != 1)
    throw TxnUnsupported("unsupported TXN version (only 0.1 handled directly)");

  // absolute AIE address -> byte offset of the word that programs it.
  std::unordered_map<uint32_t, uint32_t> addr_to_off;
  struct Patch {
    uint32_t addr;
    int32_t arg_idx;
    int32_t addend;
  };
  std::vector<Patch> patches;

  size_t i = HEADER;
  while (i < d.size()) {
    uint8_t opc = d[i];
    size_t start = i;
    if (opc == OP_WRITE) { // 24 bytes
      addr_to_off[r32(i + 8)] = static_cast<uint32_t>(i + 16);
      i += r32(i + 20);
    } else if (opc == OP_BLOCKWRITE) {
      uint32_t base = r32(i + 8);
      uint32_t op_size = r32(i + 12);
      if (op_size < 16)
        throw TxnUnsupported("malformed BLOCKWRITE op size");
      for (uint32_t j = 0; j < (op_size - 16) / 4; ++j)
        addr_to_off[base + 4 * j] = static_cast<uint32_t>(i + 16 + 4 * j);
      i += op_size;
    } else if (opc == OP_MASKWRITE) { // 28 bytes
      i += r32(i + 24);
    } else if (opc == OP_TCT) {
      i += r32(i + 4);
    } else if (opc == OP_DDR_PATCH) {
      uint32_t op_size = r32(i + 4);
      if (op_size < 44)
        throw TxnUnsupported("malformed DDR_PATCH op size");
      patches.push_back({r32(i + 24), ri32(i + 32), ri32(i + 40)});
      i += op_size;
    } else if (opc == OP_PREEMPT) { // 8-byte TxnPreemptHeader
      i += 8;
    } else {
      throw TxnUnsupported("unhandled TXN opcode " + std::to_string(opc));
    }
    if (i <= start)
      throw TxnUnsupported("zero-size TXN op");
  }

  ControlProgram out;
  out.control_code.resize(d.size() / 4);
  std::memcpy(out.control_code.data(), d.data(),
              out.control_code.size() * 4);

  for (const auto &p : patches) {
    auto it = addr_to_off.find(p.addr - 4);
    if (it == addr_to_off.end())
      throw TxnUnsupported("DDR_PATCH target has no preceding write");
    if (p.arg_idx >= 0) {
      out.patch_table.push_back(it->second);
      out.patch_table.push_back(static_cast<uint32_t>(p.arg_idx));
      out.patch_table.push_back(static_cast<uint32_t>(p.addend));
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Resolve a ControlProgram for an insts.bin / control ELF. Prefers the direct
// TXN parser (no aiebu); falls back to the aiebu-asm round-trip for an already-
// ELF input or a TXN version the direct parser doesn't know. Set
// IRON_HRX_FORCE_AIEBU=1 to always use the aiebu path.
// ---------------------------------------------------------------------------
inline ControlProgram load_control_program(const std::string &insts_path) {
  std::ifstream f(insts_path, std::ios::binary);
  if (!f)
    throw std::runtime_error("could not read insts: " + insts_path);
  std::vector<uint8_t> data((std::istreambuf_iterator<char>(f)),
                            std::istreambuf_iterator<char>());

  if (data.size() >= 4 && data[0] == 0x7f && data[1] == 'E' && data[2] == 'L' &&
      data[3] == 'F')
    return parse_control_elf(data);

  if (!std::getenv("IRON_HRX_FORCE_AIEBU")) {
    try {
      return parse_txn(data);
    } catch (const TxnUnsupported &) {
      // fall through to the aiebu round-trip
    }
  }
  return parse_control_elf(insts_to_control_elf(insts_path));
}

// ---------------------------------------------------------------------------
// Build + load an HRX executable from the artifacts, returning (exe, ordinal).
// ---------------------------------------------------------------------------
struct LoadedKernel {
  hrx_executable_t exe = nullptr;
  uint32_t ordinal = 0;
};

inline LoadedKernel load_kernel(const std::string &xclbin_path,
                                const std::string &insts_path,
                                const std::string &kernel_name) {
  // Read xclbin bytes.
  std::ifstream xf(xclbin_path, std::ios::binary);
  if (!xf)
    throw std::runtime_error("could not read xclbin: " + xclbin_path);
  std::vector<uint8_t> xclbin((std::istreambuf_iterator<char>(xf)),
                              std::istreambuf_iterator<char>());

  // insts.bin -> (control_code, patch_table). Direct TXN parse (no aiebu),
  // with an aiebu-asm fallback for ELF input / unknown TXN versions.
  ControlProgram cp = load_control_program(insts_path);

  // Serialize the XADX flatbuffer.
  uint8_t *blob = nullptr;
  size_t blob_n = 0;
  int rc = iron_build_xadx(
      xclbin.data(), xclbin.size(), cp.control_code.data(),
      cp.control_code.size(),
      cp.patch_table.empty() ? nullptr : cp.patch_table.data(),
      cp.patch_table.size(), kernel_name.c_str(), &blob, &blob_n);
  if (rc != 0 || !blob)
    throw std::runtime_error("iron_build_xadx failed (rc=" +
                             std::to_string(rc) + ")");

  Context &ctx = Context::get();
  LoadedKernel lk;
  try {
    hrx_check(hrx_executable_load_data(ctx.device, blob, blob_n,
                                       kHrxAmdxdnaFormat, &lk.exe),
              "hrx_executable_load_data");
    hrx_check(hrx_executable_lookup_export_by_name(lk.exe, kernel_name.c_str(),
                                                   &lk.ordinal),
              "hrx_executable_lookup_export_by_name");
  } catch (...) {
    iron_free_xadx(blob);
    throw;
  }
  iron_free_xadx(blob);
  return lk;
}

// ---------------------------------------------------------------------------
// One dispatch + synchronize of `lk` with `bindings`. Returns elapsed us.
// ---------------------------------------------------------------------------
inline double dispatch_once(const LoadedKernel &lk,
                            const std::vector<Buffer *> &bindings) {
  Context &ctx = Context::get();
  std::vector<hrx_buffer_ref_t> refs(bindings.size());
  for (size_t i = 0; i < bindings.size(); ++i) {
    refs[i].buffer = bindings[i]->handle();
    refs[i].offset = 0;
    refs[i].length = bindings[i]->nbytes();
  }
  hrx_dispatch_config_t cfg{};
  cfg.workgroup_count[0] = cfg.workgroup_count[1] = cfg.workgroup_count[2] = 1;
  cfg.workgroup_size[0] = cfg.workgroup_size[1] = cfg.workgroup_size[2] = 1;
  cfg.subgroup_size = 0;

  auto start = std::chrono::high_resolution_clock::now();
  hrx_check(hrx_stream_dispatch(ctx.stream, lk.exe, lk.ordinal, &cfg, nullptr, 0,
                                refs.data(), refs.size(),
                                HRX_DISPATCH_FLAG_NONE),
            "hrx_stream_dispatch");
  hrx_check(hrx_stream_synchronize(ctx.stream), "hrx_stream_synchronize");
  auto stop = std::chrono::high_resolution_clock::now();
  return std::chrono::duration_cast<std::chrono::microseconds>(stop - start)
      .count();
}

inline void report_timing(double total, double mn, double mx, int n_iters) {
  std::cout << std::endl
            << "Avg NPU time: " << (n_iters ? total / n_iters : 0.0) << "us."
            << std::endl;
  std::cout << std::endl << "Min NPU time: " << mn << "us." << std::endl;
  std::cout << std::endl << "Max NPU time: " << mx << "us." << std::endl;
}

} // namespace hrx_test

// ---------------------------------------------------------------------------
// Public surface: identical to xrt_test_wrapper.h
// ---------------------------------------------------------------------------
struct args {
  int verbosity;
  int do_verify;
  int n_iterations;
  int n_warmup_iterations;
  int trace_size;
  std::string instr;
  std::string xclbin;
  std::string kernel;
  std::string trace_file;
};

inline struct args parse_args(int argc, const char *argv[]) {
  cxxopts::Options options("HRX Test Wrapper");
  cxxopts::ParseResult vm;
  test_utils::add_default_options(options);

  struct args myargs;
  test_utils::parse_options(argc, argv, options, vm);
  myargs.verbosity = vm["verbosity"].as<int>();
  myargs.do_verify = vm["verify"].as<bool>();
  myargs.n_iterations = vm["iters"].as<int>();
  myargs.n_warmup_iterations = vm["warmup"].as<int>();
  myargs.trace_size = vm["trace_sz"].as<int>();
  myargs.instr = vm["instr"].as<std::string>();
  myargs.xclbin = vm["xclbin"].as<std::string>();
  myargs.kernel = vm["kernel"].as<std::string>();
  myargs.trace_file = vm["trace_file"].as<std::string>();
  return myargs;
}

/*
 ******************************************************************************
 * HRX based test wrapper for 2 inputs and 1 output
 ******************************************************************************
 */
template <typename T1, typename T2, typename T3, void (*init_bufIn1)(T1 *, int),
          void (*init_bufIn2)(T2 *, int), void (*init_bufOut)(T3 *, int),
          int (*verify_results)(T1 *, T2 *, T3 *, int, int)>
int setup_and_run_aie(int IN1_VOLUME, int IN2_VOLUME, int OUT_VOLUME,
                      struct args myargs, bool enable_ctrl_pkts = false) {
  using namespace hrx_test;
  (void)enable_ctrl_pkts;
  srand(time(NULL));

  try {
    if (myargs.trace_size > 0)
      std::cout << "WARNING: trace is not supported on the HRX backend yet; "
                   "running without trace.\n";

    Context &ctx = Context::get();
    LoadedKernel lk = load_kernel(myargs.xclbin, myargs.instr, myargs.kernel);

    Buffer bo_in1(ctx.stream, (size_t)IN1_VOLUME * sizeof(T1));
    Buffer bo_in2(ctx.stream, (size_t)IN2_VOLUME * sizeof(T2));
    Buffer bo_out(ctx.stream, (size_t)OUT_VOLUME * sizeof(T3));

    init_bufIn1(reinterpret_cast<T1 *>(bo_in1.host_ptr()), IN1_VOLUME);
    init_bufIn2(reinterpret_cast<T2 *>(bo_in2.host_ptr()), IN2_VOLUME);
    init_bufOut(reinterpret_cast<T3 *>(bo_out.host_ptr()), OUT_VOLUME);
    bo_in1.flush();
    bo_in2.flush();
    bo_out.flush();

    // Binding order mirrors the XRT wrapper: [in1, in2, out].
    std::vector<Buffer *> bindings = {&bo_in1, &bo_in2, &bo_out};

    unsigned num_iter = myargs.n_iterations + myargs.n_warmup_iterations;
    double npu_time_total = 0, npu_time_min = 1e30, npu_time_max = 0;
    int errors = 0;

    for (unsigned iter = 0; iter < num_iter; iter++) {
      double us = dispatch_once(lk, bindings);
      bo_out.invalidate();

      if (iter < (unsigned)myargs.n_warmup_iterations)
        continue;

      if (myargs.do_verify) {
        errors += verify_results(reinterpret_cast<T1 *>(bo_in1.host_ptr()),
                                 reinterpret_cast<T2 *>(bo_in2.host_ptr()),
                                 reinterpret_cast<T3 *>(bo_out.host_ptr()),
                                 IN1_VOLUME, myargs.verbosity);
      }
      npu_time_total += us;
      npu_time_min = us < npu_time_min ? us : npu_time_min;
      npu_time_max = us > npu_time_max ? us : npu_time_max;
    }

    report_timing(npu_time_total, npu_time_min, npu_time_max,
                  myargs.n_iterations);

    if (!errors) {
      std::cout << "\nPASS!\n\n";
      return 0;
    }
    std::cout << "\nError count: " << errors << "\n\n";
    std::cout << "\nFailed.\n\n";
    return 1;
  } catch (const std::exception &e) {
    std::cerr << "\nHRX error: " << e.what() << "\n\nFailed.\n\n";
    return 1;
  }
}

/*
 ******************************************************************************
 * HRX based test wrapper for 1 input and 1 output
 ******************************************************************************
 */
template <typename T1, typename T3, void (*init_bufIn1)(T1 *, int),
          void (*init_bufOut)(T3 *, int),
          int (*verify_results)(T1 *, T3 *, int, int)>
int setup_and_run_aie(int IN1_VOLUME, int OUT_VOLUME, struct args myargs,
                      bool enable_ctrl_pkts = false) {
  using namespace hrx_test;
  (void)enable_ctrl_pkts;
  srand(time(NULL));

  try {
    if (myargs.trace_size > 0)
      std::cout << "WARNING: trace is not supported on the HRX backend yet; "
                   "running without trace.\n";

    Context &ctx = Context::get();
    LoadedKernel lk = load_kernel(myargs.xclbin, myargs.instr, myargs.kernel);

    Buffer bo_in1(ctx.stream, (size_t)IN1_VOLUME * sizeof(T1));
    Buffer bo_out(ctx.stream, (size_t)OUT_VOLUME * sizeof(T3));

    init_bufIn1(reinterpret_cast<T1 *>(bo_in1.host_ptr()), IN1_VOLUME);
    init_bufOut(reinterpret_cast<T3 *>(bo_out.host_ptr()), OUT_VOLUME);
    bo_in1.flush();
    bo_out.flush();

    // Binding order mirrors the XRT wrapper: [in1, out].
    std::vector<Buffer *> bindings = {&bo_in1, &bo_out};

    unsigned num_iter = myargs.n_iterations + myargs.n_warmup_iterations;
    double npu_time_total = 0, npu_time_min = 1e30, npu_time_max = 0;
    int errors = 0;

    for (unsigned iter = 0; iter < num_iter; iter++) {
      double us = dispatch_once(lk, bindings);
      bo_out.invalidate();

      if (iter < (unsigned)myargs.n_warmup_iterations)
        continue;

      if (myargs.do_verify) {
        errors += verify_results(reinterpret_cast<T1 *>(bo_in1.host_ptr()),
                                 reinterpret_cast<T3 *>(bo_out.host_ptr()),
                                 IN1_VOLUME, myargs.verbosity);
      }
      npu_time_total += us;
      npu_time_min = us < npu_time_min ? us : npu_time_min;
      npu_time_max = us > npu_time_max ? us : npu_time_max;
    }

    report_timing(npu_time_total, npu_time_min, npu_time_max,
                  myargs.n_iterations);

    if (!errors) {
      std::cout << "\nPASS!\n\n";
      return 0;
    }
    std::cout << "\nError count: " << errors << "\n\n";
    std::cout << "\nFailed.\n\n";
    return 1;
  } catch (const std::exception &e) {
    std::cerr << "\nHRX error: " << e.what() << "\n\nFailed.\n\n";
    return 1;
  }
}

#endif // HRX_TEST_WRAPPER_H
