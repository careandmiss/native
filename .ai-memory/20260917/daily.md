# 2026-09-17 Daily Log

## [16:25] - Wave 2 计划书撰写: native_fill 端到端 7 阶段流水线文档化

- **文件**: docs/端到端协议计划书.md (新建, 580+ 行)
- **决策**: 把现有的 pipeline.py 7 阶段状态机形式化；列出 13 个 PR（10 已合入 + 3 规划）
- **验证**: 与现有代码逐个 PR 交叉核对

## [16:30] - PR-11 实现: generate_pptx 一键 wrapper

- **文件**: src/mcp_ppt_native_fill/entry.py (新建), src/mcp_ppt_native_fill/__init__.py (导出)
- **决策**: 不改 pipeline.py，独立 entry.py 包一层；默认 enable_llm_planner=True；smart TOC 2x4 网格；chrome 注入 on；`clean_workspace=True` one-shot 语义
- **验证**: `inspect.signature(generate_pptx)` 正确；`from mcp_ppt_native_fill import generate_pptx, DEFAULT_TOC_GRID` 成功

## [16:35] - PR-12 实现: preflight 进 phase 4 自动门禁

- **文件**: src/mcp_ppt_native_fill/pipeline.py (修改 phase4_quality + run_native_fill + run_with_mapping), src/mcp_ppt_native_fill/entry.py (转发 preflight_strict hint)
- **决策**: preflight_strict=None 继承 strict，boteng 默认 advisory；preflight 在 vendor QC 之前跑；违规前 10 条详情进 error/warning
- **验证**:
  - `inspect.signature(phase4_quality)` 含 preflight_strict
  - boteng 13 SVG `[OK] no preflight violations`
  - 合成坏 SVG `[FAIL] 4 error(s), 1 warning(s)` 全命中

## [16:42] - PR-13 实现: cairosvg PNG 预览

- **文件**: src/mcp_ppt_native_fill/render_diff.py (新建), src/mcp_ppt_native_fill/pipeline.py (phase5_export + run_native_fill + run_with_mapping 转发 render_previews), src/mcp_ppt_native_fill/entry.py (默认 True), src/mcp_ppt_native_fill/__init__.py (导出)
- **决策**: cairosvg 而非 LibreOffice（Anaconda 无 LO）；失败兜底写 1x1 PNG + .error.txt；run_with_mapping 默认 off / generate_pptx 默认 on
- **验证**: boteng 13 SVG 全部成功渲染到 validation/diff/*.png，0 failures

## [16:50] - 计划书更新: Wave 2 标 ✅ + 新增 §8.5 实施记录

- **文件**: docs/端到端协议计划书.md (修改 §4 / §8 / §9, 新增 §8.5)
- **决策**: 把 3 个 PR 的落地点、关键决策、验证证据、新增文件清单固化进文档
- **验证**: 文档自检 — PR 编号、文件路径、命令与实际合入状态一致

## [17:00] - Bug 诊断: 内容页空白 + 章节页序号缺失

- **文件**: workspace/3山西柏腾科技有限公司采购制度_auto/authoring-svg-flat/*.svg
- **决策**: 两个独立 bug —
  1. `llm_planner.py:234` prompt 让 LLM 写 `spec.phases=[...]`，但 `block_renderer.py:983` 读 `spec.steps=[...]` → ValueError
  2. `pipeline.py:758-826` 在 render 前 UNCONDITIONALLY 删 body group → render 失败后 slide 仅剩 chrome
  3. `llm_planner.py:_geometry_based_max_chars` 用 `chars_that_fit()` 返回 5（受 CJK min() 主导），导致 "PART 01" 7 字被截为 "PART 0…"
- **验证**: SVG 实读确认 (slide_partNN_div.svg 全部 4 个 shape-4 都是 "PART…"，slide_partNN_content.svg 只有标题无 body)

## [17:05] - Fix: prompt 同步 + 渲染器 defensive + 顺序重排 + 容量绕开

- **文件**:
  - src/mcp_ppt_native_fill/llm_planner.py (line 234 prompt 改 `steps`；`_geometry_based_max_chars` 加 fs≥40 & h≥50 时 max_chars=10 绕开 CJK min()；`_truncate_to_fit` 仅在截 ≥2 字时加 …)
  - src/mcp_ppt_native_fill/block_renderer.py (line 983 `steps = payload.get("steps") or payload.get("phases") or []` 防御)
  - src/mcp_ppt_native_fill/pipeline.py (line 758-826 重排：先 render，成功后才删 body_cards，避免 ValueError 路径误删)
- **决策**: 4 处定点修改，不改 `text_width._SAMPLE_LATIN`（plan 阶段发现改 Latin 样本对 `min(cjk_cap, latin_cap)` 是 no-op）
- **验证**: 重跑 generate_pptx 37s ok=True；readback.md 显示 PART 01/02/03/04 + slide 6/8/10 都有完整 body；slide 数 9→11 (LLM 多产出 part04)
- **结果**: 用户报告的两个缺陷（章节页序号缺失 + 内容页空白）已彻底修复；plan 文件 `piped-inventing-wilkinson.md` 已落地