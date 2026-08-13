"""Render page 1 of a PDF as a PNG (cover preview for the landing page)."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

PDFTOPPM_TIMEOUT = 120.0


async def render_cover_png(
    pdf: Path,
    out_png: Path,
    dpi: int = 180,
    timeout: float = PDFTOPPM_TIMEOUT,
) -> Path:
    """Rasterize the first page of `pdf` to `out_png` using pdftoppm."""
    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm not found (install poppler)")

    # pdftoppm writes <prefix>-<page>.png; we then rename to the requested name.
    prefix = out_png.with_suffix("").as_posix()
    proc = await asyncio.create_subprocess_exec(
        "pdftoppm",
        "-f", "1", "-l", "1",
        "-r", str(dpi),
        "-png",
        str(pdf),
        prefix,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"pdftoppm timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        raise RuntimeError(f"pdftoppm failed: {err.decode('utf-8', 'replace').strip()}")

    # poppler typically writes prefix-01.png or prefix-1.png depending on page count.
    candidates = [
        Path(f"{prefix}-1.png"),
        Path(f"{prefix}-01.png"),
        Path(f"{prefix}-001.png"),
    ]
    for c in candidates:
        if c.exists():
            if c != out_png:
                c.replace(out_png)
            return out_png
    raise RuntimeError(f"pdftoppm produced no recognizable output for {pdf}")
