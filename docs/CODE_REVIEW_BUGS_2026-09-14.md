# Native Fill — 代码审查 Bug 总结（2026-09-14）

> **来源**：Phase B+ 之后的静态代码审查（`/dev-expert` 子技能），仅基于源码阅读，未跑端到端。
> **审查范围**：`src/mcp_ppt_native_fill/` 下 10 个核心模块，共 5500+ 行。
> **审查方法**：只读静态分析，聚焦"功能正确性"（行为偏差、边界条件、数据丢失、状态错误），不覆盖代码风格、性能、安全。
> **代码版本**：commit `0c5d3cd`（"fix(autofix): clear solid-white decorative panels, fill='none' instead of #FFFFFF"）。
> **关联文档**：
> - `BUGS_FOUND_2026-09-13.md` — 端到端用户反馈驱动的 3 个 bug（已修）。
> - `PHASE_B_NATIVE_FILL_FIXES_2026-09-13.md` — 上一批 bug 的修复记录。
> - `PHASE_C_NATIVE_FILL_FIXES_2026-09-14.md` — **待建**：本批 bug 的修复追踪文档（按 §6 修复顺序新建）。
> **本文只记录，不修复**。所有 P0/P1/P2/P3 标记的 bug 均需在 Phase C/D 单独修复。

---

## 0. TL;DR — 21 个 Bug 按优先级汇总

| 等级 | 数量 | 触发条件（共同特征） | 修复工作量（单 bug） |
|---|---|---|---|
| **P0** | 4 | 跨模板即失效的硬编码值（画布尺寸、字号阈值、估算系数、正则锚定字符） | 各 ~30 行 + 1-2 失败测试 |
| **P1** | 5 | boteng 模板实测过、其他模板未验证的启发式（row 锚点、placeholder 模式、fallback 路径） | 各 ~15 行 + 1 测试 |
| **P2** | 8 | 边缘场景（Latin only、单元素、短 bounds、单引号 JSON、Windows 用户名） | 各 ~5-10 行 |
| **P3** | 4 | 死代码、视觉细节、字段引用错位 | 各 ~1-3 行 |

**核心结论**：代码主流程（状态机、阶段切换、auto-fix 循环、LLM 集成）架构正确；但渲染层 7 个 `_render_new_block` layout 和模板检测层（`_detect_skeleton_kind`、`_remove_empty_toc_slots`）在 **跨模板适配** 上有明显短板——很多阈值是为 boteng 模板实测调出来的硬编码，换模板即功能退化。如果项目只服务于 boteng 系列模板，大部分 P0-P1 不构成问题；若要扩展到通用 PPT 模板，需先做模板无关化重构。

---

## 1. 审查方法

### 1.1 覆盖模块（10 个，~5500 行）

| 模块 | 行数 | 职责 | 审查重点 |
|---|---|---|---|
| `pipeline.py` | 1721 | 状态机 + 5 个 phase 驱动 + 7 个 layout 渲染 | 状态转移合法性、TOC 启发式、layout 几何计算 |
| `autofix.py` | 971 | 6 个修复规则 + workspace 批量处理 | 硬编码值跨模板失效、正则兼容性 |
| `llm_planner.py` | 946 | planner prompt 构造 + skeleton 检测 + 响应归一化 | 阈值硬编码、placeholder 模式匹配 |
| `llm_client.py` | 430 | OpenAI / Anthropic HTTP 客户端 | 响应解析鲁棒性 |
| `runner.py` | 457 | 7 个 vendored 脚本的 subprocess 包装 | skill_dir / python 路径探测 |
| `server.py` | 434 | MCP stdio JSON-RPC 服务器 | 工具调度、错误封装 |
| `svg_edits.py` | 226 | XML 元素树文本编辑 + new_content_block 写入 | 命名空间处理 |
| `text_width.py` | 203 | 字符宽度估算（fork 自 ppt-master） | CJK/Latin 宽度模型 |
| `io_utils.py` | 72 | UTF-8 原子写、JSON 容错 | 无 |
| `__init__.py` / `__main__.py` | 21 | 包入口 | 无 |

### 1.2 不覆盖项

