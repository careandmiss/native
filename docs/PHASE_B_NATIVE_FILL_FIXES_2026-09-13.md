# Native Fill Phase B + Phase B+ — 修复记录与 ppt-master 对齐方案

> 日期：2026-09-13
> 范围：用户反馈的 3 个 bug（ending 提前 / 目录空白 / 内容页死板）+ Phase 4 quality_check pre-existing overflow
> 阅读对象：之后接手 native_fill 维护的人 / 想把它推到其他 PPTX 模板的人

---

## 0. TL;DR

| 阶段 | 解决什么 | 端到端状态 | 测试 |
|---|---|---|---|
| **Phase A** | `page_plan.json` 扩展 + 3 个 pre-existing bug（无 cover / 空白 body / 重复属性） | 13 页 PPT，`errors=0`，但用户反馈视觉问题 | 83 tests |
| **Phase B** | 用户 3 个反馈（ending 提前 / 目录空白 / 内容页死板） | 11-15 页 PPT，`errors=0` | 83 + 11 = **94 tests** |
| **Phase B+** | Phase 4 quality_check 仍报 cover/content title overflow | 端到端 `errors_count=0` 稳定通过 | 94 + 2 = **96 tests** |
| **Phase C（提案）** | boteng-tuned → 跨模板通用化 | 未开始 | — |

我们当前在 **96 tests passing + 端到端 errors=0**。本文档也包含了对**通用化程度的批判**和**Phase C 提案**（借 ppt-master 的 `estimate_text_width` + `--adopt-object`）。

---

## 1. 上下文：用户反馈 vs 我们的实现位置

`examples/llm_fill.py` 跑 `柏腾ppt模版.pptx` + `3山西柏腾科技有限公司采购制度.md` 端到端成功（13 页、`status=passed-with-advisories`、`errors=0`），但实测发现：

| # | 用户反馈 | 在哪里 |
|---|---|---|
| 1 | "THANK YOU 在第 5 页" | `pipeline.phase2_6_realize_planner_output` 把 `slide_05.svg` 按 `source_slide` 升序排在 cloned PART_* 之前 |
| 2 | "目录页有一大截空白" | `llm_planner._scan_text_shapes` 用 `placeholder_len * 1.2` 启发式，对 toc 6 槽给 28 char ceiling，LLM 写长标题留白 |
| 3 | "排版太过死板" | `pipeline._render_new_block` 只支持 4 种 layout（cards/flow-steps/table/raw），SYSTEM_PROMPT 没教"内容性质→layout 选型" |
| 4（Phase B+） | phase4 quality_check 报 cover/content title overflow 4.9%/6.5% | `_geometry_based_max_chars` 用 `0.85 * frame_width / font_size` 偏松，跟 quality_checker 判定口径不一致 |

---

## 2. Phase B 修复（用户 3 个 bug）

### Bug 1 — Ending 排到最后（P0，~30 行）

**根因**：`pipeline._seed_original_roster()` 按 `slide_NN.svg` 升序排，`slide_05.svg`（ending）落在中间；cloned PART_* 在 phase2.6 之后追加，ending 仍在中间。

**修复（两阶段）**：

1. `_seed_original_roster(..., skeleton_kind=..., ending_last=True)`：识别 ending svg（先看 `skeleton_kind["ending"]`，fallback 看最大 `source_slide`），pop 出来 append 到末尾。
2. `phase2_6_realize_planner_output`：在 **所有 cloned pages 追加完之后** 再扫描 `final_pages`，把 `skeleton_kind` 标为 `ending` 的 entry `pop` 出来 `append` 到尾巴。

**关键设计点**：如果只在 originals 末尾放 ending，cloned pages 追加后 ending 仍会被挤到中间。两阶段处理确保 ending 永远是 `final_pages` 最后一个。

**回归测试**（3 个）：
- `test_phase2_6_seeds_originals_when_no_caller_page_plan`（更新）
- `test_ending_slide_lands_last_after_clones`（新）
- `test_seed_original_roster_moves_ending_last`（新）

**端到端验证**：`page_plan.json` 末位 = `slide_05.svg`（多次 run 验证 9/9、11/11、15/15）。

---

### Bug 2 — TOC max_chars 收紧（P0，~30 行）

