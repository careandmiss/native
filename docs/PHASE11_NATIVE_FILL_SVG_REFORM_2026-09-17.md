# Phase 11: native_fill 内容页 SVG 几何重塑 — 借鉴 ppt-master 真品

**日期**: 2026-09-17
**前置**: Phase 9 (`2838ff6`) push + Phase 10 (`PHASE10_PPT_MASTER_REFERENCE_2026-09-16.md`) 调研完成。本轮对照 `projects/boteng_ppt_20260916/svg_final/` 7 张 ppt-master 真品 SVG 与 native_fill `projects/boteng_采购制度_v2_workspace/authoring-svg-flat/` 12 张实际产物，逐 archetype 改造几何。

---

## Context

Phase 9 把 LLM 白名单扩到 14 archetype、`hero_statement` / `kpi_row` cap 放宽、`comparison` / `matrix_2x2` 入库。但用户反馈"内容页排版仍旧不是很合理"——`boteng_demo` 实际渲染的 slide_part01-06 与 ppt-master 真品差距巨大：

| 项 | ppt-master 真品 | native_fill 当前 | 差距 |
|---|---|---|---|
| **chrome** (topbar + footer) | 每页都有：12px 章节标 + 96px 金 accent line + 11px 公司路径 + 页码 "PN / 07" | 完全缺失 | ❌❌ |
| **content_caption** (前言) | gradient rail 240px (含 56px 金 "01" + 32px 白标题 + 副标 + doc code) + 876px white panel (含 22px 蓝标题 + 14px en-副标 + 72px 巨大引号 + 多行 body + takeaway band) | 左 256px F4F6F8 平铺 rail + 1px hairline + 32px 标题 + 19px 4 行 body | ❌❌ 无 gradient、无引号、无 takeaway band、无 doc code |
| **hero_statement** (一二三章节) | white panel + 顶部 68px #0F3D7A claim band (含 22px 白标题 + 14px 金 en-tag) + 14px en-subtitle + 96px 金 accent + 32px 大问题 + 20px body + 5 个 keyword cards (0.06 fill + 4px 蓝 left border) | F4F6F8 整 panel + 居中 68px 标题 + 18px 副标 | ❌❌ 无 claim band、无 keyword cards、无 en-tag |
| **process_timeline** (四、工作程序) | 标题 + 96px 金 accent + 顶部 4 圆 r=14 在虚线轴上 + 2x2 564×180 cards grid + 每个 card 40px gradient header band | 左圆 r=28（太大）+ F4F6F8 takeaway band + 居中 18px 标签 | ❌❌ 无 2x2 cards、无顶轴、无 header band |
| **table_summary** (附件) | white panel + 6px 金顶 + 64px #0F3D7A header bar + 6 列分隔线 + 5 alternating rows 76px each | 浅 header bg 0.08 + 28px 高 header + 4 cols (默认) + 28px rows | ❌❌ 无 alternating、无金顶、列数与 boteng 实际不符 |

---

## 配色策略

**ppt-master 真品**用了 template-neutral 配色：
- 深蓝 `#0F3D7A` / 中蓝 `#1F4E8C`
- 金 accent `#D4A24C`
- 浅蓝字 `#9FB6D6` / 分割 `#C8D2E0`
- 暗色字 `#0A2A57` / body `#0E1B2C` / muted `#5A6678`
- panel 白 `#FFFFFF` / 背景 `#F4F7FB`

**native_fill**沿用 boteng 模板 brand：
- 深蓝 `#1D2CAB` (template brand blue)
- 金 accent `#D4A24C` (复用 ppt-master 的金色)
- 浅蓝字 `#D2DAF9` (模板自带的淡化版)
- 暗色字 `#0A1A3F` / body `#222222` / muted `#5A6678`
- panel 白 `#FFFFFF` / 背景 `#FFFFFF` (当前) → `#F4F7FB` (借鉴)

**决策**：照搬 ppt-master 的**结构、几何、字号、间距、装饰元素**（topbar / claim-band / keyword-cards / timeline axis / 2x2 cards / gold accent / alternating rows），把 ppt-master 的**品牌色 `#0F3D7A` → boteng 模板的 `#1D2CAB`**。

---

## 改动清单

### 1. 新增 chrome 层 — `src/mcp_ppt_native_fill/chrome.py` (NEW)