- ❌ **代码风格**（命名、注释密度、行长）—— 由 `karpathy-coding-guidelines` / `simplify` 流程覆盖
- ❌ **性能基准**（profiling、benchmark）—— 由 `performance-benchmark` 流程覆盖
- ❌ **安全红线**（XSS、命令注入、路径穿越、敏感信息泄露）—— 由 `threat-modeling` 流程覆盖
- ❌ **测试覆盖**（行覆盖率、边界用例）—— 由 `test-generation` 流程覆盖
- ❌ **API/协议设计**（MCP 协议兼容性）—— 由 `api-design` 流程覆盖
- ❌ **LLM provider 协议内部细节**（token 计算、流式响应）—— OpenAI/Anthropic SDK 自己负责

### 1.3 审查路径

1. **状态机入口**：`pipeline.run_native_fill` → `PipelineState.stage` 转移合法性
2. **渲染层**：`_render_new_block` 7 个 layout 的几何与文本边界
3. **修复规则**：`autofix` 6 个函数的正则/阈值
4. **模板检测**：`_detect_skeleton_kind` + `_remove_empty_toc_slots` 的启发式
5. **边界**：`text_width` 的 CJK/Latin 模型、`_truncate_to_fit` 的截断逻辑
6. **环境/路径**：`runner.resolve_skill_dir` / `resolve_python` 的硬编码
7. **死代码扫描**：`hasattr` / 重复赋值 / 未使用导入

---

## 2. P0 关键 Bug（必须修，影响跨模板正确性）

### Bug 01 — 画布尺寸硬编码 1280×720（`autofix.py:481`，P0）

**现象**

非 16:9 720p 模板下，"实心白色装饰面板"清理规则大面积失效。

```text
# 反例 1：1920×1080 boteng 派生模板
panel.fill = "#FFFFFF"
panel.frame = "0 0 1920 1080"   # 全画布覆盖

# 当前规则（autofix.py:481）
if fw < 0.6 * 1280 and fh < 0.6 * 720:  # 768 / 432
    continue  # ← 1920px 宽的 panel 通过 768 检查，不进清理分支
# 结果：白色 panel 留在 SVG 上，覆盖背景图
```

```text
# 反例 2：9:16 竖版（fh = 1280）
panel.frame = "0 0 720 1280"  # 长条装饰
if fw < 0.6 * 1280 and fh < 0.6 * 720:
    continue  # ← 720px < 768 → 通过；1280px > 432 但 fw 也通过
# fh = 1280 < 432 ? 否（1280 > 432）→ 进清理分支 → 误清
# 实际想要"全画布覆盖" = 720x1280 全覆盖，应该清理但规则不稳
```

**根因**

阈值 `0.6 * 1280` 和 `0.6 * 720` 是固定常量，对所有画布尺寸一视同仁。boteng 模板恰好是 1280×720，所以原代码实测通过；其他尺寸一律失效。

**修复方向**（P0 必改）

```python
# 1. 从 <svg> 根动态读取
canvas_w = float(root.get("width", "1280").rstrip("px"))
canvas_h = float(root.get("height", "720").rstrip("px"))
# 或从 viewBox 解析
# 2. 用相对阈值（0.6 * canvas_w / canvas_h）
if fw < 0.6 * canvas_w and fh < 0.6 * canvas_h:
    continue
```

工作量 ~10 行 + 1 个失败测试（用 1920×1080 fixture 断言清理生效）。

---

### Bug 02 — skeleton_kind 检测多个硬编码阈值（`llm_planner.py:574` + `pipeline.py:553-554, 557-559, 565-566`，P0）

**现象**

非 boteng 模板的"封面 / 目录 / 分隔 / 内容 / 结尾"五分类大面积错位，导致后续 page_plan_additions 克隆错骨架。

```text
# 当前规则（llm_planner.py:574）
if fs >= 50:  # divider 阈值
    kind[path.name] = "divider"

# 反例：某模板 divider 用 38pt 大标题
slide_03.svg: text font-size = 38pt
# fs = 38 < 50 → 被识别成 content，分页克隆用错的骨架

# 反例：某模板 content 标题 60pt
slide_05.svg: text font-size = 60pt
# fs = 60 >= 50 → 被识别成 divider
```

`pipeline.py:553-554` 同样问题：
```python
last_path, _, _ = per_slide_stats[-1]
kind[last_path.name] = "ending"  # 最后一张 = ending
```
无 ending 页的纯内容模板（目录 + 3 章内容 + 致谢页），致谢页被识别成 content。
封面在第 2 张的模板，介绍页被识别成 cover。

