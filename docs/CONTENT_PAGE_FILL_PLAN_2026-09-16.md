# PPT 内容页填充改进计划

**日期**：2026-09-16
**状态**：待执行
**作者**：Claude（基于 9/16 内容页诊断）
**目标分支**：`integration/sink-generate-local-ppt-2026-09-14`

---

## 1. 目标

让 native_fill 生成的 PPT 内容页**看起来有内容**，而不是「1 张 60 字截断卡片 + 大量留白」。

### 验收标准（可量化）

| 指标 | 当前（基线） | 目标 |
|---|---|---|
| boteng markdown 单页 text_run 数（中位数） | 10（Phase 0 修正：plan 原写 5 漏算模板页眉 5 项） | ≥ 15 |
| boteng markdown 单页呈现的卡片数 | 1（5/6 页） | ≥ 2 |
| boteng markdown 内容页元素 bounding box 覆盖率（客观） | 100%（5/6 页，**实际意义不大**——单 rect 撑满） | ≥ 70%（=所有 SVG 元素总 bbox 面积 / body_bounds 1060×480 面积） |
| boteng markdown 内容页 **body_cards 组内 text_nodes**（**Phase 0 新增指标**） | 2（5/6 页，slide14 例外为 24） | ≥ 5 |
| boteng markdown 内容页空白感（用户主观） | 严重 | 视觉填满，无大面积留白 |
| `examples/test_toc_4_vs_7.py` 内容页 text_run 数 | 10（Phase 0 修正：plan 原写 14 是 toc_4 slide6 不是 slide5） | ≥ 10（不破回归） |
| 现有 166 单元测试 | 通过 | 全部通过 |
| 新增测试 | — | ≥ 8 个 |

**覆盖率度量脚本**（Phase 1 步骤 1.5 之前先创建，见 Phase 0）：

```python
# scripts/inspect_pptx.py — 解压 + 数 SVG 元素 bounding box
import zipfile, re
def coverage(svg_text, body_bounds="120 130 1060 480"):
    bx, by, bw, bh = (float(t) for t in body_bounds.split())
    total_area = bw * bh
    rects = re.findall(r'<(?:rect|path)[^>]*?x="([\d.]+)"[^>]*?y="([\d.]+)"[^>]*?width="([\d.]+)"[^>]*?height="([\d.]+)"', svg_text)
    filled = sum(float(w)*float(h) for _,_,w,h in rects)
    return filled / total_area
```

---

## 2. 现状（已诊断）

### 2.1 数据
| 内容页（H1） | 当前 text_run 数 | 实际呈现 |
|---|---|---|
| 前言 | 5 | 1 张「要点」卡片（60 字截断） |
| 一、目的 | 5 | 同上 |
| 二、适用范围 | 5 | 同上 |
| 三、基本原则 | 5 | 同上 |
| 四、工作程序 | 27 | 3 张卡片（恰好因为含顶层编号项） |
| 附件 | 5 | 1 张「要点」卡片 |

5/6 页只有 1 张卡片 + 60 字截断 → 用户视觉上「内容页没有内容」。

### 2.2 根因（`toc_detection.py:cards_from_body`）
1. **60 字截断过狠**（line 609）：段落超过 60 字直接 `+ "…"`，长说明（如「前言」原文 100+ 字）只剩一句话头
2. **不识别 H2 子节**：「四、工作程序」下的 7 个 `## （一）（二）...` 子节被当作段落文本，未提取其下编号项
3. **`paragraphs[1:]` 内部编号项未遍历**：`body.split("\n\n")` 拆段后，只看了第二段起的整段文本，未逐行匹配 `1. xxx` 列表项

### 2.3 设计哲学参考（来自 `references/strategist.md` 与 `references/executor-structured.md`）
ppt-master 的核心洞察：**「约束输入是结构化的，渲染器只负责忠实呈现」**。具体体现：
- markdown 本身就是大纲，每个 H1 = 一个 Slide block（`project_specs.py:876` 校验）
- `page_layouts` 表声明每页用哪个模板 SVG
- 不做自动内容提取，不做启发式排版推断

---

## 3. 方案设计（E + A 双轨）

### 3.1 方案 E：结构化 markdown 约定（正路）

