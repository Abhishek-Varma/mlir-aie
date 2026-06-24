# Running MLIR-AIE IRON (Python) & C++ tests on the **HRX** runtime

This package (`aie.utils.hostruntime.hrxruntime`) is a drop-in **HRX** host-runtime
backend for MLIR-AIE, a sibling of the default **XRT** backend. It dispatches
IRON designs and the C++ testbench on the AMD **XDNA2 NPU** through **`libhrx`**
(the IREE-based runtime with an `amdxdna` HAL) instead of XRT, consuming the
*identical* `aiecc` artifacts (`final.xclbin` + `insts.bin`).

This README is a **self-contained runbook**: follow it top-to-bottom to build and
run both the IRON (Python) end-to-end tests and the C++ on-hardware tests with
HRX. No source edits to example/test files are needed — HRX is selected by an
environment/make variable.

> TL;DR
> - **IRON / Python:** `IRON_RUNTIME=hrx python <example>.py …`
> - **C++:** `RUNTIME=hrx make run`
> - HRX is **auto-detected** (no env vars required if it's in a standard/sibling
>   location). The XADX helper `libironhrx_xadx.so` is **built on demand**.
> - `aiebu-asm` / XRT are **not** required at runtime (the patch table is parsed
>   straight from `insts.bin`; aiebu is only a fallback).

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| **XDNA2 NPU** (Strix / `npu4` / aie2p) | `/dev/accel/accel0` present; `amdxdna` driver loaded. |
| **HRX built** (`libhrx.so` + flatcc) | See §2. This is the runtime we dispatch through. |
| **MLIR-AIE installed from *this* branch** | So `import aie` works, `aiecc` can build examples, and the wheel includes this `hrxruntime` package + the `IRON_RUNTIME` switch. See §3. |
| **A C++ toolchain + CMake ≥ 3.30** | Only for the C++ tests. (`pip install "cmake>=3.30"` into the venv if the system cmake is older.) |
| **Peano / llvm-aie** | Needed by `aiecc` to build the example `.xclbin`/`insts.bin` (same as the XRT flow). |
| XRT userspace (`aiebu-asm`, pyxrt) | **Optional.** Only used as a fallback (see §7) and for the XRT baseline. HRX itself does not need it. |

The NPU + driver + Peano requirements are exactly the same as the normal (XRT)
MLIR-AIE flow — if XRT examples build/run on this box, HRX has what it needs too,
plus a built `libhrx`.

---

## 2. Build HRX (`libhrx.so` + flatcc)

