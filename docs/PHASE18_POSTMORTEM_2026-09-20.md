# Phase 18 复盘 (2026-09-20)

## TL;DR

Phase 18 试图在 Tier 2 美化(3-col-cards 填色主次、revision-table 列对齐、Layer-2 路由)+ cover title backfill bug 修复上一并落地,但同时改了 6 个源文件、跨 archetype_meta / block_renderer / llm_planner / workspace_expand / entry / tests,**生成的内容页反而比 Phase 16 baseline 更稀疏**(slide 15/16/17 几乎空白、标题出现 3 次、TOC 每条重复 2 次)。

**结论**:全部回退到 `f766be4` (Phase 17)。本轮 0 commit、0 push。

---

## 1. Phase 18 实际做了什么(已撤销)

源文件改动(全部 `git restore`):

| 文件 | 改动 |
|---|---|
| `src/mcp_ppt_native_fill/llm_planner.py` | cover title backfill 3 个 bug 修复(`_doc_title_from_markdown` 接受 bold-only、rail-label 阈值收紧、`_pick_cover_title_shape` 按 font-size 排序) |
| `src/mcp_ppt_native_fill/archetype_meta.py` | Tier 2 body_bounds 调整(13 个 archetype 对齐 ppt-master 几何)+ 新增 `TYPOGRAPHY_RATIOS` dict |
| `src/mcp_ppt_native_fill/block_renderer.py` | 3-col-cards 主卡/次卡填色分化 + 卡片高度 140→472 + revision-table 4 列等宽重排 |
| `src/mcp_ppt_native_fill/workspace_expand.py` | 接入 `source_slide_hint` → `page_plan.source_slide` 路由 |
| `src/mcp_ppt_native_fill/pipeline.py` | `_inject_content_chrome` 接收 archetype 直接判定 chrome |
| `src/mcp_ppt_native_fill/entry.py` | (细节回忆不全,revert 时一并未保留) |
| `tests/test_native_fill.py` | 3 个新测试(3-col-cards 主次填色 / revision-table 对齐 / source_slide_hint 路由) |

产物文件(已 `rm -rf`):

- `workspace/phase18_boteng.pptx`(15 张,v1)
- `workspace/phase18_boteng_v2.pptx`(22 张,v2,**更差**)
- `workspace/phase18_archetype_demo.pptx`(5 张,几乎无法目测对比)
- 各自对应的 `_ws/` workspace 子目录

---

## 2. Phase 18 v2 失败的具体症状

对比 baseline (`3山西柏腾科技有限公司采购制度_20260918_180117.pptx`,21 张,Phase 16 输出) 与 v2 (22 张,Phase 18 输出):

| 症状 | baseline | Phase 18 v2 |
|---|---|---|
| TOC 12 条目 | 唯一 | **每条重复 2 次** |
| Slide 4 (前言) | 标题 + 8 个元素 + 段落 | **只有标题 + 页脚** |
| Slide 6 (二、基本原则) | 5 大原则 + 描述 | 只有标题 |
| Slide 8 (三、基本原则) | "秉公办事、维护公司利益" hero quote + 来源 | 只有标题 |
| Slide 11/12/13 (工作程序 3×3 网格) | 3 张密集 3×3 网格页 | **3 张近乎空白页** |
| Slide 12 (一、目的) | 标题 + 5 KPI 网格 | **标题 3 次 + 1 段** |
| Slide 18 (经办人规范) | 2-col comparison | 单段 + 标题 |
| Slide 19 (修订记录) | 4 行数据表 | (缺失) |

---

## 3. 失败根因(postmortem)

### 3.1 主因:章节被过度切片

对比 `workspace/archetype_demo_auto/page_plan.json` (Phase 17,8 个内容页**全部 source_slide=4**) 与 `workspace/phase18_boteng_v2_ws/page_plan.json` (Phase 18 v2):

```
slide_part04_content.svg      (source=6)
slide_part04b_content.svg     (source=6)
slide_part04c_content.svg     (source=6)
slide_part04d_content.svg     (source=8)  ← 不同模板,chrome 不一致
slide_part04e_content.svg     (source=7)  ← 不同模板
```

Phase 17 commit message 明确说 `section_dispatcher.py` "Not wired into workspace_expand yet; ready for a follow-up"。但我在 `workspace_expand.py` 里**接入了** dispatcher,沿用了默认 `_MAX_PER_PAGE` 阈值,导致:

- 单个章节(如 "工作程序")被拆成 5 张子页
- 每张子页只有 1-2 个 sub-item → **稀疏**
- 5 张子页用 3 种不同 `source_slide`(6/7/8)→ chrome 模板碎片化

### 3.2 次因:TOC 与 cover title 修复互相串扰

