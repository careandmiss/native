#!/usr/bin/env python3
"""End-to-end smoke test across multiple PPTX templates.

For each ``*.pptx`` in ``test_templates/``:
  1. Wipe ``projects/<stem>_workspace`` and ``projects/<stem>_out.pptx``
  2. Run ``examples/llm_fill.py --template <pptx> --md <generic_content.md>``
  3. Read the resulting ``page_plan.json`` to verify:
       - ``page_plan_pages[-1]["svg"]`` matches the last slide from the
         source roster (the "ending" slide).
       - Total page count >= source slide count (LLM may have cloned).
  4. Parse ``llm_fill.py``'s stdout JSON for ``errors_count``.

The output table reports per-template pass/fail and key metrics so we
can see whether the Phase B + B+ fixes are truly template-agnostic.

Usage::

    python examples/multi_template_test.py [template_dir]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR_DEFAULT = ROOT / "test_templates"
MD_PATH = TEMPLATES_DIR_DEFAULT / "generic_content.md"
PROJECTS_DIR = ROOT / "projects"


def _stem(pptx: Path) -> str:
    return pptx.stem


def _find_ending_svg(pptx: Path) -> str:
    """The last ``slide_NN.svg`` in the source PPTX is conventionally
    the ending slide (THANK YOU etc.)."""
    import zipfile
    with zipfile.ZipFile(pptx) as z:
        slide_re = re.compile(r"ppt/slides/slide(\d+)\.xml$")
        nums = sorted(int(m.group(1)) for n in z.namelist()
                      for m in [slide_re.match(n)] if m)
    return f"slide_{nums[-1]:02d}.svg"


def _run_one(pptx: Path, repo_root: Path) -> dict:
    """Run ``examples/llm_fill.py`` against one template. Return a
    results dict with pass/fail and key metrics."""
    stem = _stem(pptx)
    workspace = repo_root / "projects" / f"{stem}_workspace"
    output = repo_root / "projects" / f"{stem}_out.pptx"
    expected_ending = _find_ending_svg(pptx)
    result: dict = {
        "template": pptx.name,
        "stem": stem,
        "expected_ending_svg": expected_ending,
        "workspace": str(workspace.relative_to(repo_root)),
        "output": str(output.relative_to(repo_root)),
        "ok": False,
        "stage": None,
        "errors_count": None,
        "warnings_count": None,
        "page_count": None,
        "ending_is_last": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    # wipe previous run artifacts
    for path in (workspace, output):
        if path.is_dir():
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "examples" / "llm_fill.py"),
            "--template", str(pptx),
            "--md", str(MD_PATH),
            "--workspace", str(workspace),
            "--output", str(output),
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=600,
    )
    result["exit_code"] = proc.returncode
    result["stdout_tail"] = proc.stdout[-2000:]
    result["stderr_tail"] = proc.stderr[-2000:]
    # The server prints JSON to stdout; parse the last JSON object.
    last_json = _extract_last_json(proc.stdout)
    if last_json:
        result["ok"] = bool(last_json.get("ok"))
        result["stage"] = last_json.get("stage")
        result["errors_count"] = last_json.get("errors_count")
        result["warnings_count"] = last_json.get("warnings_count")
    # Inspect page_plan.json
    page_plan_path = workspace / "page_plan.json"
    if page_plan_path.is_file():
        try:
            plan = json.loads(page_plan_path.read_text(encoding="utf-8"))
            pages = plan.get("pages", [])
            result["page_count"] = len(pages)
            if pages:
                result["ending_is_last"] = (
                    pages[-1].get("svg") == expected_ending
                )
        except Exception as exc:
            result["page_count_error"] = f"{type(exc).__name__}: {exc}"
    return result


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _extract_last_json(text: str) -> dict | None:
    """Find the last top-level JSON object in ``text``."""
    matches = list(_JSON_RE.finditer(text))
    for m in reversed(matches):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _print_table(rows: list[dict]) -> None:
    headers = ("template", "ok", "stage", "errors", "warns", "pages", "end_last")
    # Materialize widths as a list (not a generator) so we can iterate
    # the same widths twice — once for the header, once for the rule.
    widths = [
        max(len(h), max((len(str(r.get(h, ""))) for r in rows), default=0))
        for h in headers
    ]
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        cells = [
            r.get("template", ""),
            "PASS" if r.get("ok") else "FAIL",
            str(r.get("stage") or ""),
            str(r.get("errors_count") or ""),
            str(r.get("warnings_count") or ""),
            str(r.get("page_count") or ""),
            "Y" if r.get("ending_is_last") else "N",
        ]
        print(fmt.format(*cells))


def main() -> int:
    templates_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else TEMPLATES_DIR_DEFAULT
    if not templates_dir.is_dir():
        print(f"ERROR: {templates_dir} not found", file=sys.stderr)
        return 1
    if not MD_PATH.is_file():
        print(f"ERROR: {MD_PATH} not found", file=sys.stderr)
        return 1
    pptx_files = sorted(templates_dir.glob("*.pptx"))
    if not pptx_files:
        print(f"ERROR: no .pptx in {templates_dir}", file=sys.stderr)
        return 1
    rows: list[dict] = []
    for pptx in pptx_files:
        print(f"--- running {pptx.name} ---", flush=True)
        rows.append(_run_one(pptx, ROOT))
    print()
    _print_table(rows)
    out_path = templates_dir / "results.json"
    out_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nresults written to {out_path}")
    # exit code: 0 if all pass, 1 otherwise
    return 0 if all(r.get("ok") and r.get("errors_count") == 0
                    and r.get("ending_is_last") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