**思路**：在 markdown 写法上加约定，让 PPT 大纲显式声明每页「说什么、怎么排」。渲染器只负责照做。

#### 3.1.1 新约定语法

在每个 H1 章节下，用 `>` 引用块声明本页元数据：

```markdown
## 一、目的
> **本页要点**：采购效率、岗位职责、成本控制、流程规范
> **采用布局**：hero-number
> **建议视觉**：标题居中 + 下方大字说明

1. 为了提高公司采购效率……

## 二、适用范围
> **本页要点**：设备/物料采购、研发/生产覆盖
> **采用布局**：bullet-list
> **建议视觉**：左侧色块 + 右侧编号要点

1. 适用于本公司……

## 四、工作程序
> **本页要点**：7 大环节
> **采用布局**：flow-steps
> **建议视觉**：左侧环节标题，右侧编号要点展开

## （一）采购基本事项
……
```

#### 3.1.2 渲染器扩展（`block_renderer.py`）

新增 3 个 layout：

| layout | 适用场景 | 实现要点 |
|---|---|---|
| `hero-number` | 单段说明、范围/目的类 | 1 大字数字（如「3 大目标」）+ 1 段说明文字 |
| `bullet-list` | 编号要点、1-7 项 | 左侧色块 + 右侧垂直编号列表（最多 10 项，超出滚动） |
| `simple-text` | 单段无要点 | 1 张大卡片 + 24pt 字号多行自动换行 |
| `flow-steps`（已有） | 步骤流程 | 不变 |

**三个新 layout 伪代码骨架**（Phase 3 实现参考）：

```python
# block_renderer.py:_render_simple_text
def _render_simple_text(spec, bounds):
    """1 张大卡片 + 24pt 自动换行。
    关键:按 chinese-width = font_size * 1.0 估算每行字数,超宽插入 <text> 分片。
    """
    x, y, w, h = parse_bounds(bounds)
    padding = 24
    inner_w = w - 2 * padding
    font_size = 24
    chars_per_line = int(inner_w / (font_size * 1.0))  # 中文等宽近似
    text = spec["text"]
    lines = [text[i:i+chars_per_line] for i in range(0, len(text), chars_per_line)]
    svg = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#1D2CAB" fill-opacity="0.08" />']
    for i, line in enumerate(lines):
        svg.append(f'<text x="{x+padding}" y="{y+padding+font_size*(i+1)}" font-size="{font_size}" fill="#222">{escape(line)}</text>')
    return "\n".join(svg)

# block_renderer.py:_render_bullet_list
def _render_bullet_list(spec, bounds):
    """左侧色块 + 右侧垂直编号列表。
    items 限 10 项;每项 1 个 <text>,行高 30px。
    """
    x, y, w, h = parse_bounds(bounds)
    items = spec["items"][:10]
    color = spec.get("color", "#1D2CAB")
    line_h = min(30, (h - 40) / max(1, len(items)))
    svg = [f'<rect x="{x}" y="{y}" width="6" height="{h}" fill="{color}" />']  # 左侧色块
    for i, item in enumerate(items):
        cy = y + 30 + line_h * i
        svg.append(f'<text x="{x+24}" y="{cy}" font-size="16" fill="#222">{i+1}. {escape(item)}</text>')
    return "\n".join(svg)

# block_renderer.py:_render_hero_number
def _render_hero_number(spec, bounds):
    """1 大字数字 + 1 段说明。
    数字字号 96pt,说明 18pt。
    """
    x, y, w, h = parse_bounds(bounds)
    n = spec.get("number", "3")
    text = spec.get("text", "")
    color = spec.get("color", "#1D2CAB")
    svg = [
        f'<text x="{x+w/2}" y="{y+h*0.45}" text-anchor="middle" font-size="96" font-weight="bold" fill="{color}">{escape(n)}</text>',
        f'<text x="{x+w/2}" y="{y+h*0.75}" text-anchor="middle" font-size="18" fill="#404040">{escape(text[:200])}</text>',
    ]
    return "\n".join(svg)
```

