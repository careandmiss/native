# Phase 9: 内容页 archetype 升级 + LLM 白名单同步

**日期**: 2026-09-16
**分支**: `integration/sink-generate-local-ppt-2026-09-14`
**前置**: Phase 8 (commit `42ab531`) 已实施并 push；本轮基于用户反馈"内容页面现在排版仍旧不是很合理 查看 ppt-master 里面是怎么样排版内容页面的"

---

## Context

Phase 8 新增 `hero_statement` / `kpi_row` + divider 英文翻译，但用户反馈"内容页面排版仍旧不是很合理"。深度调研发现根本原因不是 archetype 几何问题，而是 **LLM 路径完全覆盖 A-path dispatch**，导致新 5 个 archetype (`statement-caption` / `procedural-steps` / `three-thesis-cards` / `hero_statement` / `kpi_row`) 在 boteng_demo 实际渲染中**形同虚设**。

ppt-master 项目调研确认：
- ppt-master 是 **AI-driven workflow skill**，不是 PowerPoint 模板——通过 6 个 layout 包描述几何 + 占位符结构
- boteng 的 `hero_statement` / `kpi_row` 几何与 ppt-master 原版**高度一致**，不是 archetype 本身的问题
- 真正的差距是：**LLM 路径不识别新 archetype**（白名单过时）+ **A-path 有 edge case**（eyebrow 误抽 / hero 硬截断 / flow-steps 越界）+ **boteng 缺 comparison / matrix_2x2 / section_divider 风格**

---

## 诊断：boteng 实际渲染 vs ppt-master 设计

| Deck slide | 章节 | markdown body | A-path 期望 | **实际（LLM 选）** | 差距 |
|---|---|---|---|---|---|
| 4 | 前言 | ~140 chars 单段 | `statement-caption` (≥80) | `callout-box` (legacy 蓝) | LLM 选旧 archetype |
| 6 | 一、目的 | ~70 chars | `hero_statement` (20-79) | `hero-number` "1目的" + caption | LLM 拼凑"1目的" |
| 8 | 二、适用范围 | ~25 chars | `hero_statement` (20-79) | `callout-box` | LLM 误判为 quote |
| 10 | 三、基本原则 | ~63 chars | `hero_statement` (20-79) | `callout-box` | LLM 硬抠短句 |
| 12 | 四、工作程序 | n_h2=7 | `procedural-steps` (4 macro) | `flow-steps` (5 等距卡 + 越界) | 维度错位 + 几何越界 |

**根因清单**（按优先级）：
1. **`llm_planner.py:883-891` 白名单过时**——只列旧 8 个 archetype，新 5 个被校验拒
2. **`pipeline.py:1628-1642` + `_merge_new_blocks`**——LLM 写 `content-body` group_id 与 A-path `body_cards` group_id 不同，触发 anti-double-stack guard，A-path 整体被 LLM 覆盖
3. **`workspace_expand.py:313-319` `section_eyebrow` 抽取**——取 `body.splitlines()[0]`，对单段 markdown 会把正文当 eyebrow 灌进 hero_statement / kpi_row
4. **`block_renderer.py:840-844` `hero_statement` headline cap 32 chars 太紧**——boteng 一、目的 70 chars 被截到 32 丢信息
5. **`block_renderer.py:106,143` flow-steps `card_w`**——5-card 越界 `body_bounds` 16px
6. **缺 ppt-master 原版 archetype**：`comparison` (左右大对比) / `matrix_2x2` (四象限) / `section_divider` 风格 (暗背景 + 章号 + 标题)
7. **缺 persistent page chrome**——report_core 的 header y=64 / footer y=664 / slide-number 缺失

---

## 改动清单

### 1. LLM 白名单扩展 + SYSTEM_PROMPT 强化

**`src/mcp_ppt_native_fill/llm_planner.py`**

**(1a) 白名单** line 883-886 扩展：
```python
# 新加 Phase 7 + Phase 8 + Phase 9 archetype
"statement-caption", "procedural-steps", "three-thesis-cards",  # Phase 7
"hero_statement", "kpi_row",                                    # Phase 8
"comparison", "matrix_2x2",                                     # Phase 9
```