**根因**

skeleton 检测完全是"位置 + 字号阈值"的硬规则，没有用到：
- 标题语义关键词（THANK/Q&A/谢谢/再见/contact → ending）
- 字号 × frame 宽度的比例（标题占整页宽度 > 50% → cover）
- 槽数量 + 视觉对齐模式（≥ 6 个并列同结构槽 → toc）
- 占位文本结构（"第N章"模式 → divider）

**修复方向**（P0 必改）

引入多信号打分：

```python
def _classify(slide_stats) -> str:
    text_count, max_fs, frame_w, has_thank, has_chapter, slot_count = ...
    score_cover   = (max_fs * frame_w > 0.5 * canvas_w) * 100 + slot_count * -5
    score_toc     = slot_count * 15  # 并列槽越多越像 toc
    score_divider = max_fs * (1 if has_chapter else 0)
    score_ending  = (100 if has_thank else 0) + (-text_count * 5)
    return max(("cover", score_cover), ("toc", score_toc), ...)
```

工作量 ~50 行 + 4 个模板 fixture 测试。

---

### Bug 03 — timeline margin 估算对 CJK 不准（`pipeline.py:1666-1672`，P0）

**现象**

含 CJK 字符的 timeline detail 文本溢出 bounds 左右边距。

```python
# 当前代码（pipeline.py:1666-1672）
widest = max((len(str(s.get("detail", ""))) for s in steps), default=0)
margin = max(40.0, widest * 6.3 + 10.0)  # 6.3 px/字符假设
```

```text
# 反例：5 节点 timeline，detail 字段是 CJK
detail = "国家发改委审批"   # 7 个 CJK 字符，font-size = 12pt

# 估算：7 * 6.3 + 10 = 54.1 px
# 真实宽度（CJK = 1.0em @ 12pt = 12 px/字符）：7 * 12 = 84 px
# 缺口：84 - 54 = 30 px → 文本越过 margin，进入负坐标区
```

**根因**

估算系数 `6.3` 是按 12px 字号 × Latin 0.55em ≈ 6.6 校准的。但 `text_width._estimate_character_width` 已正确区分：
- `is_cjk_char` 返回 `font_size`（即 12pt = 12 px/字符）
- Latin digit = `0.55 * font_size`
- Latin m/M/W = `0.75 * font_size`

代码没用 `text_width.estimate_text_width`，自己简化算 `len() * 6.3` 必然失准。

**修复方向**（P0 必改）

```python
from .text_width import estimate_text_width
widest_px = max(
    (estimate_text_width(str(s.get("detail", "")), font_size=12.0)
     for s in steps),
    default=0.0,
)
margin = max(40.0, widest_px / 2 + 10.0)  # 居中锚点，半宽 + headroom
```

工作量 ~5 行替换 + 1 个测试（"国家发改委审批" 7 CJK fixture）。

---

### Bug 04 — picture_structure flat→nested 漏带路径 href（`autofix.py:563-569`，P0）

**现象**

真实模板的 `<image>` 通常含子目录路径，正则不匹配导致 flat → nested 转换失败，`svg_to_pptx` 报 picture 结构错误。

```python
# 当前正则（autofix.py:565）
r"(<g[^>]*data-pptx-object=\"picture\"[^>]*>)\s*"
r"(<image[^/]*/>)\s*"  # ← [^/]* 不允许 href 内出现 /
r"(</g>)",
```

```text
# 真实 boteng 输出
<g data-pptx-object="picture">
  <image xlink:href="media/image42.png" />   # ← 含 /，失配
</g>

# flat → nested 检测：跳过该 <g>
# 结果：svg_to_pptx 收到 flat 形式 → abort "picture structure"
```

**根因**

`<image[^/]*/>` 期望 `<image ... />` 中间不含 `/`，但 `href="media/foo.png"`、`xlink:href="../shared/img.png"` 都含 `/`。这是 vendor SVG 的事实数据形态，不是边缘情况。

**修复方向**（P0 必改）

```python
# 改用非贪婪匹配，跳过 src/href 中的 / 字符
r'(<g[^>]*data-pptx-object="picture"[^>]*>)\s*'
r'(<image\b[^>]*?/>)\s*'  # [^>]*? 自动避开 /> 闭合
r'(</g>)',
```

