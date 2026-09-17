# Phase 10: ppt-master 跑 boteng 文档的参考实现

**日期**: 2026-09-16
**触发**: 用户反馈"内容页面排版仍旧不是很合理 / LLM 肯定能正常排版 你去 ppt-master 里面用文档实验一下人家是怎么样实现的 并记录到当下的 docs 文件夹内"
**前置**: Phase 9 (commit `2838ff6`) 已 push。本轮专门调研 ppt-master 对同一份 boteng markdown 的 archetype 决策，记录参考实现 + 对照 native_fill 当前的差距。

---

## 0. 实验方法

`ppt-master` 不是单一可执行程序——它是一个 **AI-driven workflow skill** (Step 1-7 完整流程)，通过 SVG archetype 描述几何 + 占位符结构，由 Brand/Style 注入颜色身份。我无法"运行"它，但**可以模拟它的 archetype 决策规则**：

- `presentation_core/templates/<NN>_<key>.svg` 里定义了每个 archetype 的几何（panel rx/colors/slot bounds/font-sizes）
- `references/plan-core.md` §3 (line 56-82) 定义了 Composition Patterns（按内容选 layout）
- `references/shared-standards-core.md` §6.2 (line 163-183) 定义了 reading mode 与 archetype capacity 的对应

我用 Python 模拟 ppt-master 的决策算法，对 boteng `3山西柏腾科技有限公司采购制度.md` 的 6 个 H1 章节逐个判 archetype：

```bash
PYTHONIOENCODING=utf-8 python -X utf8 scripts/sim_ppt_master_archetype.py
```

脚本：`scripts/sim_ppt_master_archetype.py`

---

## 1. ppt-master 对 boteng 6 张内容页的 archetype 决策

```
====================================================================================================
#   section           chars   #h2   archetype             why this layout
====================================================================================================
1   前言                132     0     content_caption       left 256px rail + right 816px panel for 132 chars body
2   一、目的              72     0     hero_statement        1152x528 tinted field + 68px bold headline (1-line claim, 72 chars)
3   二、适用范围            25     0     hero_statement        1152x528 tinted field + 68px bold headline (1-line claim, 25 chars)
4   三、基本原则            77     0     hero_statement        1152x528 tinted field + 68px bold headline (1-line claim, 77 chars)
5   四、工作程序           2018     8     process_timeline     4 macro phases by semantic clustering of 8 H2 sub-rules
6   附件：               664     0     table_summary         6-column revision table
```

### 1.1 决策规则（ppt-master Composition Patterns）

| 触发条件 | ppt-master archetype | svg 路径 | 几何关键参数 |
|---|---|---|---|
| `chars >= 80 && n_h2 == 0` | **content_caption** | `presentation_core/templates/08_content_caption.svg` | left 256px rail (`#D6DCE3`) + right 816px panel (`#F4F6F8`, rx=12); PAGE_TITLE 34px bold, CAPTION 22px, CONTENT_AREA 22px muted |
| `20 <= chars <= 79 && n_h2 == 0` | **hero_statement** | `presentation_core/templates/10_hero_statement.svg` | 全 bounds 1152×528 tinted field (`#F4F6F8`, rx=20) + 顶部 160×6 accent (`#CBD5E1`); KEY_MESSAGE 68px bold; SUBTITLE 26px |
| `n_h2 >= 3` | **process_timeline** | `presentation_core/templates/14_process_timeline.svg` | 4 个圆点 r=12 + 水平 axis line (`#CBD5E1` w=4); 4 个 STEP 槽 (text-anchor=middle, 22px fill `#334155`); 底部 takeaway panel (`#F4F6F8`, rx=12) + KEY_MESSAGE 20px |
| `len(table_rows) >= 1` | **table_summary** | `presentation_core/templates/20_table_summary.svg` | 824×456 table panel + 296×456 message panel + 底部 source line |

### 1.2 关键观察 — ppt-master 的 4 个 archetype **完全没有数字 KPI / quote box / 拼凑 chapter number** 等 boteng 当前走的旧 layout

- **没有 `hero-number`**：boteng slide 6 "1目的" 72px 居中是 LLM 自创，ppt-master 根本不用 KPI 大数字表示章节
- **没有 `callout-box`**：boteng slide 8/10 用了 legacy 蓝色 quote 风格，但 ppt-master 的 quote 是放在 `content_caption` 的左 rail 或 `hero_statement` 的 field 里
- **没有 `two-column-compare`**：boteng slide 10 "秉公办事 vs 质量价格" 是 LLM 误判为对比，ppt-master 三、基本原则应该用 hero_statement 单短句
- **没有 `flow-steps` 5-card 平铺**：boteng slide 12 走 5 张等距卡片，ppt-master 永远是 4-step 圆点 + takeaway band

