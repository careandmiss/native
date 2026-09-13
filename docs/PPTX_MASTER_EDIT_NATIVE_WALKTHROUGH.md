# ppt-master Edit Native 流程走查 — boteng 模板

> 日期：2026-09-13
> 目的：跑一遍 ppt-master 的 edit-native workflow（人工/agent 扮演 LLM 的角色），看它怎么处理 TOC 槽数 vs 章节数不匹配的情况，给 `mcp_ppt_native_fill` 的 Phase B+ 改进找参考。
> 测试模板：boteng（5 张 slide，TOC 是 3×2 = 6 槽硬布局）
> 测试 markdown：`E:\Code\native\3山西柏腾科技有限公司采购制度.md`（6 个一级章节，正好填满 TOC）

---

## 0. TL;DR

| 问题 | 答案 |
|---|---|
| ppt-master 有没有"自动 reflow TOC"功能？ | **没有** |
| ppt-master 有没有"删除空槽"功能？ | **没有** |
| `svg_quality_checker.py --roundtrip` 对 4-of-6 TOC 槽的反应？ | **静默通过**（5 PASS / 0 ERRORS / 0 WARNINGS）|
| `svg_to_pptx.py --roundtrip` 对 4-of-6 TOC 槽的反应？ | **正常导出**（取决于编辑方式）|
| 那 TOC 空白到底由谁负责？ | **编辑者（人或 LLM agent）**。ppt-master 只校文本溢出，不管槽空 |
| 我们 `mcp_ppt_native_fill` 应该怎么办？ | 选一：**(a) 告诉 LLM 写够 6 项**（仅当内容能造出来），或 **(b) 实现 dynamic-toc 重排**（最通用，但 ~100 行） |

---

## 1. ppt-master edit-native 工作流（agent 视角）

来源：`C:\Users\24380\.claude\skills\ppt-master\workflows\edit-native-pptx.md`（171 行）

| 步骤 | 工具 | 角色 |
|---|---|---|
| 1. Round-trip workspace | `pptx_to_svg.py --roundtrip` | 把源 PPTX 转成可编辑 SVG + native 备份 |
| 2. Read summary | `authoring-svg-flat/authoring_summary.json` | 看每页的 canvas / text / image / placeholder 计数 |
| 3. Plan output deck | `page_plan.json` + confirmation gate | 决定哪些 slide 编辑、哪些 passthrough、是否 clone |
| 4. Edit pages | 直接编辑 SVG `<g id="shape-XX">` 内的 `<text>`/`<tspan>` | 改文本时保留 `data-pptx-*` 属性以保持原生 identity |
| 5. Refresh summary | `svg_authoring_view.py --refresh-summary` | 重写 summary |
| 6. Capacity gate | `svg_quality_checker.py --roundtrip` | 检查文本是否溢出 frame |
| 7. Export | `svg_to_pptx.py --roundtrip` | 生成最终 PPTX |

**关键约束**（来自 §4-5）：
- "Referenced page is never opened for writing" — slide_02 (TOC) 是源 roster 中的页面，被认为是 referenced，理论上只能 patches 不能 rebuild
- 但实际上 TOC 也是必须编辑的（占位符要换），所以**实际"patches"也足以让它变成 rebuilt**（只要动了 `<text>` 内容就触发 rebuilt）
- 这条规则意味着我们不能简单地"删除未填的 TOC 槽" — 删除结构元素违反 in-place edit 原则

---

## 2. 走查 1 — boteng + boteng MD（6 章节 / 6 TOC 槽 = 全填）

### Step 1: Roundtrip

```bash
python "C:/Users/24380/.claude/skills/ppt-master/scripts/pptx_to_svg.py" \
    "E:/Code/native/柏腾ppt模版.pptx" \
    -o "test_templates/_walkthrough" \
    --inheritance-mode both --roundtrip
```

输出：
- `authoring-svg-flat/` 5 个 SVG
- `sources/source.pptx` 原始包备份
- `animations.json`、`authoring_summary.json`、`conversion-report.json`

`authoring_summary.json` 显示：
- slide_01: text_elements=6, images=2, semantic_shapes=9
- slide_02: text_elements=13, images=1, semantic_shapes=26 ← **TOC**
- slide_03: text_elements=4, images=2, semantic_shapes=5
- slide_04: text_elements=2, images=1, semantic_shapes=8
- slide_05: text_elements=5, images=2, semantic_shapes=6