或更稳的方式：先定位 `<image\b[^>]*?>` 再向后找到首个 `/>` 闭合，不靠正则的字符类。

工作量 ~5 行 + 1 个 boteng fixture 测试。

---

## 3. P1 中等影响 Bug（boteng 实测过、其他模板需验证）

### Bug 05 — 3-column-cards 内容不限底部溢出（`pipeline.py:1429-1434`，P1）

**现象**

LLM 给 20 条要点 → 卡片底部文字飞出 bounds，撞到底层装饰或下一张 slide。

```python
# 当前代码（pipeline.py:1429-1434）
for j, item in enumerate(items):
    ty = by + 64 + j * 22  # 一直向下排，无边界检查
    parts.append(f'<text ... y="{ty:g}" ...>{_escape(item)}</text>')
```

```text
# 反例：bh = 480，items 长度 20
for j in range(20):
    ty = by + 64 + j * 22  # 末项 ty = by + 64 + 19*22 = by + 482
# ty > by + bh = by + 480 → 末 2 项溢出底部
```

**根因**

`_render_new_block("3-column-cards")` 无 `if ty > by + bh - 16: break` 守卫。同 layout 的 `revision-table` 有此检查（pipeline.py:1515），`3-column-cards` 漏写。

**修复方向**（P1 推荐）

加一行行号：
```python
for j, item in enumerate(items):
    ty = by + 64 + j * 22
    if ty > by + bh - 8:  # ← 缺这一行
        break
    parts.append(...)
```

工作量 ~2 行 + 1 个测试（items 长度 20 fixture）。

---

### Bug 06 — `_is_toc_slot_placeholder` 的 Chapter N 模式误删真实短标题（`pipeline.py:240-241`，P1）

**现象**

英文模板用 "Chapter 1" / "Chapter 2" / "Chapter 3" 做简短章节标题时，目录行被误判为占位符、整行被删除。

```python
# 当前代码（pipeline.py:240-241）
if re.fullmatch(r"[Cc]hapter\s*\d+", t):
    return True  # ← 任何 "Chapter N" 都被当成占位符
```

```text
# 真实英文模板目录条目
title = "Chapter 1"   # ← 用户的真实章节标题，不是占位符
# fullmatch 命中 → 标记为 placeholder → 行被删 → 目录少一行
```

**根因**

Boteng 模板默认占位符就是 "Chapter N" 模式，但其他英文模板可能直接用此模式作为正常章节名。占位符识别应该是白名单而非模式匹配。

**修复方向**（P1 推荐）

```python
# 1. 白名单精确匹配默认 placeholder 字符串
_PLACEHOLDER_WHITELIST = frozenset({
    "chapter", "Chapter", "CHAPTER",
    "click to add title", "click here to add title",
    "单击添加大标题", "点击添加大标题",
    # boteng 特有的 Chapter N 也包含在内
})
# 2. 不再用正则匹配 "Chapter \d+"
```

工作量 ~10 行 + 1 个英文模板 fixture 测试。

---

### Bug 07 — `_remove_empty_toc_slots` 锚点 +/- 90px 跨模板误伤（`pipeline.py:348-371`，P1）

**现象**

非 boteng 模板的 TOC 行高假设（90px）失效，清理时把相邻行也吞掉。

```python
# 当前代码（pipeline.py:348-351）
row_top = anchor - 30
row_bot = anchor + 90  # ← 硬编码 90px 行高
```

```text
# 反例：紧凑型模板，title 18pt
title height = 24px（行间距紧）
# 但 row_bot = anchor + 90 → 实际只占 24px 的行被 90px 范围覆盖
# 下一行的 accent bar（y = anchor + 70）被吞进 row → 误删
```

**根因**

90 px 行高来自 boteng 模板实测（title 24pt + subtitle 14pt + 间距 ~10px ≈ 80-90px）。其他模板字号 / 间距差异大，硬编码不稳。

**修复方向**（P1 推荐）

```python
# 用相邻 title y_top 之差动态计算行高
sorted_anchors = sorted(row_anchors)
for i, anchor in enumerate(sorted_anchors):
    next_anchor = sorted_anchors[i+1] if i+1 < len(sorted_anchors) else None
    row_h = (next_anchor - anchor) * 0.8 if next_anchor else 90.0
    row_top = anchor - 20
    row_bot = anchor + row_h
```