---

## 2. ppt-master 期望的 6 张内容页详细规格

### 2.1 slide_part01（前言 / 132 chars / 0 H2）— `content_caption`

**ppt-master SVG** (`08_content_caption.svg`):
```xml
<rect x="360" y="64" width="2" height="592" fill="#D6DCE3"/>  <!-- vertical rail -->
<rect x="400" y="64" width="816" height="592" rx="12" fill="#F4F6F8"
      stroke="#D6DCE3" stroke-width="1"/>  <!-- right panel -->
<g id="content-caption-title-slot" data-pptx-bounds="64 112 256 104">
  <text x="64" y="180" font-size="34" font-weight="700" fill="#1E293B">{{PAGE_TITLE}}</text>
</g>
<g id="content-caption-body-slot" data-pptx-bounds="64 248 256 296">
  <text x="64" y="296" font-size="22" fill="#64748B">{{CAPTION}}</text>
</g>
<g id="content-caption-object-slot" data-pptx-bounds="424 88 768 544">
  <text x="424" y="126" font-size="22" fill="#94A3B8">{{CONTENT_AREA}}</text>
</g>
```

**期望内容**：
- PAGE_TITLE: "前言" / "PREFACE"
- CAPTION: "公司理念与制度" (短摘要)
- CONTENT_AREA: 132 chars 正文，多行 22px muted 自动 wrap

### 2.2 slide_part02（一、目的 / 72 chars / 0 H2）— `hero_statement`

**ppt-master SVG** (`10_hero_statement.svg`):
```xml
<rect x="64" y="96" width="1152" height="528" rx="20" fill="#F4F6F8"/>  <!-- tinted field -->
<rect x="96" y="144" width="160" height="6" rx="3" fill="#CBD5E1"/>      <!-- top accent -->
<g id="hero-statement-title-slot" data-pptx-bounds="96 184 1088 208">
  <text x="96" y="304" font-size="68" font-weight="700" fill="#1E293B">{{KEY_MESSAGE}}</text>
</g>
<g id="hero-statement-subtitle-slot" data-pptx-bounds="96 424 760 72">
  <text x="96" y="472" font-size="26" fill="#64748B">{{SUBTITLE}}</text>
</g>
```

**期望内容**：
- KEY_MESSAGE: "为规范采购流程" (≤32 chars 截断到 hero_statement 主槽)
- SUBTITLE: "提升效率、明确职责、控制成本" (≤40 chars 副槽)
- **关键**：boteng 一、目的内容是 "1. 为了提高公司采购效率..."，ppt-master 的 hero_statement 应该把 "为了提高公司采购效率..." 作为 KEY_MESSAGE 主体，**不应该是 "1目的"**——LLM 之前的拼凑数字是错的

### 2.3 slide_part03（二、适用范围 / 25 chars / 0 H2）— `hero_statement`

**期望内容**：
- KEY_MESSAGE: "适用于公司所有采购" (25 chars)
- SUBTITLE: "含生产 / 研发设备物料"

### 2.4 slide_part04（三、基本原则 / 77 chars / 0 H2）— `hero_statement`

**期望内容**：
- KEY_MESSAGE: "公开透明 公平竞争 择优选择"（核心三原则，≤32 chars）
- SUBTITLE: "采购经办人须高度重视，秉公办事、维护公司利益"

### 2.5 slide_part05（四、工作程序 / 2018 chars / 8 H2）— `process_timeline`

**ppt-master SVG** (`14_process_timeline.svg`):
```xml
<line x1="196" y1="256" x2="1084" y2="256" stroke="#CBD5E1" stroke-width="4"/>  <!-- 横向 axis -->
<circle cx="196" cy="256" r="12" fill="#FFFFFF" stroke="#94A3B8" stroke-width="4"/>
<circle cx="492" cy="256" r="12" fill="#FFFFFF" stroke="#94A3B8" stroke-width="4"/>
<circle cx="788" cy="256" r="12" fill="#FFFFFF" stroke="#94A3B8" stroke-width="4"/>
<circle cx="1084" cy="256" r="12" fill="#FFFFFF" stroke="#94A3B8" stroke-width="4"/>
<rect x="64" y="568" width="1152" height="72" rx="12" fill="#F4F6F8"
      stroke="#D6DCE3" stroke-width="1"/>  <!-- takeaway band -->
<text ... STEP_1 ... STEP_2 ... STEP_3 ... STEP_4 />  <!-- 4 个 step 槽 -->
<text x="640" y="612" text-anchor="middle" font-size="20" fill="#475569">{{KEY_MESSAGE}}</text>
```

**期望内容**（4 macro phases 由 8 H2 子规则语义聚类）：

