# Native Fill Planner 改进方案 — 从"硬塞文本"到"结构感知排版"

> **日期**：2026-09-11
> **触发问题**：v7 e2e 用 `柏腾ppt模版.pptx` + `采购制度.md` 只填出 ~200 字正文，丢 8 个章节。
> **对照基准**：`docs/NATIVE_FILL_PIPELINE_GUIDE.md §9` 那次 run 出了 11 页、86 个 source-ref 不变、3-column-cards / 流程图 / 修订表都排好。
> **目标**：让 `llm_planner.py` 学会 ppt-master 的结构感知能力,不再丢章节。

---

## 1. 现象对比

| | v7 run（当前） | 文档 §9 run（目标态） |
|---|---|---|
| 模板 | `柏腾ppt模版.pptx`(5 张) | 同 |
| 文档 | `采购制度.md`(~2400 字 / 10 章) | `合同管理制度.md`(类似规模) |
| 输出 slide 数 | **5** | **11** |
| 输出字符数 | ~200 | ~全量覆盖 |
| 结构化板块 | 0 | 3-column-cards / 流程图 / 修订表 |
| 单 slide 字符预算 | ~28 字硬塞 | 按 frame × font-size 算真实容量 |
| 章节丢弃 | 8 章 | 0 章 |

**两个 run 用的是同一个模板**。差距完全在 planner 层 — 不是 vendor 限制,不是 pipeline bug。

---

## 2. ppt-master 是怎么做到的（机制拆解）

读了 `~/.claude/skills/ppt-master/references/` 下的 strategist / plan-core / executor-base / executor-structure / edit-native-pptx workflow,把它的"智能"拆成 **6 层机制**。每层对应到我们 MCP 的一个具体缺口。

### 机制 ① — Strategist 三方向构造（plan-core.md §1, §d）

ppt-master 在跑 page_plan 之前,LLM 已经被锁进一个 **三层 contract**:

| 层 | 决策内容 | 我们 MCP |
|---|---|---|
| Communication contract | `audience` / `communication_intent` / `audience_outcome` / `core_message` / `delivery_context` / `artifact_afterlife` — 6 个开放 prose 字段,互相约束 | ❌ 完全没这层 |
| Mode | `pyramid` / `narrative` / `instructional` / `showcase` / `briefing` — **叙事骨架**选一个 | ❌ 没有 mode 概念 |
| Visual style | 风格预设(`swiss-minimal` / `dark-tech` / `data-journalism` 等)+ **执行性 behavior prose**(不是枚举,是"用 A 不用 B"的描述) | ❌ 没有 |

**关键差异**:ppt-master 把 "**怎么讲**"(mode)和 "**讲出什么气质**"(style) **分开锁**,LLM 不能临时换叙事骨架。**Edit Native PPTX 还要求 Stage 1 BLOCKING confirmation** — LLM 必须给用户看一份完整 plan,等用户确认后才动手(`workflows/edit-native-pptx.md §4.3` ⛔ BLOCKING)。

### 机制 ② — page_plan + skeleton 复用（edit-native-pptx.md §4.1）

ppt-master 的 page_plan **不是 5 字段映射**(slide_01→shape-XX),而是**完整输出顺序 + 每页可指向任意 source slide**:

```jsonc
// ppt-master 的 page_plan 形态
{
  "schema": "ppt-master.roundtrip-page-plan.v1",
  "pages": [
    {"source_slide": 1},                              // 封面用源
    {"source_slide": 4, "svg": "chapter_market.svg"}, // 复用 slide_04 骨架
    {"source_slide": 7},                              // 引用源页
    {"source_slide": 7, "svg": "kpi_second_half.svg"} // slide_07 复制后改写
  ]
}
```

**关键能力**:
- **同一 source_slide 多次出现** → 必须先 `Copy-Item` SVG 文件起新名,再登记
- **新页 = 复制最近骨架页** → 删除其 slide-local 内容,在空白画布上重新画
- **`--adopt-object` 跨页搬元素** → 把别的页的 `<g>` 整个搬过来(丢 native identity)