工作量 ~15 行 + 1 个紧凑模板 fixture。

---

### Bug 08 — `_seed_original_roster` fallback 把"最大 source_slide"当 ending（`pipeline.py:631`，P1）

**现象**

无 LLM 路径（纯 caller）或 LLM 没返回 `skeleton_kind` 时，ending fallback 选错——拿 source_slide 最大的那张当 ending，但很多模板的"附录"slide_NN 比 ending slide_NN 数字更大。

```python
# 当前代码（pipeline.py:631）
if ending_idx is None:
    ending_idx = len(candidates) - 1  # fallback: highest NN
```

```text
# 反例：slide_05 = ending, slide_10 = 附录
# candidates 排序：[01, 02, 03, 04, 05, 10]
# ending_idx = 5（slide_10）→ ending 出现在中间
```

**根因**

Fallback 退化成"按位置猜"，没有用任何语义信号。已知 BUGS_FOUND_2026-09-13.md Bug 1 是修复过 ending 提前的，但 fallback 路径没覆盖到。

**修复方向**（P1 推荐）

```python
# 优先：filename 含 thank / ending / closing 关键词
for idx, (_, name) in enumerate(candidates):
    if re.search(r"(thank|ending|closing|back_cover)", name, re.I):
        ending_idx = idx
        break
# 次优：内容含 THANK YOU / 谢谢 / Q&A
else:
    # 用 SVG body 文字搜索
    for idx, (_, name) in enumerate(candidates):
        svg_text = (authoring_dir / name).read_text(encoding="utf-8")
        if "THANK" in svg_text or "谢谢" in svg_text or "Q&A" in svg_text:
            ending_idx = idx
            break
```

工作量 ~15 行 + 1 个 fallback fixture 测试。

---

### Bug 09 — `phase4_quality` 死代码 + 错字段引用（`pipeline.py:1076-1078`，P1）

**现象**

```python
# 当前代码（pipeline.py:1076-1078）
if state.page_plan_pages if hasattr(state, "page_plan_pages") else None:
    # Already validated in phase3; refresh summary regardless.
    pass
```

`PipelineState` dataclass 未定义 `page_plan_pages` 字段，`hasattr` 永远 False → 这段永远不执行。同时注释说"refresh summary"但代码 `pass` 什么都没做。

实际数据存放在 `state.context["page_plan_pages"]`（见 phase2_6_realize_planner_output:841）。

**根因**

开发者本意是用 `page_plan_pages` 状态字段做校验，但忘了落地到 dataclass；`context` 用作 scratchpad 后这段代码忘了更新引用。

**修复方向**（P1 推荐）

二选一：
- A. 删掉这段（注释也是无意义 stub）
- B. 改为读 `state.context.get("page_plan_pages")` 做实际校验（如对每个 page 检查 `source_slide` 在 valid_source_slides 内）

工作量 ~3 行 + 注释清理。

---

## 4. P2 边界 Bug（Latin-only / 单元素 / 短 bounds / 单引号 JSON）

### Bug 10 — revision-table 把 list 项拼成单行（`pipeline.py:1519-1525`，P2）

**现象**

LLM 把 `row["content"]` 写成 `["步骤1", "步骤2"]`，渲染成单行 `"步骤1; 步骤2"` 而非多行。

```python
# 当前代码（pipeline.py:1523）
if isinstance(raw_val, (list, tuple)):
    cell_text = "; ".join(str(v) for v in raw_val)
```

表格单元格语义应是多行（每条一行），现在是分号拼接。

**修复方向**

```python
if isinstance(raw_val, (list, tuple)):
    cell_text = "\n".join(str(v) for v in raw_val)  # ← 改换行
# 同时 text 元素加 <tspan dy="22">...</tspan>
```

---

### Bug 11 — `fix_text_overflow` 多轮 shrink 后字号不可读（`autofix.py:289, 325-326`，P2）

**现象**

`max_fix_iterations=3` 时，3 轮后字号缩为原 61%。原 14pt → 8.5pt（不可读）。

```python
# 当前代码（autofix.py:289）
shrink_factor: float = 0.85,  # 默认
```

**修复方向**