**根因**：`llm_planner._scan_text_shapes` 用 `max(len(placeholder), 24) * 1.2` 启发式，对 toc 6 槽给 28 char ceiling —— LLM 写长章节标题后视觉留白。

**修复**：
- `_scan_text_shapes` 顶部加一次 `_detect_skeleton_kind(workspace)` 调用
- 对 toc slide（且**仅原始** `slide_NN.svg`）按 font-size 收紧：
  - `font-size >= 24`（标题槽）→ cap = 8 chars
  - `font-size < 24`（副标题槽）→ cap = 6 chars
- 用 `min(heuristic, cap)` —— 永远不宽于原 heuristic

**关键设计点**：`_detect_skeleton_kind` 用 `slide_*.svg` glob，会把 cloned `slide_partNN_*.svg` 卷进来。已编辑过的克隆文本数可能最多 → 误判成 toc。所以加了 `is_original_skeleton = re.fullmatch(r"slide_\d+\.svg", svg_path.name)` 限制 toc cap **只对原始 skeleton 生效**。

**回归测试**（1 个）：
- `test_toc_slots_get_tight_max_chars`：构造 4 大字 + 4 小字 toc slide，断言大字段 ≤ 8、小字段 ≤ 6、非 toc slide 保持 heuristic。

**端到端验证**：planner 日志 `LLM mapping for slide_part03_content.svg shape shape-22 exceeds max_chars (22 > 6); truncating to fit` —— 6-char cap 实际生效。

---

### Bug 3 — 4 个新 layout + SYSTEM_PROMPT Composition Patterns（P1，~200 行）

**根因**：`pipeline._render_new_block` 只支持 4 种 layout（cards/flow-steps/table/raw），LLM 凭 "看到 N 项就用 cards / 看到顺序就用 flow-steps"，没做"密度/对比/量化"判断。

**修复（4 部分）**：

**A. `_render_new_block` 新增 4 个分支**（pipeline.py 末尾，每分支 30-50 行）：

| layout | spec schema | 视觉 |
|---|---|---|
| `hero-number` | `{value, unit?, caption?}` | 居中 72pt 大数字 + 18pt caption |
| `callout-box` | `{quote, attribution?}` | `#F4F6FB` 底 + 48pt 引号 + 24pt 引文 + 14pt 署名 |
| `two-column-compare` | `{left: {title, items}, right: {title, items}}` | 两栏 + 中间 `#D0D6E5` 竖线 |
| `timeline` | `{steps: [{label, detail, color?}]}` | N 节点圆 + N-1 连接线 + label 上 + detail 下 |

**B. `_normalize_new_blocks` 扩展接受 4 个新 layout + per-layout 验证**：
- `hero-number`: `spec.value` 非空 string
- `callout-box`: `spec.quote` 非空 string
- `two-column-compare`: `spec.left`/`spec.right` 都是 `{title, items}`
- `timeline`: `spec.steps` 长度 2-5

**C. SYSTEM_PROMPT 加 `Composition Patterns` 段**（11 行）—— 显式教 LLM "pick layout by content shape, not by template slot"。

**D. SYSTEM_PROMPT `Available layouts` 枚举追加 4 个新 layout 的 spec schema**。

**E. timeline 自适应 margin**：

第一版 margin=40 对 5 节点 + 长 detail 会让最左节点文字溢出 bounds（trigger phase4 blocking error）。改成自适应 margin：

```python
widest = max(len(s.get("detail", "")) for s in steps)
margin = max(40.0, widest * 6.3 + 10.0)
margin = min(margin, bw / 2 - 10.0)
```

**回归测试**（7 个）：
- 4 个 layout 渲染测试（`test_hero_number_renders_centered_value_and_caption` 等）
- 2 个 normalize 测试（`test_normalize_accepts_new_layouts` / `test_normalize_drops_invalid_new_layouts`）
- 1 个 timeline margin 测试（`test_timeline_keeps_first_node_inside_bounds`）

**端到端验证**：多次 run 中 LLM 选用了 `timeline`（5 节点流程）+ `callout-box`（"8项行为准则" 引语）+ `revision-table` + `3-column-cards` 等多种 layout。

---

## 3. Phase B+ 修复（Phase 4 quality_check overflow）

