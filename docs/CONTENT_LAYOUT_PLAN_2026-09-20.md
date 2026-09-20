# Content Layout Quality Plan — Phase 19 (2026-09-20)

> **Author**: handoff 阶段的接续工作
> **Goal**: 修掉 `boteng_demo_out.pptx`(8 sections / 20 slides)里 5 类排版问题,
> 让 MCP 端到端输出**视觉正确**而非只是 zip 合法
> **Status**: 计划已落地,**未实施**

---

## 1. 背景

2026-09-20 17:23 `examples/boteng_demo.py` 跑通,**MCP 端到端 (server.py → pipeline.run_with_mapping → 5 phase vendor) 出 PPTX**:
- `output_size_bytes = 43,396,341` (41.4 MB)
- 20 slides (5 source + 8 div + 8 content + 1 TOC overflow clone)
- `validation/boteng_demo_out.delivery.json::status = "passed-with-advisories"`
- 0 errors,4 warnings(P2 picture advisory)

但**视觉层面 5 类排版错误**(见 §2),用户反馈 "目录排版等比较乱"。

下游 `5商务部工作手册2.md` 文档本身就结构异常(见 §3.1),这是触发大部分问题的源头。

---

## 2. Slide-by-slide 现状(对照表)

| # | 类型 | 渲染结果 | 错误类别 |
|---|---|---|---|
| 1 | Cover | 商务部标准化管理工作手册 / 让工业更智能… | ✓ |
| 2-3 | TOC 1/2 + overflow | 6 + 2 章节 | ✓ |
| 4 | Part 01 div | PART 01 / 一、工作手册的原则 / **"Working Manual Princi…" 截断** | ④ |
| 5 | Part 01 content | title 工作手册编制的目的 / **"PART 01 — PREFACE"** / **body 含 cover 页元素** | ① + ③ |
| 6 | Part 02 div | PART 02 / 工作手册适用范围 / Application Scope | ✓ |
| 7 | Part 02 content | title 工作手册适用范围 / **"PART 02 — OBJECTIVE"** / **"PURPOSE"** / **body = section 1 的 purpose 文本** | ① + ② |
| 8 | Part 03 div | PART 03 / **"工作手册适用范围"(应=三、工作手册规范性文件)** / Application Scope | ② + ④ |
| 9 | Part 03 content | 标题错 / "PART 03 — SCOPE" / body = section 2 的 scope 文本 / **"P04 / 10"** | ② + ⑤ |
| 10 | Part 04 div | PART 04 / **"工作手册适用规范性文…" 截断** / **"Specification Documen…"** | ④ |
| 11 | Part 04 content | "PART 04 — PRINCIPLE" / body = section 3 表格 | ① |
| 12 | Part 05 div | PART 05 / 四、职能部门权责 / Department Authority | ✓ |
| 13 | Part 05 content | **"PART 05 — WORKFLOW"** | ① |
| 14 | Part 06 div | PART 06 / 部门组织架构 / Organization | ✓ |
| 15 | Part 06 content | **"PART 06 — ATTACHMENT"** | ① |
| 16 | Part 07 div | PART 07 / 二、KTR产品订货流程 / KTR Product Ordering | ✓ |
| 17 | Part 07 content | **"PART 07 — REFERENCE"** | ① |
| 18 | Part 08 div | PART 08 / 二、KTR订货流程 / **"KTR Ordering Procedur…"** | ④ |
| 19 | Part 08 content | **"PART 08 — CHAPTER"** | ① |
| 20 | THANK YOU | 山西柏腾科技 | ✓ |

**总计 16 / 20 张 slide 有视觉问题**(cover + TOC + 1 个 ✓ divider = 4 张干净)。

---

## 3. 根因分类

### 3.1 数据源问题 — MD 文件结构异常

`5山西柏腾科技有限公司商务部工作手册2.md`(232 行,16 KB)的 H1 结构:

```
L1:   # 5山西柏腾科技有限公司商务部工作手册.doc     ← 这是文件名,不是真章节
L41:  # 一、工作手册的原则
L47:  # 工作手册适用范围                          ← 缺 "二、" 前缀
L51:  # 三、工作手册规范性文件                    ← 缺 "二、"
L59:  # 四、职能部门权责
L61:  # 部门组织架构
L128: # 二、KTR产品订货流程                       ← 又用 "二、"
L195: # 二、KTR订货流程                            ← 又用 "二、"
```