`_doc_title_from_markdown()` 加了 bold-only 行扫描后,TOC 模板的 `<text>` 解析命中了重复条目,导致每个目录项出现 2 次。

### 3.3 次因:三联标题注入

`_pick_cover_title_shape()` 按 font-size 排序选 shape 后,content 页也走了类似的 shape 选择逻辑,但 content 页的 hero / section title / page-title 三个 shape 没有按位置去重 → 同一字符串渲染 3 次。

### 3.4 流程根因

1. **同时改 6 个文件**,每个单独看都合理,组合起来互相影响无法分离诊断
2. **没有分阶段提交**,没法 bisect 找出哪一改动引入回归
3. **没有回归 baseline**,只跟 Phase 17 自己的 archetype_demo 比,但 archetype_demo 比 采购制度 简单得多
4. **workspace_expand 改动最深**,但它是影响 slide 多页结构的核心入口

---

## 4. 与原 Phase 18 plan 的关系

`humble-plotting-frog.md` (plan) 列了 7 步,影响 6 个文件,目标 5 项 Tier 2 修复 + `outline-toc` 新 archetype。**这次实施证明计划太激进**:

- Tier 2 修复(3-col-cards 主次填色、revision-table 对齐)单独看都合理,但叠加 dispatcher 接入 + Layer-2 路由 + cover title 修复就崩了
- 用户当时的反馈 "排版仍旧不是非常好" 不应解读为 "立即实施 Tier 2 全部",而应解读为 "先确保不破坏现有 baseline"

---

## 5. 新方向:验证先行 + 最小修复

收到用户 "回退到 phase17 重新开始分析" 的反馈后,已确认:

- HEAD 在 `f766be4`,无 commit 无 push
- workspace 失败产物已清
- 选择方案 = **"先验证,再最小修复"**

### 5.1 阶段 0(本文件,完成)

- 写复盘文档(本文件)
- 清失败产物(完成)
- 0 行代码改动

### 5.2 阶段 1 — 验证 Phase 17 不退化(下一步)

用 `examples/boteng_demo.py` 在当前 Phase 17 代码上重新生成 `3山西柏腾科技有限公司采购制度`,对照 baseline:

| 检查项 | baseline | Phase 17 期望 |
|---|---|---|
| 总页数 | 21 | 20-21 |
| TOC 唯一条目 | 12 | ≥10 |
| Slide 4 (前言) | ≥6 元素 | ≥6 元素 |
| Slide 11-13 (工作程序) | 密集 3 张 | 密集 3 张(可能 archetype 略变) |
| Slide 19 (修订记录) | 4 行数据 | 4 行数据 |
| Cover 标题填充 | 是 | 是 |

如果任何一项退化 → 立即定位 + 最小修复(单文件单函数)。

### 5.3 阶段 2 — 选定一个具体 Phase 17 bug 修复(待定)

候选(等阶段 1 输出后再选):
- archetype_demo slide 4 的 hero_statement 同句渲染 2 次
- 单一 archetype 的特定填充问题

每修一个 1 commit,文件 ≤2。

### 5.4 阶段 3 — Tier 2 美化(再次延后)

Phase 19+ 再考虑。原 plan 的 Tier 2 项目(3-col-cards 填色、revision-table 对齐)必须**先在 Phase 17 baseline 上验证稳定**再动。

---

## 6. 教训 (给未来的我)

1. **一次只改一个文件或一个 archetype 的渲染分支**。Phase 18 的 6 文件改动不可接受。
2. **任何接入 `section_dispatcher` 的改动必须先在 archetype_demo + 真实文档(采购制度)两边回归**。
3. **不要同时修 bug + 做新功能**。cover title 修复是 bug,3-col-cards 美化是新功能,合并到一起无法诊断。
4. **永远保留 baseline 工作区**。`workspace/3山西柏腾科技有限公司采购制度_20260918_180117.pptx` 是 Phase 16 baseline 的实物证据,清理失败产物时不要动它。
5. **每个小功能单独 commit**。便于回滚与 bisect。

---

## 7. 文件清单

- 本文件:`docs/PHASE18_POSTMORTEM_2026-09-20.md`
- 上游分析(仍有效):`docs/CONTENT_LAYOUT_QUALITY_ANALYSIS_PHASE18_2026-09-20.md`
- 上游诊断:`docs/CONTENT_LAYOUT_QUALITY_DIAGNOSIS_2026-09-18.md`
- 失败 plan(已废弃):`humble-plotting-frog.md`(plan 系统内,不入 git)

---

**作者**:Claude (Phase 18 复盘)
**日期**:2026-09-20
**状态**:Phase 18 取消,等待阶段 1 验证结果