**实现注意**：
- 三个 layout 共用 `bounds="120 130 1060 480"`（与 Phase 0.1 基线一致）
- 颜色继承现有 `colors = ["#1D2CAB", "#EE822F", "#75BD42"]`
- 字体继承 `font-family="思源黑体 CN Bold"`（与模板 SVG `<svg>` 根的 font-family 一致）
- 行高/字号参考 Phase C3 的 `MIN_READABLE_FONT = 14`、`fix_text_overflow` 防止越界

#### 3.1.3 解析器扩展（`toc_detection.py:split_markdown_sections`）

在现有 title/body 提取基础上，新增 `meta` 字段：

```python
section = {
    "title": title,
    "body": body,
    "meta": {
        "items": ["采购效率", "岗位职责", ...],   # 来自"本页要点"
        "layout": "hero-number",                  # 来自"采用布局"
        "visual_hint": "标题居中 + ...",          # 来自"建议视觉"
    }
}
```

提取规则（正则）：

**格式约束（强制）**：
- 必须用中文全角冒号 `：`（不是半角 `:`）
- 冒号紧贴 `**字段名**`（不允许 `**字段名** ：` 或 `**字段名** :`）
- 字段名仅 3 个白名单值：`本页要点` / `采用布局` / `建议视觉`
- 值字段内允许中英文标点

```python
META_RE = re.compile(
    r"^>\s*\*\*(本页要点|采用布局|建议视觉)\*\*：(.+?)\s*$",
    re.MULTILINE
)
for m in META_RE.finditer(body):
    key = m.group(1)
    value = m.group(2).strip()
    if key == "本页要点":
        section["meta"]["items"] = [
            s.strip() for s in re.split(r"[、,，;；]", value) if s.strip()
        ]
    elif key == "采用布局":
        section["meta"]["layout"] = value
    elif key == "建议视觉":
        section["meta"]["visual_hint"] = value
```

**剥离 meta 行**（避免下游把 `> **本页要点**：...` 当作普通 blockquote 重复处理）：

```python
# 在 split_markdown_sections 返回前
body = META_RE.sub("", body).strip()
```

**测试覆盖要求**：
- ✅ `> **本页要点**：x、y` 接受
- ✅ `> **本页要点**： x`（冒号后空格）接受
- ❌ `> **本页要点**: x`（半角冒号）拒绝
- ❌ `> **本页要点** ：x`（冒号前空格）拒绝
- ❌ `> **其他字段**：x`（非白名单字段）拒绝
- ✅ meta 行被剥离后 body 中不应残留 `> **xxx**`

#### 3.1.4 调用链变更（`workspace_expand.py` + `toc_detection.py`）

**签名变更**：`_cards_for_section` 从返回 `list[dict]` 改为返回 `(cards, meta)` 元组，让调用方明确知道 meta 是否存在：

```python
# toc_detection.py
def cards_for_section(sections, stem) -> tuple[list[dict], dict]:
    """Returns (cards, meta). meta is empty dict when no E outline declared."""
    # ... existing logic ...
    section = sections[idx] if idx in range else {}
    meta = section.get("meta", {})
    return cards, meta
```

`expand_workspace_from_markdown` 调用约定（line 177 附近）：

```python
cards, meta = _cards_for_section(sections, stem)
if meta.get("items"):
    # E 路径：用显式大纲
    spec = {
        "layout": meta.get("layout", "bullet-list"),
        "spec": {
            "items": meta["items"],
            "text": section["body"][:300] if meta.get("layout") == "simple-text" else None,
        },
        "bounds": body_bounds,
    }
else:
    # A 路径：用现有 cards_from_body 启发式
    spec = {"layout": layout, "spec": {"cards": cards}, "bounds": body_bounds}
```

**同步更新 `fill_missing_content_blocks`**（`toc_detection.py:642`）：

```python
# 旧调用: cards = cards_for_section(sections, stem)
# 新调用:
cards, meta = _cards_for_section(sections, stem)
# meta 在 fill_missing_content_blocks 路径下也生效：
# - 有 meta → 用 E 路径生成的 spec（已通过 _format 处理）
# - 无 meta → fallback 到原 cards（保持原行为）
```

**回归测试**：`tests/test_native_fill.py:2299/2331/2349` 三处 `_fill_missing_content_blocks` 调用必须加 meta-aware 断言（meta 字段不丢失、spec 结构与原 cards 路径等价）。

