---
name: mcp-ppt-native-fill
description: >
  Stdlib-only MCP server that wraps ppt-master v6.3.0's Edit Native PPTX
  round-trip pipeline (native fill) behind one ``native_fill`` tool. Fills an
  existing PPTX template with new content while preserving its original
  design byte-for-byte. Auto-fixes six common authoring pitfalls per
  NATIVE_FILL_PIPELINE_GUIDE §6 (gradient, picture structure, text overflow,
  viewBox, font stack, page_plan validation). Default skill_dir is the
  platform-standard ppt-master install (C:/Users/<user>/.claude/skills/
  ppt-master on Windows, ~/.claude/skills/ppt-master on POSIX).
metadata:
  version: "0.1.0"
  protocol: "MCP 2024-11-05"
  vendor: "Independent stdlib implementation; zero references to mcp_ppt_master"
  upstream: "ppt-master v6.3.0 (https://github.com/hugohe3/ppt-master)"
  license: "MIT"
---

# mcp-ppt-native-fill

Self-contained **MCP server** that wraps the ppt-master Edit Native PPTX
round-trip pipeline. Speaks the standard MCP stdio JSON-RPC protocol — works
with Claude Desktop, Claude Code, VS Code Continue, Cline, Cursor, or any
MCP-compatible client.

## Quick start

```bash
# Install — nothing to install, stdlib-only.
cd D:\Code\tst\native_fill
pip install -e .          # optional, exposes `mcp-ppt-native-fill` CLI

# Run the server over stdio (this is what MCP clients invoke).
python -m mcp_ppt_native_fill.server

# Smoke test the protocol.
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m mcp_ppt_native_fill.server
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python -m mcp_ppt_native_fill.server
```

## Tool: `native_fill`

| Field | Type | Required | Description |
|---|---|---|---|
| `source_pptx` | path | ✅ | Absolute path to the source .pptx template |
| `workspace` | path | ✅ | Round-trip workspace directory (created if missing) |
| `output_pptx` | path | ✅ | Absolute path for the final .pptx |
| `content_mapping` | object | ✅ | `{svg_filename: {shape_id: new_text}}` |
| `page_plan` | array | optional | `[{source_slide, svg}]`; omit = use source roster |
| `new_content_blocks` | object | optional | `{svg_filename: {shape_id: {bounds, layout, ...}}}` |
| `options.auto_fix` | bool | optional (default `true`) | Apply 6-class auto-fix loop |
| `options.max_fix_iterations` | int | optional (default `3`) | Cap on auto-fix attempts |
| `options.validate_strict` | bool | optional (default `true`) | Reject advisory delivery status |
| `options.skill_dir` | path | optional | Override `PPT_MASTER_SKILL_DIR` |
| `options.inheritance_mode` | enum | optional (default `"both"`) | `both` / `layered` / `flat` |

## SKILL_DIR resolution

Priority (first match wins):

1. `options.skill_dir` argument
2. `PPT_MASTER_SKILL_DIR` environment variable
3. Windows default: `C:\Users\<user>\.claude\skills\ppt-master`
4. POSIX default: `~/.claude/skills/ppt-master`
5. Probe `<cwd>/ppt-master/scripts/attribution_guard.py`

The resolved path is logged to stderr on startup.

## Pipeline

`native_fill` runs the 5-phase pipeline from
[`docs/NATIVE_FILL_PIPELINE_GUIDE.md`](docs/NATIVE_FILL_PIPELINE_GUIDE.md):

```
INIT → IMPORTED → PLANNED → AUTHORED → QUALITY_PASSED → EXPORTED → VALIDATED → DONE
       (phase 2) (plan)   (phase 3) (phase 4)            (phase 5) (5b/5c)
```

If the quality check returns exit 1, auto-fix iterates (capped by
`max_fix_iterations`) before giving up.

## Auto-fix catalogue

| Issue | Detection | Action |
|---|---|---|
| text_overflow | quality_check reports `overflow horizontal N%` | shrink `font-size` ×0.85 |
| viewbox_missing | quality_check ERROR or export abort | restore `<svg viewBox="0 0 W H">` |
| gradient_unexportable | export reports `Edited round-trip source object did not produce a DrawingML shape` on a gradient | strip `<defs><linearGradient>`; rewrite `url(#x)` → `#FFFFFF` |
| picture_structure | same, on `<image>` or nested `<svg><image/></svg>` | toggle between nested and flat |
| unsafe_font | quality_check WARN | `"思源黑体 CN …"` → `"微软雅黑", sans-serif` |
| page_plan | export abort | validated at write time (single-source-of-truth check) |

Each fix is recorded in the tool result `fix_iterations[]` with
`before`/`after` for audit.

## Client configuration

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "mcp-ppt-native-fill": {
      "command": "python",
      "args": ["-m", "mcp_ppt_native_fill.server"],
      "cwd": "D:/Code/tst/native_fill"
    }
  }
}
```

### Claude Code / Cline / Cursor

Same shape — point `cwd` at this folder. Use `python -m mcp_ppt_native_fill.server`
as the command.

## Tests

```bash
python -m unittest discover -s tests -p "test_native_fill.py" -v
```

## Repository layout

```
native_fill/
├── src/mcp_ppt_native_fill/         # the MCP server package
│   ├── server.py                   # stdio JSON-RPC dispatch + native_fill tool
│   ├── runner.py                   # 7 subprocess wrappers + skill_dir resolver
│   ├── pipeline.py                 # 6-state machine, drives Phase 2→5
│   ├── autofix.py                  # 5 SVG-level auto-fixes
│   ├── svg_edits.py                # surgical SVG text edits (ElementTree)
│   └── io_utils.py                 # atomic UTF-8 file I/O
├── tests/test_native_fill.py       # unittest
├── examples/native_fill_demo.py    # e2e demo with 柏腾ppt模版
├── docs/                           # pipeline guide + this design doc
├── SKILL.md                        # this file
└── pyproject.toml
```

## Hard rules

1. **stdlib-only**: no `mcp[cli]`, no `fastmcp`, no third-party deps. Drop
   the folder into any Python ≥ 3.10 environment and it runs.
2. **Zero references to mcp_ppt_master**: this project is independent. It
   only spawns ppt-master scripts as subprocesses.
3. **Logs to stderr**: stdout is the JSON-RPC transport channel.
4. **Tools return plain JSON**: `tools/call` carries a `content[0].text` JSON
   blob plus `structuredContent` and `isError`.
5. **No state held between requests**: each `tools/call` is its own
   subprocess orchestration; the server is a pure dispatcher.