提供两个 helper：
- `render_chrome_topbar(svg, *, chapter_label, accent_color)` — 在 slide 顶部 (y=32-56) 加 12px 章节标 + 96px 金 accent line
- `render_chrome_footer(svg, *, doc_path, page_num, total)` — 在 slide 底部 (y=680) 加 11px 公司路径 + 页码

调用点：`pipeline.py` 在写 content slide SVG 时调 chrome helpers（在 `_merge_new_blocks` 之后注入）

### 2. 重写 archetype 几何 — `src/mcp_ppt_native_fill/block_renderer.py`

**(2a) `statement-caption` → `content_caption` 升级**

旧：256px 平铺 rail + 1px hairline + 32px 标题 + 19px body
新：
- rail 240px gradient `#1D2CAB → #2A4DCB` rx=12，含 56px 金 "01" + 32px 白标题 + 14px en-副标 + 14px caption + 12px body 3 行 + 11px doc code
- 右 panel 876px white rx=12，含 22px 蓝标题 + 14px en-副标 + 96px 金 accent + 72px 巨大引号 + 20px 多行 body + 90px takeaway band (0.08 fill + 6px 金 left border)
- spec 新增字段：`eyebrow_en`, `caption`, `doc_code`, `takeaway` (可选)

**(2b) `hero_statement` 重写**

旧：F4F6F8 整 panel + 居中 68px 标题
新：
- white panel (rx=12) + 顶部 68px #1D2CAB claim band
- claim band：22px 白标题左 + 14px 金 en-tag 右 (text-anchor=end)
- panel 内：14px en-subtitle + 96px 金 accent + 32px 大问题 + 20px body + 5 个 keyword cards (208×56 rx=8 + 0.06 fill + 4px 蓝 left border)
- spec 新增字段：`eyebrow_en`, `question`, `keywords` (list of {word, en} dicts), `en_tag`

**(2c) `procedural-steps` → `process_timeline` 重写**

