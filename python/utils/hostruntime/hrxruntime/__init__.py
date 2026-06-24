# SPDX-FileCopyrightText: Copyright (C) 2026 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
ctypes bindings for the HRX C ABI (``libhrx.so``) plus the small XADX builder
helper (``libironhrx_xadx.so``).

This is the HRX analogue of pyxrt for the IRON host stack: it binds only the
handful of ``hrx_*`` entry points the dispatch path needs (the same set the
FastFlowLM interposer proved), and wraps them in a tiny ``HRXContext`` singleton
that owns the device + stream and builds/loads XADX executables.

Library discovery order for ``libhrx.so``:
  1. ``$HRX_LIBHRX``                       (explicit full path to the .so)
  2. ``$LIBHRX_DIR/libhrx.so``             (set by activate_env.sh)
  3. ``$HRX_BUILD/libhrx/src/libhrx/libhrx.so``
  4. plain ``libhrx.so`` via the loader (LD_LIBRARY_PATH)
"""

import ctypes
import logging
import os
from pathlib import Path

from .discovery import ensure_xadx_helper, find_libhrx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enum / flag constants (mirror hrx_runtime.h; values match IREE HAL).
# ---------------------------------------------------------------------------
HRX_MEMORY_TYPE_HOST_LOCAL = 0x00000046
HRX_MEMORY_TYPE_DEVICE_VISIBLE = 0x00000010

HRX_BUFFER_USAGE_DEFAULT = 0x00000C03
HRX_BUFFER_USAGE_MAPPING_PERSISTENT = 0x02000000
HRX_BUFFER_USAGE_MAPPING_SCOPED = 0x01000000

HRX_MAP_READ = 0x01
HRX_MAP_WRITE = 0x02

HRX_DISPATCH_FLAG_NONE = 0

# The HAL executable format string for the amdxdna xclbin-based package that
# build_xadx() produces (same string the FLM shim passes).
HRX_AMDXDNA_FORMAT = b"amdxdna-xclbin-fb"


class HRXError(RuntimeError):
    """Raised when an hrx_* call returns a non-OK status."""


# ---------------------------------------------------------------------------
# ctypes struct mirrors
# ---------------------------------------------------------------------------
class HrxDispatchConfig(ctypes.Structure):
    _fields_ = [
        ("workgroup_count", ctypes.c_uint32 * 3),
        ("workgroup_size", ctypes.c_uint32 * 3),
        ("subgroup_size", ctypes.c_uint32),
    ]


class HrxBufferRef(ctypes.Structure):
    _fields_ = [
        ("buffer", ctypes.c_void_p),
        ("offset", ctypes.c_size_t),
        ("length", ctypes.c_size_t),
    ]


def _load_libhrx() -> ctypes.CDLL:
    last_err = None
    tried = []
    # Auto-detected path first (env hints + standard locations), then a bare
    # name so the dynamic loader's LD_LIBRARY_PATH search still works.
    for c in [find_libhrx(), "libhrx.so"]:
        if not c:
            continue
        tried.append(c)
        try:
            return ctypes.CDLL(c, mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            last_err = e
    raise HRXError(
        f"Could not load libhrx.so (tried: {tried}). "
        f"Install HRX to a standard location, set HRX_DIR/LIBHRX_DIR, or add "
        f"libhrx to LD_LIBRARY_PATH. Last error: {last_err}"
    )


def _load_xadx_helper() -> ctypes.CDLL:
    # Auto-detect a prebuilt helper; if absent, build it on first use from the
    # detected HRX tree (no manual step). Falls back to the loader search path.
    candidates = [ensure_xadx_helper(), "libironhrx_xadx.so"]
    last_err = None
    tried = []
    for c in candidates:
        if not c:
            continue
        tried.append(c)
        try:
            return ctypes.CDLL(c)
        except OSError as e:
            last_err = e
    raise HRXError(
        f"Could not load libironhrx_xadx.so (tried: {tried}). "
        f"It is built on first use from the HRX tree; ensure HRX_DIR/HRX_BUILD "
        f"point at a full HRX checkout + build, or run build_xadx_helper.sh. "
        f"Last error: {last_err}"
    )


_lib = _load_libhrx()
_xadx = _load_xadx_helper()

# Status is an opaque pointer; NULL == OK.
_status_t = ctypes.c_void_p
_handle = ctypes.c_void_p


def _decl(fn, restype, argtypes):
    f = getattr(_lib, fn)
    f.restype = restype
    f.argtypes = argtypes
    return f


# Status helpers
_hrx_status_code = _decl("hrx_status_code", ctypes.c_int, [_status_t])
_hrx_status_to_string = _decl(
    "hrx_status_to_string",
    _status_t,
    [_status_t, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_size_t)],
)
_hrx_status_free_message = _decl(
    "hrx_status_free_message", None, [ctypes.c_char_p]
)
_hrx_status_ignore = _decl("hrx_status_ignore", None, [_status_t])

# Lifecycle
_hrx_gpu_initialize = _decl("hrx_gpu_initialize", _status_t, [ctypes.c_uint32])
_hrx_gpu_device_get = _decl(
    "hrx_gpu_device_get", _status_t, [ctypes.c_int, ctypes.POINTER(_handle)]
)
_hrx_stream_create = _decl(
    "hrx_stream_create",
    _status_t,
    [_handle, ctypes.c_uint32, ctypes.POINTER(_handle)],
)

# Buffers
_hrx_buffer_allocate = _decl(
    "hrx_buffer_allocate",
    _status_t,
    [
        _handle,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_handle),
    ],
)
_hrx_buffer_map_persistent = _decl(
    "hrx_buffer_map_persistent",
    _status_t,
    [_handle, ctypes.c_uint16, ctypes.POINTER(ctypes.c_void_p)],
)
_hrx_buffer_flush_range = _decl(
    "hrx_buffer_flush_range",
    _status_t,
    [_handle, ctypes.c_size_t, ctypes.c_size_t],
)
_hrx_buffer_invalidate_range = _decl(
    "hrx_buffer_invalidate_range",
    _status_t,
    [_handle, ctypes.c_size_t, ctypes.c_size_t],
)
_hrx_buffer_release = _decl("hrx_buffer_release", None, [_handle])
_hrx_stream_copy_h2d = _decl(
    "hrx_stream_copy_h2d",
    _status_t,
    [_handle, ctypes.c_void_p, _handle, ctypes.c_size_t, ctypes.c_size_t],
)

# Executables
_hrx_executable_load_data = _decl(
    "hrx_executable_load_data",
    _status_t,
    [
        _handle,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.POINTER(_handle),
    ],
)
_hrx_executable_lookup_export_by_name = _decl(
    "hrx_executable_lookup_export_by_name",
    _status_t,
    [_handle, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)],
)
_hrx_executable_release = _decl("hrx_executable_release", None, [_handle])

# Dispatch / sync
_hrx_stream_dispatch = _decl(
    "hrx_stream_dispatch",
    _status_t,
    [
        _handle,
        _handle,
        ctypes.c_uint32,
        ctypes.POINTER(HrxDispatchConfig),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(HrxBufferRef),
        ctypes.c_size_t,
        ctypes.c_uint32,
    ],
)
_hrx_stream_synchronize = _decl("hrx_stream_synchronize", _status_t, [_handle])

# XADX builder helper
_iron_build_xadx = _xadx.iron_build_xadx
_iron_build_xadx.restype = ctypes.c_int
_iron_build_xadx.argtypes = [
    ctypes.c_void_p,  # xclbin
    ctypes.c_size_t,  # xclbin_n
    ctypes.c_void_p,  # cc
    ctypes.c_size_t,  # cc_n
    ctypes.c_void_p,  # patch (flat uint32 triples) or NULL
    ctypes.c_size_t,  # patch_n (element count = 3 * n_relocs)
    ctypes.c_char_p,  # entry_name
    ctypes.POINTER(ctypes.c_void_p),  # out
    ctypes.POINTER(ctypes.c_size_t),  # out_n
]
_iron_free_xadx = _xadx.iron_free_xadx
_iron_free_xadx.restype = None
_iron_free_xadx.argtypes = [ctypes.c_void_p]


def _check(status, what: str):
    """Raise HRXError if status is non-OK (non-NULL)."""
    if not status:  # NULL == OK
        return
    msg_ptr = ctypes.c_char_p()
    msg_len = ctypes.c_size_t()
    s2 = _hrx_status_to_string(
        status, ctypes.byref(msg_ptr), ctypes.byref(msg_len)
    )
    code = _hrx_status_code(status)
    text = msg_ptr.value.decode("utf-8", "replace") if msg_ptr.value else "?"
    # to_string may allocate the message; free it.
    if msg_ptr.value:
        _hrx_status_free_message(msg_ptr)
    _hrx_status_ignore(s2)
    _hrx_status_ignore(status)
    raise HRXError(f"{what} failed (hrx status code {code}): {text}")


# ---------------------------------------------------------------------------
# Direct TXN -> (control_code, patch_table)   [no aiebu dependency]
# ---------------------------------------------------------------------------
class TxnUnsupportedError(HRXError):
    """Raised when an insts.bin TXN stream can't be parsed directly.

    Signals the caller to fall back to the aiebu-asm round-trip
    (:func:`insts_to_control_elf` + :func:`parse_control_elf`).
    """


# XAie_TxnOpcode values (mlir-aie lib/Targets/AIETargetNPU.cpp).
_TXN_OP_WRITE = 0x00
_TXN_OP_BLOCKWRITE = 0x01
_TXN_OP_MASKWRITE = 0x03
_TXN_OP_PREEMPT = 0x06
_TXN_OP_TCT = 0x80  # XAIE_IO_CUSTOM_OP_BEGIN
_TXN_OP_DDR_PATCH = 0x81  # XAIE_IO_CUSTOM_OP_DDR_PATCH
_TXN_HEADER_BYTES = 16


def parse_txn(insts_bytes: bytes):
    """Extract ``(control_code, patch_table)`` straight from a raw ``insts.bin``.

    This removes the runtime dependency on ``aiebu-asm``: instead of converting
    the TXN to an aiebu control ELF and reading its ``.rela.dyn`` (see
    :func:`parse_control_elf`), it walks the TXN op stream itself and derives the
    same patch table that aiebu would emit. Validated byte-for-byte against
    ``aiebu-asm -t aie2txn`` output.

    Background (op layout from ``AIETargetNPU.cpp`` / ``AIEToConfiguration.cpp``):

      * The aiebu ``.ctrltext`` is the raw TXN bytes verbatim, so the **control
        code is just the ``insts.bin`` words**.
      * 16-byte header: ``major=byte[0]``, ``minor=byte[1]``, ``num_ops`` @ byte
        8, ``txn_size`` @ byte 12. Only TXN v0.1 is currently emitted/handled.
      * ``BLOCKWRITE`` (op 0x01): base addr @ +8, opSize @ +12, then a payload of
        32-bit words starting @ +16; payload word ``j`` programs absolute address
        ``base + 4*j``.
      * ``DDR_PATCH`` (op 0x81): opSize @ +4, ``addr`` @ +24, ``argIdx`` @ +32,
        ``argPlus`` @ +40. ``argIdx`` is already the 0-based buffer/binding index
        (aiebu would turn it into the XRT arg index ``argIdx + 3``).

    The relocation lands on the *low* word of the BD's 64-bit DDR address, i.e.
    the TXN word at absolute address ``addr - 4``; its byte position in the
    control code is the patch ``offset``.

    Returns ``(control_code: np.uint32[], patch_table: np.uint32[] flat triples)``
    identical in shape/meaning to :func:`parse_control_elf`.
    Raises :class:`TxnUnsupportedError` on any unrecognized header/opcode.
    """
    import struct

    import numpy as np

    d = insts_bytes
    if len(d) < _TXN_HEADER_BYTES:
        raise TxnUnsupportedError("insts.bin too small for a TXN header")

    def r32(o):
        return struct.unpack_from("<I", d, o)[0]

    def s32(o):
        return struct.unpack_from("<i", d, o)[0]

    major, minor = d[0], d[1]
    if (major, minor) != (0, 1):
        raise TxnUnsupportedError(
            f"unsupported TXN version {major}.{minor} (only 0.1 handled directly)"
        )

    # Map absolute AIE address -> byte offset of the word that programs it, so a
    # DDR_PATCH's target address resolves to a patch offset in the control code.
    addr_to_off = {}
    patches = []  # (target_addr, arg_idx, addend)

    i = _TXN_HEADER_BYTES
    try:
        while i < len(d):
            opc = d[i]
            start = i
            if opc == _TXN_OP_WRITE:  # 24 bytes
                addr_lo = r32(i + 8)
                addr_to_off[addr_lo] = i + 16
                i += r32(i + 20)
            elif opc == _TXN_OP_BLOCKWRITE:
                base = r32(i + 8)
                op_size = r32(i + 12)
                if op_size < 16:
                    raise TxnUnsupportedError("malformed BLOCKWRITE op size")
                for j in range((op_size - 16) // 4):
                    addr_to_off[base + 4 * j] = i + 16 + 4 * j
                i += op_size
            elif opc == _TXN_OP_MASKWRITE:  # 28 bytes
                i += r32(i + 24)
            elif opc == _TXN_OP_TCT:
                i += r32(i + 4)
            elif opc == _TXN_OP_DDR_PATCH:
                op_size = r32(i + 4)
                if op_size < 44:
                    raise TxnUnsupportedError("malformed DDR_PATCH op size")
                patches.append((r32(i + 24), s32(i + 32), s32(i + 40)))
                i += op_size
            elif opc == _TXN_OP_PREEMPT:  # 8-byte TxnPreemptHeader
                i += 8
            else:
                raise TxnUnsupportedError(f"unhandled TXN opcode {hex(opc)} @ {i}")
            if i <= start:
                raise TxnUnsupportedError(f"zero-size TXN op {hex(opc)} @ {start}")
    except struct.error as e:
        raise TxnUnsupportedError(f"TXN truncated while parsing: {e}") from e

    control_code = np.frombuffer(d, dtype=np.uint32).copy()

    patch = []
    for target_addr, arg_idx, addend in patches:
        # The patch lands on the low DDR-address word, at absolute addr-4.
        off = addr_to_off.get(target_addr - 4)
        if off is None:
            raise TxnUnsupportedError(
                f"DDR_PATCH target {hex(target_addr)} has no preceding write "
                f"(can't resolve patch offset)"
            )
        if arg_idx >= 0:
            patch.extend((off, arg_idx, addend & 0xFFFFFFFF))
    patch_table = np.asarray(patch, dtype=np.uint32)
    return control_code, patch_table


# ---------------------------------------------------------------------------
# Control-ELF -> (control_code, patch_table)
# ---------------------------------------------------------------------------
def parse_control_elf(elf_bytes: bytes, scalar_args: int = 3):
    """Extract the TXN control code and buffer patch table from a control ELF.

    This is a Python port of ``parse_control_elf`` in the FastFlowLM interposer
    (``hrx-integration/src/xrt_coreutil_shim.cpp``). The ELF is the ``aiebu``
    instruction-transaction blob ``aiecc``/``aiebu-asm`` produces from the
    mlir-aie TXN stream:

      * ``.ctrltext``  -> the TXN control code (uint32 words)
      * ``.rela.dyn``  -> one relocation per I/O buffer reference; each symbol
        name is the *XRT kernel arg index* as a decimal string. The user
        buffers start at arg ``scalar_args`` (3 = opcode, instr_bo, instr_size),
        so ``arg_idx = atoi(name) - scalar_args`` indexes the dispatch bindings.

    Returns ``(control_code, patch_table)`` where ``control_code`` is a
    ``numpy.uint32`` array and ``patch_table`` is a flat numpy array of
    ``(offset, arg_idx, addend)`` uint32 triples, ready for :meth:`build_xadx`.
    """
    import struct

    import numpy as np

    d = elf_bytes
    if len(d) < 52 or d[0] != 0x7F or d[1:4] != b"ELF":
        raise HRXError("control ELF is not a valid ELF32 file")

    def rd(fmt, off):
        return struct.unpack_from(fmt, d, off)[0]

    # ELF32 header fields (little-endian).
    e_shoff = rd("<I", 0x20)
    e_shentsize = rd("<H", 0x2E)
    e_shnum = rd("<H", 0x30)
    e_shstrndx = rd("<H", 0x32)

    def sh(i, f):
        return e_shoff + i * e_shentsize + f

    shstr_off = rd("<I", sh(e_shstrndx, 0x10))

    def sname(i):
        nm = rd("<I", sh(i, 0))
        end = d.index(b"\x00", shstr_off + nm)
        return d[shstr_off + nm : end].decode("ascii", "replace")

    ctrl = rela = dynsym = dynstr = -1
    for i in range(e_shnum):
        n = sname(i)
        if n == ".ctrltext":
            ctrl = i
        elif n == ".rela.dyn":
            rela = i
        elif n == ".dynsym":
            dynsym = i
        elif n == ".dynstr":
            dynstr = i
    if ctrl < 0:
        raise HRXError("control ELF has no .ctrltext section")

    coff = rd("<I", sh(ctrl, 0x10))
    csize = rd("<I", sh(ctrl, 0x14))
    control_code = np.frombuffer(d[coff : coff + csize], dtype=np.uint32).copy()

    patch = []
    if rela >= 0 and dynsym >= 0 and dynstr >= 0:
        roff = rd("<I", sh(rela, 0x10))
        rsize = rd("<I", sh(rela, 0x14))
        symoff = rd("<I", sh(dynsym, 0x10))
        symentsz = rd("<I", sh(dynsym, 0x24)) or 16
        strtab = rd("<I", sh(dynstr, 0x10))
        o = 0
        while o + 12 <= rsize:
            r_offset = rd("<I", roff + o + 0)
            r_info = rd("<I", roff + o + 4)
            r_addend = rd("<i", roff + o + 8)
            symidx = r_info >> 8
            st_name = rd("<I", symoff + symidx * symentsz)
            end = d.index(b"\x00", strtab + st_name)
            name = d[strtab + st_name : end].decode("ascii", "replace")
            try:
                arg_idx = int(name) - scalar_args
            except ValueError:
                o += 12
                continue
            if arg_idx >= 0:
                patch.extend((r_offset, arg_idx, r_addend & 0xFFFFFFFF))
            o += 12
    patch_table = np.asarray(patch, dtype=np.uint32)
    return control_code, patch_table


def insts_to_control_elf(insts_path) -> bytes:
    """Convert a raw mlir-aie ``insts.bin`` TXN stream into a control ELF.

    Prefers a sibling ``<stem>.elf`` if it already exists (e.g. emitted by
    ``aiecc --aie-generate-elf``). Otherwise it runs ``aiebu-asm -t aie2txn``,
    which reads the TXN's embedded ``DDR_PATCH`` ops and emits ``.ctrltext`` +
    ``.rela.dyn`` — exactly the format :func:`parse_control_elf` consumes.
    """
    import shutil
    import subprocess

    insts_path = Path(insts_path)
    sibling = insts_path.with_suffix(".elf")
    if sibling.exists() and sibling.stat().st_mtime >= insts_path.stat().st_mtime:
        return sibling.read_bytes()

    aiebu = (
        os.environ.get("AIEBU_ASM")
        or shutil.which("aiebu-asm")
        or "/opt/xilinx/xrt/bin/aiebu-asm"
    )
    if not Path(aiebu).exists():
        raise HRXError(
            "aiebu-asm not found; set AIEBU_ASM or add /opt/xilinx/xrt/bin to PATH "
            "(needed to convert insts.bin -> control ELF for HRX patching)."
        )
    try:
        subprocess.run(
            [aiebu, "-t", "aie2txn", "-c", str(insts_path), "-o", str(sibling)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise HRXError(
            f"aiebu-asm failed converting {insts_path} to a control ELF: "
            f"{e.stderr.decode('utf-8', 'replace')}"
        ) from e
    return sibling.read_bytes()


def control_code_and_patch_table(insts_path, scalar_args: int = 3):
    """Resolve ``(control_code, patch_table)`` for an ``insts.bin`` / control ELF.

    Strategy (avoids ``aiebu-asm`` whenever possible):
      1. If the file is already an ELF, parse it with :func:`parse_control_elf`.
      2. Otherwise parse the raw TXN directly with :func:`parse_txn` (no aiebu).
      3. If direct parsing isn't supported (unknown TXN version/opcode), fall
         back to the ``aiebu-asm`` round-trip
         (:func:`insts_to_control_elf` + :func:`parse_control_elf`).

    Set ``IRON_HRX_FORCE_AIEBU=1`` to always use the aiebu path (debugging /
    bringing up a new TXN version before the direct parser learns it).
    """
    insts_path = Path(insts_path)
    data = insts_path.read_bytes()

    if data[:4] == b"\x7fELF":
        return parse_control_elf(data, scalar_args=scalar_args)

    if not os.environ.get("IRON_HRX_FORCE_AIEBU"):
        try:
            return parse_txn(data)
        except TxnUnsupportedError as e:
            logger.debug("direct TXN parse failed (%s); falling back to aiebu", e)

    elf_bytes = insts_to_control_elf(insts_path)
    return parse_control_elf(elf_bytes, scalar_args=scalar_args)


class HRXContext:
    """Process-wide singleton owning the HRX device + dispatch stream.

    The amdxdna NPU is a single shared device, so a single device/stream pair is
    shared across all tensors and kernels in the process (mirrors the FLM shim's
    Forwarder singleton).
    """

    _instance = None

    def __init__(self):
        self.device = _handle()
        self.stream = _handle()
        _check(_hrx_gpu_initialize(0), "hrx_gpu_initialize")
        _check(_hrx_gpu_device_get(0, ctypes.byref(self.device)), "hrx_gpu_device_get")
        _check(
            _hrx_stream_create(self.device, 0, ctypes.byref(self.stream)),
            "hrx_stream_create",
        )

    @classmethod
    def get(cls) -> "HRXContext":
        if cls._instance is None:
            cls._instance = HRXContext()
        return cls._instance

    # -- buffers -----------------------------------------------------------
    def allocate_persistent(self, size: int):
        """Allocate a device-visible, host-coherent BO and map it persistently.

        Returns (buffer_handle, host_ptr). Coherence is maintained explicitly
        via flush_range / invalidate_range (the BoImpl strategy from the FLM
        shim).
        """
        buf = _handle()
        _check(
            _hrx_buffer_allocate(
                self.stream,
                ctypes.c_size_t(size),
                HRX_MEMORY_TYPE_HOST_LOCAL | HRX_MEMORY_TYPE_DEVICE_VISIBLE,
                HRX_BUFFER_USAGE_DEFAULT | HRX_BUFFER_USAGE_MAPPING_PERSISTENT,
                ctypes.byref(buf),
            ),
            "hrx_buffer_allocate",
        )
        ptr = ctypes.c_void_p()
        _check(
            _hrx_buffer_map_persistent(
                buf, HRX_MAP_READ | HRX_MAP_WRITE, ctypes.byref(ptr)
            ),
            "hrx_buffer_map_persistent",
        )
        return buf, ptr.value

    def flush_range(self, buf, offset: int, size: int):
        _check(
            _hrx_buffer_flush_range(buf, ctypes.c_size_t(offset), ctypes.c_size_t(size)),
            "hrx_buffer_flush_range",
        )

    def invalidate_range(self, buf, offset: int, size: int):
        _check(
            _hrx_buffer_invalidate_range(
                buf, ctypes.c_size_t(offset), ctypes.c_size_t(size)
            ),
            "hrx_buffer_invalidate_range",
        )

    def release_buffer(self, buf):
        if buf:
            _hrx_buffer_release(buf)

    # -- executables -------------------------------------------------------
    def build_xadx(
        self, xclbin_bytes: bytes, cc_words, entry_name: str, patch_table=None
    ) -> bytes:
        """Wrap xclbin + control-code (uint32 words) + patch_table into a XADX.

        ``patch_table`` is a flat sequence of (offset, arg_idx, addend) uint32
        triples (as produced by :func:`parse_control_elf`). HRX uses it to
        host-patch each I/O buffer's device address into the control code; if it
        is omitted the addresses stay 0 and the NPU produces all-zero output.
        """
        import numpy as np

        cc = np.ascontiguousarray(cc_words, dtype=np.uint32)
        if patch_table is not None and len(patch_table):
            pt = np.ascontiguousarray(patch_table, dtype=np.uint32).ravel()
            patch_ptr = ctypes.cast(pt.ctypes.data, ctypes.c_void_p)
            patch_n = ctypes.c_size_t(pt.size)
        else:
            patch_ptr = ctypes.c_void_p(0)
            patch_n = ctypes.c_size_t(0)
        out_ptr = ctypes.c_void_p()
        out_n = ctypes.c_size_t()
        rc = _iron_build_xadx(
            ctypes.cast(ctypes.c_char_p(xclbin_bytes), ctypes.c_void_p),
            ctypes.c_size_t(len(xclbin_bytes)),
            ctypes.cast(cc.ctypes.data, ctypes.c_void_p),
            ctypes.c_size_t(cc.size),
            patch_ptr,
            patch_n,
            entry_name.encode("utf-8"),
            ctypes.byref(out_ptr),
            ctypes.byref(out_n),
        )
        if rc != 0 or not out_ptr.value:
            raise HRXError(f"iron_build_xadx failed (rc={rc})")
        try:
            blob = ctypes.string_at(out_ptr, out_n.value)
        finally:
            _iron_free_xadx(out_ptr)
        return blob

    def load_executable(self, xadx_blob: bytes):
        exe = _handle()
        _check(
            _hrx_executable_load_data(
                self.device,
                ctypes.cast(ctypes.c_char_p(xadx_blob), ctypes.c_void_p),
                ctypes.c_size_t(len(xadx_blob)),
                HRX_AMDXDNA_FORMAT,
                ctypes.byref(exe),
            ),
            "hrx_executable_load_data",
        )
        return exe

    def lookup_export(self, exe, name: str) -> int:
        ordv = ctypes.c_uint32()
        _check(
            _hrx_executable_lookup_export_by_name(
                exe, name.encode("utf-8"), ctypes.byref(ordv)
            ),
            "hrx_executable_lookup_export_by_name",
        )
        return ordv.value

    def release_executable(self, exe):
        if exe:
            _hrx_executable_release(exe)

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, exe, export_ordinal: int, bindings):
        """Dispatch `exe` with `bindings` (list of (buffer_handle, size)).

        cfg is the unit config the amdxdna path expects ({1,1,1}/{1,1,1}); the
        I/O addresses are bound by binding order + the TXN DDR-patch ops.
        """
        n = len(bindings)
        arr = (HrxBufferRef * n)()
        for i, (buf, size) in enumerate(bindings):
            arr[i].buffer = buf
            arr[i].offset = 0
            arr[i].length = size
        cfg = HrxDispatchConfig()
        cfg.workgroup_count[0] = cfg.workgroup_count[1] = cfg.workgroup_count[2] = 1
        cfg.workgroup_size[0] = cfg.workgroup_size[1] = cfg.workgroup_size[2] = 1
        cfg.subgroup_size = 0
        _check(
            _hrx_stream_dispatch(
                self.stream,
                exe,
                ctypes.c_uint32(export_ordinal),
                ctypes.byref(cfg),
                None,
                0,
                arr,
                ctypes.c_size_t(n),
                ctypes.c_uint32(HRX_DISPATCH_FLAG_NONE),
            ),
            "hrx_stream_dispatch",
        )

    def dispatch_chain(self, items):
        """Record a sequence of dispatches into one command buffer (no submit).

        ``items`` is an iterable of ``(executable, export_ordinal, bindings)``,
        where ``bindings`` is a list of ``(buffer_handle, size)`` (same shape
        :meth:`dispatch` takes). This is the HRX analogue of an ``xrt::runlist``:
        each :meth:`dispatch` records into the stream's pending command buffer,
        and HRX inserts an execution + memory barrier after every dispatch, so a
        later dispatch observes an earlier one's device writes (producer ->
        consumer chains are correct). The whole batch stays pending until
        :meth:`synchronize`, which submits it as a single execution — the
        amdxdna HAL lowers a multi-dispatch command buffer into one
        ``ERT_CMD_CHAIN`` issued/waited once.

        Records only; call :meth:`synchronize` to submit and wait.
        """
        for exe, export_ordinal, bindings in items:
            self.dispatch(exe, export_ordinal, bindings)

    def synchronize(self):
        _check(_hrx_stream_synchronize(self.stream), "hrx_stream_synchronize")