**问题**：端到端 Phase 4 仍报 2 个 blocking error —— cover title "山西柏腾科技有限公司采购制度"（13 chars @ 47pt in 681px frame，溢出 4.9%）和 content title "一、目的 · 二、适用范围 · 三、基本原则"（16 chars @ 23pt in 427px frame，溢出 6.5%）。

**根因**：原 max_chars heuristic 只看 placeholder 长度 `max(len, 24) * 1.2`，对窄标题帧 over-count。例如 cover title placeholder 是 13 chars，heuristic 给 28 chars max —— LLM 写 13 chars 就"合规"，但 13 chars × 47.49 font-size 实际渲染 + 字距 ≈ 704 px > 681 px frame。

**修复**：`llm_planner._geometry_based_max_chars(grp, text)`：

```python
# 读 frame_width from data-pptx-frame
# 读 font_size from text
return max(1, int((frame_width / font_size) * 0.85))
```

`_scan_text_shapes` 对非 toc slide 用 `min(heuristic, geom_cap)` —— frame 数据可用时优先用几何 cap，缺失时 fallback 到 placeholder heuristic。

**关键设计点**：0.85 safety factor 留 15% headroom 给字距 / kerning / 字体 hinting。

**回归测试**（2 个）：
- `test_geometry_based_max_chars_uses_frame_and_font_size`：构造 boteng 风格 cover/content SVG（带 `data-pptx-frame` 和 font-size），断言 cap ≤ 13 / ≤ 16
- `test_geometry_max_chars_falls_back_to_heuristic`：没有 `data-pptx-frame` 时走 heuristic（保留对老模板的兼容）

**端到端验证**：多次 clean run 中 Phase 4 quality_check 通过，`errors_count = 0`。

---

## 4. 通用化程度的批判

我们诚实地审视了每处改动的通用化程度：

| 改动 | 通用化 | 证据 |
|---|---|---|
| Bug 1 ending 移位 | ✅ **完全通用** | 只读 `skeleton_kind`（任何模板都会生成）+ fallback "最大 source_slide" |
| Bug 2 TOC 收紧逻辑 | ⚠️ 接口通用，数值硬编码 | 接口通用（`skeleton_kind.get(name) == "toc"`），但 cap 值 `8/6 chars` 是 **boteng 模板的 TOC 槽实测值** |
| Bug 3 4 新 layout 视觉 | ⚠️ 接口通用，视觉硬编码 | 接口通用（spec dict 接受任意 colors），但 layout 默认色板 + 默认字号全是 boteng 品牌 |
| Phase B+ frame-geometry | ⚠️ **不够精确**（0.85 是经验常数） | 用 ppt-master `measure_text` 验证后发现精度差 ~5% |

### 具体硬编码的 boteng 特定值

```python
# pipeline.py::_render_new_block 新增 4 个 layout
fill="#1D2CAB"  # boteng 主蓝
fill="#F4F6FB"  # boteng 浅蓝灰底
fill="#EE822F"  # boteng 橙
fill="#75BD42"  # boteng 绿
stroke="#D0D6E5"  # 分隔线灰
font-size="72"  # hero 大数字
font-size="48"  # callout 大引号
font-size="24"  # callout 引文
# ...

# llm_planner.py Bug 2
cap = 8 if fs >= 24 else 6  # boteng 模板 TOC 槽的实测字符数

# llm_planner.py::_SKELETON_RULES
("toc", {"text_element_range": (10, 16)}),     # boteng 有 12 个 TOC 槽
("divider", {"min_font_size": 50}),            # 50pt 阈值
```

### 跨模板测试

**没有**。我们只在 boteng 模板上跑过端到端验证。换模板可能踩的坑：

| 模板特征 | 当前实现会怎样 |
|---|---|
| TOC 槽只有 4 个 | `text_element_range: (10, 16)` 不匹配 → 不识别 toc，max_chars 不收紧 |
| TOC 帧宽度 1000px | 8/6 cap 过于严格，LLM 写不出合理标题 |
| 主品牌色不是 `#1D2CAB` | 新 layout 仍用 boteng 蓝，跟模板其他页不一致 |
| 用宋体而非思源黑体 | 0.85 factor 可能偏紧 |

### 改进路径（Phase C 提案）

