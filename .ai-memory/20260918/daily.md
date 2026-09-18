# 2026-09-18 Daily Log

## [09:30] - Round-2 修复: 章节页乱 + THANK YOU 附录污染 + 内容溢出

- **文件**:
  - src/mcp_ppt_native_fill/block_renderer.py (`_fs` 改写 + `_compute_max_body_lines` / `_body_lines_cascade` 辅助函数；应用到 statement-caption + hero_statement)
  - src/mcp_ppt_native_fill/llm_planner.py (SYSTEM_PROMPT 加 ending/raw 防御条目；`_ending_target_filter` + `_detect_raw_svg_overflow` 辅助函数；`_normalize_new_blocks` 接受 `skeleton_index` 参数)
  - src/mcp_ppt_native_fill/pipeline.py (`phase3_author` 加 skeleton_kind=='ending' 防御)
  - src/mcp_ppt_native_fill/autofix.py (`fix_text_overflow_in_groups` 独立函数 + 接入 `run_autofix_round`)
- **决策**: 4 个 bug 独立 fix；用 Workflow 并行 4 个 agent 各负责一个文件；slide_05.svg 用 reject 而非 redirect (Plan agent 验证 redirect 与 page_plan 顺序交互危险)
- **验证**:
  - 单元测试: `_fs({font_size:'32'}, 'bullet')` → '13' (was '32'); `_compute_max_body_lines(bh=480, by=130, body_y0=303.7, line_h=26.5, block_after_h=91.1)` → 8 (was 硬编码 4)
  - 端到端: 14 页 PPTX，35 s ok=True
  - readback.md: slide 14 纯 THANK YOU (无 修订状态/首次发布); slide 13 是附件修订记录; slide 10 procedural-steps 字体 12/13/32 分布正确 (was 全部 32); slide 4 段落 6 行 (was 4); slide 8 引用正确归因
- **结果**: 推送 commit `4f484f7` 到 `origin/integration/sink-generate-local-ppt-2026-09-14`

## [11:35] - Round-3: Content-adaptive Layer 1+2 实施 + 端到端验证

- **文件**:
  - src/mcp_ppt_native_fill/archetype_meta.py (NEW, 86 LOC — 17 个 archetype 的 metadata 单一来源)
  - src/mcp_ppt_native_fill/relationships_detector.py (NEW, 145 LOC — 7 种 atom 启发式检测)
  - src/mcp_ppt_native_fill/chrome.py (+28 LOC — `chrome_suppress_for(archetype, rhythm)`)
  - src/mcp_ppt_native_fill/block_renderer.py (+120 LOC — 16 个 archetype 分支读 `archetype_meta.body_bounds`)
  - src/mcp_ppt_native_fill/llm_planner.py (+102 LOC — `_normalize_new_blocks` 加 4 字段 + frozenset 白名单)
  - src/mcp_ppt_native_fill/workspace_expand.py (+104 LOC — `content_skeleton_pool: list[int]`)
  - src/mcp_ppt_native_fill/pipeline.py (multiple — `realize_plan` 接受 pool + `_inject_content_chrome` archetype-aware + `_apply_archetype_meta` helper)
  - src/mcp_ppt_native_fill/entry.py (加 `content_skeleton_pool = hints.get(...)` 传递)
  - tools/clone_content_template.py (NEW, 245 LOC — slide_04 克隆到 slide_06/07/08,带 image rId 保留)
- **决策**:
  - 用户确认 Layer 1+2 范围;hero archetypes 抑制 topbar
  - 3 个独立 agent 并行 Phase 1 (archetype_meta + detector / chrome_suppress + cloner / llm_planner); 3 个独立 agent 并行 Phase 2 (block_renderer / workspace_expand / pipeline)
  - Phase 2F (pipeline) 被 Agent 工具自动拒绝 → 9 个手动 Edit 完成
  - cloner 的 _copy_image_rels 修两次: 第一次 target_ref 错,第二次 rId 不一致 → 加 `_rewrite_picture_embeds`
  - preflight 跑在 autofix 之前 → 报 parse-error 但已被 autofix 修好 → 调整顺序 (preflight 之后)
  - 修 2 个无关 pre-existing bug: header_map (list→dict coerce) / image rels preservation
  - **关键修复**: pipeline.py `realize_plan` 的 layout_hint 查 `new_blocks` 而不是 `page_plan_additions` (LLM 把 archetype 放在 block 上,不在 addition 上)
- **验证**:
  - **v1 模板 + pool=[4,4,4]**: ok=True, stage=done, 15 slides
  - **v1 模板 (无 pool)**: ok=True (回归基线)
  - archetype routing log: `slide_part02 → statement-caption / part03 → callout-box / part04 → hero_statement / part05b → 3-column-cards / part06 → revision-table`
  - chrome 变体: slide_part05b (3-column-cards) topbar=False,其余 topbar=True
  - LLM 通过 spec["body_bounds"] 显式覆盖 → 6 页 body_bounds 相同;LLM prompt 优化空间留给下一轮
  - **未通过**: v2 模板 (克隆 slide_06/07/08) + pool=[6,7,8] → phase5 svg_to_pptx `Edited round-trip source object did not produce a DrawingML shape: 2` — 克隆 slide 缺 source_ref,需要更深 roundtrip 修复
- **结果**: 端到端通过 (v1 + pool=[4,4,4]),archetype 路由工作;v2 + pool=[6,7,8] 需要后续 roundtrip 修复

## [14:35] - Phase 15: LLM 内容分页 prompt (1 divider + N content per section)

- **文件**: src/mcp_ppt_native_fill/llm_planner.py (SYSTEM_PROMPT 新增 "Content splitting (Phase 15)" 节,改 mandatory-body 规则)
- **背景**: 用户反馈内容页固定 1 div + 1 content,长章节应被拆成多 content 页;LLM 输出不稳定 (4-9 new_blocks per 5 sections)
- **决策**:
  - 新增 prompt 节 "Content splitting" — 按段落数/字符数/子要点数决定 content 页数 (1 段 → 1 页, 4+ 要点 → 2-3 页, 5+ 子标题 → 3-4 页, 10+ → 考虑 revision-table)
  - 命名约定:多 content 页用 `_b`, `_c`, `_d` 后缀 (slide_part02b_content.svg)
  - Rule of thumb:宁愿多一页,不要挤一页
  - mandatory-body 规则强调每个 cloned content 必须有匹配的 new_blocks,且各页 layout 应有变化 (hero_statement intro → 3-column-cards enum → bullet-list)
- **验证**:
  - 5 章节 → 19 slides (vs 之前 13)
  - 章节四 (4 个分要点 + 多子标题) → 1 div + 3 content
  - 章节五 (各岗位职责) → 1 div + 3 content
  - 章节六 (日常事务管理) → 1 div + 2 content
  - 内容密度自适应:LLM 根据段落/要点数决定
- **结果**: 内容自适应分页生效,19 张 slide 取代 13 张