### 3.2 方案 A：cards_from_body 改进（兜底）

**思路**：即使 markdown 没按 E 约定写，也要让内容页不那么空。保留启发式但修复 3 个问题。

#### 3.2.1 解除 60 字截断（line 609）

```python
# Before:
"items": [first[:60] + ("…" if len(first) > 60 else "")],

# After:
MAX_FIRST_CARD = 200
"items": [first[:MAX_FIRST_CARD] + ("…" if len(first) > MAX_FIRST_CARD else "")],
# 渲染器侧：simple-text/bullet-list 自动按字体宽度换行
```

**验证（Phase 1 阶段）**：单页 text_run 维持 5（**不期望增加**）——Phase 1 只改截断长度，渲染器侧换行节点要在 Phase 3 才会出现；本阶段验证不破基线即可。Phase 3 后通过 `simple-text` 渲染器实现自动换行，text_run 数才会增长到 ≥ 15。

#### 3.2.2 识别 H2 子节作为「子项」卡片

```python
h2_re = re.compile(r"^##\s+(.+)$")
sub_cards: list[dict] = []
for p in paragraphs[1:]:
    h2m = h2_re.match(p)
    if not h2m:
        continue
    sub_title = h2m.group(1).strip()
    if not sub_title or sub_title.startswith("**"):
        sub_title = sub_title.replace("*", "").strip()
    # Collect numbered items below this H2
    items = []
    for line in p.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lm = list_re.match(line)
        if lm:
            items.append(lm.group(2).strip()[:30] + ("…" if len(lm.group(2).strip()) > 30 else ""))
    if items:
        sub_cards.append({
            "title": sub_title[:12],   # 放宽到 12：容纳"（七）采购经办人行为规范"
            "color": colors[len(cards) % 3],
            "items": items[:4],
        })

cards.extend(sub_cards[:2])  # 最多 2 个 H2 子卡片
```

#### 3.2.3 段落内部编号项递归收集

```python
# Before（line 614-620）: 只对 paragraphs[1:] 的整段做行匹配
# After: 对所有 paragraphs（包括 paragraphs[0]）的内部编号项都扫描
list_items: list[str] = []
for p in paragraphs:
    for line in p.splitlines():
        line = line.strip()
        lm = list_re.match(line)
        if lm and lm.group(2).strip() not in list_items:
            list_items.append(lm.group(2).strip())
```

**注意**：要排除 H2 标题行（已在 3.2.2 中处理），避免「（一）采购基本事项」被当作 item。

#### 3.2.4 单卡片回退改用段落块

当 `cards_from_body` 只产出 1 张「要点」卡片时，**切换到 `simple-text` layout**（在 `expand_workspace_from_markdown` 中判断）：

```python
if len(cards) == 1:
    spec = {
        "layout": "simple-text",
        "spec": {"text": section["body"][:300]},
        "bounds": body_bounds,
    }
else:
    spec = {
        "layout": layout,
        "spec": {"cards": cards},
        "bounds": body_bounds,
    }
```

---

## 4. 实施步骤（按 Phase 推进）

### Phase 0：前置检查（0.5h，必须先完成）

| Step | 任务 | 验收 |
|---|---|---|
| 0.1 | **基线记录**：跑 `examples/test_toc_4_vs_7.py` + `examples/smart_toc_fill.py`，解压新 pptx 记录每个内容页的 text_run 数（不修改任何文件）。**基线值见下表**。 | 文档化基线 |
| 0.2 | **创建 `scripts/inspect_pptx.py`**：解压 pptx、提取 `<a:t>` 文本、计算 SVG 元素 bbox 覆盖率（实现见 §1 验收标准的脚本框）。`if __name__ == "__main__"` 接受 `pptx_path` 参数 | 脚本可跑，输出覆盖率 |
| 0.3 | **同步检查 `fill_missing_content_blocks`**：grep 它对 `cards_for_section` 的调用，确认新签名 `(cards, meta)` 元组返回值能无缝接入（line 685 附近 `cards = cards_for_section(...)` 需改为 `(cards, meta) = ...`） | 改前 diff 截图留底 |