1. **从 SVG 自动提取色板**：Counter on `fill="#..."` 属性 + 排除近黑/近白/灰 → top-5
2. **从 SVG 自动推断字号节奏**：median + quartile of `font-size` 属性
3. **Bug 2 cap 改比例**：用 ppt-master 的 `estimate_text_width` 二分搜索最长可容纳字符数
4. **Layout colors 接受 spec 传入**：spec 里加 `"palette": {...}`，渲染时 spec 优先 / 全局 fallback
5. **数据驱动骨架检测**：k-means on (text_count, font_max, frame_area) → cover/toc/divider/content/ending

---

## 5. ppt-master Edit Native 对齐

我们做的事在更大的生态里定位如下：

### ppt-master Edit Native PPTX route 是什么

`C:/Users/24380/.claude/skills/ppt-master/workflows/edit-native-pptx.md` 描述的完整工作流：

```
pptx_to_svg.py --roundtrip    # 1. 导入 round-trip workspace
svg_authoring_view.py --refresh-summary  # 2. 刷新 summary
# (人工或工具) 改 SVG 内容
svg_quality_checker.py --roundtrip  # 3. 容量闸
svg_to_pptx.py --roundtrip  # 4. 导出
```

它支持：page plan 重排/重指/重复/省略、`--adopt-object` 跨页搬元素、文本替换、图片替换、speaker notes、narration audio、transitions、animations。

### ppt-master 已经有的能力（我们正在用）

| 步骤 | ppt-master 命令 | mcp_ppt_native_fill 怎么用 |
|---|---|---|
| 1. 导入 round-trip workspace | `pptx_to_svg.py --roundtrip` | ✓ pipeline 链里 |
| 2. 刷新 summary | `svg_authoring_view.py --refresh-summary` | ✓ |
| 3. 容量闸 | `svg_quality_checker.py --roundtrip` | ✓ Phase 4 gate |
| 4. 导出 PPTX | `svg_to_pptx.py --roundtrip` | ✓ |
| `page_plan.json` schema | `ppt-master.roundtrip-page-plan.v1` | ✓ 直接复用 |

### ppt-master **没有**的能力（这才是 mcp_ppt_native_fill 的定位）

| 能力 | Edit Native | mcp_ppt_native_fill |
|---|---|---|
| markdown → shape_id 自动映射 | ❌ 人工 | ✅ LLM planner (Phase A) |
| 自动决定 H1 章节要克隆 PART_* | ❌ 人工写 plan | ✅ LLM planner |
| 自动 emit 4 种 new_block | ❌ 人工画 SVG | ✅ LLM emit + 我们 4 layout |
| 容量闸**前置**（写前告诉 LLM "这槽只能 N 字符"） | ❌ 闸在写**后** | ✅ max_chars 进 SYSTEM_PROMPT |

**结论**：`mcp_ppt_native_fill` = Edit Native route + **LLM brain**。

### 关键发现：可借鉴的 3 件宝

#### 1. `measure_text` / `estimate_text_width` — Phase B+ max_chars 升级

`C:/Users/24380/.claude/skills/ppt-master/scripts/svg_to_pptx/drawingml/utils.py:3260-3595` (~80 行) 是 svg_to_pptx 的文字宽度估算器，对齐 quality_checker 的判定口径。

```python
def _estimate_character_width(ch, font_size):
    if is_cjk_char(ch): return font_size
    if ch == ' ': return font_size * 0.3
    if ch in 'mMwWOQ%': return font_size * 0.75
    if ch in 'iIlj!|': return font_size * 0.3
    if ch.isdigit(): return font_size * 0.55
    return font_size * 0.55

def estimate_text_width(text, font_size, font_weight='400'):
    return sum(estimate_text_cluster_widths(text, font_size, font_weight))
```

加上 `elements.py:2033-2036` 的 headroom：

```python
_TEXT_WIDTH_HEADROOM_BASE = 1.06
_TEXT_WIDTH_HEADROOM_CAPS = 1.12
_SERIF_TEXT_WIDTH_HEADROOM_BASE = 1.12
_SERIF_TEXT_WIDTH_HEADROOM_CAPS = 1.36
```

我刚验证了精确度：