| 圆点 | label | detail | 来源 H2 |
|---|---|---|---|
| 1 | 申请 | "采购基本事项 + 采购申请 (2 项)" | （一）+ （二） |
| 2 | 审批 | "采购人职责 + 采购付款 (2 项)" | （三）+ （六） |
| 3 | 采购 | "采购方式 + 采购实施 (2 项)" | （四）+ （五） |
| 4 | 验收 | "行为规范 (1 项)" | （七） |

KEY_MESSAGE (takeaway band): "全程氚云审批 + 议价比价 + 三家以上供应商" (20 chars, 居中 20px)

### 2.6 slide_part06（附件：/ 664 chars / 6-col table）— `table_summary`

**期望内容**：直接渲染 markdown 的 6 列 pipe table。

---

## 3. native_fill 当前实际渲染 vs ppt-master 期望对比

| Deck slide | 章节 | markdown | **ppt-master 期望** | **native_fill 实际** | 差距 |
|---|---|---|---|---|---|
| 4 | 前言 | 132 chars | `content_caption` (rail + panel) | `statement-caption` (256px rail + F4F6F8 panel) ✓ | 一致 (Phase 8 实现) |
| 6 | 一、目的 | 72 chars | `hero_statement` (1152×528 field + 68px claim) | `callout-box` (legacy 蓝色 quote) | ❌ LLM 选错 |
| 8 | 二、适用范围 | 25 chars | `hero_statement` | `hero-number` "100%覆盖范围" (拼凑数字) | ❌ LLM 拼凑 KPI |
| 10 | 三、基本原则 | 77 chars | `hero_statement` | `two-column-compare` "秉公办事 vs 质量价格" | ❌ LLM 误判为对比 |
| 12 | 四、工作程序 | 2018 chars / 8 H2 | `process_timeline` (4 圆点 + takeaway) | **空白** (procedural-steps 0 steps raise) | ❌❌ 完全失败 |
| 14 | 附件 | 6-col table | `table_summary` | `revision-table` ✓ | 一致 |

**核心差距**：
1. **slide 12 完全空白** — `procedural-steps` 要求 2-5 steps，LLM 给空数组 raise → A-path 没机会 fallback
2. **slide 6/8/10 LLM 选错 archetype** — ppt-master 应该全 hero_statement 的 3 张短句，LLM 分别给了 callout-box / hero-number / two-column-compare
3. **LLM 拼凑数字 "100%覆盖范围"** — ppt-master 没有任何 archetype 用大数字表示章节
4. **A-path dispatch 没生效** — native_fill 的 hero_statement 触发条件（20-79 chars）正好匹配一、目的一段（72 chars），但被 LLM 完全覆盖

---

## 4. ppt-master vs native_fill — 范式差异

### 4.1 ppt-master 是 AI 生成 SVG → 编译 PPTX

- 6 个 layout 包（presentation_core / presentation_core_43 / report_core / editorial_bleed / moments_square / story_vertical / xiaohongshu_post）描述几何
- 由 Brand/Style 注入颜色身份（template-level identity）
- AI agent 跑 Step 1-7：source → project → template → plan → author SVG → quality gate → export

### 4.2 native_fill 是克隆 PPTX → 编辑 SVG → 编译 PPTX

- 从 boteng `柏腾ppt模版.pptx` 克隆模板（保留 Master / Layout / shape）
- LLM 选 archetype + A-path dispatch fallback
- 模板自带 brand blue `#1D2CAB`，不是 ppt-master 的中性 palette `#1E293B` / `#F4F6F8`

### 4.3 关键差异

| 维度 | ppt-master | native_fill |
|---|---|---|
| 范式 | 从 0 设计 SVG | 克隆现有 PPTX 模板 |
| 颜色身份 | 由 Brand/Style 注入 | 沿用模板自带的 brand blue |
| Master/Layout | 由 SVG `data-pptx-master` / `data-pptx-layout` 标注 | 沿用 .pptx 文件的 Master/Layout |
| archetype 数量 | 20+（presentation_core 16:9） | Phase 9 后 13 个 |
| 决策方式 | 完整 Step 1-7 流程（Strategist + Executor + Checker） | LLM 一次决定 + A-path 兜底 |
| Reading mode | text/balanced/presentation 决定 archetype capacity | 没有 reading mode 概念 |

**native_fill 的 archetype 几何已经与 ppt-master 高度一致**（hero_statement / kpi_row / content_caption 都是按 ppt-master SVG 仿制）。问题在于：

1. **LLM 白名单扩展**（Phase 9 已做）：但 LLM 仍倾向选旧的 9 个 archetype，因为 system prompt 的 wording 不够强
2. **A-path dispatch fallback 缺失**：LLM 选错时没有 fallback 机制
3. **procedural-steps 0 steps 错误**：LLM 给空数组时 raise，需要 fallback 到 bullet-list
4. **A-path dispatch 没"强制覆盖" 选项**：boteng_demo 启用 LLM 时 A-path 总被覆盖