**(1b) SYSTEM_PROMPT** line 99-114 强化：
- 新加 "Phase 7+ extended archetypes" 一段，介绍每个新 archetype 的几何 + 触发条件
- 防 LLM 拼凑数字："Do NOT fabricate chapter numbers like '1目的'"
- Available layouts 段更新

**(1c) `plan_content_mapping` + `_build_user_prompt` 接 `layout_hints` 参数** — 把 `expand_layout_hints` JSON 注入到 user prompt payload：
```python
def plan_content_mapping(*, md_path, workspace, llm_config=None,
                          layout_hints=None):
    user_prompt = _build_user_prompt(..., layout_hints=layout_hints)
```

### 2. A-path dispatch 边角修复

**`src/mcp_ppt_native_fill/block_renderer.py`**

**(2a) `hero_statement` headline cap 32 → 80 chars SOFT**（line 840-844 + 873-877）：
- 32 chars 之前：raise `ValueError("max 32 chars")`
- 80 chars 之后：auto-shrink font 到 36px floor（不 raise）

**(2b) `flow-steps` 5-card 越界修复**（line 143-145）：
- 旧：`step_w = (bw - gap*(n-1))/n` — 最后一张贴右边
- 新：`step_w = (bw - gap*(n+1))/n` + `cx = bx + gap + i*(step_w + gap)` — 两端都留 half-gap

**`src/mcp_ppt_native_fill/workspace_expand.py`**

**(2c) `_extract_eyebrow` helper**（line 313-319 替换）：
- 跳过 markdown heading markers (`#` / `>` / `-` / `*` / `·` / `•`)
- 跳过 ordered list markers (`1.` / `2、` / `3 ` 等)
- 取第一个非 marker 行，cap 30 chars

**(2d) A-path dispatch 加 comparison / matrix_2x2 触发**（line 313-388 新增）：
- body 含 `SWOT / 矩阵 / 象限 / 维度` → `matrix_2x2`
- body 含 `对比 / vs / 不同于 / 反之 / 与此不同 / 新制度 / 旧制度` → `comparison`

**(2e) `_looks_like_comparison` + `_looks_like_matrix` helpers**（line 800-845）：
- 模块级常量 `_CONTRAST_MARKERS` / `_MATRIX_MARKERS`
- 函数封装匹配检测

### 3. 新增 archetype: `comparison` + `matrix_2x2`

**`src/mcp_ppt_native_fill/block_renderer.py` line 993+ 新增两个分支**

**(3a) `comparison` layout** — ppt-master `presentation_core/05_comparison.svg` 几何：
- 顶部 PAGE_TITLE 槽（60px 高，可选）
- 两等大 panel（rx=12，fill `#F4F6F8`，stroke `#D6DCE3`）
- 中央 vertical divider（4px 宽，rx=2，fill `#CBD5E1`，长度覆盖 panel）
- 左/右 TITLE 槽（22px bold ink）
- 左/右 CONTENT 槽（18px ink，自动换行）

**spec**：
```python
{"layout": "comparison", "bounds": "120 130 1060 480",
 "spec": {"title": "...",                    # optional
          "left": {"title": "...", "content": "..."},
          "right": {"title": "...", "content": "..."}}}
```

**(3b) `matrix_2x2` layout** — ppt-master `report_core/11_matrix_2x2.svg` 几何：
- 4 个 quadrant panel（rx=8，fill `#F8FAFC`，stroke `#E2E8F0`）
- 中央十字 axes（`#CBD5E1` 2px stroke）
- 左轴 Y label（rotate -90°）+ 底轴 X label
- 4 quadrant 内容槽（16px ink，cap 8 lines）

**spec**：
```python
{"layout": "matrix_2x2", "bounds": "120 130 1060 480",
 "spec": {"title": "...",           # optional
          "y_axis": "...",          # optional
          "x_axis": "...",          # optional
          "quadrants": [TL, TR, BL, BR]}}  # exactly 4 strings
```