**对应执行**:
```bash
# 1. 复制骨架
Copy-Item authoring-svg-flat/slide_03.svg authoring-svg-flat/slide_part02_div.svg
# 2. 在新文件里改 <text>
# 3. 登记到 page_plan.json
# 4. 切忌先复制再修模板(§6.7 次序敏感性)
```

**v7 完全没做 page_plan 扩页** — 这就是为什么 5 张卡死。

### 机制 ③ — 关系原子驱动的版式决策（executor-structure.md §1）

ppt-master 不是"看一个 section → 套个 3-column-cards"。它先**识别内容的关系类型**,再**决定版式**:

| 关系原子(atom) | 含义 | 推荐版式起点 |
|---|---|---|
| `order` | 顺序 / 进展 / 排名 | 单条阅读路径:开/闭、直/弯/阶/旋、升/降、扩张/收缩 |
| `link` | 依赖 / 交换 / 影响 | 直接 / 中心 / 链 / 分支 / 合并 / 反馈 |
| `parent` | 父→子分解 | 辐射 / 缩进 / 嵌套 / 缩放 |
| `membership` | 成员归属 | 包含 / 频带 / 通道 / 集群 / 重复 |
| `contrast` | 对比 | 共享基线 / 对立区域 / 平行框架 |
| `overlap` | 重叠 / 共享子集 | 精确排他 / 共享区 / 链式交集 |

文档 §9 run 的 3-column-cards 是 `membership` + `contrast` 组合(三个平级成员共享一张卡片区);流程图是 `order` + `link`;修订表是 `membership` + `order`(按日期)。

**v7 完全没有这层** — LLM 看到 "采购方式" 4 个并列子节,只能用相同的 slot 重复塞。

### 机制 ④ — 几何→容量精确预算（svg_quality/checker.py）

ppt-master 的质量门**计算真实容量**,不是字数:

```python
# scripts/svg_quality/checker.py 核心算法
font_sizes = _resolve_project_font_sizes(root)         # 从 <text font-size> 解析
letter_spacings = _resolve_project_letter_spacings(...) # letter-spacing 属性
# 然后逐 <text> 算:
estimated = _estimated_text_bounds(
    text_element, font_sizes, letter_spacings,
    include_headhead=True,
)
# estimated = (left, top, right, bottom) 是渲染后的真实矩形
# 对比 owning frame:
metrics = _bounds_overflow_metrics(estimated, frame)
# metrics = (axes, horizontal_ratio, vertical_ratio)
# horizontal_ratio > 5% → ERROR, < 5% → WARN
```

**关键参数**:
- 用源 PPTX 的未编辑 `<text>` 做 baseline 校准(`_ROUNDTRIP_TEXT_CALIBRATION_CAP`) — 量出 PPTX 实际渲染 vs SVG 估算的差值,加到 overflow 计算里
- `horizontal_ratio = (rendered_width - frame_width) / frame_width` — 浮点比,不是字符数
- 同时检查 horizontal + vertical (`_bounds_overflow_metrics`)

**v7 的 `max_chars = max(len(placeholder), 24) * 1.2` 完全不靠** — 不知道 font-size、不知道 frame width、不知道 letter-spacing、不知道 CJK char width vs Latin char width。

### 机制 ⑤ — 编辑 → 决策树（edit-native-pptx.md §5）

ppt-master 给 LLM 的 "文字太长" 修复决策树:

> **Edit Native PPTX §5 / Hard rule**:
> "Fit the slot's visual capacity from its **geometry and font size**, not the old placeholder length; resolve overflow by **rewriting shorter → splitting across another selected page → choosing a larger source layout**; shrinking type is **last** and **never deck-wide**."

| 优先级 | 动作 | 风险 |
|---|---|---|
| 1 | **缩短文字**(preserve 原意) | 低 |
| 2 | **拆到另一页**(split section) | 中 — 需要新页 |
| 3 | **换更大的 source layout** | 中 — 需要 page_plan |
| 4 | **缩字号**(per-slot,**禁止全 deck 缩**) | 高 — 视觉跳变 |
| ❌ | 全 deck 缩字号 | **严禁** |

