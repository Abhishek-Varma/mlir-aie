# SPDX-FileCopyrightText: Copyright (C) 2026 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Filesystem discovery for the HRX runtime (no ctypes / no dlopen).

This module mirrors ``FindHRX.cmake`` for the Python host stack: it locates
``libhrx.so`` and the XADX builder helper by probing standard install locations,
treating the ``HRX_*`` environment variables only as high-priority *hints*. It
deliberately performs no ``dlopen`` so it can be used as a cheap capability
probe (the analogue of ``import pyxrt`` for the XRT backend) before committing to
the heavier ``hrxruntime`` import that actually loads the libraries.
"""

import os
import subprocess
from pathlib import Path
from typing import List, Optional

__all__ = [
    "find_libhrx",
    "find_xadx_helper",
    "find_hrx_dir",
    "find_hrx_build",
    "ensure_xadx_helper",
    "hrx_available",
]

_HOME = Path(os.path.expanduser("~"))

# Standard source-checkout roots probed when HRX_DIR is unset.
_HRX_ROOT_CANDIDATES = [
    _HOME / "hrx",
    _HOME / "avarma_repro" / "hrx",
    Path("/opt/hrx"),
    Path("/usr/local/hrx"),
]

# Build-dir suffixes under an HRX source root.
_BUILD_SUFFIXES = ["build/cmake", "build-amdxdna-native", "build"]


def _existing(paths: List[Optional[str]]) -> List[str]:
    out = []
    for p in paths:
        if p and Path(p).exists() and p not in out:
            out.append(p)
    return out


def find_hrx_dir() -> Optional[str]:
    """Locate the HRX source checkout (dir with libhrx/include/hrx_runtime.h)."""
    hints = [os.environ.get("HRX_DIR")] + [str(c) for c in _HRX_ROOT_CANDIDATES]
    for c in hints:
        if c and (Path(c) / "libhrx" / "include" / "hrx_runtime.h").is_file():
            return str(Path(c).resolve())
    return None


def find_hrx_build(hrx_dir: Optional[str] = None) -> Optional[str]:
    """Locate the HRX build dir (dir containing libflatcc_runtime.a)."""
    if os.environ.get("HRX_BUILD"):
        b = os.environ["HRX_BUILD"]
        if (Path(b) / "libflatcc_runtime.a").is_file():
            return str(Path(b).resolve())
    hrx_dir = hrx_dir or find_hrx_dir()
    if hrx_dir:
        for suf in _BUILD_SUFFIXES:
            b = Path(hrx_dir) / suf
            if (b / "libflatcc_runtime.a").is_file():
                return str(b.resolve())
    return None


def find_libhrx() -> Optional[str]:
    """Locate libhrx.so, honoring env hints then standard locations."""
    hrx_dir = find_hrx_dir()
    hrx_build = os.environ.get("HRX_BUILD")
    libhrx_dir = os.environ.get("LIBHRX_DIR")

    candidates: List[Optional[str]] = [
        os.environ.get("HRX_LIBHRX"),
        os.path.join(libhrx_dir, "libhrx.so") if libhrx_dir else None,
        os.path.join(hrx_build, "libhrx", "src", "libhrx", "libhrx.so")
        if hrx_build
        else None,
    ]
    if hrx_dir:
        for suf in _BUILD_SUFFIXES:
            candidates.append(
                os.path.join(hrx_dir, suf, "libhrx", "src", "libhrx", "libhrx.so")
            )
    candidates += ["/usr/lib/libhrx.so", "/usr/local/lib/libhrx.so"]

    found = _existing(candidates)
    return found[0] if found else None


def find_xadx_helper() -> Optional[str]:
    """Locate a prebuilt libironhrx_xadx.so (env override or next to this pkg)."""
    here = Path(__file__).resolve().parent
    candidates = [
        os.environ.get("IRON_HRX_XADX"),
        str(here / "libironhrx_xadx.so"),
    ]
    found = _existing(candidates)
    return found[0] if found else None


def ensure_xadx_helper() -> Optional[str]:
    """Return a usable libironhrx_xadx.so, building it from source if missing.

    Build is a no-manual-step fallback: when no prebuilt helper is found but the
    HRX tree (schema + flatcc) is present, run build_xadx_helper.sh to produce
    the .so next to this package (ABI-matched to the detected HRX build). Returns
    None if neither a prebuilt nor the build inputs are available.
    """
    existing = find_xadx_helper()
    if existing:
        return existing

    here = Path(__file__).resolve().parent
    script = here / "build_xadx_helper.sh"
    hrx_dir = find_hrx_dir()
    hrx_build = find_hrx_build(hrx_dir)
    if not (script.exists() and hrx_dir and hrx_build):
        return None

    env = dict(os.environ, HRX_DIR=hrx_dir, HRX_BUILD=hrx_build)
    try:
        subprocess.run(
            ["bash", str(script)], check=True, capture_output=True, env=env
        )
    except subprocess.CalledProcessError:
        return None
    out = here / "libironhrx_xadx.so"
    return str(out) if out.exists() else None


def hrx_available() -> bool:
    """Cheap capability probe: True if libhrx.so can be located on this host."""
    return find_libhrx() is not None