Clone HRX, check out the `amdxdna` HAL branch (**PR #37**), and build it.

> **Placement (do this for zero-config detection):** clone HRX as a **sibling of
> `mlir-aie`** (i.e. into `<parent-of-mlir-aie>/hrx`). Auto-detection then finds
> it with no env vars. (`~/hrx`, `/opt/hrx`, `/usr/local/hrx` also work; anything
> else needs the env hints in §4.)

```bash
# from the directory that contains your mlir-aie checkout:
git clone https://github.com/ROCm/hrx-system.git hrx
cd hrx
gh pr checkout 37          # or: git fetch origin pull/37/head:amdxdna && git checkout amdxdna

python dev.py cmake setup
python dev.py cmake configure -DIREE_HAL_DRIVER_AMDXDNA=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
python dev.py cmake build
```

> **If `python dev.py cmake build` fails** on an unrelated `loom` `static_assert`
> in some other target, build just the runtime library — it's all HRX needs here:
> ```bash
> python dev.py cmake build --target libhrx_src_libhrx_hrx
> ```

**Verify** the build produced everything the MLIR-AIE HRX backend consumes (the
`libhrx.so` to dispatch through, plus the flatcc + generated schema used to build
the XADX helper):

```bash
B=build/cmake   # HRX build dir
ls $B/libhrx/src/libhrx/libhrx.so
ls $B/libflatcc_runtime.a
ls $B/_deps/flatcc-src/include
ls $B/runtime/src/iree/schemas/amdxdna_xclbin_executable_def_builder.h
nm -D $B/libhrx/src/libhrx/libhrx.so | grep -E 'hrx_buffer_map_persistent|hrx_stream_dispatch'
```

All five must exist. If you cloned HRX as a sibling of `mlir-aie`, you're done —
no env vars needed; skip to §3. Otherwise set the hints in §4.

---

## 3. Install MLIR-AIE (from this branch)

Install the `mlir_aie` + `llvm_aie` wheels per the project's normal instructions,
**built from this branch** so the wheel ships this `hrxruntime` package and the
`IRON_RUNTIME` selection logic in `aie/utils/__init__.py`.

Verify the imported `aie.utils` is the one from this branch (it must expose the
HRX hooks):

```bash
python3 - <<'PY'
import aie.utils as u, inspect
print("aie.utils from:", inspect.getfile(u))
print("has hrx hooks  :", hasattr(u, "has_hrx"))   # must be True
PY
```

> **Two-trees caveat.** If you instead use a *prebuilt* wheel and edit this source
> tree directly, the edits won't take effect until copied into the imported
> package dir (the path printed above). The robust fix is to **(re)build/install
> the wheel from this branch**. If you must hot-patch, copy:
> `python/utils/__init__.py` → `<aie>/utils/__init__.py` and
> `python/utils/hostruntime/hrxruntime/*` → `<aie>/utils/hostruntime/hrxruntime/`,
> then `rm -rf <aie>/utils/hostruntime/hrxruntime/__pycache__`.

---

## 4. Environment (optional — only if auto-detection fails)

Auto-detection (`FindHRX.cmake` for C++, `hrxruntime/discovery.py` for Python)
probes standard locations and a sibling `../hrx` checkout. If your HRX lives
elsewhere, export these **hints** (highest priority):

```bash
export HRX_DIR=<hrx>                         # checkout w/ libhrx/include/hrx_runtime.h
export HRX_BUILD=<hrx>/build/cmake           # build dir w/ libflatcc_runtime.a
export LIBHRX_DIR=$HRX_BUILD/libhrx/src/libhrx
export LD_LIBRARY_PATH=$LIBHRX_DIR:$LD_LIBRARY_PATH
```

**Verify HRX is discoverable.** Primary check (robust — prints `True`/`False`
even when HRX is missing; this loads `libhrx` but does **not** init the device or
run a kernel):

```bash
python3 -c "import aie.utils as u; print('has_hrx:', u.has_hrx)"   # -> True
```

If `has_hrx` is `True`, you're ready. To see the resolved paths (only meaningful
once HRX is found):

```bash
python3 -c "from aie.utils.hostruntime.hrxruntime import discovery as d; \
print('libhrx :', d.find_libhrx()); print('build  :', d.find_hrx_build())"
```

The XADX helper `libironhrx_xadx.so` is compiled automatically on first use
(Python) or by the CMake build (C++).

---

## 5. Run IRON (Python) end-to-end tests with HRX

Select the backend with **`IRON_RUNTIME=hrx`**. Everything else is the normal
IRON flow. Two common shapes:

### 5a. `test.py`-driven example

```bash
cd programming_examples/basic/vector_scalar_mul
make                                  # builds build/final_8192.xclbin + build/insts_8192.bin (aiecc)
IRON_RUNTIME=hrx python3 test.py \
  --xclbin build/final_8192.xclbin --instr build/insts_8192.bin \
  --kernel MLIR_AIE --in1-size 8192 --in2-size 4 --out-size 8192
# expect: PASS!
```

### 5b. `@iron.jit` self-running example

```bash
cd programming_examples/basic/vector_vector_add
IRON_RUNTIME=hrx python3 vector_vector_add.py     # expect: PASS!
```

### Backend selection semantics (`IRON_RUNTIME`)

- `hrx`  — force HRX; clear error if `libhrx` can't be found.
- `xrt`  — force XRT (default upstream behavior).
- `auto` *(default when unset)* — XRT if available, else HRX, else CPU.

Quick smoke test (no hardware dispatch — just import + selection):

```bash
IRON_RUNTIME=hrx python3 -c "import aie.utils as u; print(u.DEFAULT_TENSOR_CLASS.__name__)"
# -> HRXTensor
```

Designs known to pass on HRX: `vector_scalar_mul`, `vector_vector_add`,
`vector_scalar_add`, `vector_reduce_add`, `passthrough_dmas`,
`vector_reduce_max`.

### 5c. Multi-dispatch chains / runlists (`run_chain`)