文档 §9.5 那次的 7 步 auto-fix 全是 1-2 级动作(删字段、缩短英文、缩小单个 font-size),没碰 3-4 级 — 因为它**先做了 page_plan 扩页**所以有 slot 可用。

**v7 走的是 -1 级** — `_truncate_to_fit()` 无脑砍字符,丢语义。

### 机制 ⑥ — 修模板先于拷模板（§6.7）

ppt-master 用血换来的规则:

> "复制前**先修好模板**,再拷贝。否则复制品保留了坏结构。"

文档 §6.7 直接命名:"本次踩坑:先复制 slide_03.svg → slide_part02_div.svg,**之后**才修了 slide_03.svg 的 picture 结构。结果 slide_part02_div.svg 保留了**坏掉的 picture 结构**,export 时它先过(因为它在 page_plan 第 5 位),但 slide_part02_content.svg 也坏掉。"

**v7 没碰过 copy skeleton 所以这条不命中** — 但一旦启用 page_plan,这条立刻成为必修。

---

## 3. 我们的 gap 清单

按"修了能填满文档"的优先级排:

| # | gap | 影响 | 修复成本 |
|---|---|---|---|
| G1 | **没 page_plan 扩页机制** — 模板 5 张锁死 | 全局性 — 任何 >5 章节文档都装不下 | 中(改 pipeline + planner schema) |
| G2 | **max_chars 启发式拍平** — `max(len,24)*1.2` | 即使扩页也按错的容量算 | 低(20 行) |
| G3 | **没 new_content_blocks 实现** — schema 有声明但 planner/pipeline 没接 | 即使 LLM 想加 cards 也加不了 | 中(改 pipeline) |
| G4 | **没骨架识别** — 不识别 slide_03 = divider / slide_04 = content | page_plan 扩页会乱 | 中(加 scan 阶段) |
| G5 | **没关系原子** — 不识别 order/link/contrast | LLM 不知道怎么排 4 个并列子节 | 高(改 SYSTEM_PROMPT) |
| G6 | **truncate 是砍字符不是压缩语义** | 内容缩水 | 中(改 SYSTEM_PROMPT + post-edit 校验) |
| G7 | **没次序敏感性** | 启用 page_plan 后会踩 | 低(改 pipeline) |
| G8 | **没 source-passthrough / rebuild 区分** | 编辑过的 slide 走 rebuilt;没编辑的应该 passthrough 节省字节保 byte-for-byte | 低(改 export_summary 解析) |

---

## 4. 修复方案 — 5 阶段路线

按依赖关系排:**G1/G3/G4 先做(没它们后面无效)→ G2 → G5 → G6 → G7/G8**。

### Phase A — page_plan 扩页(MCP 顶层)

**目标**:LLM 在 prompt 里看到"markdown 有 8 章 / 模板只有 5 张",**自动复制 slide_03 / slide_04 当骨架**,登记到 page_plan.json。

**改 4 处**:

1. **`llm_planner.SYSTEM_PROMPT`** — 加段:

```
Skeleton detection (REQUIRED):
  1. 从 authoring_summary.json 读每页 kind/text_elements 数量。
  2. 识别:
     - cover (slide_01): "封面" 模式
     - toc (slide_02): "目录" 模式,text_elements 中 shape-id 连续且 font-size 呈 32/16 双层
     - divider (slide_03): PART NN 模式,只有 1-2 个 text,font-size ≥ 55
     - content (slide_04): "章节扉页 + 大正文" 模式,font-size ≤ 40,有充足 vertical space
     - ending (slide_05): THANK YOU 模式
  3. 把这些 kind 加到 shape_index 输出里。
  
Skeleton reuse (REQUIRED):
  If markdown H1 count > source_slide_count - 2 (excluding cover/ending):
    for each H1 (excluding 前言):
      new_page = {
        "source_slide": <divider_id>,  # 比如 3
        "svg": "<stem>_part<NN>_div.svg",
        "edits": {
          "shape-<big_title_id>": "<H1 主标题>",
          "shape-<sub_id>": "<H1 副标题>"
        }
      }
      new_page2 = {
        "source_slide": <content_id>,  # 比如 4
        "svg": "<stem>_part<NN>_content.svg",
        "edits": { ... body ... }
      }
```

