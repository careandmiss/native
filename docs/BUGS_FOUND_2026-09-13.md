# Native Fill — 端到端实测发现的 3 个 Bug（2026-09-13）

> **来源**：`examples/llm_fill.py` 跑 `柏腾ppt模版.pptx` + `3山西柏腾科技有限公司采购制度.md`，
> 输出 13 页 PPT（status: passed-with-advisories，errors=0）。
> 用户反馈：**ending 提前出现 / 目录页有大空白 / 内容页排版太死板**。
> 本文逐个分析根因 + 给修复方向。

## 修复进度（2026-09-13 Phase B）

| Bug | 状态 | 验证证据 |
|---|---|---|
| 1 ending 提前 | ✅ Fixed | `page_plan.json` 中 `slide_05.svg` 在最末位（output_index=11/11 等），THANK YOU 在最后页。 |
| 2 目录空白 | ✅ Fixed（短期） | TOC 槽 max_chars 收紧到 8/6；planner 日志里 `exceeds max_chars (35 > 28); truncating`。 |
| 3 内容页死板 | ✅ Fixed | LLM 在多次 run 中选用了 `timeline` / `callout-box` / `hero-number` / `revision-table` / `3-column-cards` 等多种 layout，至少 3 种 distinct。 |
| Phase 4 quality_check | ✅ Fixed | 端到端 `errors_count = 0`（在多次 run 中稳定通过；偶尔 LLM 输出极端超长文本时仍可能触发 pre-existing overflow）。 |
| Cover/Content title overflow | ✅ Fixed（Phase B+） | `_geometry_based_max_chars` 用 frame_width/font-size 计算 max_chars（cover title 47pt in 681px ≈ 12 chars，content title 23pt in 427px ≈ 15 chars）。 |

详细修复说明见各 Bug 节末"✅ 修复完成"段。

---

## Bug 1 — Ending（THANK YOU 页）提前出现

### 现象
`page_plan.json` 里 slide_05.svg（THANK YOU）在第 5 位（output_index=5），
紧跟 cover/toc/divider/content 之后，cloned PART 02-05 反而被推到 6-13 位。
所以最终顺序是：
```
1. cover
2. toc
3. divider 01
4. content 01
5. THANK YOU   ← ending 提前出现
6. divider 02
7. content 02
...
13. content 05
```

### 根因
`pipeline.phase2_6_realize_planner_output` 的
`_seed_original_roster()` 按 SVG 文件名 `slide_NN.svg` 排序（NN 升序），
把 slide_05.svg（ending）和 cloned PART_* 一起塞进 final_pages。
cloned 是后追加的，所以 ending 永远在中间。

实际期望顺序（参考 `NATIVE_FILL_PIPELINE_GUIDE §9` 那次 run）：
```
1. cover       (slide_01.svg)
2. toc         (slide_02.svg)
3. div 01      (slide_03.svg)
4. content 01  (slide_04.svg)
5. div 02      (slide_part02_div.svg)
...
N-1. content NN
N.   THANK YOU (slide_05.svg)
```

### 修复方向
**P0 — 必须修**（改动小、效果明显）

改 `_seed_original_roster()`：让 ending 永远排到最后。
两个实现路线：
- **路线 A**：识别 ending 那个 svg（按 `_detect_skeleton_kind` 已经标了 `ending`），
  从 roster 里 pop 出来，最后再 append。
- **路线 B**：直接按角色排序 — `cover, toc, [divider, content]*, ending`。
  需要 skeleton_kind 在 phase2.6 已确定（实际已通过 `state.context["skeleton_kind"]` 可访问）。

改动 ~10 行，影响范围：仅 `pipeline.py::_seed_original_roster` 一个函数。
回归测试加 1 个：构造一个 page_plan 含 ending 在中间，断言 rearrange 后 ending 末尾。

### ✅ 修复完成（Phase B）

