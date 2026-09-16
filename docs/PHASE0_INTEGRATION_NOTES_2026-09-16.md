# Phase 0 集成点清单（2026-09-16）

> 用于 Phase 2 实施 `cards_for_section` 签名破坏性改动时的 checklist。
> 计划详见 `docs/CONTENT_PAGE_FILL_PLAN_2026-09-16.md`。

## 1. `cards_for_section` 调用点

| 文件 | 行号 | 当前代码 | 需要改动 |
|---|---|---|---|
| `src/mcp_ppt_native_fill\toc_detection.py` | 563 | `def cards_for_section(sections, stem) -> list[dict]` (定义) | **是** — 签名改为 `(sections, stem) -> tuple[list[dict], dict]` |
| `src/mcp_ppt_native_fill\toc_detection.py` | 685 | `cards = cards_for_section(sections, stem)` | **是** — 改为 `cards, meta = cards_for_section(sections, stem)` |
| `src\mcp_ppt_native_fill\workspace_expand.py` | 177 | `cards = _cards_for_section(sections, stem)` | **是** — 改为 `cards, meta = _cards_for_section(sections, stem)`，并把 line 178-188 的 cards 处理逻辑保持在 unpack 之后 |
| `src\mcp_ppt_native_fill\pipeline.py` | 208 | `cards_for_section as _cards_for_section,` (alias import) | **否** — 仅别名，无需改 |
| `src\mcp_ppt_native_fill\workspace_expand.py` | 34 | `cards_for_section as _cards_for_section,` (alias import) | **否** — 仅别名，无需改 |
| `src\mcp_ppt_native_fill\workspace_expand.py` | 17 | docstring 引用（提及 `cards_for_section`） | **否** — 仅文档引用 |

**外部（tests/examples）**：
- 无直接测试调用 `cards_for_section` 的代码（`grep` 确认）
- 所有测试覆盖都通过 `pipeline._fill_missing_content_blocks` 间接触发

## 2. `fill_missing_content_blocks` 调用点

| 文件 | 行号 | 当前代码 | 需要改动 |
|---|---|---|---|
| `src\mcp_ppt_native_fill\toc_detection.py` | 642 | `def fill_missing_content_blocks(*, ...)` (定义) | **是** — 内部 line 685 的 `cards = cards_for_section(...)` 需改为 unpack |
| `src\mcp_ppt_native_fill\pipeline.py` | 211 | `fill_missing_content_blocks as _fill_missing_content_blocks,` (alias import) | **否** |
| `src\mcp_ppt_native_fill\pipeline.py` | 498 | `_fill_missing_content_blocks(...)` (生产调用) | **否** — 函数签名未变 |
| `tests\test_native_fill.py` | 2299 | `pipeline._fill_missing_content_blocks(...)` (happy path with markdown) | **是** — 加 meta-aware 断言（plan §3.1.4 已记录） |
| `tests\test_native_fill.py` | 2331 | `pipeline._fill_missing_content_blocks(...)` (pre-existing blocks) | **是** — 同上 |
| `tests\test_native_fill.py` | 2349 | `pipeline._fill_missing_content_blocks(...)` (no markdown) | **是** — 同上 |

## 3. `split_markdown_sections` 调用点

| 文件 | 行号 | 当前代码 | 需要改动 |
|---|---|---|---|
| `src\mcp_ppt_native_fill\toc_detection.py` | 543 | `def split_markdown_sections(md_text) -> list[dict[str, str]]` (定义) | **是** — 返回值加 `meta` 字段：`[{title, body, meta}, ...]`，meta 默认空 dict |
| `src\mcp_ppt_native_fill\toc_detection.py` | 675 | `sections = split_markdown_sections(md_text) if md_text else []` | **是** — sections 元素多了一个 `meta` key，但下游消费方不读 meta 不影响行为 |
| `src\mcp_ppt_native_fill\workspace_expand.py` | 129 | `sections = _split_markdown_sections(md_text)` | **否** — 下游只读 `s["title"]` 和 `s["body"]`，加 `meta` key 不破现有逻辑 |
| `src\mcp_ppt_native_fill\workspace_expand.py` | 292 | `sections = _split_markdown_sections(md_path.read_text(encoding="utf-8"))` | **否** — 同上 |
| `src\mcp_ppt_native_fill\workspace_expand.py` | 396 | `sections = _split_markdown_sections(md_path.read_text(encoding="utf-8"))` | **否** — 同上 |
| `src\mcp_ppt_native_fill\pipeline.py` | 216 | `split_markdown_sections as _split_markdown_sections,` (alias import) | **否** |
| `tests\test_native_fill.py` | 3288/3297/3303/3310 | 测试用例（间接调用） | **是** — 测试期望 `[{title, body}]` 仍是有效 dict，加 `meta` key 后 dict 仍是 dict，断言应继续通过；新增 meta 解析测试 |

## 4. Phase 2 实施 checklist

按顺序修改（自上而下依赖最小）：

1. `src\mcp_ppt_native_fill\toc_detection.py:543` — `split_markdown_sections` 返回值加 `meta`
2. `src\mcp_ppt_native_fill\toc_detection.py:563` — `cards_for_section` 签名改为 `tuple`
3. `src\mcp_ppt_native_fill\toc_detection.py:685` — 改为 unpack
4. `src\mcp_ppt_native_fill\workspace_expand.py:177` — 改为 unpack + 处理 meta 优先
5. `tests\test_native_fill.py:3288-3310` — 补 meta-aware 断言（additive，不破既有断言）
6. `tests\test_native_fill.py:2299/2331/2349` — 补 meta-aware 断言

## 5. 验证标准

- 所有 166 现有单元测试通过
- 新增 ≥ 6 个 `TestSplitMarkdownSectionsMeta` 测试通过
- 新增 ≥ 1 个 `TestExpandWorkspaceMarkdownMeta` 集成测试通过
- `examples/smart_toc_fill.py` 跑通（未结构化 markdown 应走 fallback 路径，无 meta = `{}`）

## 6. Out of Scope（Phase 2 不动）

- `cards_from_body` 签名（保持不变，由 Phase 1 改内部逻辑）
- `block_renderer.py` 任何 layout（Phase 3）
- `examples/` 任何脚本（Phase 4）
- `pipeline.py` 任何函数（除 line 498 的 `_fill_missing_content_blocks` 调用本身不变）