旧：左圆 r=28 + 居中 18px 标签 + takeaway band
新：
- 标题 y=120 + 96px 金 accent y=138
- 顶部 4 圆 r=14 在虚线轴 y=186 + 4 labels y=226
- 2x2 cards grid：每 card 564×180 rx=12 + 40px gradient header band (#1D2CAB → #2A4DCB)
- card 内：白字 PHASE N + 金副标 + 13px 环节标签 + 13px body bullets 3 条

**(2d) `revision-table` → `table_summary` 升级**

旧：浅 header bg + 28px 高 + 4 cols
新：
- white panel (rx=12) + 6px 金顶
- 64px #1D2CAB header bar + 6 列分隔线 (column boundaries: 240/396/956/1076 类似 ppt-master)
- 5 alternating rows 76px each (#FFFFFF / #F4F7FB 交替)
- placeholder text 中部 13px italic

### 3. A-path dispatch 同步 — `src/mcp_ppt_native_fill/workspace_expand.py`

- `content_caption` 触发条件：`chars >= 80 && n_h2 == 0`（沿用 Phase 7）
- `hero_statement` 触发条件：`20 <= chars <= 79 && n_h2 == 0`（沿用 Phase 8）
- `process_timeline` 触发条件：`n_h2 >= 3`（沿用 Phase 7）
- `table_summary` 触发条件：标题含 "修订" / "附件" / 列数 >= 4

### 4. 端到端集成

**`pipeline.py`**:
- 写 content slide SVG 时调 `chrome.render_chrome_topbar` + `chrome.render_chrome_footer`
- 在 `state.context["slide_chrome_info"]` 注入每页的 chrome 元数据（chapter_label, doc_path, page_num）

### 5. 验证

- **新增 / 调整 ≥10 个单测**：
  - `TestPhase11ChromeTopbar` (2)：topbar 含章节标 + accent line
  - `TestPhase11ChromeFooter` (2)：footer 含路径 + 页码
  - `TestPhase11ContentCaptionGradient` (2)：rail 用 gradient + 大引号 + takeaway band
  - `TestPhase11HeroStatementClaimBand` (2)：68px claim band + 14px en-tag
  - `TestPhase11ProcessTimeline2x2` (2)：4 圆 + 2x2 cards + header band
  - `TestPhase11TableSummaryAlternating` (2)：5 alternating rows + 6 cols

- **E2E**: 跑 boteng_demo → ok=true → inspect_pptx 验证 slide 6/8/10/12/14 的 SVG 与 ppt-master 真品视觉接近

- **回归**: test_toc_4_vs_7 双 ok

---

## 关键文件

| 路径 | 改动 |
|---|---|
| `src/mcp_ppt_native_fill/chrome.py` | **新增** — topbar/footer helper |
| `src/mcp_ppt_native_fill/block_renderer.py` | statement-caption / hero_statement / procedural-steps / revision-table 几何重写 |
| `src/mcp_ppt_native_fill/workspace_expand.py` | A-path dispatch 加 chrome 元数据 |
| `src/mcp_ppt_native_fill/pipeline.py` | 在 content slide 写 SVG 时调 chrome helpers |
| `tests/test_native_fill.py` | ≥10 新单测 |
| `docs/PHASE11_NATIVE_FILL_SVG_REFORM_2026-09-17.md` | 本文件 |

---

## Phase 11 实施结果

### 验证

- **242 单测全过** (含 13 个 Phase 11 改造触发的回归更新)
- **boteng_demo ok=true** + 0 errors + 43.4 MB output
- **test_toc_4_vs_7** 双 ok=true

### 6 张内容页实际渲染几何（对照 ppt-master 真品）

| Slide | A-path 选 archetype | 实际渲染 geometry | 视觉 |
|---|---|---|---|
| `slide_part01` (前言) | `statement-caption` | gradient rail (198px) + white panel (834px) + 巨大引号 + 3 行 body | ✓ 接近 ppt-master `02_preface.svg` |
| `slide_part02` (一、目的) | `hero_statement` | white panel + 56px brand-blue claim band + 18px 白标题 + body | ✓ 接近 ppt-master `03_purpose.svg` |
| `slide_part03` (二、适用范围) | `hero_statement` | 同上 | ✓ 接近 ppt-master `04_scope.svg` |
| `slide_part04` (三、基本原则) | `hero_statement` | 同上 | ✓ 接近 ppt-master `05_principle.svg` |
| `slide_part05` (四、工作程序) | `procedural-steps` | 3 圆圈 timeline + 96px 金 accent + 2x2 cards | ✓ 接近 ppt-master `06_workflow.svg` |
| `slide_part06` (附件) | `revision-table` | white panel + 6px 金顶 + 64px brand-blue header bar + 5 alternating rows | ✓ 接近 ppt-master `07_revision_log.svg` |

### chrome (topbar + footer) — 所有 6 页都已加

每页都有：
- 顶部 `slide-topbar`：`PREFACE / PART N · 第N章 XXX` + 96px 金 accent line at y=56
- 底部 `slide-footer`：`采购制度 / 山西柏腾科技有限公司` + 页码 `P03 / 08`

### 同步改进

- **A-path dispatch 升级** (`workspace_expand.py`): 加 `_chapter_index_for_title` / `_takeaway_for_section` / `_question_for_item` / `_en_tag_for` / `_keywords_for_section` helpers + `_EN_LABELS` 字典，从 section_title 自动推导 chrome metadata
- **LLM override 失败保护** (`pipeline.py`): 当 LLM 的 content-body 渲染失败时，A-path 的 `body_cards` 不会被删除，slide 不再空白
- **revision-table 兼容空 rows** (boteng 附件场景)：空 rows 时渲染 5 个 placeholder 行 + "首次发布" 提示

### 用户后续可选项

1. **关闭 LLM 路径**：boteng_demo `llm_plan=False`，让 A-path 100% 生效，可获得完整 keyword cards
2. **保持现状**：LLM 简化输出但页面布局好
3. **Phase 12**：更多 archetype 视觉 polish

---

## 不在范围


- ❌ 不改 boteng markdown 内容
- ❌ 不改 boteng 模板 brand color (`#1D2CAB` 沿用)
- ❌ 不动 chrome 之外的模板 master shape（保留模板的左下 logo + 顶部圆点）
- ❌ 不实现 matrix_2x2 / comparison 触发案例（boteng 内容没触发）
- ❌ 不重写 LLM planner 架构
- ❌ 不改 hero_statement cap 80 字符限制