### Step 2: Read TOC structure

解析 `slide_02.svg` 找到所有带 `data-pptx-frame` 的 `<g id="shape-NN">`：

| shape | y | h | 角色 |
|---|---|---|---|
| shape-57 | 89.4 | 144.2 | "CONTENTS目录"标题 |
| shape-62, 69, 70 | 282, 275, 327 | 34, 46, 31 | 行 1 左（accent/title/subtitle）|
| shape-71, 72, 73 | 282, 275, 327 | 34, 46, 31 | 行 1 右 |
| shape-76, 77, 78 | 422, 416, 468 | 34, 46, 31 | 行 2 左 |
| shape-79, 80, 81 | 422, 416, 468 | 34, 46, 31 | 行 2 右 |
| shape-85, 86, 87 | 564, 557, 609 | 34, 46, 31 | 行 3 左 |
| shape-88, 89, 90 | 564, 557, 609 | 34, 46, 31 | 行 3 右 |

**确认结构**：3×2 = 6 槽，每槽 3 个 shape（accent bar + title + subtitle）。

### Step 3: Edit TOC

写 `edit_toc.py`（~70 行 stdlib），替换 6 个 title + 6 个 subtitle 的 `<tspan>` 内容：

```python
TOC_ENTRIES = [
    ("前言", "Preface"),       ("一、目的", "Purpose"),
    ("二、适用范围", "Scope"),  ("三、基本原则", "Core Principles"),
    ("四、工作程序", "Procedure"), ("附件：", "Annex"),
]
```

通过 `re.sub(r"(<tspan[^>]*>)[^<]*(</tspan>)", ...)` 替换每个 shape 的第一个 `<tspan>` 内容，**保留** `font-size` / `fill` / `position` 属性。

### Step 4: Refresh summary

```bash
python svg_authoring_view.py authoring-svg-flat --refresh-summary
```

输出 `authoring_summary.json`（2938 字节，重写）。

### Step 5: Capacity gate

```bash
python svg_quality_checker.py . --roundtrip
```

输出：
```
[SUMMARY] Check Summary
  [OK] Fully passed: 5 (100%)
  [WARN] With warnings: 0 (0%)
  [ERROR] With errors: 0 (0%)
```

### Step 6: Export

```bash
python svg_to_pptx.py . --roundtrip -o _walkthrough/full.pptx
```

输出（节选）：
```
[Slide 1/5] slide_01.svg (Source slide passthrough)
[Slide 2/5] slide_02.svg - ... patched
[Slide 3/5] slide_03.svg (Source slide passthrough)
[Slide 4/5] slide_04.svg (Source slide passthrough)
[Slide 5/5] slide_05.svg (Source slide passthrough)
Round-trip export summary: output_pages=5 passthrough=4 patched=1 rebuilt=0
```

5 张全出。slide_02 是 "patched"（text 改了但其他属性保持 → native identity 保留）。**TOC 满填时一切都干净**。

---

## 3. 走查 2 — boteng + generic MD（4 章节 / 6 TOC 槽 = 缺 2 槽）

模拟场景：LLM 看到 generic_content.md 只有 4 个 H1 章节，按字面意义填了 4 个 TOC 槽，剩下 2 个（行 3 左右）保持占位符。

### 操作

复用同样的 edit_toc.py 思路，但只编辑前 2 行（4 个 slot），行 3 保持未填：

```python
SLOT_SHAPES = {
    (0, "L"): ("shape-62", "shape-69", "shape-70"),
    (0, "R"): ("shape-71", "shape-72", "shape-73"),
    (1, "L"): ("shape-76", "shape-77", "shape-78"),
    (1, "R"): ("shape-79", "shape-80", "shape-81"),
    # row 2 (y=557) left empty on purpose
}
```

### 关键发现

```bash
python svg_quality_checker.py . --roundtrip
```

输出：
```
[SUMMARY] Check Summary
  [OK] Fully passed: 5 (100%)
  [WARN] With warnings: 0 (0%)
  [ERROR] With errors: 0 (0%)
```

**5 PASS / 0 ERRORS / 0 WARNINGS** — `svg_quality_checker` 对空 TOC 槽**完全静默**。