**基线值（必填，作为 Phase 1+ 的回归参考）**（2026-09-16 跑出）：

| pptx | slide (pptx_idx) | H1 | text_runs (整页) | bbox coverage | text_nodes in body_cards |
|---|---|---|---|---|---|
| `projects/smart_toc_boteng_out.pptx` | slide6 | 前言 | 10 | 100.0% | 2 |
| `projects/smart_toc_boteng_out.pptx` | slide8 | 一、目的 | 10 | 100.0% | 2 |
| `projects/smart_toc_boteng_out.pptx` | slide14 | 四、工作程序 | 32 | 97.0% | 24 |
| `projects/toc_4_test_out.pptx` | slide6（修正：plan §4 原写 slide5 是错的，slide5 是 PART 01 divider，不是内容页） | 第一章 项目管理 | 10 | 100.0% | 2 |

**度量脚本**：`scripts/inspect_pptx.py <pptx> --workspace <dir> --slides 6,8,14`

**关键洞察**：
- text_runs 是**整页**的 `<a:t>` 数（包括模板页眉 "高效协同 开放坦诚…" 在每个内容页贡献 5 个）——不是 plan §1 假设的「卡片内 text_run 数」
- **真实"内容密度"指标是 `text_nodes in body_cards`**（`body_cards` 组内的 `<text>` 节点数）
- 6 个内容页里有 5 个只有 2 个 text_nodes（标题 + 1 段截断文本）——这就是"视觉空洞"的真实度量
- bbox coverage 100% 不可作为"内容密集"指标：单个全屏半透明 rect 就是 100%，里面可能只有 1 个 text 节点
- Phase 1/3 应**同时跟踪 text_runs (整页) 和 text_nodes in body_cards 两个指标**

**基线中位数**：text_runs (整页) = 10，text_nodes in body_cards = 2。

### Phase 1：方案 A 兜底（0.5h，半天内可验证）

| Step | 任务 | 文件 | 预计时间 |
|---|---|---|---|
| 1.1 | 改 `cards_from_body`：200 字截断 | `src/mcp_ppt_native_fill/toc_detection.py:609` | 5 分钟 |
| 1.2 | 改 `cards_from_body`：H2 子节识别（`sub_title[:12]`） | `src/mcp_ppt_native_fill/toc_detection.py:590-639` | 20 分钟 |
| 1.3 | 改 `cards_from_body`：段落内部编号项递归 | 同上 | 10 分钟 |
| 1.4 | `expand_workspace_from_markdown` 加单卡片 fallback → simple-text | `src/mcp_ppt_native_fill/workspace_expand.py:189` | 10 分钟 |
| 1.5 | 重跑 `smart_toc_fill.py` + `inspect_pptx.py` + 写对比日志 | — | **25 分钟**（含 5 分钟跑脚本、5 分钟解包、10 分钟写对比、5 分钟 review） |

**阶段验证**（对照基线，Phase 1 不期望 text_run 增长）：
- 166 单元测试仍通过
- boteng 单页 text_run **维持 5（不退化）**——Phase 1 只动 toc_detection 截断，渲染器换行要等 Phase 3
- boteng 单页呈现卡片数 ≥ 2（中位数，**A 路径可达成**）
- boteng 单页元素覆盖率 ≥ 50%（**A 路径目标**；≥ 70% 是 Phase 3 的目标）
- `toc_4_test_out.pptx` slide5 text_run ≥ 14（不破回归）

### Phase 2：方案 E 解析器（1 天）

| Step | 任务 | 文件 | 预计时间 |
|---|---|---|---|
| 2.1 | `split_markdown_sections` 加 `meta` 字段 | `src/mcp_ppt_native_fill/toc_detection.py:543-560` | 1 小时 |
| 2.2 | `cards_for_section` 优先用 `meta.items` | `src/mcp_ppt_native_fill/toc_detection.py:563-587` | 1 小时 |
| 2.3 | 单元测试：META_RE 提取、`cards_for_section` 优先 meta | `tests/test_native_fill.py`（新 TestSectionMeta 类） | 2 小时 |
| 2.4 | 文档：`docs/STRUCTURED_MARKDOWN_OUTLINE_GUIDE.md` | 新文件 | 1 小时 |

