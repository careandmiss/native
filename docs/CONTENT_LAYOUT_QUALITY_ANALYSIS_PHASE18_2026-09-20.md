# Content Page SVG Layout Quality Analysis — Phase 18 (2026-09-20)

## 背景

继 [`CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md`](./CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md) 提出的 6 类根因 (R1–R6) 和 Tier 1/2/3 三阶段路线图之后,本轮 (Phase 18) 完成:

- **Phase 16 (commit `d498148`, 2026-09-18 15:03)** —— Tier 1 全部修复:
  R2 (`body_bounds` 硬编码)、R3 (`chrome_suppress_for` 未生效)、R4 (statement-caption 重复)、R5 (raw 兜底空白)
- **Phase 17 (commit `f766be4`, 2026-09-18 18:06)** —— "智能布局"四件套:
  A) `archetype_router.py` 11 条规则自动选 archetype;
  B) `bounds_optimizer.py` snug-fit bounds;
  C) `block_renderer.py` 内 bullet-list / 3-column-cards 自适应字号;
  D) `section_dispatcher.py` (留作 building block,未接入)

用户在 Phase 17 后再次反馈:

> "我们的 ppt 生成的内容页排版仍旧不是非常好"

本文档作为 Phase 18 实施前的复盘,完成三件事:

1. **沉淀 ppt-master 的内容页设计资源**作为本次借鉴目标;
2. **明确 Phase 17 已交付与遗留差距**(为什么"仍旧不是非常好");
3. **给出 Phase 18 五项修复任务清单**(对应 plan 文件 `humble-plotting-frog.md`)。

---

## 1. 借鉴目标 —— ppt-master 技能的内容页设计资源

技能路径:`C:\Users\Administrator\.claude\skills\ppt-master\`

### 1.1 7 套 layout 库 (`templates/layouts/`)

| Library ID | Canvas | 内容页 archetype 数 | 关键 archetype |
|---|---|---|---|
| `presentation_core` | 1280×720 (16:9) | 20 | `title_content`、`two_content`、`comparison`、`content_caption`、`editorial_split`、`three_card`、`kpi_dashboard`、`process_timeline`、`data_story`、`chart_insight`、`table_summary` |
| `presentation_core_43` | 1024×768 (4:3) | 16 | 含 `kpi_grid`、`stacked_split` |
| `report_core` | 1280×720 | 13 | 含 `kpi_row`、`matrix_2x2`、`appendix`,持久 header/footer + 页码 |
| `editorial_bleed` | 1280×720 | 10 | 暗底 (`#0F172A`) 大图出血版式 |
| `moments_square` | 1080×1080 | 8 | 朋友圈方版 |
| `story_vertical` | 1080×1920 | 9 | 抖音竖版 |
| `xiaohongshu_post` | 1242×1660 | 10 | 小红书图文 |

每个 SVG 都遵循 `data-pptx-bounds="x y width height"` 契约 + `<g data-pptx-placeholder="title|body|object">` slot 系统。

### 1.2 核心内容页几何 (从 `presentation_core` 实测)

| 模板 | 几何要点 |
|---|---|
| `02_title_content.svg` | title slot `64 40 1152 72`,content panel `64 152 1152 504 rx=12 fill=#F4F6F8`,rule at y=120 (96px accent bar) |
| `04_two_content.svg` | 双面板 `552 × 504 rx=12`,**gutter = 48 px** (664 − 64 − 552) |
| `05_comparison.svg` | 同上 + 垂直 `comparison-divider` `x=638 y=184 w=4 h=440 fill=#CBD5E1` |
| `08_content_caption.svg` | 左 rail `x=360 y=64 w=2 h=592` + 右 panel `400 64 816 592 rx=12`,**左 caption + 右 content 非对称几何** |
| `11_editorial_split.svg` | 44/49 文本/图片,rail `552 64 4 592`,**gutter = 40 px** |
| `12_three_card.svg` | 三面板 `368 × 472 rx=16`,**gutter = 24 px** |
| `13_kpi_dashboard.svg` | 4 KPI 卡 `264 × 128 rx=12`,**gutter = 32 px** + evidence panel |
| `14_process_timeline.svg` | 横轴 `y=256`,4 节点 `r=12`,step slot `232 × 344` 居中于节点下方 + takeaway panel `64 568 1152 72` |
| `19_chart_insight.svg` | 2 面板 (760×456 + 360×456),**gutter = 32 px** |
| `20_table_summary.svg` | 表 824×456 + 摘要 296×456,**gutter = 32 px** |

### 1.3 设计令牌 (本次参照)

- **页面边距**: `64 px` (16:9) / `48 px` (report_core)
- **内 panel padding**: 24 px L/R
- **面板间距**: 24–48 px (随 panel 数量减少而增加)
- **配色 (60-30-10)**:
  - 字段/背景:`#FFFFFF` / `#F4F6F8` / `#F8FAFC`
  - 描边/分隔:`#D6DCE3` / `#E2E8F0` / `#CBD5E1`
  - 正文次要:`#94A3B8` / `#64748B` / `#475569`
  - 标题:`#334155` / `#1E293B`
  - 暗底 (`editorial_bleed`):`#0F172A`