具体来说，`svg_quality_checker` 关心的（来自 `svg_quality/checker.py`）：
- 文本是否溢出 frame（hard fail）
- viewBox 是否一致
- Native XML 结构是否合法
- Paint 一致性（uppercase #RRGGBB）
- foreignObject 是否滥用

**它不关心**：
- 槽是否为空
- 视觉是否平衡
- 章节数 vs 槽数匹配

### 视觉确认

用 `cairosvg` 把两种状态渲染成 PNG，用 MCP 图像识别对比：

**Full 6-of-6**（看图）：
- 3 行 × 2 列 = 6 项全部填
- 视觉对称、底部无空白
- "moderately balanced but slightly bottom-heavy with empty whitespace" — 即便全填，slide 本身的 padding 让底部仍有些空

**Partial 4-of-6**：
- 上 2 行填（前言 / 一、目的 / 二、适用范围 / 三、基本原则）
- **整个底部 1/3 是空的**（y ≈ 560-720）
- "**top-heavy**"，缺失行有 ~3-4× 正常 padding 的间隔
- 视觉上**清楚可辨**为"漏了一行"，不是"故意留白"

空白对比：4-of-6 比 6-of-6 **多约 25-30% 底部空白**。

---

## 4. ppt-master 处理 TOC 的边界（明确的"不做什么"清单）

通过走查确认，ppt-master edit-native 在 TOC 上**明确不做的**事：

| 它不做 | 证据 |
|---|---|
| ❌ 检测章节数 vs 槽数不匹配 | quality_checker 对 4-of-6 静默通过 |
| ❌ 自动删除空槽 `<g>` | svg_to_pptx 接受 unchanged 空 `<g>` 节点 |
| ❌ 自动 reflow 把 N 项均分到可用槽 | 没有 layout-adaptation 逻辑 |
| ❌ 自动把 TOC 内容迁移到 content slide | edit-native §4 反对这种行为 |
| ❌ 自动缩小字号以填充 | edit-native §5 明确说"shrinking type is last and never deck-wide" |
| ❌ 警告 LLM/agent "TOC 不完整" | 无此信号 |

**结论**：TOC 空白是**视觉问题**，**不在 ppt-master 的自动处理范围**。它把责任留给 agent / 人 / LLM —— "你决定填多少，我不管"。

---

## 5. 对 mcp_ppt_native_fill 的影响

### 当前行为

LLM 在 `llm_planner.py` 里通过 `_scan_text_shapes` + `_normalize_mapping` 收到 max_chars 限制（Bug 2 修了 toc 用 8/6 chars）。LLM 按 markdown 字面填 TOC 项。

- 当 markdown 章节数 ≤ TOC 槽数：LLM 填 N 个，剩下的空。**视觉上**有空白。
- ppt-master 不会修，quality_checker 不报警。bug 是**纯视觉**的。

### 三个修法选项

#### 选项 A：限制 markdown 章节数（不通用）

- 在 SYSTEM_PROMPT 加硬约束："TOC 有 N 槽，章节必须 ≤ N"
- 不通用：boteng 6 槽但 thu 可能 4 槽，github 1 槽
- **不建议**

#### 选项 B：dynamic-toc new_block layout（**推荐**）

- 新增 `_render_new_block("dynamic-toc", ...)` —— 接受 `{items: [{title, subtitle}, ...]}`
- phase3_author 在 TOC slide 上：删除原 6 个 `<g>` 槽 → 插入一个 `<g id="toc-dynamic">`
- LLM 决定 N 后，layout 自动计算每行行高（720 / max(1, ceil(N/2))）和字号缩放
- 工作量：~100-150 行（pipeline.py 改 phase3 + llm_planner.py 加 schema + _render_new_block 新分支）

#### 选项 C：post-process 删除空槽（最小修）

- phase3_author 后扫 slide_02.svg，找标题槽 = 空 + accent bar 仍存在 → 删除该行的 3 个 shape
- 简单但破坏源骨架（`slide_02.svg` 改成 "rebuilt" 而不是 "patched"），违反 edit-native §5
- 工作量：~30 行
- 不推荐（违反 ppt-master 哲学）

### 推荐

走 **选项 B**。但具体何时做看用户优先级 — 这个 bug 不阻塞 Phase B+ 的其他改进（estimate_text_width, palette, font rhythm）。

