# mcp-ppt-native-fill

> **Independent stdlib-only MCP server** that wraps ppt-master v6.3.0's Edit
> Native PPTX round-trip pipeline (native fill). Fills an existing PPTX
> template with new content while preserving its original design byte-for-byte.

This project lives at `D:\Code\tst\native_fill\` and is **fully self-contained**.
It does **not** import, fork, or reference any code from `mcp_ppt_server` (a
separate project that wraps the Generate PPTX route). Both projects spawn
ppt-master scripts as subprocesses — that's the only common dependency.

## Why this exists

The ppt-master skill ships a 5-phase pipeline for editing native PPTX files
without regenerating them from scratch (see
[`docs/NATIVE_FILL_PIPELINE_GUIDE.md`](docs/NATIVE_FILL_PIPELINE_GUIDE.md)).
The pipeline involves seven `python scripts/*.py` calls plus careful SVG
editing. Driving it from an LLM chat loop is error-prone: the LLM has to
read SKILL.md, run attribution_guard, scaffold a workspace, write
`page_plan.json`, edit SVG XML without breaking `data-pptx-*` attributes,
survive six common authoring pitfalls (gradients, picture structure, text
overflow, missing viewBox, non-PPT-safe fonts, malformed `page_plan`).

This MCP server wraps the whole flow behind **one tool call** and applies
the six auto-fixes from the pipeline guide automatically.

## Tool surface

```jsonc
{
  "name": "native_fill",
  "input": {
    "source_pptx":      "C:/.../柏腾ppt模版.pptx",       // required
    "workspace":        "C:/.../projects/boteng_xxx",    // required
    "output_pptx":      "C:/.../out.pptx",               // required
    "content_mapping":  {"slide_01.svg": {"shape-23": "..."}},
    "page_plan":        [{"source_slide": 1, "svg": "slide_01.svg"}],   // optional
    "new_content_blocks": {"slide_part02.svg": {"shape-2": {"bounds": "120 130 1060 480", "layout": "3-column-cards"}}},
    "options": {
      "auto_fix": true,            // default
      "max_fix_iterations": 3,     // default
      "validate_strict": true,     // default
      "skill_dir": null,           // override PPT_MASTER_SKILL_DIR
      "inheritance_mode": "both"   // both / layered / flat
    }
  }
}
```

Returns a structured JSON envelope with `ok`, `stage`, `export_summary`,
`delivery`, `readback_md`, `fix_iterations[]`, `warnings[]`, `errors[]`.

## SKILL_DIR resolution

1. `options.skill_dir` argument
2. `PPT_MASTER_SKILL_DIR` environment variable
3. Windows default: `C:\Users\<user>\.claude\skills\ppt-master`
4. POSIX default: `~/.claude/skills/ppt-master`
5. Probe `<cwd>/ppt-master/scripts/attribution_guard.py`

## Quick start

```bash
cd D:\Code\tst\native_fill
pip install -e .

# Boot the stdio MCP server (what MCP clients invoke).
python -m mcp_ppt_native_fill.server

# Smoke test the protocol.
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  | python -m mcp_ppt_native_fill.server
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python -m mcp_ppt_native_fill.server

# Run unit tests.
python -m unittest discover -s tests -p "test_native_fill.py" -v
```

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

### Claude Code / Cline / Cursor / Continue

Point `cwd` at this folder; use `python -m mcp_ppt_native_fill.server`.

## Repository layout

```
native_fill/
├── src/mcp_ppt_native_fill/
│   ├── __init__.py
│   ├── __main__.py          # python -m mcp_ppt_native_fill → server.main
│   ├── server.py            # stdio JSON-RPC 2024-11-05 + native_fill tool
│   ├── runner.py            # 7 subprocess wrappers + skill_dir resolver
│   ├── pipeline.py          # 6-state machine driving Phase 2→5
│   ├── autofix.py           # 5 SVG-level auto-fixes
│   ├── svg_edits.py         # surgical SVG text edits (ElementTree)
│   └── io_utils.py          # atomic UTF-8 file I/O
├── tests/test_native_fill.py
├── examples/native_fill_demo.py
├── docs/
│   ├── NATIVE_FILL_PIPELINE_GUIDE.md   # upstream pipeline spec (read-only)
│   ├── mcp-project-layout.md            # upstream MCP layout spec (read-only)
│   └── native-fill-mcp-design.md        # this MCP's design notes
├── SKILL.md                 # Claude skill entry point
├── pyproject.toml
├── README.md                # this file
├── .gitignore
└── .env.example
```

## Hard rules

1. **stdlib-only** — no `mcp[cli]`, no `fastmcp`, no third-party deps.
2. **Zero references** to `mcp_ppt_master` or any sibling package.
3. **Logs to stderr** — stdout is the JSON-RPC transport channel.
4. **Tools return plain JSON** — `tools/call` carries `content[0].text` JSON
   plus `structuredContent` and `isError`.
5. **No state held between requests** — each `tools/call` orchestrates its own
   subprocesses; the server is a pure dispatcher.

## License

MIT. Vendored ppt-master scripts are © Hugo He under MIT (see
`C:\Users\Administrator\.claude\skills\ppt-master\LICENSE`).