- **字号锚**:
  - Title:`font-size=36 weight=700 fill=#1E293B`
  - Subtitle/comparison heading:`24 weight=700 fill=#334155`
  - Body:`22 fill=#94A3B8`
  - KPI value:`28 weight=700 fill=#334155`
  - Annotation:`12–14 fill=#94A3B8`
- **字号比例** (body=1×):
  - Cover title / single-focus hero:**2.5–5×**
  - Chapter title:**2–2.5×**
  - Page title / KPI hero:**1.5–2×**
  - Subtitle:**1.2–1.5×**
  - Lead:**1.1–1.4×**
  - Body:**1×**
  - Annotation:**0.7–0.85×**
  - Footnote:**0.5–0.65×**
- **Leading (mandatory)**:
  - Multiline titles: 1.2–1.3 × font-size
  - Dense/small body: 1.4–1.5 ×
  - Ordinary body: 1.5–1.6 ×
  - Large/sparse/breathing body: 1.6–2.0 ×
- **`page_rhythm` (mandatory per page)**:
  - `anchor` — structural (cover/chapter/TOC/ending)
  - `dense` — 信息密集基线 (默认)
  - `breathing` — 低密度暂停 (hero quote / 大数字 / 出血图 + 浮标 caption)
- **Composition checks (几何硬约束)**:bounds overlap >1 px fail;module text overflow >5% fail;peer containers 共享 treatment 除非语义区分。

---

## 2. Phase 17 已交付 + 遗留差距

### 2.1 已交付 (working)

| 模块 | 状态 | 影响 |
|---|---|---|
| `archetype_router.py` | ✅ | 11 条规则取代 LLM 随机选 archetype;KPI / ordered-list / contrast / pipe-table 触发稳定 |
| `bounds_optimizer.py` | ✅ | `compute_optimal_bounds()` snug-fit;`compute_optimal_font_size()` 二分收缩字体 (min 12pt) |
| `block_renderer.py` 内 bullet-list / 3-column-cards 自适应字号 | ✅ | per-item 字号 16→14→13→12 + 横向截断兜底 |
| `_normalize_new_blocks()` body_bounds 自动填充 | ✅ | 6 类 bounds tuple 取代 `120 130 1060 480` 单一硬编码 |
| `chrome_suppress_for` 真正生效 | ✅ | hero archetype 抑制 topbar;`new_blocks_by_svg` lookup 让 regex 抓 `_b/_c/_d` |

### 2.2 遗留差距 (为什么"仍旧不是非常好")

阅读 `workspace/3山西柏腾科技有限公司采购制度_auto/authoring-svg-flat/` 与 `workspace/archetype_demo_auto/`,发现以下 5 类仍未修问题:

#### Gap 1 — Layer-2 路由未生效 (`source_slide_hint` 声明但 page_plan 全 4)

- **位置**:`archetype_meta.py` 第 27–30 行 文档明确说明 `source_slide_hint` 是 Layer-2 路由值 (hero→6, wide→7, full→8)
- **现象**:`workspace/3采购制度_auto/page_plan.json` 所有内容页 `source_slide: 4`;`archetype_demo_auto/page_plan.json` 同样全 4
- **后果**:hero_statement / hero-number / callout-box / statement-caption (Layer-2 应 route 到 slide_06 = 无椭圆 chrome) 与 3-column-cards / procedural-steps / comparison (Layer-2 应 route 到 slide_08 = full chrome) **视觉差异为 0**
- **诊断**:这是用户"排版仍旧不是非常好"的最直接原因 —— 不同 archetype 看着"差不多"

#### Gap 2 — `3-column-cards` 主次填色同质

- **位置**:`block_renderer.py` 3-column-cards 分支,约 line 1230–1280
- **现象**:3 张卡片 `fill-opacity=0.12` 完全均匀 (参见 `slide_part04b_content.svg`)
- **后果**:无视觉层级 → "看着像 3 张一样的卡片"
- **ppt-master 对照**:`12_three_card.svg` 主卡用 `rx=16 fill=#F4F6F8 stroke=#D6DCE3` (中明度),次卡用更浅的 stroke;主卡标题 24pt,次卡 20pt

#### Gap 3 — `3-column-cards` 卡片高度过短 (140 px 嵌 530 px 框架,74% 空白)

- **现象**:`slide_part04b_content.svg` 3 卡 `352 × 140` anchored at y=300,总占用 ~26% 框架高度
- **后果**:大块白色空白,内容稀疏时尤其严重
- **修复方向**:提高卡片高度到 472 px (同 `12_three_card.svg`),同步调整 title / body 行数与字号

#### Gap 4 — `revision-table` 列对齐错位

- **位置**:`block_renderer.py` revision-table 分支
- **现象**:分隔线 x=385/650/915 ≠ 文本中心 252.5/517.5/782.5/1047.5
- **后果**:表头视觉错位 30–40 px,数据列读起来割裂
- **ppt-master 对照**:`20_table_summary.svg` 表 panel `824 × 456`,列用 inline JSON `schema: "ppt-master.semantic-table.v2"` 显式声明 `column_widths` + `row_heights`