加下限 + fallback 改为 truncate：
```python
MIN_READABLE = 8.0
new_fs = round(old_fs * shrink_factor, 2)
if new_fs < MIN_READABLE:
    # 改为截断文本而非继续 shrink
    truncated = text[:int(len(text) * MIN_READABLE / old_fs)]
    text_elem.text = truncated
    continue
```

---

### Bug 12 — `hero-number` 短 bounds 时数字飞出顶部（`pipeline.py:1553`，P2）

**现象**

72pt 数字的视觉高度 ≈ 90px，baseline 在 `value_y`。若 `bh < 90` 且 `by` 在画布顶部，数字上半部分在 bounds 外。

```python
# 当前代码（pipeline.py:1553）
value_y = by + bh * 0.55  # 短 bounds 时数字底部超出
```

**修复方向**

```python
# 自适应缩放 font-size
fs = min(72.0, bh * 0.5)  # 至少占 bounds 一半高度
value_y = by + bh * 0.55
```

---

### Bug 13 — `callout-box` 引号字符硬编码（`pipeline.py:1587`，P2）

**现象**

RTL 语言（阿拉伯 / 希伯来）需要右引号 `”`，英文撇号场景用 `‘` / `’`。

```python
# 当前代码（pipeline.py:1587）
f'<text ...>{_escape("“")}</text>'  # 只用左双引号
```

**修复方向**

```python
# 根据 quote 首字符类别选引号
quote_first = quote[0] if quote else ""
if unicodedata.bidirectional(quote_first) == "R":  # RTL
    open_q, close_q = "”", "“"  # 反转
elif quote_first in ("'", "‘"):  # English apostrophe scene
    open_q, close_q = "‘", "’"
else:
    open_q, close_q = "“", "”"
```

---

### Bug 14 — `two-column-compare` items 拼接错（`pipeline.py:1641`，P2）

**现象**

与 Bug 10 同模式：`_coerce_str_list` 默认 `"; "` 拼接，对比列项目应分行而非一行。

```python
# 当前代码（pipeline.py:1641）
parts.append(f'<text ...>{_escape(str(item))}</text>')
# item 是 list 时，被 coerce 成 "; item1; item2"
```

**修复方向**

```python
# items 已经是 list，不需要 coerce_str_list
if isinstance(side["items"], list):
    for j, item in enumerate(side["items"]):
        ty = by + 64 + j * 22
        if ty > by + bh - 8: break
        parts.append(...)
```

---

### Bug 15 — `_extract_first_json_object` 不处理单引号 JSON（`llm_client.py:236-237`，P2）

**现象**

小模型（Llama-3-8B 等）偶尔输出 `{ 'key': 'value' }` 单引号 JSON。当前解析器遇到单引号不进 string 状态，内部 `{` 会增加 depth，误匹配外层 `}`。

```python
# 当前代码（llm_client.py:236-237）
if ch == '"':
    in_string = True  # ← 只识别双引号
```

**修复方向**

```python
# 1. 决定 string delimiter 的方向：第一个引号字符
# 2. 或：让 prompt 更严格（要求双引号 JSON）+ 在 prompt 里加 example
# 3. 或：直接重试一次，告诉 LLM "请用双引号"
```

---

### Bug 16 — `_truncate_to_fit` 末尾标点剥离无 ellipsis 补回（`pipeline.py:734`，P2）

**现象**

LLM 输出"采购管理流程 8 个步骤包括:计划、审批..."被截到 `max_chars=14` → `"采购管理流程 8 个"`；末尾的 `:` 被剥掉但没补省略号 → 用户觉得数据损坏。

```python
# 当前代码（pipeline.py:734）
return truncated.rstrip(" ,.;:!?。；：、""''")
# 没有 append "..."
```

**修复方向**

```python
truncated = text[:max_chars]
truncated = truncated.rstrip(" ,.;:!?。；：、""''")
if len(truncated) < len(text):
    truncated = truncated + "..."  # ← 补回省略号
return truncated
```

---

### Bug 17 — `_DEFAULT_WINDOWS_SKILL_DIR` 用户名硬编码（`runner.py:40`，P2）

**现象**

