# Content Page Density Plan — Phase 19 continuation (2026-09-20)

> **Author**: 接 Phase 19 plan (`docs/CONTENT_LAYOUT_PLAN_2026-09-20.md`) P0-A 之后
> **Goal**: 让 8 张 content slide 每张都有**实质内容**(不再是"几乎空白"),
> 根因 ① (archetype 错配) 修到底
> **Status**: P0-B 半成品落地,**未跑通验证**

---

## 1. 背景

Phase 19 P0-A 已落地 (commit `5963101`),boteng_demo 现在跑出 18 张 slide 而非 20 张
(filename H1 + cover-page 段被 strip,Part 02 / 03 内容对齐到真实 section body)。

但用户反馈"内容页基本上都是空白内容",对照 `validation/readback.md`:

| Slide | section | 实际渲染 | body 量 | 评级 |
|---|---|---|---|---|
| 5 (Part 01) | 一、工作手册的原则 | 1 句话(显示 2 次) | 1 段 | **稀疏** |
| 7 (Part 02) | 工作手册适用范围 | 1 句"适用于所有商务相关人员" | 1 句 | **稀疏** |
| 9 (Part 03) | 三、工作手册规范性文件 | 表格 4 行 | OK | ✓ |
| **11 (Part 04)** | **四、职能部门权责** | **完全空白(只有 title + footer + 页码)** | **0 句** | **空白** |
| 13 (Part 05) | 部门组织架构 | 3 条(都截断) | 3 条 | 稀疏 |
| 15 (Part 06) | 二、KTR产品订货流程 | 5 步流程 | OK | ✓ |
| 17 (Part 07) | 二、KTR订货流程 | 12 条 bullet | OK | ✓ |

3 张 OK,3 张稀疏,1 张完全空白。

---

## 2. 根因(沿用 Phase 19 plan §3 编号,继续)

### 根因 ①-b — `_EN_LABELS` 写死轮替

**症状**:7 张 content slide 的 page_chrome topbar 显示
"PREFACE / OBJECTIVE / SCOPE / PRINCIPLE / WORKFLOW / ATTACHMENT / REFERENCE"
按 part index 循环,跟 section 语义完全脱钩。

**根因**:`src/mcp_ppt_native_fill/workspace_expand.py:1036` 的
`_EN_LABELS = {1: "PREFACE", 2: "OBJECTIVE", 3: "SCOPE", 4: "PRINCIPLE",
5: "WORKFLOW", 6: "ATTACHMENT", 7: "REFERENCE"}` 是**纯字面量字典**,
`pipeline._derive_default_chrome_plan:1198` 调用
`en_label = _EN_LABELS.get(idx, "CHAPTER")` 直接按 idx 取。

**对应诊断**(已部分实施):
- 新增 `_SECTION_INTENT_PATTERNS` 7 个 regex 检测 H1 标题关键词
- 新增 `_INTENT_LABELS` intent → (topbar, body_cards) 映射
- 新增 `detect_section_intent(title) -> str` 纯函数
- 新增 `intent_label_pair(title) -> tuple[str, str]` 便利包装
- 新增 `_LegacyEnLabels` 兼容旧 import 的 dict-like shim
- `pipeline.py` 顶部 import 了 `detect_section_intent, intent_label_pair`
- `_derive_default_chrome_plan` **尚未切换**到新函数

### 根因 ①-c — Part 01 同句重复渲染

**症状**:`slide_part01_content.svg` 里 "为了更好的对公司商务行为进行管理..."
出现 2 次。

**根因(推测)**:A-path fallback (`_fallback_synthesize_three_cards_from_markdown` 等)
+ LLM 真实输出 (`body_cards` + `content-body` 两组都填了同样的 body),
或 `_render_new_block` 调用了 2 次。

**待定位**:`slide_part01_content.svg` 内两组 `body_cards` / `content-body` group
哪个是哪个、为何都填了同一段 text。

### 根因 ①-d — Part 04 完全空白(空 body fallback 缺失)

**症状**:MD 里 `# 四、职能部门权责` 后**没有任何正文**直接接下一个 H1
`# 部门组织架构`,所以 section[3] 的 body 长度 = 0。pipeline 没有"空 body
fallback"逻辑,渲染时 body_cards group 直接空着。

**根因**:`src/mcp_ppt_native_fill/block_renderer.py` 的 `cards_from_body`
对空 body 返回 `[]`,后续 `_render_new_block` 找不到 cards 走 raw 路径,
raw 路径下 body 完全空。

---

## 3. 实施计划

### 3.1 P0-B-1 完成 `_derive_default_chrome_plan` 切换

**单文件**:`src/mcp_ppt_native_fill/pipeline.py`

**改动**:
- 在 `phase2c_realize_plan` 阶段写入 `page_plan.json` 时,把每个 part 的
  `section_title` 一并存到 `page_plan.json` 的 metadata 字段
  (如 `pages[].section_title` 来自 `_split_markdown_sections` 的 title)
- `_derive_default_chrome_plan:1190` 循环里,读出对应 part 的
  `section_title`,调 `intent_label_pair(section_title)` 替代
  `_EN_LABELS.get(idx)`
- 兼容老 `page_plan.json`(无 `section_title` 字段)→ 退化到
  `_EN_LABELS.get(idx)` 旧行为