2. **`llm_planner.plan_content_mapping()` 返回值** — 从 `dict[svg][shape_id] -> str` 改成:

```python
@dataclass
class PlannerResult:
    content_mapping: dict[str, dict[str, str]]   # 现有 slot 填充
    page_plan_additions: list[PageAddition]      # 新页
    new_blocks: list[NewBlock]                   # 现有 slide 上的新板块
    skeleton_kind: dict[str, str]                # 每张 slide 的角色
```

3. **`pipeline.phase2_5_llm_plan()`** — 接收 `PlannerResult`,**先**:
   - 对每个 `PageAddition`:`shutil.copy2(source_svg, new_svg)` + 改新 svg 的 `<text>`
   - 对每个 `NewBlock`:在目标 svg 里插 `<g id="..." data-pptx-bounds="...">` 块
   - 写 `page_plan.json`(`schema: ppt-master.roundtrip-page-plan.v1`)

4. **`pipeline.phase3 / phase4 / phase5`** — 必须**按 page_plan 顺序**跑 export,不能按物理 svg 文件名。`svg_to_pptx.py --roundtrip` 会自己读 page_plan,我们只负责写对。

**验证**:跑 e2e,期望 `Round-trip export summary: output_pages=11 rebuilt=11 passthrough=0`(同文档 §9.7)。

### Phase B — max_chars 真实容量(G2)

**目标**:从启发式字符串 → 从 SVG 几何算。

**改 1 处**:**`llm_planner._scan_text_shapes()`** 改为:

```python
def _scan_text_shapes(workspace: Path) -> dict[str, dict[str, Any]]:
    # ... 现有扫描逻辑 ...
    for grp in root.iter(svg_ns + "g"):
        # ...
        for t in grp.findall(f".//{svg_ns}text"):
            fs = float(t.get("font-size") or "16")  # default 16
            x = float(t.get("x") or "0")
            # 取祖先 data-pptx-frame (ppt-master 把它放在最近 <g> 上)
            frame = _ancestor_pptx_frame(grp)
            if frame is None:
                # 退而求其次:用 viewBox 推算
                frame = (0, 0, viewbox_w - x, viewbox_h)
            # PPT-safe 字宽:
            #   CJK: 1em ≈ fs px
            #   Latin: 1em ≈ fs * 0.55 px
            # 单行字数 = frame_width / (fs * 0.85)  (混合加权)
            single_line = int(frame[2] / (fs * 0.85))
            # 多行:用 frame 高度 / 行高(fs * 1.4)估算行数,打折 80%
            lines = max(1, int((frame[3] / (fs * 1.4)) * 0.8))
            max_chars = single_line * lines
            per_slide[gid]["max_chars"] = max_chars
            per_slide[gid]["font_size"] = fs
            per_slide[gid]["frame"] = frame
```

**验证**:跑 v7 同 input,期望 shape-70(slide_02,16pt)max_chars 从 28 → ~100+,slide-4(slide_03,80pt)从 28 → ~6。

### Phase C — new_content_blocks 实现(G3)

**目标**:`PlannerResult.new_blocks` 被真的写到 SVG。

**改 2 处**:

1. **新 helper `pipeline._emit_new_block(svg_path, block)`**:
   - 用 `xml.etree.ElementTree`,在 `<svg>` 末尾插 `<g id="{block.id}" data-pptx-bounds="{block.bounds}">`
   - 块内容按 `layout` 模板生成:
     - `3-column-cards` → 3 个 `<rect rx>` + `<text>` 标题 + `<text>` 子项
     - `revision-table` → `<text>` 表头 + N 行 `<text>` 数据
     - `flow-steps` → N 个 `<rect>` 节点 + `<line>` 连接
   - 所有色值用我们 palette token(`#1D2CAB` / `#EE822F` / `#75BD42`),不写死 hex

2. **`pipeline.phase2_5_llm_plan()`** — 在改 `<text>` 之后,再走一遍 new_blocks emit