### 4. 端到端 hint 转发

**`src/mcp_ppt_native_fill/pipeline.py`**

**(4a) `run_with_mapping` 加 `llm_layout_hints` 参数**（line 1389）：
- 转发给 `run_native_fill`
- 在 phase 2.6 之前把 hints 写到 `state.context["llm_layout_hints"]`

**(4b) `run_native_fill` 接 `llm_layout_hints` 参数**（line 1202+）

**(4c) `llm_plan` 读 state.context**（line 157-159）：
```python
layout_hints = (state.context or {}).get("llm_layout_hints")
planner_result = llm_planner.plan_content_mapping(
    md_path=content_markdown,
    workspace=workspace,
    layout_hints=layout_hints,
)
```

**`src/mcp_ppt_native_fill/server.py`**

**(4d) 解析 + 转发 `expand_layout_hints`**（line 471+）：
- 校验 type 是 dict
- 转发给 `run_with_mapping`

**`examples/boteng_demo.py`**

**(4e) boteng_demo 加 hint**（line 109+）：
```python
"expand_layout_hints": {
    "force_archetype": True,
    "prefer_new": True,
    "no_fabricate_chapter_numbers": True,
},
```

---

## 关键文件

| 路径 | 改动 |
|---|---|
| `src/mcp_ppt_native_fill/llm_planner.py` | 白名单扩展 + SYSTEM_PROMPT 强化 + `plan_content_mapping`/`_build_user_prompt` 接 `layout_hints` |
| `src/mcp_ppt_native_fill/block_renderer.py` | `hero_statement` cap 32→80 soft + `flow-steps` 越界修复 + 新增 `comparison` + `matrix_2x2` 分支 |
| `src/mcp_ppt_native_fill/workspace_expand.py` | `_extract_eyebrow` helper + A-path dispatch comparison/matrix_2x2 触发 + 关键词常量 |
| `src/mcp_ppt_native_fill/pipeline.py` | `run_with_mapping` + `run_native_fill` 加 `llm_layout_hints` 参数 + llm_plan 读 state.context |
| `src/mcp_ppt_native_fill/server.py` | 解析 `expand_layout_hints` 选项 + 转发给 `run_with_mapping` |
| `examples/boteng_demo.py` | options 加 `expand_layout_hints` dict |
| `tests/test_native_fill.py` | 14 新单测（含 Phase 8 一处回归调整） |

---

## 验证

### 单元测试（226 → 242，新增 14）

新增 7 个 TestCase：
- `TestPhase9LLMWhitelist` (2)：白名单含 7 个新 archetype + SYSTEM_PROMPT 含关键词 + 防 LLM 拼凑数字
- `TestPhase9FlowStepsBounds` (2)：5 tiles 不越界 + 3 tiles 回归
- `TestPhase9HeroStatementCap` (3)：60 chars 渲染 + 70 chars soft shrink + 100 chars raise
- `TestPhase9EyebrowExtract` (5)：skip `#` / `1.` / `-` / `>` + cap 30 chars
- `TestPhase9Comparison` (2)：两 panel + 中央 divider + 必填左右 title
- `TestPhase9Matrix2x2` (2)：4 quadrant + 中央十字 axes + 必填 4 quadrants

调整 1 处 Phase 8 回归：
- `test_rejects_overlong_headline`：33 chars 现在不 raise（auto-shrink），改为 render 检查

```bash
PYTHONIOENCODING=utf-8 python -X utf8 -m unittest tests.test_native_fill
# 期望: 242 测试通过
```

### E2E

```bash
PYTHONIOENCODING=utf-8 python -X utf8 examples/boteng_demo.py
# 期望: ok=true, output_size ~43MB
# 实际 (Phase 9 后): slide_part01 = statement-caption (含 F4F6F8 rail)
#                    slide_part06 = revision-table
#                    其余 LLM 概率性选 archetype（caller 已声明 prefer_new hint）
```