8 个 H1,4 处编号不一致,第 1 个是文件名假冒 H1。
第 1 个 H1 之前还有 cover page 元素(L3-L39):编号、标题、副标题、日期、目录列表。

### 3.2 根因 ① — LLM planner 的 page-chrome / body_cards archetype 标签错配

**症状**:8 张 content slide 的 page-chrome 和 body_cards 标签从 `PREFACE / OBJECTIVE / SCOPE / PRINCIPLE / WORKFLOW / ATTACHMENT / REFERENCE / CHAPTER` 里**按位置轮替**,**完全没看 section 语义**。

**根因文件**:
- `src/mcp_ppt_native_fill/llm_planner.py:82-340` — SYSTEM_PROMPT 只有 archetype 描述("短句 → hero_statement" 类),没有 section-intent 到 archetype 的映射规则
- `src/mcp_ppt_native_fill/block_renderer.py` — page-chrome + body_cards 的文案是 archetype 列表里随机选

**触发场景**:planner 看到 section 1 有"原则"二字就硬塞 `PREFACE`,section 2 看到"范围"就塞 `OBJECTIVE`+`PURPOSE`,section 4 看到"职责"就塞 `WORKFLOW`,section 5 看到"组织架构"就塞 `ATTACHMENT`。**完全没区分 archetype 的语义角色**。

### 3.3 根因 ② — Section 内容索引 off-by-one(Part 02 ↔ Part 03 内容互换)

**症状**:
- Part 02 content body 是 section 1 的 purpose 文本("为了更好的对公司商务行为进行管理...")
- Part 03 content body 是 section 2 的 scope 文本("本手册适用于所有商务相关人员")
- Part 04 content body 才是 section 3 的表格(对上号)

**根因文件**:
- `src/mcp_ppt_native_fill/workspace_expand.py` — `_split_markdown_sections` 切 H1 时
  - 把第 1 个 H1(filename `"5...doc"`)当成 section 0
  - 后续 7 个 H1 编为 section 1..7