**验证**:slide_part02_content.svg 里出现 3 个 `<rect rx=...>` + `<text>` 标题。

### Phase D — 关系原子 → SYSTEM_PROMPT(G5)

**改 1 处**:**`SYSTEM_PROMPT`** 加一节 "Composition Patterns":

```
Composition patterns (use these when content has clear structure):

- 4+ parallel items  → "3-column-cards" (max 3 cols) or "2x2 grid"
- order/step list    → "flow-steps" (5 steps max, horizontal)
- comparison/contrast → "two-column-compare" with shared header
- membership/cluster → "card-grid" (N cards, 1-2-3 column responsive)
- dense list (8+ items) → "revision-table" (date + status + content + author)
- single big claim   → "hero-number" or "callout-box"
- no obvious structure → "bullet-list" (default fallback)

When emitting a block, declare:
  layout: <one of the above>
  bounds: "<x> <y> <width> <height>"   # never overlap existing shape-IDs
  items: [...]                         # per layout schema
```

**验证**:给"采购方式"4 子节,LLM 自动出 `flow-steps` 或 `card-grid`,而不是塞 4 个一样的小字。

### Phase E — truncate 改成压缩(G6)

**目标**:`_truncate_to_fit` 不是砍尾,而是**提摘要**。

**改 2 处**:

1. **`_truncate_to_fit()` 改名 `_summarize_to_fit()`** — 当超过 max_chars:
   - 提取核心名词短语(标点分隔、保留首句)
   - 优先保留数字 + 关键动词
   - 输出 "..." 提示截断

2. **`SYSTEM_PROMPT`** — 加 "When text overflows slot capacity":
```
NEVER just truncate mid-sentence. Instead:
  1. Keep the first independent clause (before first 。 / . / ；)
  2. Compress modifiers: 删除 "的"、"了"、"应当"、"应当"
  3. If still over: pick the most important noun phrase
  4. Append " ..." if compressed
If the full meaning cannot fit, PREFER skipping the slot over producing broken text.
```

---

## 5. 验证口径

| 验证项 | 期望 | 怎么测 |
|---|---|---|
| **page_plan 扩页生效** | e2e 文档 `output_pages > 5` | `validation/readback.md` 第一节显示 N 页 |
| **max_chars 真实** | slide_03 的 80pt 槽 max_chars < 10,16pt 槽 max_chars > 80 | 在 shape_index 里看 |
| **new_content_blocks 真出** | `slide_part0X_content.svg` 里含 `<g data-pptx-bounds>` | `grep` svg |
| **关系原子生效** | "采购方式"4 子节 → flow-steps 或 card-grid | `readback.md` 里看到 "1. ..." 列表 |
| **覆盖度** | readback.md 出现原文 ≥ 70% H1 标题 | 文本 diff |
| **delivery passed** | `validation/<output>.delivery.json status == passed` | jq |
| **次序敏感** | 修模板 → 拷贝,不能反过来 | 手测一次坏次序,期望 export 报错 |

---

## 6. 不在本次范围(明确不做)

- **不要重写** `svg_quality_checker.py` — 我们调用 vendor 的就好,不要自己写 checker
- **不要重写** `svg_to_pptx.py` — 同上
- **不要加 LLM 多轮交互** — Edit Native PPTX 的 BLOCKING confirmation 是留给 Claude Code UI 用的,MCP 是 offline 跑
- **不要做 prompt 工程的"few-shot examples"** — SYSTEM_PROMPT 已经足够指导,加了反而占 token

---

## 7. 一句话总结

> **当前 v7 是"按 slot 字数硬塞",文档 §9 run 是"按关系原子 + 骨架复用 + 真实容量 + 几何决策"四层联合排版**。
>
> 修复路径:Phase A 扩页(page_plan)→ Phase B 算真容量(max_chars)→ Phase C 接 new_content_blocks → Phase D 教关系原子 → Phase E 改 truncate。
>
> 5 个 Phase 跑完,期望 v7 同 input 能出 11 页 + 3-column-cards + 真实内容覆盖 ≥ 70%,与文档 §9 那次对齐。