**阶段验证**：
- 单元测试：旧 166 + 新 ≥ 6 个
- `examples/smart_toc_fill.py` 跑通（用未结构化 markdown 应走 fallback 路径）

### Phase 3：方案 E 渲染器（1 天）

| Step | 任务 | 文件 | 预计时间 |
|---|---|---|---|
| 3.1 | `block_renderer.py` 加 `simple-text` layout | `src/mcp_ppt_native_fill/block_renderer.py:65-` | 2 小时 |
| 3.2 | `block_renderer.py` 加 `bullet-list` layout | 同上 | 2 小时 |
| 3.3 | `block_renderer.py` 加 `hero-number` layout | 同上 | 1 小时 |
| 3.4 | 单元测试：3 个新 layout 各 2 个 case（共 6 个） | `tests/test_native_fill.py` | 2 小时 |
| 3.5 | `workspace_expand.py` 把 `meta.layout` 串到 `spec.layout` | `src/mcp_ppt_native_fill/workspace_expand.py:189` | 30 分钟 |

**阶段验证**（Phase 3 是 text_run 真正增长的阶段）：
- 单元测试：累计 ≥ 14 个新增（Phase 1 ≥ 5 + Phase 2 ≥ 3 + Phase 3 ≥ 6 = 14；与 §5.1 表格 4+3+2+2+2+1 严格对齐）
- **boteng 单页 text_run ≥ 15（中位数），覆盖率 ≥ 70%**（**Phase 3 渲染器加入后此目标才可达**；Phase 1 只验证不退化）
- 用结构化 markdown 跑 `examples/structured_markdown_demo.py`（新建）验证视觉

### Phase 4：文档与回归（0.5 天）

| Step | 任务 | 文件 | 预计时间 |
|---|---|---|---|
| 4.1 | 更新 boteng markdown 加 E 约定 | `3山西柏腾科技有限公司采购制度.md`（备份原文件为 `*_original.md`） | 1 小时 |
| 4.2 | 跑 `smart_toc_fill.py` 生成新 pptx | `projects/smart_toc_boteng_out.pptx` | 10 分钟 |
| 4.3 | 对比新旧 pptx（text_run 数、视觉） | — | 30 分钟 |
| 4.4 | 跑 `test_toc_4_vs_7.py` 回归 | `projects/toc_4_test_out.pptx` + `toc_7_test_out.pptx` | 10 分钟 |
| 4.5 | 更新 `README.md` 与 `SKILL.md`：新增「结构化 markdown 用法」章节（README）、新增 expand_*_layout 选项说明（SKILL） | `README.md` + `SKILL.md` | 30 分钟 |
| 4.6 | commit：3 次提交（commit 1: Phase 1 A 兜底；commit 2: Phase 2+3 E 完整实现；commit 3: Phase 4 docs + boteng markdown 更新 + demo） | — | 10 分钟 |

**总计：2–2.5 天**（含 Phase 0）

---

## 5. 测试策略

### 5.1 单元测试（新增 ≥ 14 个）

| 测试类 | 测试方法 | 覆盖 |
|---|---|---|
| `TestCardsFromBodyImprovements` | 4 个 | 200 字截断 / H2 子节 / 段落内部编号 / 单卡 fallback |
| `TestSplitMarkdownSectionsMeta` | 3 个 | META_RE 提取 / 多个 meta 字段 / 无 meta 时为空 |
| `TestBlockRendererSimpleText` | 2 个 | 短文本 / 超长文本换行 |
| `TestBlockRendererBulletList` | 2 个 | 5 项 / 10 项上限 |
| `TestBlockRendererHeroNumber` | 2 个 | 单数字 / 数字+说明 |
| `TestExpandWorkspaceMarkdownMeta` | 1 个 | meta.items 优先于 cards_from_body |

### 5.2 端到端测试（保留 3 个）

| 文件 | 验证 |
|---|---|
| `examples/smart_toc_fill.py` | boteng markdown 全流程跑通，无 regression |
| `examples/test_toc_4_vs_7.py` | 4/7 章节 TOC 场景仍 `ok=true` |
| `examples/structured_markdown_demo.py`（新建） | 用结构化 markdown 验证 E 路径 |