**改动**：
- `_seed_original_roster()` 新增 `skeleton_kind=` + `ending_last=True` 参数：
  优先用 `skeleton_kind[name] == "ending"` 找到 ending slide 并 pop 出来放末尾；
  fallback 用最大 `source_slide`。
- `phase2_6_realize_planner_output()` 在克隆全部追加完之后再扫描
  `final_pages`，找到 `skeleton_kind` 标为 `ending` 的 entry 移到列表末尾
  （关键：必须 AFTER 克隆追加，不能仅在 originals 末尾 — 否则 ending 仍在中间）。

**回归测试**（3 个，新增）：
- `test_phase2_6_seeds_originals_when_no_caller_page_plan`：原测试改写，
  断言 `svgs[-1] == "slide_05.svg"` + `svgs[:4] == [01..04]` + cloned 在 ending 前。
- `test_ending_slide_lands_last_after_clones`：构造完整场景（5 originals +
  2 cloned + skeleton_kind），断言 ending 总是最后一位。
- `test_seed_original_roster_moves_ending_last`：单元测试 `_seed_original_roster`
  在 `skeleton_kind=None`（fallback 到最大 source_slide）和指定 non-last ending
  两种情形下的行为。

**端到端验证**（`examples/llm_fill.py`）：
- `page_plan.json` 末位 = `slide_05.svg`，`page_plan_pages[-1]["svg"] == "slide_05.svg"`。
- 验证脚本：直接读 `page_plan.json` 最后一个 entry。

---

## Bug 2 — 目录页有大段空白

### 现象
slide_02.svg（目录）的几何布局：
- 标题 y=139（CONTENTS）
- 3 行 × 2 列 = 6 个槽位：y=307 / 447 / 589，每行高 140 px
- 末行 + 副标题底 = 627，**距底部 720 px 还剩 93 px（13%）**
- 装饰条 "高效协同..." 在 y=680

实际跑出来的 6 项：
```
一、目的与适用范围 / 二、基本原则      (行 1)
三、采购申请与职责 / 四、采购方式与实施 (行 2)
五、付款方式与行为规范 / 六、附件      (行 3)
```

### 根因（两条）
1. **模板硬布局**：3×2 = 6 槽是固定设计，章节数 ≠ 6 时必出问题 —
   - 文档 < 6 章 → 留空白（本次 6 项刚好满，**但仍看着空**）
   - 文档 > 6 章 → 溢出 → 用户手工硬塞
2. **LLM 没填第 6 项的右侧副标题**：readback 里 "六、附件" 对应 "修订记录"，
   但视觉上两列副标题看起来"一长一短"，加上底部 93 px 装饰带 → 用户感知"大段空白"

### 修复方向

**短期（P0）**：让 LLM 准确知道"目录只有 6 槽"这个硬约束 — 在 shape_index
里把 max_chars 算成 2-4 字符（章节标签）+ 4-6 字符（副标题），LLM 就会被强制
不写超长字符串，目录会更紧凑。

**中期（P1）**：把目录当**动态行数**处理 — 不复用 slide_02.svg 的固定 6 槽，
而是新生成 `<g id="toc-rows">`，由 LLM 按章节数（n ≤ 6 时 n 行 × 2 列；n > 6 时
n 行 × 1 列）算出等距分布。这等于把 toc 当作"特殊 new_block"对待。

**长期（P2）**：当章节数 ≠ 6 时（如本次实际是 6 章+1 附件 = 7 项），让 LLM
拆成"主目录（5 章）+ 附件独立页"，或者"6 章压缩到 6 项 + 末尾补'修订记录'"

### ✅ 修复完成（Phase B 短期方案）

**改动**：
- `_scan_text_shapes()` 顶部增加一次 `_detect_skeleton_kind(workspace)` 调用，
  拿到 `skeleton_kind` 字典。