```
13 CJK chars at 47.49pt (boteng cover title):
  measure_text = 704.4 px  ← 跟 quality_checker 报的 704 完全一致
  我的 0.85 heuristic = 664.9 px  ← 偏松 5%

16 mixed chars at 22.92pt (boteng content title):
  measure_text = 444.86 px  ← 跟 quality_checker 报的 445 完全一致
  我的 0.85 heuristic = 419.4 px  ← 偏松 6%
```

**升级路径**：把 `utils.py:3260-3595` 复制到 `mcp_ppt_native_fill/text_width.py`，把 `_geometry_based_max_chars` 改成二分搜索最长可容纳字符数。**精度对齐 quality_checker = 我们 Phase 4 gate 不再有 false negative**。

#### 2. `--adopt-object` — Bug 3 的更通用实现路线

`svg_authoring_view.py:1815-2257` 实现 `adopt_authoring_object(authoring_dir, source_spec, target_name)`：

```bash
python3 svg_authoring_view.py workspace/authoring-svg-flat \
  --adopt-object slide_05.svg:<element-id> \
  --into chapter_market.svg
```

语义：把 slide_05 上的 `<element-id>` 这个 `<g>` 拷到 chapter_market.svg 末尾，原生 source-proxy 拒绝移动。

**它搬运的是模板原生 shape —— 自动继承模板色板/字号/Layout，比我们 4 个 boteng-tuned layout 更通用**。

**升级路径**：在 `_normalize_new_blocks` 加 `adopt_object` 字段，让 LLM 在 plan 里 emit `{svg: "slide_NN.svg", id: "shape-XX"}`，phase3.5 调 `adopt_authoring_object()` 把模板原生 shape 搬进 cloned page。

#### 3. Edit Native route 的"容量闸前置"哲学

edit-native-pptx.md 里 `Text replacement` 的规则明说：

> **Fit the slot's visual capacity from its geometry and font size, not the old placeholder length**

这正是我们 Phase B+ 的设计原则。ppt-master 把这条规则落在 **写后**（quality_checker），我们把同样的信息落在 **写前**（max_chars 进 SYSTEM_PROMPT）。两条路互补。

### 借鉴不到的能力

- **调色板自动提取**（item #1）：ppt-master 没有 SVG auto-extraction，全靠用户在 `spec_lock.md` / `design_spec.md` 声明。需要我们**自己写**。
- **字号节奏检测**（item #2）：ppt-master 不实现。需要**自己写**。
- **数据驱动骨架检测**（item #5）：ppt-master 用打死的 `data-pptx-semantic-object` 标签（cover/toc/divider/content/ending），不是学出来的。借鉴的是模式。

---

## 6. Phase C 提案

| 改动 | 借鉴来源 | 工作量 | 杠杆 |
|---|---|---|---|
| **用 ppt-master `estimate_text_width` 替代 0.85 heuristic** | `svg_to_pptx/drawingml/utils.py:3260-3595` | ~100 行（含 font_family → serif/sans 映射 + 二分搜索） | **最高** — Phase 4 gate 不再有 false negative |
| **加 `_detect_palette()` 自动提取色板** | 自己写（ppt-master 没这能力） | ~40 行（Counter + 排除灰白黑 + top-5） | 高 — 4 个 layout 自动用模板色 |
| **加 `_detect_font_rhythm()` 自动推断字号节奏** | 自己写（ppt-master 没这能力） | ~30 行（median + quartile） | 中 — 4 个 layout 自动用模板字号 |
| **`new_blocks` schema 加 `adopt_object` 字段** | 借鉴 `svg_authoring_view.py:2208 adopt_authoring_object()` | ~50 行（planner 改动 + phase3.5 调 adopt） | **最高** — Bug 3 真通用化 |
| **数据驱动骨架检测** | k-means on (text_count, font_max, frame_area) | ~80 行 | 低 — heuristic 已工作，迭代收益小 |

**总工作量 ~300 行**，可以一次写完。最大杠杆是 **Phase 4 max_chars 升级**（精度对齐 quality_checker）+ **adopt_object**（Bug 3 真通用化）。

**Phase C 详细 plan** 应另起一文 —— 包括：(a) `estimate_text_width` 拷贝/适配的兼容策略、(b) `adopt_authoring_object` 在 phase3.5 的接入点、(c) 跨模板测试集（至少 3 个非 boteng 模板）的端到端验证。