### 折中：C+D

如果要做最小可用版：先 **D**：在 SYSTEM_PROMPT 加警告

> "TOC slide has N pre-defined slots (3×2 = 6 by default for boteng). If your markdown has fewer sections, the empty slots will leave visible blank space — this is intentional but the visual may look unbalanced. Consider whether to add filler sections or trim the deck."

加 SYSTEM_PROMPT 后用户能看到这个限制，但仍然视觉空白 —— 不解决根本问题。

---

## 6. 关键 takeaway（给后续实现者）

1. **TOC reflow 是 ppt-master 留给 LLM/agent 的责任**，不在它的自动处理范围。我们的 `mcp_ppt_native_fill` 也应该明确这点 —— 把 dynamic-toc 当 new_block 的一种 layout，让 LLM 决定要不要用。

2. **不要试图"删除空槽"修复空白** —— 这违反 edit-native 的 in-place edit 原则（§5 hard rule）。如果走选项 C，要清楚地标注为 "rebuilt slide" 而不是"patched"，并接受相关 export 副作用。

3. **质量闸对 TOC 完整性不负责** —— `svg_quality_checker.py` 只检查 text-overflow。要查"槽是否为空"，需要新写一个校验函数（独立于 svg_quality_checker），放进我们 pipeline 的 phase4。或者直接用 image-based 检测（rendering 检查）但成本高。

4. **跨模板影响**：thu 模板的 TOC 槽数不一定是 6（需查），github_social 没 TOC，tuda_en 没 TOC 也不是 boteng 风格。dynamic-toc 方案要基于实际 slot 数动态算。

---

## 7. 文档 / 数据附录

| 路径 | 内容 |
|---|---|
| `test_templates/_walkthrough/` | 完整 6-of-6 走查的工作目录 |
| `test_templates/_walkthrough/edit_toc.py` | 填充 6 槽的脚本 |
| `test_templates/_walkthrough_4of6/` | 4-of-6 走查工作目录 |
| `test_templates/_walkthrough_4of6/edit_toc_4of6.py` | 填 4 槽脚本（行 3 留空）|
| `test_templates/slide_02_full.png` | Full TOC 渲染图 |
| `test_templates/slide_02_4of6.png` | Partial TOC 渲染图 |

走查用命令（可重放）：
```bash
# Setup
rm -rf test_templates/_walkthrough
python "C:/Users/24380/.claude/skills/ppt-master/scripts/pptx_to_svg.py" \
    "test_templates/boteng.pptx" \
    -o "test_templates/_walkthrough" \
    --inheritance-mode both --roundtrip

# Edit (full)
cd test_templates/_walkthrough && python edit_toc.py

# Gate
python "C:/Users/24380/.claude/skills/ppt-master/scripts/svg_authoring_view.py" authoring-svg-flat --refresh-summary
python "C:/Users/24380/.claude/skills/ppt-master/scripts/svg_quality_checker.py" . --roundtrip

# Export
python "C:/Users/24380/.claude/skills/ppt-master/scripts/svg_to_pptx.py" . --roundtrip -o full.pptx
```

---

## 8. 对 Phase B+ 路线的影响

PHASE_B doc 之前列了 Phase C 候选 5 项（C1 text estimator 已做，C2-C5 未做）。本走查增加了 1 个**新候选**：

**C6: dynamic-toc layout**

- 来源：用户反馈"目录页还是有一部分空白"（2026-09-13）
- 走查发现：ppt-master **不解决**这个问题，把它推给 agent/LLM
- 实现路径：在 `_render_new_block` 加 `dynamic-toc` 分支；phase3 在 TOC slide 删除原固定 `<g>` 槽、插入动态布局
- 工作量：~100-150 行
- 杠杆：中等（用户感知明显但仅影响 TOC 一处）
- 优先级：低于 C2 (palette) / C3 (font rhythm) / C4 (adopt_object)，但**优先级高于 C5 override**

**Phase B+ 候选优先级（更新）**：
1. **C1** ✅ done — text_width estimator
2. **C6** ⭐ new — dynamic-toc layout (user-reported visual bug)
3. C2 — palette auto-extraction
4. C3 — font rhythm auto-extraction
5. C4 — adopt_object (highest leverage but biggest scope, ~544 lines)
6. C5 — skeleton_kind override file (lowest)