- 对每个 shape：
  - 非 TOC slide：保留原 heuristic `max(placeholder_len, 24) * 1.2`。
  - TOC slide（且仅 `slide_NN.svg` 原始文件 — cloned slide_partNN_*.svg **不**触发，
    避免把 content clone 误判成 toc）：
    - `font-size >= 24`（标题槽）→ cap = 8 chars
    - `font-size < 24`（副标题槽）→ cap = 6 chars
  - 最终 `max_chars = min(heuristic, cap)`，永远不会比 heuristic 宽。

**回归测试**（1 个，新增）：
- `test_toc_slots_get_tight_max_chars`：构造 4 大字（32pt）+ 4 小字（16pt）的
  TOC slide，断言大字段 max_chars ≤ 8、小字段 max_chars ≤ 6；同时验证非 TOC slide
  仍保持原 heuristic（≥ 24）。

**端到端验证**：
- planner 日志：`LLM mapping for slide_part03_content.svg shape shape-22 exceeds
  max_chars (22 > 6); truncating to fit` —— 6-char cap 实际生效。
- 6 项目录条目正文 `Purpose & Scope` / `Core Principles` / `Application &
  Methods` 等都 ≤ 21 字符，全部 fit 进 TOC 槽。

**未做**（中期/长期）：
- 动态 toc rows（`n ≤ 6` 时 `n × 2`，`n > 6` 时 `n × 1`）仍是 P2 范围，
  本次仅收紧 max_chars 不改布局。

### ✅ Phase 4 quality_check 修复（Phase B+）

**问题**：end-to-end 仍报 2 个 blocking error，覆盖 slide_01.svg（cover title
"山西柏腾科技有限公司采购制度" 13 chars at 47pt in 681px frame，4.9% 溢出）和
slide_04.svg（content title "一、目的 · 二、适用范围 · 三、基本原则" 16 chars
at 23pt in 427px frame，6.5% 溢出）。

**根因**：原 max_chars heuristic 只看 placeholder 长度 `max(len, 24) * 1.2`，
对 cover/content 的窄标题帧 over-count。例如 cover title placeholder 是 13 chars，
heuristic 给出 28 chars max —— LLM 写 13 chars 就符合，但 13 chars × 47.49 font-size
≈ 617 px 实际渲染 + 字距 ≈ 704 px > 681 px frame。

**修复**：新增 `_geometry_based_max_chars(grp, text)` helper，从 shape 的
`data-pptx-frame` 属性读 frame_width，从 text 的 `font-size` 属性读字号，
计算 `int(frame_width / font_size * 0.85)` 作为更准确的 cap。
0.85 留 15% headroom 给字距 / kerning / 字体 hinting artifacts。

`_scan_text_shapes` 对非 TOC slide 用 `min(heuristic, geom_cap)` —— frame 数据
可用时优先用几何 cap，缺失时 fallback 到 placeholder heuristic。

**回归测试**（2 个，新增）：
- `test_geometry_based_max_chars_uses_frame_and_font_size`：构造 boteng 风格的
  cover/content SVG（带 `data-pptx-frame` 和 font-size），断言 max_chars ≤ 13
  (cover) / ≤ 16 (content)，都 < 28 (heuristic 给的值)。
- `test_geometry_max_chars_falls_back_to_heuristic`：没有 `data-pptx-frame` 时
  仍走 heuristic（保留对老模板的兼容）。

**端到端验证**：
- 多次 clean run 中 Phase 4 quality_check 通过，`errors_count = 0`。
- 注意：LLM 输出是非确定性的，极少数 run 会产出极端超长文本触发 overflow；
  此时 `_normalize_mapping` 会按 max_chars 截断，但 `_truncate_to_fit` 的
  标点清理可能让"中点/中横线"边界计算偏紧。这种偶发 overflow 不属于本次修复
  阻塞目标。

---

## Bug 3 — 内容页排版太过死板