### 5.3 视觉对比（手动）

| 对比项 | 方法 |
|---|---|
| 文本密度 | `python scripts/inspect_pptx.py` 对比 text_run 数（中位数） |
| 视觉填满度 | 截图肉眼 review |
| 排版正确性 | 用 LibreOffice 打开 pptx 翻页检查 |

---

## 6. 风险与回滚

### 6.1 风险矩阵

| 风险 | 可能性 | 影响 | 缓解 |
|---|---|---|---|
| `cards_from_body` 改动破坏现有测试 | 中 | 中 | 单元测试先加 case 再改代码 |
| 新 layout 越界（超出 body_bounds） | 中 | 中 | 借鉴 Phase C3 的 Bug 05/12 修复（`fix_text_overflow` + `MIN_READABLE_FONT`） |
| 结构化 markdown 解析崩溃 | 低 | 中 | META_RE 容错（缺字段不报错，meta 默认为空） |
| LLM 大纲路径冲突 | 极低 | 低 | E 是独立 opt-in，旧路径完全不变 |
| boteng 模板 shape-3 与新内容重叠 | 中 | 低 | 新 layout 的 `bounds` 复用 `body_bounds="120 130 1060 480"`，与 shape-3 位置一致 |

### 6.2 回滚策略

| Phase | 回滚方式 |
|---|---|
| Phase 1 | `git revert` 单 commit，cards_from_body 改动小，影响面有限 |
| Phase 2 | `git revert` 单 commit；旧 markdown 不受影响（无 meta 走 fallback） |
| Phase 3 | `git revert` 单 commit；旧 markdown 不受影响（默认 3-column-cards 保留） |
| Phase 4 | 备份原 markdown 为 `*_original.md`，新 markdown 保留原文件名（**不要**给新文件加后缀，避免路径引用断裂） |

### 6.3 安全红线

- ✅ 不修改 `examples/test_toc_4_vs_7.py` 的预期结果
- ✅ 不修改 166 个单元测试的既有断言
- ✅ 不修改 ppt-master 兼容接口（`svg_to_pptx` 接受新 layout）

---

## 7. 进度追踪

- [ ] **Phase 0：前置检查**（0.5h，必须先完成）
  - [ ] 0.1 跑基线 + 文档化 text_run 数（覆盖 slide6/8/14 + toc_4 slide5）
  - [ ] 0.2 创建 `scripts/inspect_pptx.py`
  - [ ] 0.3 同步检查 `fill_missing_content_blocks`
- [ ] **Phase 1：方案 A 兜底**（0.5h）
  - [ ] 1.1 200 字截断
  - [ ] 1.2 H2 子节识别（sub_title[:12]）
  - [ ] 1.3 段落内部编号递归
  - [ ] 1.4 单卡 fallback → simple-text
  - [ ] 1.5 重跑 `smart_toc_fill.py` + inspect_pptx.py + 写对比日志（25 分钟）
- [ ] **Phase 2：方案 E 解析器**（1 天）
  - [ ] 2.1 `split_markdown_sections` 加 meta 字段 + 剥离 meta 行
  - [ ] 2.2 `cards_for_section` 签名改为 `(cards, meta)` 元组
  - [ ] 2.3 单元测试（≥ 3 个，含 META_RE 格式矩阵；与 §5.1 TestSplitMarkdownSectionsMeta 对齐）
  - [ ] 2.4 `docs/STRUCTURED_MARKDOWN_OUTLINE_GUIDE.md`
- [ ] **Phase 3：方案 E 渲染器**（1 天）
  - [ ] 3.1 `simple-text` layout
  - [ ] 3.2 `bullet-list` layout
  - [ ] 3.3 `hero-number` layout
  - [ ] 3.4 单元测试（≥ 6 个，3 个新 layout 各 2 个 case；与 §5.1 对齐）
  - [ ] 3.5 `workspace_expand.py` 串联
- [ ] **Phase 4：文档与回归**（0.5 天）
  - [ ] 4.1 更新 boteng markdown（备份原文件）
  - [ ] 4.2 重生成 pptx
  - [ ] 4.3 视觉对比
  - [ ] 4.4 跑回归
  - [ ] 4.5 更新 README/SKILL（明确范围）
  - [ ] 4.6 commit（3 次：Phase 1 / Phase 2+3 / Phase 4）

