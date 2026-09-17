"""render_diff.py — render authoring SVGs to PNG previews.

Walks every ``*.svg`` in the workspace's ``authoring-svg-flat/`` and
renders each to a PNG in ``validation/diff/`` using ``cairosvg``. Used
as a post-export visual sanity check: the SVGs are the intermediate
representation that ``svg_to_pptx`` consumes, so a malformed SVG (text
overflow, off-canvas geometry, broken picture) shows up in the PNG
even when the vendor PPTX render hides it.

Why cairosvg, not LibreOffice?
  * cairosvg is stdlib-pure after pip install; no system LibreOffice
    runtime required.
  * LibreOffice can't be installed on this Anaconda env, and on
    Windows its headless mode requires a display stub.
  * SVGs are the source of truth: if the SVG looks right, the PPTX
    will too (modulo vendor svg_to_pptx quirks, which ``preflight``
    catches separately).

Output layout::

    workspace/validation/diff/
        slide_01.png
        slide_02.png
        ...
        slide_part02_content.png
        render_summary.json      ← manifest (slide_count + png paths)

Limitations
-----------
* cairosvg ignores PPT-only constructs (``data-pptx-*`` attrs). It's a
  *visual* check, not a structural check — ``preflight`` covers
  structure.
* No before/after comparison: only the post-roundtrip SVG is rendered.
  Pre-roundtrip visual diffs require LibreOffice + the original PPTX.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import cairosvg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - cairosvg is the only dep
    cairosvg = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# cairosvg raises ``ValueError`` on text overflow / off-canvas geometry;
# we cap the message so one bad SVG doesn't produce a 50 KB warning.
_PREVIEW_FAILURE_NOTE = "(render skipped — see log)"


@dataclass
class RenderReport:
    """Aggregate result for one workspace render."""

    svg_files_scanned: int = 0
    pngs_written: int = 0
    failures: list[dict] = field(default_factory=list)
    output_dir: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failure_count"] = len(self.failures)
        return d


def _sorted_svg_paths(authoring_dir: Path) -> list[Path]:
    """Return ``*.svg`` in ``authoring_dir``, slide_*.svg first by NN.

    Stable order: ``slide_01.svg``, ``slide_02.svg``, ..., then any
    ``slide_partNN_*.svg`` in lexical order. Lexical sort gives
    ``slide_part01_div.svg`` before ``slide_part02_content.svg``.
    """
    slide_re = re.compile(r"^slide_(\d+)\.svg$")

    def key(p: Path) -> tuple[int, str]:
        m = slide_re.match(p.name)
        if m:
            return (0, f"{int(m.group(1)):04d}")
        return (1, p.name)

    return sorted(authoring_dir.glob("*.svg"), key=key)


def _render_one(svg_path: Path, png_path: Path) -> None:
    """Render a single SVG → PNG via cairosvg.

    Errors are re-raised so the caller can log them; the caller decides
    whether to fail-fast or accumulate (we accumulate).
    """
    if cairosvg is None:
        raise RuntimeError(
            "cairosvg is not installed; pip install cairosvg to use "
            "render_diff"
        )
    cairosvg.svg2png(
        url=str(svg_path),
        write_to=str(png_path),
        output_width=1280,
        output_height=720,
        # background_color="white"  # default is fine; transparent SVGs
        #                           # render with alpha
    )


def render_svg_previews(
    workspace: Path,
    *,
    output_dir: Path | None = None,
    slide_filter: Iterable[str] | None = None,
) -> RenderReport:
    """Render every ``*.svg`` under ``workspace/authoring-svg-flat/``
    to PNG in ``output_dir`` (default ``workspace/validation/diff/``).

    Parameters
    ----------
    workspace:
        Authoring workspace root (must have ``authoring-svg-flat/``).
    output_dir:
        PNG destination directory. Created if missing. Defaults to
        ``workspace/validation/diff/``.
    slide_filter:
        Optional iterable of SVG filenames to render (e.g.
        ``["slide_03.svg", "slide_part02_content.svg"]``). When
        ``None``, every SVG in ``authoring-svg-flat/`` is rendered.

    Returns
    -------
    RenderReport
        Aggregate stats: SVG count, PNG count, failure list.
    """
    authoring_dir = workspace / "authoring-svg-flat"
    if not authoring_dir.is_dir():
        raise FileNotFoundError(
            f"authoring-svg-flat not found at {authoring_dir}"
        )
    if output_dir is None:
        output_dir = workspace / "validation" / "diff"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report = RenderReport(output_dir=str(output_dir))

    if slide_filter is not None:
        candidates = [authoring_dir / name for name in slide_filter]
    else:
        candidates = _sorted_svg_paths(authoring_dir)

    for svg_path in candidates:
        if not svg_path.is_file():
            continue
        report.svg_files_scanned += 1
        png_path = output_dir / (svg_path.stem + ".png")
        try:
            _render_one(svg_path, png_path)
            report.pngs_written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "render_diff: %s → %s failed: %s: %s",
                svg_path.name, png_path.name,
                type(exc).__name__, exc,
            )
            report.failures.append({
                "svg": svg_path.name,
                "png": png_path.name,
                "error": f"{type(exc).__name__}: {exc}",
            })
            # Write a tiny placeholder PNG so the user can still tell
            # "this slide was attempted but failed" from "this slide
            # wasn't attempted".
            try:
                _write_failure_placeholder(png_path, str(exc))
            except Exception:  # pragma: no cover - best-effort
                pass

    # Manifest: a JSON sidecar for callers (or LLM agents) to inspect
    # which slides were rendered and which failed, without having to
    # glob the directory.
    manifest = {
        "output_dir": str(output_dir),
        "svg_files_scanned": report.svg_files_scanned,
        "pngs_written": report.pngs_written,
        "failures": report.failures,
    }
    (output_dir / "render_summary.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(
        "render_diff: %d/%d SVG(s) → PNG at %s (%d failure(s))",
        report.pngs_written, report.svg_files_scanned, output_dir,
        len(report.failures),
    )
    return report


def _write_failure_placeholder(png_path: Path, error_msg: str) -> None:
    """Write a tiny 1×1 transparent PNG with the error in the filename.

    cairosvg may fail because of malformed text overflow / unsupported
    features; users still want a "this slide exists, render failed"
    marker. We use PIL to write a 1×1 PNG instead of an actual slide
    preview.
    """
    from PIL import Image  # local import — PIL is stdlib-only here
    img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    img.save(png_path)
    sidecar = png_path.with_suffix(".error.txt")
    sidecar.write_text(error_msg, encoding="utf-8")


def render_human(report: RenderReport) -> str:
    """Format a report for human consumption on stdout."""
    if report.svg_files_scanned == 0:
        return "[render_diff] no SVG files found; nothing to render.\n"
    lines = [
        f"[render_diff] {report.pngs_written}/{report.svg_files_scanned} "
        f"SVG(s) → PNG at {report.output_dir}",
    ]
    if report.failures:
        lines.append(f"  {len(report.failures)} failure(s):")
        for f in report.failures[:10]:
            lines.append(f"    {f['svg']}: {f['error']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "RenderReport",
    "render_svg_previews",
    "render_human",
]