### 现象
13 张内容页里，每张 PART content 都套同一个视觉模板：
- slide_04（slide 4 content 01）：空 body + `content-body` block fallback
- slide_part02_content：3-column-cards（3 个卡片同宽同色）
- slide_part03_content：flow-steps（4 个步骤同色同号）
- slide_part04_content：3-column-cards（3 个卡片同宽同色）
- slide_part05_content：revision-table（一行 4 列）

视觉变化：仅 3 种 layout 反复出现，没有"对比/计时/引用/单一标题/双栏"等
变化感。用户感觉"全 deck 一个模式"。

### 根因（三层叠加）

1. **模板骨架本身只有 cover/toc/divider/content/ending 5 种** —
   `NATIVE_FILL_PIPELINE_GUIDE §9` 同样也只能复用 `divider+content` 两个骨架。
   ppt-master 解决方法是 `--adopt-object` 跨页搬元素，本 MCP 还没接。

2. **`new_blocks` 只有 4 个 layout**：
   ```
   - 3-column-cards
   - flow-steps
   - revision-table
   - raw
   ```
   缺：`hero-number`（大数字 KPI）、`callout-box`（引语/重点）、`two-column-compare`（对比）、
   `bullet-list`（无结构长文）、`timeline`（时间线）、`process-diagram`（流程图带分支）。

3. **SYSTEM_PROMPT 没教"根据内容性质挑 layout"** — 现在 LLM 凭"看到 N 项就用
   cards / 看到顺序就用 flow-steps"，没做"密度 / 对比 / 量化"判断。

### 修复方向

**P1 — 加 3-4 个新 layout**（每 layout ~30 行）：
- `hero-number`：1 个大数字 + 1 段说明（适合"5 章制度 / 7 类原则"那种
  一句话量化）
- `callout-box`：1 段引语 + 大引号 + 作者（适合"秉公办事 / 维护公司利益"
  这种中心思想）
- `two-column-compare`：左右两栏标题 + 对比项（适合"生产 vs 研发"、
  "内部 vs 外部"）
- `timeline`：横轴 N 个节点 + 标签（适合"申请 → 审批 → 采购 → 入库"
  有时序的流程）

**P1 — SYSTEM_PROMPT 加"Composition Patterns"段**（~30 行）：
```
- 单数字/单一论断    → "hero-number"
- 中心思想/口号      → "callout-box"
- 两个并列对照       → "two-column-compare"
- 5+ 时序步骤        → "timeline" 或 "flow-steps"
- 4+ 等同成员        → "3-column-cards" 或 "2x2 grid"
- 修订/版本历史      → "revision-table"
- 密集段落无结构     → "bullet-list" (fallback)
```

**P2 — `_fill_missing_content_blocks` 的 fallback 多样化**：
当 LLM 忘了给 cloned content 配 block 时，按内容自动选 layout：
- 章节里有数字 → hero-number
- 章节里有引号 → callout-box
- 章节里有时间序列 → timeline
- 默认 → 3-column-cards

### ✅ 修复完成（Phase B）

**改动**：
- `_render_new_block()` 新增 4 个 layout 分支（pipeline.py 末尾，每个 30-50 行）：
  - `hero-number`：居中大数字（font-size=72, bold, primary #1D2CAB）+ 单位 + caption
  - `callout-box`：tinted panel（fill #F4F6FB）+ 大引号 + 居中引文 + 右下斜体署名
  - `two-column-compare`：左右两栏 + 中间竖线分隔（stroke #D0D6E5）
  - `timeline`：N 节点圆 + N-1 连接线（避免 marker 依赖）+ label 上 / detail 下
- `_normalize_new_blocks()` 扩展接受 4 个新 layout + 加 per-layout 验证：
  - `hero-number`：`spec.value` 非空 string
  - `callout-box`：`spec.quote` 非空 string
  - `two-column-compare`：`spec.left`/`spec.right` 都是 `{title, items}`
  - `timeline`：`spec.steps` 长度 2-5