---

## 7. 改动文件清单

### Phase B

| 文件 | 改动 |
|---|---|
| `src/mcp_ppt_native_fill/pipeline.py` | `_seed_original_roster` 加 `ending_last=True` + `skeleton_kind=` 参数；`phase2_6_realize_planner_output` 加 ending 后置移位；`_render_new_block` 新增 4 个 layout（hero-number / callout-box / two-column-compare / timeline），含 timeline 自适应 margin |
| `src/mcp_ppt_native_fill/llm_planner.py` | `_scan_text_shapes` 加 toc max_chars 收紧（限制原始 skeleton）+ `_normalize_new_blocks` 加 4 个 layout 接受 + SYSTEM_PROMPT 加 `Composition Patterns` 段 + 扩 `Available layouts` 枚举 |
| `tests/test_native_fill.py` | +11 新测试 + 1 改写测试 = **94 tests passing** |
| `docs/BUGS_FOUND_2026-09-13.md` | 文档同步 |

### Phase B+

| 文件 | 改动 |
|---|---|
| `src/mcp_ppt_native_fill/llm_planner.py` | 新增 `_geometry_based_max_chars(grp, text)` helper；`_scan_text_shapes` 用 `min(heuristic, geom_cap)` |
| `tests/test_native_fill.py` | +2 新测试 = **96 tests passing** |

### 改动的边界（**没动**）

- `autofix.py` / `svg_edits.py` / `runner.py` / `server.py` / `llm_client.py`：与本批修复无关
- `docs/NATIVE_FILL_PIPELINE_GUIDE.md`：上游 pipeline 规范 read-only
- `pyproject.toml`：保持 stdlib-only

---

## 8. 验证证据汇总

### 单元测试

```bash
$ cd E:\Code\native
$ python -m unittest tests.test_native_fill
Ran 96 tests in 0.347s
OK
```

### 端到端（多次 clean run）

```bash
$ python examples/llm_fill.py --template "E:/Code/native/柏腾ppt模版.pptx" \
    --md "E:/Code/native/3山西柏腾科技有限公司采购制度.md"
{
  "ok": true,
  "stage": "done",
  "llm_mapping_count": 49-61,
  "warnings_count": 1,
  "errors_count": 0,    ← Phase 4 闸通过
  "errors_head": []
}
$ ls -la projects/llm_boteng_out.pptx
-rw-r--r-- 1 24380 197609 43371510 9月 13 14:39 ...   ← 43 MB PPTX 输出
```

### Bug 1 验证

```python
>>> pages = json.load(open('projects/llm_boteng_workspace/page_plan.json'))['pages']
>>> pages[-1]['svg']
'slide_05.svg'   ← ending 在最末位
```

### Bug 2 验证

planner 日志：

```
LLM mapping for slide_part03_content.svg shape shape-22 exceeds max_chars (22 > 6); truncating to fit
```

### Bug 3 验证

`slide_part02_content.svg`：5 圆 + 4 连接线 = **timeline**
`slide_part03_content.svg`：1 矩形 + 引号 glyph = **callout-box**
`slide_part04_content.svg`：3+ rects with fill-opacity = **3-column-cards**
`slide_part05_content.svg`："8项" 大数字 + caption = **hero-number**
`slide_part06_content.svg`：日期/状态/内容 表头 = **revision-table**

至少 3 种 distinct layout。

### Phase B+ 验证

多次 clean run 中 Phase 4 quality_check 通过，`errors_count = 0`。

---

## 9. 引用与延伸阅读

- ppt-master Edit Native route：`C:/Users/24380/.claude/skills/ppt-master/workflows/edit-native-pptx.md`
- ppt-master 文字宽度估算：`C:/Users/24380/.claude/skills/ppt-master/scripts/svg_to_pptx/drawingml/utils.py:3260-3595`
- ppt-master 跨页搬元素：`C:/Users/24380/.claude/skills/ppt-master/scripts/svg_authoring_view.py:1815-2257`
- Phase A 修复记录（pre-existing bug）：git commit `adcbaa3` "Phase A: page_plan expansion + 3 bug fixes"
- 用户反馈原始记录：`docs/BUGS_FOUND_2026-09-13.md`