- `src/mcp_ppt_native_fill/llm_planner.py` — 收到的 `section_index` 从 0 开始计数
- 但 `workspace_expand` 给 div / content 写 page_plan 时用 `nn = f"{i:02d}"` 从 1 开始
- → LLM planner 把 section[0] 的 body 灌进 part 01,section[1] 灌进 part 02,**但 cover-page 段(H1 #1 之前)也被算成 section[0] 的一部分**,导致 part 01 拿到 cover + 真的 section 1 body,part 02 拿到 section 1 body 尾部,part 03 拿到 section 2 body,以此类推 — **整体后移 1 位**

### 3.4 根因 ③ — Cover page 内容渗透到 Part 01 content

**症状**:Part 01 content slide 出现 "**编号:BT-GL-MOC-001**" / "动态管理" / "DYNAMIC" / "梳理决策" / "合作伙伴" 这些 cover 页元素。

**根因文件**:`src/mcp_ppt_native_fill/workspace_expand.py` 的 `_split_markdown_sections`。

具体:H1 #1(`5...doc`,line 1) 被当成 section[0],但 H1 #1 之前的 cover-page 段(L3-L39:**编号:BT-GL-MOC-001** / **商务部标准化管理工作手册** / 工业设备智能化服务商 / 2023年第一版 / 2023-XX-XX发布 / 目录列表)被算作 section[0] 的 body。然后 part 01 content 渲染时,LLM planner 拿到这份带 cover 元素的 body,把所有内容灌进去。

### 3.5 根因 ④ — shape-70 副标题文本框固定宽度,英文长词被截

**症状**:
- "Working Manual Principles"(25 char × 21pt ≈ 525px)→ 显示 "Working Manual Princi…"
- "Specification Documents"(23 char)→ 显示 "Specification Documen…"
- "KTR Ordering Procedure"(22 char)→ 显示 "KTR Ordering Procedur…"

**根因文件**:
- 模板 `slide_03.svg` 的 `<g id="shape-70" data-pptx-frame="76.2 469.87 479 68.2">` — 479px 宽 × 68px 高 × 21.33pt 字号
- 这一尺寸写在 vendor 模板的 `data-pptx-frame` 里,pipeline 不会改
- 英文在 21pt 下 ~21px/char,479px / 21 ≈ 22 char 截断

### 3.6 根因 ⑤ — 模板 chrome 写死

**症状**:
- 所有 content slide 页脚 "采购部 / 山西柏腾科技有限公司" — 模板 `slide_03.svg` / `slide_04.svg` 里 hardcoded,从不变化
- 所有 content slide 页码 "P04 / 10" / "P06 / 10" / "P07 / 10" — 模板 hardcode "P0X" 格式 + "/ 10" 写死总数,但实际总页数是 20

**根因文件**:
- 模板 chrome shape 在 vendor `slide_03.svg` / `slide_04.svg` 里是普通 `<text>`,不是 placeholder
- pipeline 没有 post-process 这一类 text 的 hook

---

## 4. 修法计划(按优先级)

### 4.1 P0-A 修 ② + ③ (`_split_markdown_sections` 边界修正)

**单文件**: `src/mcp_ppt_native_fill/workspace_expand.py`

**改动**:
1. `_split_markdown_sections` 检测首个 H1,如果其内容**以 `.doc` / `.md` 结尾 OR 与文件名重复**,则视为 filename-style 标题,跳过它,不计入 sections
2. 跳过 filename-style H1 之前的 `**bold**` 单行 + 目录列表 + 单行 `日期:xxxx` 等 cover-page 元素,不当作任何 section 的 body
3. sections list 的 index 跟 part index 对齐(i = 1..N,part = `slide_part{i:02d}_*`)
4. `expand_workspace_from_markdown` 传 `section_index = i` 给后续 LLM planner 调用

**新增单测** `tests/test_native_fill.py::TestSplitMarkdownSections`:
- 文件名 H1 + cover page + 7 个真实 sections → 产出 7 个 sections,index 1..7,cover 全部丢弃
- 没有 filename-style H1 → 产出 N 个 sections(N = H1 数)
- Cover 段被剥离后,section 1 的 body 不再含 "编号:xxx"

**验证**:`python examples/boteng_demo.py`,对照 baseline:
- Part 02 content body = section 2 实际 scope 文本 ✓
- Part 03 content body = section 3 表格 ✓
- Part 01 content body = section 1 实际 purpose 文本(无 cover 元素)✓

**风险**:低。`_split_markdown_sections` 是新加的 helper(Phase 16 之后才有),可以局部改,不动其他 phase。

### 4.2 P0-B 修 ① (LLM planner SYSTEM_PROMPT 加 section-intent → archetype 映射)

**单文件**: `src/mcp_ppt_native_fill/llm_planner.py`

**改动**:
在 SYSTEM_PROMPT "Content splitting (Phase 15)" 节后新增 "Section intent mapping (Phase 19)" 节,显式声明:

```
section_intent → archetype + page_chrome + body_label 映射表:

| section_intent    | keywords                       | archetype         | page_chrome       | body_label       |
| principles        | 原则 / 规范 / 准则              | principle-statement | "PRINCIPLES"      | "PRINCIPLE"      |
| scope             | 范围 / 适用 / 应用               | scope-callout       | "APPLICATION SCOPE"| "SCOPE"          |
| specification     | 规范文件 / 标准 / 文件清单         | revision-table      | "SPECIFICATIONS"  | "SPECIFICATION"  |
| authority         | 职责 / 权限 / 权责                | kpi-grid            | "AUTHORITY"       | "AUTHORITY"      |
| organization      | 组织 / 架构 / 部门结构            | org-chart           | "ORGANIZATION"    | "ORG CHART"      |
| procedure         | 流程 / 程序 / 步骤                | process-timeline    | "PROCEDURE"       | "PROCEDURE"      |
| appendix          | 附件 / 附录 / 修订                | revision-table      | "APPENDIX"        | "APPENDIX"       |
```

**实现**:
- `llm_planner.plan_content_mapping` 多返回 `section_intent` 字段(基于 H1 title 关键词 regex 匹配)
- `pipeline._derive_default_chrome_plan` 读 `section_intent` 查表,直接覆盖 LLM 选的 archetype/page_chrome/body_label
- LLM 仍然选 archetype 用于 layout 但文本标签被 deterministic 覆盖

**新增单测** `tests/test_native_fill.py::TestSectionIntentMapping`:
- "一、工作手册的原则" → intent="principles" → page_chrome="PRINCIPLES"
- "工作手册适用范围" → intent="scope" → page_chrome="APPLICATION SCOPE"
- "三、工作手册规范性文件" → intent="specification" → page_chrome="SPECIFICATIONS"
- "四、职能部门权责" → intent="authority" → page_chrome="AUTHORITY"
- 7 个真实 section 标题全部命中 ✓

**验证**:同 4.1,Part 02 content page_chrome 应该是 "APPLICATION SCOPE" / body_label "SCOPE"。

**风险**:中。改 SYSTEM_PROMPT 会影响所有 LLM 路径,但只新增一段,不动现有 archetype 选择逻辑,fallback 到 LLM 选的结果。

### 4.3 P1 修 ④ (副标题截断 — 自动 shrink 字号或扩 shape-70)

**单文件**: `src/mcp_ppt_native_fill/svg_edits.py` + `pipeline.py`

**方案 A (推荐)**:加 `expand_divider_subtitle_auto_fit: bool = True` 选项,在 `apply_text_edits` 写入 shape-70 text 后:
- 如果 text 长度 > 18 char,**自动 shrink 字号**(21pt → 18pt → 16pt → 14pt)直到 `text_width < frame_width * 0.95`
- 加 `data-pptx-carrier` 不动,只动 `font-size` 属性

**方案 B (备选)**:扩 shape-70 的 `data-pptx-frame` width 479 → 720 (但会改动原模板设计意图,可能挤掉其他 shape)

**新增单测** `tests/test_native_fill.py::TestSubtitleAutoFit`:
- "Working Manual Principles"(25 char)→ font-size 14pt,text 完整
- "Scope"(5 char)→ font-size 21pt 不变
- 18 char 临界值测试

**验证**:Part 01/04/08 divider 副标题完整显示。

**风险**:低。改的只是字号属性,不影响 layout。

### 4.4 P1 修 ④ 备选 — 改 SECTION_TITLE_EN 缩短英文

**单文件**: `examples/boteng_demo.py`

如果 4.3 自动 shrink 不想做,可直接给 SECTION_TITLE_EN 提供 ≤18 char 的英文:
```python
SECTION_TITLE_EN = {
    "一、工作手册的原则": "Per Principles",       # was "Working Manual Principles"
    "工作手册适用范围": "Application",
    "三、工作手册规范性文件": "Specifications",
    "四、职能部门权责": "Authority",
    "部门组织架构": "Organization",
    "二、KTR产品订货流程": "KTR Ordering",
    "二、KTR订货流程": "KTR Procedure",
}
```

**风险**:零。但用户可能希望看到完整英文,所以优先级低于 4.3。

### 4.5 P2 修 ⑤ — 模板 chrome footer + 页码动态化

**多文件**:
- `src/mcp_ppt_native_fill/pipeline.py` — 新增 `expand_footer_template` 选项 + `_post_process_footer` step
- `examples/boteng_demo.py` — 传 `expand_footer_template` + `expand_total_slides`

**改动**:
1. 在 phase 5 svg_to_pptx **之前**(即 phase 4 之后),扫所有 `slide_partNN_content.svg`,找 `slide-footer` group,正则替换:
   - `采购部 / 山西柏腾科技有限公司` → `expand_footer_template.format(dept=..., company=...)` 结果
   - `P04 / 10` → `P{nn} / {total_slides}` 结果
2. `total_slides` = 5 (源) + N*2 (div + content) + 1 (THANK YOU) + TOC overflow 数
3. `expand_footer_template` 默认 None → 不动;显式传 → 替换

**新增单测** `tests/test_native_fill.py::TestFooterPostProcess`:
- 8 parts + 20 slides → 所有 content slide_footer 替换为 `P0X / 20`
- 不传 expand_footer_template → 不动(回归)

**验证**:Part 01..08 content slide 页码变成 P0X / 20。

**风险**:低-中。Post-process 在 phase 4 后做,可能撞 vendor schema;需要先 dry-run 验证。

---

## 5. 实施顺序(单 commit per fix)

```
P0-A   fix(workspace-expand): skip filename-style H1 + cover-page strip         [1 commit]
P0-B   feat(llm-planner): section_intent → archetype/chrome/body mapping        [1 commit]
P1-A   feat(svg-edits): divider subtitle auto-fit by char width                 [1 commit]
P1-B   (optional) docs(boteng-demo): shorten SECTION_TITLE_EN to ≤18 char       [1 commit]
P2     feat(pipeline): dynamic footer total_slides + expand_footer_template     [1 commit]
```

每个 commit ≤ 2 文件,每个 commit 后跑 `python examples/boteng_demo.py` 验证 baseline 不退化(20 slides, ok=true, slide count 正确, 仅增加新 warning 数量为 0)。

---

## 6. 验证清单(全部修完后)

- [ ] Phase 2-5 exit=0
- [ ] `boteng_demo_out.pptx` 存在,大小 ≈ 41 MB
- [ ] 20 slides (5 源 + 8 div + 8 content + 1 THANK YOU + 1 TOC overflow = 23?) — 重新确认精确数
- [ ] **Part 02 content body = section 2 实际 scope 文本**(非 section 1 purpose)
- [ ] **Part 03 content body = section 3 表格**(非 section 2 scope)
- [ ] **Part 01 content body 不含 cover 元素**(编号、动态管理、DYNAMIC)
- [ ] **Part 04/08 content page_chrome = "SPECIFICATIONS" / "PROCEDURE"**(非 PRINCIPLE/CHAPTER)
- [ ] **副标题完整显示**(Working Manual Principles / KTR Ordering Procedure 无省略号)
- [ ] **页码 "P0X / 20"**(非 / 10)
- [ ] `tests/test_native_fill.py` 全过(新增 4 个 test class)
- [ ] `pytest tests/test_native_fill.py::TestSplitMarkdownSections` ✓
- [ ] `pytest tests/test_native_fill.py::TestSectionIntentMapping` ✓
- [ ] `pytest tests/test_native_fill.py::TestSubtitleAutoFit` ✓
- [ ] `pytest tests/test_native_fill.py::TestFooterPostProcess` ✓
- [ ] 回归:`python examples/boteng_demo.py` → ok=true, errors_count=0

---

## 7. 不在本计划范围

- **MD 文件本身的结构修复** — 这是用户的内容问题,不在 mcp-ppt-native-fill 修复范围。
  boteng_demo.py 用 `5商务部工作手册2.md` 现状跑通即可。
- **cover slide 视觉美化** — Slide 1 已经干净,不动。
- **TOC 视觉美化** — Slide 2-3 已经有内容,只是 8 章里 6+2 分配,不动。
- **THANK YOU 排版** — Slide 20 干净,不动。
- **vendor ppt-master 内部修改** — frozen copy,本计划只在我们的 `src/mcp_ppt_native_fill/` 范围动。
- **新 archetype 增加**(`outline-toc` 等) — 已在 Phase 18 取消,本计划不重启。

---

## 8. 相关文档

- `docs/PHASE18_POSTMORTEM_2026-09-20.md` — Phase 18 取消原因(同时改 6 文件,内容页反而比 baseline 稀疏)
- `docs/CONTENT_LAYOUT_QUALITY_ANALYSIS_PHASE18_2026-09-20.md` — Phase 18 实施前的 5 类问题清单(本计划是它的延续)
- `docs/CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md` — 6 类根因 R1-R6 + Tier 1/2/3 路线图
- `docs/NATIVE_FILL_PLANNER_IMPROVEMENTS.md` — planner 历史改进记录
- `.ai-memory/20260920/handoff-phase5-svg-to-pptx-animation-target.md` — 同一日的 phase 5 handoff 文档(不冲突)

---

**作者**:Claude (Phase 19 plan)
**日期**:2026-09-20
**目标 commit 数**:5 (含 P0-A 必做 + P0-B 必做 + P1-A 推荐 + P1-B 可选 + P2 推荐)
**目标验证**:boteng_demo.py ok=true, 5 类问题全部消失, 0 新增 warning