---

## 8. 相关文件

| 路径 | 角色 |
|---|---|
| `src/mcp_ppt_native_fill/toc_detection.py` | `cards_from_body`、`split_markdown_sections`、`cards_for_section` |
| `src/mcp_ppt_native_fill/workspace_expand.py` | `expand_workspace_from_markdown`（spec 组装） |
| `src/mcp_ppt_native_fill/block_renderer.py` | `render_new_block`（layout 渲染） |
| `src/mcp_ppt_native_fill/svg_edits.py` | `write_new_content_block`（SVG 注入） |
| `tests/test_native_fill.py` | 单元测试入口 |
| `examples/smart_toc_fill.py` | 主验证脚本 |
| `examples/test_toc_4_vs_7.py` | TOC 场景回归 |
| `3山西柏腾科技有限公司采购制度.md` | boteng 测试 markdown |
| `projects/smart_toc_boteng_out.pptx` | 主验证产物 |

---

## 9. 参考资料

> **路径说明**：ppt-master 在本机安装于 `C:\Users\Administrator\.claude\skills\ppt-master\`，下表使用绝对路径以便复现。

- ppt-master `C:\Users\Administrator\.claude\skills\ppt-master\references\strategist.md` — 大纲即 markdown 哲学
- ppt-master `C:\Users\Administrator\.claude\skills\ppt-master\references\executor-structured.md` — mirror 与 layout 模式
- ppt-master `C:\Users\Administrator\.claude\skills\ppt-master\scripts\project_management\project_specs.py:876` — 「heading 必须对应 Slide block」校验
- 本仓库 `docs/PHASE5_SOURCE_REF_FIX_2026-09-15.md` — 改 toc_detection 的最近一次实践参考
- 本仓库 `docs/PHASE6_TOC_CARRIER_FIX_2026-09-15.md` — 最近一次内容相关修改

---

## 10. 决策记录

| 日期 | 决策 | 理由 | 撤销条件 |
|---|---|---|---|
| 2026-09-16 | 选 E + A 双轨，不选 D（LLM 大纲生成） | boteng markdown 已经是半结构化（每节 H1 明确），LLM 提炼大纲的边际价值低；E 路径可控、可重现、无外部依赖，对齐 ppt-master「约束输入」哲学；A 是 E 未采用时的渐进回退，保证零回归 | 若 E 实施中发现解析路径太脆，或业务要求支持完全自由文本（如未结构化的演讲稿） |
| 2026-09-16 | E 是 opt-in，旧路径完全保留 | 不破现有 166 测试；无 E 元数据的 markdown 走 A 启发式路径，行为不退化 | — |
| 2026-09-16 | 不做 P2 mirror 模式 | mirror 模式需要修改 `C:\Users\Administrator\.claude\skills\ppt-master\scripts\svg_to_pptx\svg_to_pptx.py` 的源码（让 vendor 接受 `data-pptx-placeholder="body"`），超出本仓库修改范围；E + A 已能解决 80% 视觉空问题；剩余 20% 接受作为已知限制 | 若后续需要支持更多模板原生占位符，且 svg_to_pptx 上游愿意配合 |
| 2026-09-16 | `cards_for_section` 签名改为 `(cards, meta) -> tuple` | 让调用方明确知道 meta 是否存在，避免误用 A fallback 覆盖 E 路径；破坏性变更只影响本仓库调用方（`workspace_expand.py` 和 `fill_missing_content_blocks`），可控 | 若 Phase 2 单元测试发现下游误用超过 3 处 |
| 2026-09-16 | `META_RE` 强制中文全角冒号 `：` | 与 markdown 中文文档惯例一致；半角冒号太容易和正常 `:` 冲突；白名单字段名（3 个）避免误识别普通 blockquote | 若用户反馈半角冒号需求强烈 |
| 2026-09-16 | commit 拆 3 次而非 4 次 | Phase 2（解析器）和 Phase 3（渲染器）必须配套发布——解析出来的东西渲染器不识别就是 bug；合并 commit 避免半完成状态入库；Phase 1 单独 commit 因其单文件、低风险 | — |