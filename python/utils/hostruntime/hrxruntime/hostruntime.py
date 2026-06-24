# SPDX-FileCopyrightText: Copyright (C) 2026 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
HRX-based implementation of the HostRuntime.

Drop-in sibling of ``XRTHostRuntime``: it consumes the *same* artifacts
(``final.xclbin`` + ``insts.bin``) produced by ``aiecc``, but dispatches through
``libhrx`` instead of XRT. The single XRT dispatch line

    h = kernel(3, insts_bo, insts_bytes, *buffers); r = h.wait()

maps 1:1 onto:

    insts.bin words -> XADX control_code  (one executable, cached by content)
    I/O tensors     -> bindings           (group_id order = arg_idx - 3)
    hrx_stream_dispatch(...) + hrx_stream_synchronize(...)

HRX host-patches the buffer addresses into the control code from binding order +
the TXN's own DDR-patch ops (npu4 COMMAND_CHAIN path).
"""

import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from ..hostruntime import HostRuntime, HostRuntimeError, KernelHandle, KernelResult
from .tensor import HRXTensor
from . import HRXContext, HRXError, control_code_and_patch_table

if TYPE_CHECKING:
    from aie.iron.device import Device

logger = logging.getLogger(__name__)


class HRXKernelHandle(KernelHandle):
    """Handle for a loaded HRX executable (one XADX export)."""

    def __init__(self, executable, export_ordinal, kernel_name, xclbin_path, insts_path):
        self.executable = executable
        self.export_ordinal = export_ordinal
        self.kernel_name = kernel_name
        self.xclbin_path = xclbin_path
        self.insts_path = insts_path


class HRXKernelResult(KernelResult):
    """Result wrapper for an HRX dispatch.

    HRX raises (via ``_check``) on a non-OK dispatch/sync, so reaching
    construction means the run completed.
    """

    def __init__(self, npu_time, success=True, trace_config=None):
        super().__init__(npu_time, trace_config)
        self._success = success

    def is_success(self) -> bool:
        return self._success


class HRXHostRuntime(HostRuntime):
    """HostRuntime that dispatches IRON designs through HRX (libhrx + amdxdna)."""

    _tensor_class = HRXTensor

    def __init__(self):
        self._ctx = HRXContext.get()
        # Executable cache keyed by (xclbin_path, xclbin_mtime, insts_path,
        # insts_mtime) — the HRX analogue of CachedXRTRuntime's context cache.
        self._exe_cache = OrderedDict()
        self._cache_size = int(os.environ.get("HRX_EXE_CACHE_SIZE", "32"))
        # Device generation: amdxdna targets Strix (npu2). Overridable.
        self._device_gen = os.environ.get("IRON_HRX_DEVICE", "npu2")

    def load(self, npu_kernel, **kwargs) -> HRXKernelHandle:
        self.check_device_consistency()
        xclbin_path = Path(npu_kernel.xclbin_path).resolve()
        insts_path = Path(npu_kernel.insts_path).resolve()
        kernel_name = npu_kernel.kernel_name or "MLIR_AIE"

        if not xclbin_path.exists() or not xclbin_path.is_file():
            raise HostRuntimeError(
                f"xclbin {xclbin_path} does not exist or is not a file."
            )
        if not insts_path.exists() or not insts_path.is_file():
            raise HostRuntimeError(
                f"insts {insts_path} does not exist or is not a file."
            )

        key = (
            str(xclbin_path),
            xclbin_path.stat().st_mtime,
            str(insts_path),
            insts_path.stat().st_mtime,
            kernel_name,
        )
        if key in self._exe_cache:
            self._exe_cache.move_to_end(key)
            exe, ordv = self._exe_cache[key]
            return HRXKernelHandle(
                exe, ordv, kernel_name, xclbin_path, insts_path
            )

        xclbin_bytes = xclbin_path.read_bytes()
        # HRX needs the (offset, arg_idx, addend) patch table so it can host-patch
        # each I/O buffer's device address into the control code. The raw insts.bin
        # carries the addresses only as embedded DDR_PATCH ops, so we extract the
        # patch table from the TXN directly (no aiebu-asm), falling back to the
        # aiebu control-ELF round-trip only for TXN versions the direct parser
        # doesn't know. Without the patch table the NPU writes to address 0 and the
        # output is all zeros.
        try:
            cc_words, patch_table = control_code_and_patch_table(
                insts_path, scalar_args=3
            )
            xadx = self._ctx.build_xadx(
                xclbin_bytes, cc_words, kernel_name, patch_table=patch_table
            )
            exe = self._ctx.load_executable(xadx)
            ordv = self._ctx.lookup_export(exe, kernel_name)
        except HRXError as e:
            raise HostRuntimeError(f"HRX failed to load kernel: {e}") from e

        if len(self._exe_cache) >= self._cache_size:
            _, (old_exe, _) = self._exe_cache.popitem(last=False)
            self._ctx.release_executable(old_exe)
        self._exe_cache[key] = (exe, ordv)

        return HRXKernelHandle(exe, ordv, kernel_name, xclbin_path, insts_path)

    def run(
        self,
        kernel_handle: KernelHandle,
        args,
        trace_config=None,
        fail_on_error: bool = True,
        only_if_loaded: bool = False,
        **kwargs,
    ) -> HRXKernelResult:
        assert isinstance(kernel_handle, HRXKernelHandle)
        self.check_device_consistency()

        args = [a for a in args if not callable(a)]
        if not all(isinstance(a, self._tensor_class) for a in args):
            raise HostRuntimeError(
                f"The {self.__class__.__name__} can only take "
                f"{self._tensor_class.__name__} as arguments, but got: {args}"
            )

        # Push host-side inputs to the device. flush_range is a cheap clflush on
        # the persistent mapping (no copy); doing it for every binding is safe
        # (outputs are about to be overwritten) and guarantees correctness even
        # when callers mutate tensor .data in place without going through to().
        for a in args:
            a.to("npu")
            a._sync_to_device()

        bindings = [(a.buffer_object(), a.nbytes_alloc()) for a in args]

        start = time.time_ns()
        try:
            self._ctx.dispatch(
                kernel_handle.executable, kernel_handle.export_ordinal, bindings
            )
            self._ctx.synchronize()
        except HRXError as e:
            if fail_on_error:
                raise HostRuntimeError(f"HRX dispatch failed: {e}") from e
            stop = time.time_ns()
            return HRXKernelResult(stop - start, success=False)
        stop = time.time_ns()

        # Outputs were written on-device; the persistent host mapping is stale.
        # Leave the tensors marked device="npu" so the next host read
        # (numpy()/to("cpu")) invalidates the cache via _sync_from_device.
        for a in args:
            a.device = "npu"

        return HRXKernelResult(stop - start, success=True)

    def device(self) -> "Device":
        from aie.iron.device import from_name

        return from_name(self._device_gen, n_cols=None)


class CachedHRXRuntime(HRXHostRuntime):
    """Alias matching the XRT naming (CachedXRTRuntime).

    HRXHostRuntime already caches executables, so this is just the public,
    cache-by-default entry point used by the default-runtime selector.
    """

    pass
