"""runner.py — subprocess wrappers for the seven ppt-master native-fill scripts.

Each ``run_*`` returns a :class:`ScriptResult` carrying exit code, stdout,
stderr, duration, and a parsed payload where applicable. Nothing here is
shared with mcp_ppt_master — every utility is hand-rolled against
``subprocess.run`` with the ``attribution_guard`` / ``console_encoding``
PYTHONPATH prepended.

SKILL_DIR resolution priority (matches plan §4):
  1. function argument ``skill_dir``
  2. environment variable ``PPT_MASTER_SKILL_DIR``
  3. Windows default: ``C:\\Users\\Administrator\\.claude\\skills\\ppt-master``
  4. POSIX default:  ``~/.claude/skills/ppt-master``
  5. probe ``<cwd>/ppt-master/scripts/attribution_guard.py``
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import io_utils

log = logging.getLogger("mcp_ppt_native_fill.runner")


# ---------------------------------------------------------------------------
# Skill directory resolution.
# ---------------------------------------------------------------------------

# Bug 17 fix: was hardcoded ``C:\Users\Administrator\...`` which only
# worked for one Windows account. Derive from Path.home() so the
# default works for any user. Posix and Windows defaults are
# identical on all platforms (just ``~/.claude/skills/ppt-master``)
# since Claude Code uses ``.claude`` on every platform.
_DEFAULT_WINDOWS_SKILL_DIR = Path.home() / ".claude/skills/ppt-master"
_DEFAULT_POSIX_SKILL_DIR = Path.home() / ".claude/skills/ppt-master"


def resolve_skill_dir(skill_dir: str | os.PathLike | None = None) -> Path:
    """Resolve the ppt-master skill directory per the 5-step priority."""
    if skill_dir is not None:
        p = Path(skill_dir).expanduser().resolve()
        if (p / "scripts" / "attribution_guard.py").is_file():
            return p
        raise FileNotFoundError(
            f"skill_dir={p} does not contain scripts/attribution_guard.py"
        )

    env = os.environ.get("PPT_MASTER_SKILL_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / "scripts" / "attribution_guard.py").is_file():
            return p
        log.warning(
            "PPT_MASTER_SKILL_DIR=%s does not contain attribution_guard.py; "
            "falling through to defaults",
            env,
        )

    candidates = [_DEFAULT_WINDOWS_SKILL_DIR, _DEFAULT_POSIX_SKILL_DIR]
    for c in candidates:
        if (c / "scripts" / "attribution_guard.py").is_file():
            return c.resolve()

    # Probe CWD
    cwd_probe = Path.cwd() / "ppt-master"
    if (cwd_probe / "scripts" / "attribution_guard.py").is_file():
        return cwd_probe.resolve()

    # Last resort: walk upwards (helps when MCP is run from a sub-project)
    cursor = Path(__file__).resolve().parent
    for _ in range(6):
        candidate = cursor / "ppt-master" / "scripts" / "attribution_guard.py"
        if candidate.is_file():
            return candidate.parent.parent.resolve()
        if cursor.parent == cursor:
            break
        cursor = cursor.parent

    raise FileNotFoundError(
        "Cannot locate ppt-master skill directory. Set PPT_MASTER_SKILL_DIR or "
        "install ppt-master to ~/.claude/skills/ppt-master/."
    )


# ---------------------------------------------------------------------------
# Python interpreter discovery.
# ---------------------------------------------------------------------------

_CANDIDATE_EXES: tuple[str, ...] = (
    sys.executable or "python",
    "python3",
    "python",
    r"C:\Python313\python.exe",
    r"C:\Python311\python.exe",
    r"C:\Users\Administrator\Anaconda3\python.exe",
    "/usr/bin/python3",
)

_cached_python: str | None = None


def resolve_python() -> str:
    """Return an absolute path to a working Python interpreter (cached)."""
    global _cached_python
    if _cached_python:
        return _cached_python

    env_override = os.environ.get("PPT_MASTER_PYTHON") or os.environ.get("MCP_PPT_PYTHON")
    if env_override and Path(env_override).exists():
        _cached_python = env_override
        return _cached_python

    for candidate in _CANDIDATE_EXES:
        if not candidate:
            continue
        p = Path(candidate)
        if p.exists():
            _cached_python = str(p)
            return _cached_python
        resolved = shutil.which(candidate)
        if resolved and Path(resolved).exists():
            _cached_python = resolved
            return _cached_python

    raise RuntimeError(
        "No Python interpreter found. Set PPT_MASTER_PYTHON or MCP_PPT_PYTHON."
    )


# ---------------------------------------------------------------------------
# ScriptResult + subprocess.
# ---------------------------------------------------------------------------

@dataclass
class ScriptResult:
    script: str
    args: list[str]
    exit: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    parsed: dict | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit == 0 and not self.timed_out

    def to_dict(self) -> dict:
        out = {
            "script": self.script,
            "args": list(self.args),
            "exit": self.exit,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "stdout_tail": self.stdout[-800:] if self.stdout else "",
            "stderr_tail": self.stderr[-800:] if self.stderr else "",
        }
        if self.parsed is not None:
            out["parsed"] = self.parsed
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


def _run(
    script_name: str,
    args: Sequence[str],
    *,
    skill_dir: Path,
    timeout_ms: int = 240_000,
) -> ScriptResult:
    """Spawn ``python <scripts>/<script_name> <args>...`` and capture output."""
    scripts_dir = skill_dir / "scripts"
    script_path = scripts_dir / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"script not found: {script_path}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # PYTHONLEGACYWINDOWSSTDIO=0 forces the vendor's Python to use UTF-8 for
    # sys.stdout/stderr encoding instead of the legacy cp936/gbk. This is
    # required on Chinese Windows where the default legacy encoding corrupts
    # Chinese filenames in stdout/stderr that we later read with encoding="utf-8".
    env["PYTHONLEGACYWINDOWSSTDIO"] = "0"
    # Make ppt-master modules importable (console_encoding, attribution_guard,
    # svg_quality/, svg_to_pptx/, etc.).
    sep = ";" if os.name == "nt" else ":"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{scripts_dir}{sep}{existing}" if existing else str(scripts_dir)
    )

    py = resolve_python()
    full_args = [py, str(script_path), *args]
    # Log the actual argv at the byte level so encoding mismatches are
    # diagnosable from server stderr.
    argv_repr = [repr(a) for a in full_args]
    log.info(
        "spawning %s argv=%s cwd=%s",
        script_name,
        argv_repr,
        scripts_dir,
    )
    start = time.time()
    try:
        proc = subprocess.run(
            full_args,
            cwd=str(scripts_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_ms / 1000,
        )
        duration = int((time.time() - start) * 1000)
        return ScriptResult(
            script=script_name,
            args=list(args),
            exit=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_ms=duration,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = int((time.time() - start) * 1000)
        return ScriptResult(
            script=script_name,
            args=list(args),
            exit=None,
            stdout=(
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            ),
            stderr=(
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            ),
            duration_ms=duration,
            timed_out=True,
        )


# ---------------------------------------------------------------------------
# High-level wrappers — one per ppt-master script.
# ---------------------------------------------------------------------------

# Regex for the receipt line svg_to_pptx prints on success.
_EXPORT_RECEIPT_RE = re.compile(
    r"Round-trip export summary:\s*"
    r"output_pages\s*=\s*(\d+)\s+"
    r"passthrough\s*=\s*(\d+)\s+"
    r"cloned_passthrough\s*=\s*(\d+)\s+"
    r"patched\s*=\s*(\d+)\s+"
    r"rebuilt\s*=\s*(\d+)"
)


def run_attribution_guard(skill_dir: Path, *, timeout_ms: int = 30_000) -> ScriptResult:
    """Phase 1 — fail-closed skill integrity gate. Exit 0 / 78."""
    res = _run("attribution_guard.py", [], skill_dir=skill_dir, timeout_ms=timeout_ms)
    res.warnings.append(
        "exit=78 means skill integrity broken — abort pipeline (per master §3)"
        if res.exit == 78
        else "attribution guard passed"
        if res.ok
        else f"unexpected attribution_guard exit={res.exit}"
    )
    return res


def run_pptx_to_svg(
    skill_dir: Path,
    source_pptx: Path,
    workspace: Path,
    *,
    inheritance_mode: str = "both",
    roundtrip: bool = True,
    timeout_ms: int = 120_000,
) -> ScriptResult:
    """Phase 2 — import round-trip workspace."""
    # Resolve to absolute paths because vendor scripts run with
    # cwd=<skill_dir>/scripts — relative paths would land under
    # ``<scripts>/projects/...`` instead of the caller's intent.
    source_pptx = Path(source_pptx).resolve()
    workspace = Path(workspace).resolve()
    args = [
        str(source_pptx),
        "-o",
        str(workspace),
        "--inheritance-mode",
        inheritance_mode,
    ]
    if roundtrip:
        args.append("--roundtrip")
    res = _run("pptx_to_svg.py", args, skill_dir=skill_dir, timeout_ms=timeout_ms)
    # Parse "Slides converted: N" line if present.
    m = re.search(r"Slides converted:\s*(\d+)", res.stdout)
    parsed: dict[str, Any] = {}
    if m:
        parsed["slides_converted"] = int(m.group(1))
    if res.ok:
        parsed["workspace"] = str(workspace)
        parsed["authoring_dir"] = str(workspace / "authoring-svg-flat")
        parsed["source_pptx_frozen"] = str(workspace / "sources" / "source.pptx")
    res.parsed = parsed
    return res


def run_svg_authoring_view_refresh(
    skill_dir: Path,
    authoring_dir: Path,
    *,
    timeout_ms: int = 30_000,
) -> ScriptResult:
    """Phase 3 helper — refresh authoring_summary.json."""
    return _run(
        "svg_authoring_view.py",
        [str(authoring_dir), "--refresh-summary"],
        skill_dir=skill_dir,
        timeout_ms=timeout_ms,
    )


def run_svg_quality_check(
    skill_dir: Path,
    workspace: Path,
    *,
    timeout_ms: int = 60_000,
) -> ScriptResult:
    """Phase 4 — round-trip quality gate. Exit 0 = OK/WARN, 1 = blocking ERROR."""
    # Resolve to absolute path — vendor cwd is <skill_dir>/scripts, so
    # a relative workspace would land under ``scripts/projects/...``.
    workspace = Path(workspace).resolve()
    res = _run(
        "svg_quality_checker.py",
        [str(workspace), "--roundtrip"],
        skill_dir=skill_dir,
        timeout_ms=timeout_ms,
    )
    # Parse the human summary into counts.
    parsed = _parse_quality_summary(res.stdout, res.stderr)
    res.parsed = parsed
    return res


def run_svg_to_pptx(
    skill_dir: Path,
    workspace: Path,
    output_pptx: Path,
    *,
    timeout_ms: int = 240_000,
) -> ScriptResult:
    """Phase 5 — export a native DrawingML PPTX."""
    # Resolve to absolute paths — vendor cwd is <skill_dir>/scripts.
    workspace = Path(workspace).resolve()
    output_pptx = Path(output_pptx).resolve()
    res = _run(
        "svg_to_pptx.py",
        [str(workspace), "--roundtrip", "-o", str(output_pptx)],
        skill_dir=skill_dir,
        timeout_ms=timeout_ms,
    )
    parsed: dict[str, Any] = {}
    m = _EXPORT_RECEIPT_RE.search(res.stdout)
    if m:
        parsed["export_summary"] = {
            "output_pages": int(m.group(1)),
            "passthrough": int(m.group(2)),
            "cloned_passthrough": int(m.group(3)),
            "patched": int(m.group(4)),
            "rebuilt": int(m.group(5)),
        }
    if output_pptx.exists():
        parsed["output_bytes"] = output_pptx.stat().st_size
    res.parsed = parsed
    return res


def run_pptx_delivery_check(
    skill_dir: Path,
    output_pptx: Path,
    *,
    timeout_ms: int = 60_000,
) -> ScriptResult:
    """Phase 5b — structural integrity of the exported PPTX."""
    res = _run(
        "pptx_delivery_check.py",
        [str(output_pptx)],
        skill_dir=skill_dir,
        timeout_ms=timeout_ms,
    )
    parsed: dict[str, Any] = {}
    if res.stdout.strip():
        try:
            report = json.loads(res.stdout)
            parsed["status"] = report.get("status")
            parsed["zip_integrity"] = (
                report.get("package", {}).get("zip_integrity")
            )
            parsed["relationships_problems"] = (
                report.get("relationships", {}).get("problems", [])
            )
            slides = report.get("slides", {})
            parsed["slides_count"] = slides.get("count")
            parsed["hidden_count"] = slides.get("hidden_count", 0)
            parsed["advisories"] = report.get("advisories", [])
            parsed["errors"] = report.get("errors", [])
        except json.JSONDecodeError:
            parsed["raw"] = res.stdout[-2000:]
    res.parsed = parsed
    # Exit 1 means status=="failed"; exit 0 means passed/advisories.
    res.warnings.append(
        f"delivery_check status={parsed.get('status', 'unknown')}"
    )
    return res


def run_source_to_md(
    skill_dir: Path,
    output_pptx: Path,
    readback_md: Path,
    *,
    timeout_ms: int = 60_000,
) -> ScriptResult:
    """Phase 5c — human-readable content readback."""
    return _run(
        "source_to_md.py",
        [str(output_pptx), "-o", str(readback_md)],
        skill_dir=skill_dir,
        timeout_ms=timeout_ms,
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _parse_quality_summary(stdout: str, stderr: str) -> dict[str, Any]:
    """Best-effort parse of svg_quality_checker --roundtrip output."""
    out = stdout + "\n" + stderr
    parsed: dict[str, Any] = {}
    # Final lines like "X errors, Y warnings"
    m = re.search(r"(\d+)\s+errors?,\s*(\d+)\s+warnings?", out, re.IGNORECASE)
    if m:
        parsed["errors"] = int(m.group(1))
        parsed["warnings"] = int(m.group(2))
    # Per-slide overflow lines: "<text> exceeds owning frame ... overflow horizontal N%"
    overflow_lines = re.findall(r"overflow horizontal (\d+(?:\.\d+)?)%", out)
    if overflow_lines:
        parsed["overflow_lines"] = [float(x) for x in overflow_lines]
        parsed["overflow_count"] = len(overflow_lines)
    if "errors" not in parsed:
        parsed["errors"] = 0
    if "warnings" not in parsed:
        parsed["warnings"] = 0
    return parsed