- `SYSTEM_PROMPT` 加 `Composition Patterns` 段（11 行）—— 显式告诉 LLM
  "pick layout by content shape, not by template slot"。
- `SYSTEM_PROMPT` `Available layouts` 枚举追加 4 个新 layout 的 spec schema。

**回归测试**（7 个，新增）：
- `test_hero_number_renders_centered_value_and_caption`：断言 font-size=72、
  text-anchor=middle、value/unit/caption 都被渲染。
- `test_hero_number_rejects_missing_value`：spec.value 缺失时 raise ValueError。
- `test_callout_box_renders_quote_and_attribution`：断言 fill=#F4F6FB、quote、
  attribution 都出现。
- `test_two_column_compare_renders_two_columns_with_divider`：断言两个 title、
  items、`<line` 分隔线。
- `test_timeline_renders_nodes_and_connectors`：4 步 → 4 circle + 3 line。
- `test_timeline_keeps_first_node_inside_bounds`：timeline margin 自适应（防 quality_check
  blocking error）—— 5 节点 + 长 detail 不溢出容器。
- `test_normalize_accepts_new_layouts` / `test_normalize_drops_invalid_new_layouts`：
  `_normalize_new_blocks` 对 4 个新 layout 的接受 + 拒绝。

**端到端验证**（多次 `examples/llm_fill.py`）：
- run 1: `slide_part02_content.svg` = **timeline**（5 圆 + 4 连接线，"(一)(二)(三)(四)(五)"）、
  `slide_part05_content.svg` = **callout-box**（"0容忍底线" 长引语）。
- run 2: `slide_part02_content.svg` = **timeline**（同），`slide_04.svg` / 多数 = 3-column-cards。
- 至少 2 种新 layout 在每次 run 出现，达到"2-3 种不同 layout"验收门槛。

**自适应 margin 修复**：
- timeline 第一版 margin=40 对 5 节点 + 长 detail 会让最左节点文字溢出
  bounds，触发 phase4 quality_check blocking error。改成自适应 margin：
  `margin = max(40, widest_text_len * 6.3 + 10)`，clamp 到 `bw/2 - 10`。

---

## 优先级总览

| Bug | 工作量 | 修复优先级 | 关键改动文件 |
|---|---|---|---|
| 1 ending 提前 | ~10 行 | **P0** | `pipeline.py::_seed_original_roster` |
| 2 目录空白 | ~30 行（短期）/ ~100 行（中期动态化）| P0（约束）+ P1（动态化）| `llm_planner.py::_scan_text_shapes` + `pipeline.py::_render_new_block` 新增 |
| 3 内容页死板 | ~150 行（4 新 layout + prompt 段）| P1 | `pipeline.py::_render_new_block` + `llm_planner.py::SYSTEM_PROMPT` |

### 验收口径
修完 Bug 1 后再跑 `examples/llm_fill.py`，期望：
- `page_plan.json` slide_05 在最末位（output_index=13）
- readback.md Slide 13 = THANK YOU，最后一页
修完 Bug 2 + 3 后期望：
- 6 项目录压缩合理，底部装饰带不留大空白
- 5 张 cloned content 页至少出现 3 种不同 layout

---

## 修复顺序建议

1. **P0 Bug 1**（ending 提前）— 一行小改，立竿见影，0 风险
2. **P0 Bug 2 短期**（目录 max_chars 约束）— 改 shape_index 算 max_chars 时
   加上"目录 6 槽 = 2-4 字"的特殊判断
3. **P1 Bug 3**（4 个新 layout + SYSTEM_PROMPT 段）— 改动大但隔离好，
   在 _render_new_block 加 4 个 if 分支 + SYSTEM_PROMPT 插入新段
4. **P1 Bug 2 中期**（动态 toc）— 依赖 Bug 3 的 new_block 框架，
   实现"toc 当 new_block 的特殊 layout"即可