`HRXHostRuntime.run_chain([(handle, args), ...])` is the HRX analogue of
`xrt::runlist` (the XRT `test_runlist.cpp` testbench): it records several
dispatches into one HRX command buffer — in order, with an execution + memory
barrier between them — and submits the whole batch with a single
`synchronize`. The amdxdna HAL lowers the multi-dispatch command buffer into one
`ERT_CMD_CHAIN`. Because of the barrier, a later run observes an earlier run's
device writes, so producer→consumer chains work (one run's output buffer is the
next run's input). Entries may share one `handle` (re-dispatch the same kernel)
or use different handles (a true multi-kernel pipeline).

A chained test mirroring `test_runlist.cpp` (`run0: out0 = in+1`,
`run1: out1 = out0+1`, plus a deeper N-link chain):

```bash
cd programming_examples/basic/vector_scalar_add
make all                              # build/final.xclbin + build/insts.bin
IRON_RUNTIME=hrx python3 test_chain_hrx.py \
  --xclbin build/final.xclbin --instr build/insts.bin --kernel MLIR_AIE
# expect: PASS!
```

---

## 6. Run C++ on-hardware tests with HRX

Select the backend with **`RUNTIME=hrx`** on the example's `make`. This builds the
C++ host exe against `libhrx` (no XRT SDK headers needed) and runs it on the NPU.

```bash
cd programming_examples/basic/vector_reduce_max/single_core_designs
make all                       # aiecc: build/final.xclbin + build/insts.bin
RUNTIME=hrx make run           # build C++ host vs HRX + run on the NPU
# expect: ... PASS!
```

What `RUNTIME=hrx` does under the hood (no per-example edits):
- `makefile-common` adds `-DUSE_HRX=ON` to every `build_host_exe` cmake call.
- `programming_examples/common.cmake` runs `find_package(HRX)`, compiles the XADX
  helper `libironhrx_xadx.so` from source (ABI-matched to your `libhrx`), links
  `libhrx.so` + the helper, and defines `TEST_UTILS_USE_HRX` so the shared
  `xrt_test_wrapper.h` pulls in `hrx_test_wrapper.h`. A dummy `xrt_coreutil`
  INTERFACE target neutralizes the examples' `-lxrt_coreutil`.

> If the system `cmake` is < 3.30, install a newer one (`pip install "cmake>=3.30"`)
> and ensure it's first on `PATH` before `make`.

`make run` builds the host exe into `_build/` (alongside the freshly compiled
`_build/libironhrx_xadx.so`, on the exe's RPATH), copies it to
`./<targetname>.exe`, and runs it. You can re-run it directly:

```bash
./vector_reduce_max.exe -x build/final.xclbin -i build/insts.bin -k MLIR_AIE
```

> **Switching backends?** If you previously built an example (XRT or an older
> HRX build) and then hit `error while loading shared libraries:
> libironhrx_xadx.so` at run time, you have a **stale exe** with an outdated
> RPATH. Run `make clean` first, then `RUNTIME=hrx make run`. (A fresh checkout
> has no stale artifacts, so this only bites when reusing a build tree.)

### 6b. C++ multi-dispatch chains / runlists

`hrx_test::dispatch_chain({{&lk, {&a, &b}}, ...})` (in `hrx_test_wrapper.h`) is
the C++ analogue of `xrt::runlist` (the XRT `test_runlist.cpp` testbench) and of
the Python `run_chain`: it records several kernel dispatches into one HRX command
buffer — in order, with an execution + memory barrier between them — then submits
the batch with a single `synchronize` (the amdxdna HAL lowers it into one
`ERT_CMD_CHAIN`). A later run sees an earlier run's device writes, so
producer→consumer chains work; entries may share one `LoadedKernel` or use
different ones (a multi-kernel pipeline).

A chained testbench mirroring `test_runlist.cpp` (`run0: out0 = in+1`,
`run1: out1 = out0+1`, where `run1`'s input is `run0`'s output) lives in
`vector_scalar_add`. Its make target self-selects the HRX backend
(`-DUSE_HRX=ON`), so you don't even need `RUNTIME=hrx`:

```bash
cd programming_examples/basic/vector_scalar_add
make all                       # build/final.xclbin + build/insts.bin
make run_runlist_hrx           # build the HRX chain testbench + run on the NPU
# expect: ... PASS!
```

> The HRX runlist testbench reads `insts.bin` directly (the wrapper parses the
> TXN itself — no aiebu/ELF needed), unlike the XRT `run_runlist` target which
> consumes `insts.elf`.

---

## 7. How patch tables work (and why aiebu isn't required)

HRX's `amdxdna` COMMAND_CHAIN path host-patches each I/O buffer's device address
into the control code using a **patch table** of `(offset, arg_idx, addend)`
triples. The control code is the raw `insts.bin` TXN words; the patch table is
parsed **directly from the TXN's embedded `DDR_PATCH` ops** (`parse_txn` in
`__init__.py` for Python, `parse_txn()` in `hrx_test_wrapper.h` for C++).

Without the patch table the NPU writes to address 0 → **all-zero output**. The
direct parser is validated byte-for-byte against `aiebu-asm`.

- The direct parser handles TXN **v0.1** (what `aiecc` emits today).
- If it ever meets an unknown TXN version/opcode, it falls back to the
  `aiebu-asm` round-trip (needs XRT's `aiebu-asm`).
- Force the fallback for debugging with `IRON_HRX_FORCE_AIEBU=1` (Python and C++).

---

## 8. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `IRON_RUNTIME=hrx … ImportError: libhrx.so could not be located` | HRX not found. Build it (§2), place it as a sibling `../hrx`, or set `HRX_DIR`/`LIBHRX_DIR` (§4). Verify with the §4 probe. |
| C++ configure: `USE_HRX=ON but the HRX runtime was not found` | Same as above (CMake side). Set `HRX_DIR`/`LIBHRX_DIR` or co-locate `../hrx`. |
| `Cannot provide the XADX helper … flatcc/schema not found` | Your `HRX_BUILD` is incomplete. Ensure `libflatcc_runtime.a`, `_deps/flatcc-src/include`, and the generated `amdxdna_xclbin_executable_def_builder.h` exist (§2). |
| Output is **all zeros** but no error | Patch table empty/incorrect. Confirm you're on this branch; check `parse_txn` ran (unset `IRON_HRX_FORCE_AIEBU`). File a note — the design's arg order may be unusual. |
| `has_hrx`/import works but run hangs or `hrx_stream_synchronize … INTERNAL` | Possible NPU wedge (see §9). Recover with a driver reload. |
| `import aie.utils` lacks `has_hrx` | You're importing an upstream wheel, not this branch (§3 two-trees caveat). |
| C++ run: `error while loading shared libraries: libironhrx_xadx.so` | **Stale exe** from a previous build with an outdated RPATH. `make clean`, then `RUNTIME=hrx make run` (rebuilds the helper into `_build/` on the exe's RPATH). |
| C++ build: `CMake 3.30 or higher is required` | Old system cmake; install `cmake>=3.30` and put it first on `PATH`. |
| `aiebu-asm not found` (only if the direct parser fell back) | Either install XRT (`/opt/xilinx/xrt/bin/aiebu-asm`) or report the unhandled TXN version so `parse_txn` can learn it. |

---

## 9. Safety: do not wedge the NPU

The NPU is a **single shared device**. On dev firmware, tight repeated dispatch
or `SIGKILL`-ing a process **mid-dispatch** can wedge the device so *both* XRT and
HRX then fail even a single run. Guidelines:

- Prefer **single dispatches / small loops** when validating.
- **Never** `SIGKILL` a running dispatch; let it finish or time out.
- Recover a wedged device with a driver reload:
  ```bash
  sudo rmmod amdxdna && sudo modprobe amdxdna   # then re-check with one run
  ```

---

## 10. Files in this package

| File | Role |
|---|---|
| `__init__.py` | ctypes bindings to `libhrx` + the XADX helper; `HRXContext` (device/stream/buffer/exe/dispatch); `parse_txn` (direct TXN → control code + patch table); `parse_control_elf` + `insts_to_control_elf` (aiebu fallback); `control_code_and_patch_table` dispatcher. |
| `hostruntime.py` | `HRXHostRuntime` / `CachedHRXRuntime` (the IRON `HostRuntime` implementation). |
| `tensor.py` | `HRXTensor` (persistent host-mapped device buffer, zero-copy numpy view). |
| `discovery.py` | Path-only HRX discovery (no dlopen): `find_libhrx`/`find_hrx_dir`/`find_hrx_build`/`ensure_xadx_helper`/`hrx_available`. |
| `iron_hrx_xadx.cpp` | Tiny C-ABI helper that serializes the amdxdna XADX flatbuffer (built into `libironhrx_xadx.so`). |
| `build_xadx_helper.sh` | Builds `libironhrx_xadx.so` (invoked automatically on first Python use). |

Related (outside this package):
- `cmake/modules/FindHRX.cmake` — HRX auto-detection for CMake.
- `programming_examples/common.cmake` — `USE_HRX` wiring + builds the XADX helper.
- `programming_examples/makefile-common` — the `RUNTIME=xrt|hrx` switch.
- `runtime_lib/test_lib/hrx_test_wrapper.h` — C++ HRX backend (mirrors `xrt_test_wrapper.h`).