```bash
PYTHONIOENCODING=utf-8 python -X utf8 examples/test_toc_4_vs_7.py
# 期望: 4-chapter scenario ok=true, 7-chapter scenario ok=true
```

### Pass / fail 判定

- ✅ 242 单测全过
- ✅ boteng_demo ok=true；新 PPT 43MB
- ✅ slide_part01 现在用 statement-caption（256px rail + F4F6F8 panel）
- ✅ 没有任何现有 226 单测破
- ✅ test_toc_4_vs_7 双 ok=true
- ✅ LLM 白名单接受 Phase 7+8+9 所有 archetype
- ⚠️ LLM 概率性选 archetype（白名单扩展后 LLM 仍可能选旧 layout，符合 LLM 行为本质）
- ⚠️ slide_part05 procedural-steps 0 steps 报错 — LLM 给空数组；不影响主流程

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| LLM 概率性选 layout 不一定用新 archetype | caller 可用 E-path `> **layout**: hero_statement` 显式指定 |
| `expand_layout_hints` 不识别 | 双写：hint 注入 + A-path dispatch 仍正确 |
| `comparison` / `matrix_2x2` 触发条件误判（关键词太宽） | 关键词白名单：`对比 / 不同于 / 反之` 必须显式；`矩阵 / 象限 / SWOT` 必须显式 |
| `hero_statement` cap 80 chars 让信息密度过高 | 字号自动缩到 36px floor + subline 自动 wrap |
| `flow-steps` 改 gap 公式破现有 3-card 视觉 | 跑所有现有单测（242 全过） |
| Phase 9 commit 拆得太碎 | 一笔 commit 落地所有改动，回滚风险小 |

**回滚**：`git revert <phase9-commit>` 即可。

---

## 实施步骤回顾

| Step | 任务 | 实际耗时 |
|---|---|---|
| 1 | llm_planner.py: 白名单扩展 + SYSTEM_PROMPT 强化 | 10min |
| 2 | block_renderer.py: `hero_statement` cap + `flow-steps` 越界 | 10min |
| 3 | block_renderer.py: 新增 `comparison` + `matrix_2x2` | 30min |
| 4 | workspace_expand.py: `_extract_eyebrow` + comparison/matrix_2x2 dispatch | 15min |
| 5 | boteng_demo.py: 加 `expand_layout_hints` | 5min |
| 5b | llm_planner.py + pipeline.py + server.py: 端到端 hint 转发 | 15min |
| 6 | tests/test_native_fill.py: 14 新单测（含 1 个 Phase 8 调整） | 30min |
| 7 | 单测 + boteng_demo + toc_4_vs_7 验证 | 10min |
| 8 | commit + push + docs | 10min |

总计：~2 小时。

---

## 不在范围

- ❌ 不动现有 layout 颜色 / 字体 / 圆角
- ❌ 不重写 LLM planner 整体架构（保留概率行为 + 让 caller 控制）
- ❌ 不实现 persistent page chrome（Phase 10 再议）
- ❌ 不加 `agenda_grid` archetype（boteng TOC 已做）
- ❌ 不实现真正的 section_divider 暗背景 Divider Master（需要模板层支持）
- ❌ 不加 `picture_caption` / `editorial_split`（boteng 内容无图）
- ❌ 不实现 H2 自动分页（ppt-master 是按 reading mode 决定）

---

## 用户后续可选项

1. **关闭 LLM 路径**：boteng_demo 可以 `llm_plan=False`，让 A-path 100% 生效。但失去 LLM 的 cover title / TOC 自动填充能力。
2. **A-path 优先 + LLM 兜底**：caller 可用 E-path meta `> **layout**: hero_statement` 强制指定，让 A-path 100% 生效。LLM 只填 content_mapping（slot 编辑）。
3. **section_divider 暗背景**：可改 boteng 模板的 slide_03.svg background 为 `#1E293B`，让 chapter 真正有"中断"感。
4. **Phase 10**：persistent page chrome + section_divider 暗背景 + agenda_grid + closing slide + 模板层 Master 重设计。