`C:\Users\Administrator\` 是当前部署账户；其他 Windows 账户下安装的 skill 找不到。

```python
# 当前代码（runner.py:40）
_DEFAULT_WINDOWS_SKILL_DIR = Path(r"C:\Users\Administrator\.claude\skills\ppt-master")
```

**修复方向**

```python
# 删掉硬编码路径，完全交给 Path.home()
_DEFAULT_WINDOWS_SKILL_DIR = Path.home() / ".claude/skills/ppt-master"
_DEFAULT_POSIX_SKILL_DIR = Path.home() / ".claude/skills/ppt-master"
# 两个常量合并为一个
```

---

## 5. P3 低优先级 Bug（死代码 / 视觉细节）

### Bug 18 — `phase4_quality` 重复赋值 `authoring_dir`（`pipeline.py:1071`，P3）

```python
authoring_dir = workspace / "authoring-svg-flat"  # 1057 行
...
repaired = autofix.repair_workspace_svgs(authoring_dir)
restored = autofix.restore_shape_attrs(authoring_dir)
...
authoring_dir = workspace / "authoring-svg-flat"  # 1071 行重复
```

无害但应删除第二次赋值。

---

### Bug 19 — `flow-steps` 箭头被下一卡片遮挡（`pipeline.py:1482-1489`，P3）

箭头画在 `cx + step_w + gap/2`，下一卡片从 `cx + step_w + gap` 开始，箭头尖端在下一卡片矩形内部。

```text
[step1 rect][arrow][step2 rect][arrow][step3 rect]
                   ^ arrow 尖端被 step2 rect 覆盖