#### Gap 5 — LLM SYSTEM_PROMPT 的 archetype 决策树缺乏正反例

- **位置**:`llm_planner.py` SYSTEM_PROMPT 约 line 82–340
- **现象**:仅描述"short claim → hero_statement"类单行规则,没有正反例对照
- **后果**:`route_archetype()` 兜底时,LLM 仍可能给错 archetype (例如把 "5 大目标" 当 bullet-list 而非 hero-number)
- **修复方向**:在 SYSTEM_PROMPT 显式列 12 条 priority table + 4–6 个正反例

---

## 3. Phase 18 五项修复任务 (本次交付)

| # | 任务 | 文件 | 工作量 |
|---|---|---|---|
| 1 | 调整 13 个 archetype 的 `body_bounds` 对齐 ppt-master 几何 | `src/mcp_ppt_native_fill/archetype_meta.py` | 1 h |
| 2 | 修复 `3-column-cards` 主次填色 (0.85 vs 0.06) + 增高卡片 | `src/mcp_ppt_native_fill/block_renderer.py` | 2 h |
| 3 | 修复 `revision-table` 列对齐 (4 列等宽 + 分隔线重算) | `src/mcp_ppt_native_fill/block_renderer.py` | 1.5 h |
| 4 | 接入 Layer-2 路由 (`source_slide_hint` → `page_plan.source_slide`) | `src/mcp_ppt_native_fill/workspace_expand.py` + `src/mcp_ppt_native_fill/pipeline.py` | 2 h |
| 5 | 强化 LLM SYSTEM_PROMPT archetype 决策树 + 正反例 | `src/mcp_ppt_native_fill/llm_planner.py` | 1 h |
| 6 | 新增 `outline-toc` archetype (左侧 rail + 右侧 6 行列表) | `src/mcp_ppt_native_fill/archetype_meta.py` + `src/mcp_ppt_native_fill/block_renderer.py` + `tests/test_native_fill.py` | 2 h |

合计 ~9.5 h,小到中改动。

---

## 4. 验证策略

1. **单元测试**:`pytest tests/test_native_fill.py -k "3_column_cards or revision_table or source_slide or outline_toc or layout_meta" -v`
   - 新增 4 个测试:3-column-cards 主次填色差异、revision-table 列对齐、source_slide_hint 路由、outline-toc 渲染
2. **端到端**:`python examples/structured_demo.py` 重新生成 `workspace/archetype_demo_PHASE18_*.pptx`
3. **目测对比**:
   - 旧:`workspace/archetype_demo_20260920_084417.pptx`
   - 新:`workspace/archetype_demo_PHASE18_*.pptx`
   - 验证:hero_statement / hero-number 有大量负空间;3-column-cards 第一张卡明显比其余两张深;revision-table 文本与列分隔线对齐
4. **`page_plan.json` 路由验证**:
   ```bash
   cat workspace/archetype_demo_PHASE18_*/page_plan.json | jq '.pages[] | {layout, source_slide}'
   ```
   预期:出现 `source_slide=6/7/8` 多样化,而非全部 4

---

## 5. 范围之外 (Phase 19+ 候选)

来自 [`CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md`](./CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md) Tier 3 + 本轮发现的新问题:

- **R1. 顶部 chrome 三重叠加** 完全重构 (template 的 shape-17 y=25 vs injected topbar y=32)
- **Cover title 重复**:slide 1/4/6/11/12/15 在 `readback.md` 出现"标题文本 ×2" (chrome + body 同时渲染)
- **Footer 页码硬编码** "P04 / 12" 不随实际 slide 总数变化
- **8 层字体规范全栈强制**:`TYPOGRAPHY` dict 已有但 `compute_optimal_font_size` 只在 bullet-list / 3-column-cards 用,其余 archetype 仍 hardcode
- **Canvas 利用率自动收缩算法升级**:Phase 17-B 仅 snug-fit,但 hero archetype 在大画布仍有过多负空间
- **`section_dispatcher` (Phase 17-D) 接入 workspace_expand**:当前留作 building block,LLM 仍按 prompt 分页
- **真实长文档 (5商务部工作手册2) 上的 E2E 验证**:本次只在 archetype_demo 上跑

---

## 6. 与 Phase 17 文档的关系

| 文档 | 角色 |
|---|---|
| `CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md` | 6 类根因 + Tier 1/2/3 路线图 |
| `CONTENT_ADAPTIVE_LAYOUT_2026-09-18.md` | Round-3 设计 (Layer 1+2) |
| `CONTENT_LAYOUT_QUALITY_ANALYSIS_PHASE18_2026-09-20.md` (本文) | Phase 18 实施前的复盘 + ppt-master 借鉴 + 5 项任务清单 |
| `humble-plotting-frog.md` (plan) | Phase 18 的具体步骤 + 文件清单 + 验证 |

---

**作者**:Claude (Phase 18 planner)
**日期**:2026-09-20
**版本**:v1
**状态**:已批准 (ExitPlanMode)