**单测** `tests/test_native_fill.py::TestSectionIntentMapping` (5 个):
- `test_intent_principles_matches_原则` → ("PRINCIPLES", "PRINCIPLE")
- `test_intent_scope_matches_范围` → ("APPLICATION SCOPE", "SCOPE")
- `test_intent_specification_matches_规范性文件` → ("SPECIFICATIONS", "SPECIFICATION")
- `test_intent_authority_matches_权责` → ("AUTHORITY", "AUTHORITY")
- `test_intent_procedure_matches_订货流程` → ("PROCEDURE", "PROCEDURE")
- `test_intent_unknown_falls_back_to_default` → ("CHAPTER", "CHAPTER")

### 3.2 P0-B-2 修 Part 01 同句重复

**单文件**:`src/mcp_ppt_native_fill/pipeline.py` (phase3 + phase4)

**思路**:
- 检查 `slide_part01_content.svg` 里两个 `<text>` 都在哪个 group 下
- 如果一个在 `body_cards` 一个在 `content-body`,决定只保留一个
- 如果都在 `body_cards`,找到重复来源(text edit apply 了 2 次)
- 加 dedup 步骤:对每个 `<g id="body_cards">` 内的 `<text>` 内容去重

**单测** `tests/test_native_fill.py::TestPart01NoDuplicateBody`:
- 跑一次 expand,从 page_plan 验证 body_cards 内 text 不重复

### 3.3 P0-B-3 修 Part 04 空 body fallback

**单文件**:`src/mcp_ppt_native_fill/block_renderer.py` 或 `pipeline.py`

**思路**:
- `cards_from_body` 对空 body 返回 `[]` 改为返回
  `[{"title": section_title, "body": "（暂无详细说明）", "color": "..."}]` placeholder
- 或在 `_render_new_block` 加:cards 为空且 layout 是 `simple-text` 时
  自动降级为 "**{section_title}**" 单一 body
- 改完后 Part 04 会显示 "四、职能部门权责" 作为标题 + "（暂无详细说明）" 作为 body

**单测** `tests/test_native_fill.py::TestEmptyBodyFallback`:
- 传入空 body section,验证 cards_for_section 返回非空 placeholder
- 验证 placeholder 文字含 section title

### 3.4 P0-B-4 验证

跑 `python examples/boteng_demo.py`,期望:
- 8 张 content slide 全部有 body
- topbar 标签跟 section 语义对齐 (e.g. Part 02 = "APPLICATION SCOPE", Part 03 = "SPECIFICATIONS")
- Part 01 没有重复 body
- Part 04 不再空白(显示 section title + placeholder)

---

## 4. 实施顺序(单 commit per fix)

```
P0-B-1   refactor(chrome-plan): use intent_label_pair(section_title)  [1 commit]
P0-B-2   fix(content-block): dedup body_cards text                      [1 commit]
P0-B-3   feat(content-block): empty body fallback to section title     [1 commit]
P0-B-4   test(chrome): assert intent_label_pair mapping for 7 intents [1 commit]
```

每 commit 后跑 `python examples/boteng_demo.py` 验证 baseline:
- `ok: true`
- 18 slides
- 0 errors
- topbar 标签 = section 语义对应的 label

---

## 5. 验证清单(全部修完后)

- [ ] `python examples/boteng_demo.py` ok=True
- [ ] 18 slides (P0-A 已减 2)
- [ ] **Part 01 content** 显示完整 1 段 + 无重复 text
- [ ] **Part 02 content** topbar = "APPLICATION SCOPE" + 显示 scope 实际 body
- [ ] **Part 03 content** topbar = "SPECIFICATIONS" + 显示规范文件表格
- [ ] **Part 04 content** 不再空白(显示 title + placeholder)
- [ ] **Part 05 content** topbar = "AUTHORITY" + 显示 3 条职责
- [ ] **Part 06 content** topbar = "PROCEDURE" + 显示 5 步流程
- [ ] **Part 07 content** topbar = "PROCEDURE" + 显示 12 条 bullet
- [ ] 8 张 content slide 都有 visible body(不是空白)
- [ ] `pytest tests/test_native_fill.py` 新增 4 个 test class 全过

---

## 6. 不在本计划范围

- **MD 文件本身修复**(选项 D — 加缺失的"二、"前缀)— Phase 19 plan §7 已排除
- **TOC 排序(选项 A/B/C/D/E)** — 用户单独决定中,本计划不覆盖
- **P1-A 副标题自动 fit** — Phase 19 plan §4.3
- **P2 模板 chrome 页码动态化** — Phase 19 plan §4.5
- **vendor 改动** — 沿用 PHASE18_POSTMORTEM 教训

---

## 7. 相关文档

- `docs/CONTENT_LAYOUT_PLAN_2026-09-20.md` — 上游 Phase 19 整体 plan
- `docs/CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md` — R1-R6 6 类根因
- `docs/PHASE18_POSTMORTEM_2026-09-20.md` — "一次只改一个文件" 教训
- `src/mcp_ppt_native_fill/workspace_expand.py:1034+` — section_intent 检测代码
- `src/mcp_ppt_native_fill/pipeline.py:1115+` — `_derive_default_chrome_plan` 待切换点

---

**作者**:Claude (Phase 19 plan 续)
**日期**:2026-09-20
**目标 commit 数**:4 (P0-B-1 切换 + P0-B-2 dedup + P0-B-3 fallback + P0-B-4 单测)
**目标验证**:boteng_demo ok=True, 8 张 content 全部有 body, 0 空白, 0 重复