```

**修复方向**

```python
# 箭头从当前卡片右边 + 2px 开始，到下一卡片左边 - 2px 结束
ax_start = cx + step_w + 2
ax_end = cx + step_w + gap - 2
parts.append(f'<path d="M {ax_start:g} ... L {ax_end:g} ..." />')
```

---

### Bug 20 — `fix_invalid_source_ref` 仅 strip `<g>` 上的 source-ref（`autofix.py:876-879`，P3）

```python
pattern = re.compile(
    r'(<g\b[^>]*?\bdata-pptx-source-ref="slide:)(\d+)("[^>]*>)',
)
```

PPTX 规范允许任意元素带 `data-pptx-source-ref`，但当前正则仅匹配 `<g>`。少数 vendor 输出在 `<rect>` / `<text>` 上带 source-ref，strip 不到。

**修复方向**

```python
# 去掉开头的 <g\b
pattern = re.compile(
    r'(\bdata-pptx-source-ref="slide:)(\d+)(")',
)
```

---

### Bug 21 — `_detect_skeleton_kind` cover/toc 启发式不完整（`llm_planner.py:488-498`，P3）

注释里提"first slide, large title slot"但代码只检查 `text_element_range=(4, 8)`，没检查字号。

**修复方向**

补上字号信号（与 Bug 02 修复方向合并处理）。

---

## 6. 修复顺序建议（按依赖关系）

| 阶段 | 涉及 Bug | 工作量 | 前置依赖 |
|---|---|---|---|
| **Phase C1** — 底层基础类 | Bug 01（画布）、Bug 02（skeleton 阈值） | ~60 行 + 5 测试 | 无 |
| **Phase C2** — 状态机/检测类 | Bug 08（ending fallback）、Bug 09（死代码）、Bug 21（cover 启发式） | ~25 行 + 3 测试 | C1 |
| **Phase C3** — 渲染 layout 类 | Bug 03（timeline CJK）、Bug 05（cards 溢出）、Bug 11（shrink 下限）、Bug 12（hero-number 短 bounds） | ~30 行 + 4 测试 | C1 |
| **Phase C4** — 边缘鲁棒性 | Bug 04（picture href）、Bug 06（Chapter N）、Bug 07（TOC 锚点）、Bug 10（revision list）、Bug 13（引号）、Bug 14（compare list）、Bug 15（单引号 JSON）、Bug 16（ellipsis） | ~70 行 + 8 测试 | 无 |
| **Phase C5** — 清理类 | Bug 17（Windows 用户名）、Bug 18（死代码）、Bug 19（箭头遮挡）、Bug 20（source-ref 范围） | ~15 行 + 2 测试 | 无 |

**强烈建议**：C4 的 8 个 bug 严重依赖"先写失败测试再修"，否则回归风险高。

---

## 7. 跨模板影响评估

| 模板场景 | 受影响 Bug |
|---|---|
| **boteng 16:9（当前唯一实测）** | 全部 21 个，均需验证 |
| **1920×1080 16:9** | 01（画布阈值）、05（cards 溢出）、07（TOC 锚点）、12（hero-number） |
| **9:16 竖版** | 01、05、12 |
| **4:3 1024×768** | 01、12 |
| **英文模板** | 03（CJK margin 反向：Latin 内容居中估算偏大）、06（Chapter N 误删） |
| **RTL 语言** | 13（引号方向）、19（箭头方向） |
| **CJK 密集模板（所有）** | 03、10、14 |
| **含子目录 media 的模板** | 04 |

**重点关注**：01（画布阈值）和 02（skeleton 检测）—— 这两个 bug 是 boteng 模板"碰巧通过"的硬编码值，换任何其他模板都会失效。

---

## 8. 验证证据（本任务不执行）

为每类 Bug 建议验证方法（后续 Phase C/D 实施时使用）：

| Bug | 验证方法 | 断言 |
|---|---|---|
| 01, 12 | 用 1920×1080 boteng 派生模板跑端到端 | svg_to_pptx 不再 abort；hero-number 数字在 bounds 内 |
| 02, 08, 21 | 用 4 个不同模板（cover 在第 2 张 / 无 ending / 致谢非最后 / 字号 38pt divider）跑 detect | 骨架分类正确 |
| 03, 10, 14 | 构造"含 7 CJK 字符 timeline detail" / "20 items 列表" / "5 CJK 对比项" fixture | 渲染后文本不超 bounds |
| 04 | 用真实 boteng 含 `<image xlink:href="media/foo.png"/>` 的 SVG | flat→nested 转换成功 |
| 05 | 构造 20 items fixture | 卡片末项 y < by + bh |
| 06 | 用英文模板"Chapter 1/2/3"短标题 | 目录行不被删 |
| 07 | 用紧凑型模板（title 18pt、行间距 8px） | TOC 行清理只删空行，不误伤 |
| 09 | 删除 phase4_quality 死代码后跑 e2e | 无 regression |
| 11 | 用 LLM 输出超长文本 fixture | shrink 3 轮后字号 ≥ 8pt，否则 truncate |
| 13 | 用阿拉伯文 quote 文本 | 引号方向正确（右引号开头） |
| 15 | 喂小模型 `{ 'key': 'val' }` 输出 | 不抛 LLMError，落到 balanced-brace 回退 |
| 16 | 用 `"采购管理流程 8 个步骤包括:计划"` fixture | truncate 后末尾为 `"…"` |
| 17 | 在 `C:\Users\OtherUser\.claude\skills\ppt-master` 安装 skill | resolve_skill_dir 找到 |
| 19 | 视觉确认 flow-steps | 箭头不被下一卡片覆盖 |

---

## 9. 引用与延伸阅读

- **`docs/BUGS_FOUND_2026-09-13.md`** — 端到端用户反馈驱动的 3 个 bug（已修）。
- **`docs/PHASE_B_NATIVE_FILL_FIXES_2026-09-13.md`** — 上一批 bug 的修复记录（与本文互补）。
- **`docs/PHASE_C_NATIVE_FILL_FIXES_2026-09-14.md`** — **待建**：本批 21 个 bug 的修复追踪文档（按 §6 Phase C1-C5 顺序新建）。
- **`docs/NATIVE_FILL_PIPELINE_GUIDE.md` §6** — autofix 设计意图（本批 bug 主要偏离此处：阈值未模板无关化）。
- **`docs/PPTX_MASTER_EDIT_NATIVE_WALKTHROUGH.md`** — ppt-master edit-native 工作流走查（确认 TOC 空白等问题在 ppt-master 端未解决，需本项目自己处理）。
- **`src/mcp_ppt_native_fill/text_width.py`** — `chars_that_fit` / `estimate_text_width` 已被 Bug 03 修复方向引用。
- **`src/mcp_ppt_native_fill/pipeline.py::_render_new_block`** — 7 个 layout 的几何与文本渲染（本批 bug 主要集中在 §4 §5）。
- **`src/mcp_ppt_native_fill/llm_planner.py::_detect_skeleton_kind`** — 模板分类启发式（Bug 02 主战场）。

---

**审查完成时间**：2026-09-14
**审查者**：`/dev-expert` 子技能（claude-sonnet-5）
**本文档定位**：诊断 + 优先级 + 修复方向。**不包含实际代码改动**——所有 P0-P3 修复需在 Phase C/D 单独实施并配套失败测试留仓。