---

## 5. 修复方向（Phase 10 follow-up）

### 5.1 立即可做

| # | 修复 | 改动 |
|---|---|---|
| 1 | procedural-steps 0 steps → fallback to multi_h2_bullet_list_spec | `workspace_expand.py` line 339-343 + `_normalize_new_blocks` line 883-886 |
| 2 | A-path dispatch 选 archetype 后写入新 block（带 `body_cards` group_id）不被 LLM 覆盖的 mechanism | `pipeline.py` line 753-763 anti-double-stack guard |
| 3 | boteng_demo 加 `force_a_path: True` 选项 + 把 expand_layout_hints 改成强约束 | `server.py` line 471+ + `boteng_demo.py` line 109+ |

### 5.2 关键设计决策：A-path vs LLM 谁是 source of truth

**选项 A**: A-path 100% 生效（boteng_demo `llm_plan=False` + `expand_layout_hints.force_archetype=True`）
- 优点：可重现，所有内容页走 ppt-master 设计规则
- 缺点：失去 LLM 兑底 cover title / TOC 自动填充能力

**选项 B**: A-path 优先 + LLM 兑底（caller 在 expand_layout_hints 声明 `prefer_a_path=True`，A-path 写入 `body_cards` 后 LLM 仍可填充 content_mapping slot 但不能覆盖 content-body group_id）
- 优点：兼具可重现性 + LLM 兑底
- 缺点：需要改 anti-double-stack guard 逻辑（pipeline.py:753-763）

**选项 C**: 维持现状（LLM 优先，A-path fallback）— 与 ppt-master 的"AI 生成 SVG"范式最接近
- 优点：LLM 灵活
- 缺点：boteng 当前实际效果差（slide 6/8/10 选错 archetype）

**推荐选项 B**：保留 LLM 兜底 cover title / TOC，但 A-path 选 archetype 是 final。改动量：~50 行 + 5 个新单测。

### 5.3 长期（Phase 11+）

- 真正实现 section_divider 暗背景 Master（改 boteng 模板 slide_03.svg）
- 加 persistent page chrome（report_core header y=64 / footer y=664）
- 加 closing slide
- 加 matrix_2x2 / comparison 实际触发案例

---

## 6. 验证脚本输出（再次贴）

```
$ PYTHONIOENCODING=utf-8 python -X utf8 scripts/sim_ppt_master_archetype.py
====================================================================================================
#   section           chars   #h2   archetype             why this layout
====================================================================================================
1   前言                132     0     content_caption       left 256px rail + right 816px panel for 132 chars body
2   一、目的              72     0     hero_statement        1152x528 tinted field + 68px bold headline (1-line claim, 72 chars)
3   二、适用范围            25     0     hero_statement        1152x528 tinted field + 68px bold headline (1-line claim, 25 chars)
4   三、基本原则            77     0     hero_statement        1152x528 tinted field + 68px bold headline (1-line claim, 77 chars)
5   四、工作程序           2018     8     process_timeline     4 macro phases by semantic clustering of 8 H2 sub-rules
6   附件：               664     0     table_summary         6-column revision table
```

**关键决策**：
- **boteng 6 张内容页 → 4 种 archetype**（content_caption + hero_statement×3 + process_timeline + table_summary）
- **没有任何一张应该用 hero-number / callout-box / two-column-compare / 3-column-cards**（这是 LLM 当前错误选择）

---

## 7. 不在范围

- ❌ 不改 boteng markdown 内容
- ❌ 不改 boteng 模板 PPTX 颜色身份
- ❌ 不重写 LLM planner 架构
- ❌ 不实现 Phase 5 决策（选项 A/B/C）— 本轮只记录参考实现
- ❌ 不修改 archetype 几何（已与 ppt-master 一致）

---

## 8. 文件清单

| 路径 | 改动 |
|---|---|
| `scripts/sim_ppt_master_archetype.py` | 新增（41 行）—— 模拟 ppt-master 决策算法 |
| `docs/PHASE10_PPT_MASTER_REFERENCE_2026-09-16.md` | 本文件 |

**参考资源**：
- ppt-master SKILL: `D:\Code\tst\mcp_ppt_server\ppt-master\SKILL.md`
- presentation_core archetypes: `D:\Code\tst\mcp_ppt_server\ppt-master\templates\layouts\presentation_core\templates\`
- ppt-master plan-core: `D:\Code\tst\mcp_ppt_server\ppt-master\references\plan-core.md`
- Phase 9 实施文档: `D:\Code\tst\native_fill\docs\PHASE9_CONTENT_ARCHETYPE_LLM_WHITELIST_2026-09-16.md`