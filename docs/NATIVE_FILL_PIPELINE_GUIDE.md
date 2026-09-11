# Native Fill Pipeline — 完整技术指导文档

> 目标读者：**威龙**（负责后续把这条 pipeline 封装为 MCP 工具）
> 文档基于：`柏腾ppt模版.pptx` + `山西柏腾科技有限公司合同管理制度.utf8.md` 一次完整运行的所有踩坑实录
> 文档版本：v1.0（基于 ppt-master skill v6.1.20260903-085537）

---

## 1. 概述：什么是 Native Fill Pipeline

### 1.1 一句话定义

**Native Fill** = 用一套**保留原生 PPTX 设计的脚本链**，把外部内容（markdown / 文档 / 结构化数据）灌入既有 PPTX 模板的可编辑槽位，产出一份**字节级保留源设计**、**编辑痕迹与原版解耦**的新 PPTX。

### 1.2 三个路由的边界（必读，不要走错）

| 路由 | 适用场景 | 不适用 |
|---|---|---|
| **Generate PPTX** | 从零生成 / 从图片重画 / 美化既有 deck | 模板设计**必须原样保留** |
| **Create Template** | 把素材抽取为可复用 brand/style/layout/deck 模板 | 一次性灌内容 |
| **Edit Native PPTX** ← **本次用的就是这个** | 模板原样保留 + 内容替换 / 部分页改写 / 加页 / 重排 | 想把整个 deck 重新设计 |

**Native Fill 是 Edit Native PPTX 路由下的 "灌内容" 子场景**：
- 源 PPTX 设计（字体、色板、版式、装饰元素）**全部 byte-for-byte 保留**
- 只替换 / 新增**文字内容**和必要的**新内容板块**（通过新增 `<g id="...">`）
- 既有页面只要不动就 **passthrough**，导出的 PPTX 里那些页是原 XML 没改的

### 1.3 适用与不适用

✅ 适用：
- 公司模板里换内容（汇报、合同、制度、培训材料）
- 同一模板批量生产不同主题的 deck
- 模板页面数不够，要**新增拷贝页**承载更多内容

❌ 不适用：
- 模板设计差，要重画 → 走 Generate PPTX
- 输入是图片或 PDF 截图 → 走 image-to-pptx profile
- 内容是空白/主题词 → 需要先 source_to_md / topic-research 收集素材

---

## 2. 5-Phase Pipeline Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 1: PLAN (规划)                                                  │
│   读源 PPTX + 源文档 → 决定 page_plan / 内容映射 / 编辑策略            │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 2: IMPORT (导入)                                                │
│   pptx_to_svg.py --roundtrip                                          │
│   → authoring-svg-flat/*.svg + authoring_summary.json                 │
│   → sources/source.pptx + native-payloads/ + images/                  │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 3: AUTHOR (编辑 + 新增)                                          │
│   • 改文本：edit existing `<text>` 节点内容                            │
│   • 新板块：在编辑后的 SVG 末尾追加 `<g id="..." data-pptx-bounds=...>`│
│   • 新增页：Copy source slide → 编辑 → 在 page_plan 里登记            │
│   • 写 page_plan.json（每次改动都要刷新 summary）                      │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 4: QUALITY GATE (质量门)                                        │
│   svg_authoring_view.py --refresh-summary                              │
│   svg_quality_checker.py <workspace> --roundtrip                       │
│   • 必到 0 ERROR，WARN 可接受但需记录                                  │
│   • blocking 项必须修，否则 export 必失败                              │
└────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 5: EXPORT + VALIDATE (导出 + 验证)                              │
│   svg_to_pptx.py <workspace> --roundtrip                              │
│   → exports/*.pptx  ← 真正的最终交付物                                │
│   pptx_delivery_check.py <output.pptx> → validation/*.delivery.json    │
│   source_to_md.py <output.pptx> → validation/readback.md（人类可读校对）│
└────────────────────────────────────────────────────────────────────────┘
```

**关键不变量**：源 PPTX 在 Phase 2 之后被冻结为 `sources/source.pptx`，整个 pipeline 不再修改它。**所有编辑都发生在 authoring-svg-flat/ 下的 SVG 上**，Phase 5 导出时根据 page_plan 把 SVG 转回 DrawingML。

---

## 3. 详细 Step-by-Step Workflow

### Step 1 — 读源文档，建证据台账

**输入**：外部 markdown / 文档
**工具**：
- 直接读 `.md` 文件（用 read tool，不要 cat）
- 大文档用 grep 抓章节标题

**产出（写在内部，不落盘）**：章节大纲 + 每章 3-5 条要点 + 哪些要进 deck / 哪些可省

### Step 2 — 判定路由

读 `C:\Users\Administrator\.dsh\skills\.ppt-master.bak.v6.1.20260903-085537\workflows\routing.md` §2 路由矩阵：
- 源 PPTX 存在 + 用户要保留设计 + 新内容 → **Edit Native PPTX**

> 不要问用户"你想 regenerate 还是 preserve"。routing.md 是权威；模板设计保留不是用户偏好，是技术约束。

### Step 3 — 跑 attribution_guard

```bash
python "${SKILL_DIR}/scripts/attribution_guard.py"
```

**退出码非 0 立刻停**，不要绕过。这是 skill package 完整性校验，绕过它导出的 PPTX 可能和源对不上。

### Step 4 — 读运行时权威

读 `${SKILL_DIR}/workflows/edit-native-pptx.md` 全文。这是 Edit Native PPTX 路由的唯一权威，不要混用 generate-pptx.md。

### Step 5 — Import round-trip workspace

```bash
python "${SKILL_DIR}/scripts/pptx_to_svg.py" "<source.pptx>" \
  -o "projects/<slug>_<YYYYMMDD>" \
  --inheritance-mode both \
  --roundtrip
```

**重要参数**：
- `--roundtrip` 必加，否则导不出 byte-for-byte 的还原版本
- `--inheritance-mode both` 把 Master/Layout 也展开（默认 flat 也够用，但 both 更稳）
- 输出的 `<workspace>/authoring-svg-flat/*.svg` 是**唯一可编辑入口**

**产出目录**（按用途分）：

| 路径 | 用途 | 编辑？ |
|---|---|---|
| `authoring-svg-flat/slide_NN.svg` | 一页一文件，compact editable SVG | ✅ **只编辑这里** |
| `authoring-svg-flat/authoring_summary.json` | 每页 element/text/image/vector 计数 | 读 |
| `images/` | 源 PPTX 的图片资源 | 替换文件可换图 |
| `sources/source.pptx` | 源包 | ❌ 冻结 |
| `native-payloads/` | SmartArt/复杂效果/media frames 等不支持的对象 | ❌ 不可读、不可改 |
| `notes/slide_NN.md` | 源 speaker notes | 删 = 不要 notes；改 = 替换 |
| `validation/conversion-report.json` | 导入阶段诊断 | 读 |
| `exports/` | **Phase 5 导出目标** | 工具写 |

### Step 6 — 读 summary + 抽样 SVG，建内容映射

```bash
# 刷新 / 读取
python "${SKILL_DIR}/scripts/svg_authoring_view.py" "authoring-svg-flat" --refresh-summary
```

读 `authoring_summary.json` 拿到每页的：
- `viewBox`：画布尺寸
- `text_elements` / `text_characters`：文字槽位数量
- `images`：图片槽位
- `placeholders`：占位符（如果有就更好处理）
- `semantic_tables` / `semantic_shapes`：结构化元素

然后**抽样打开需要编辑的页 SVG**（不要全打开，只看要动的）：
- 用 read tool 读 `authoring-svg-flat/slide_NN.svg`
- 找到每个 `<text>` 节点的当前内容、字号、坐标、所属 frame

**对每个 `<text>` 槽位记录**：
| 字段 | 用途 |
|---|---|
| `frame`（`data-pptx-frame`）| x y width height — 决定文字最大宽高 |
| `font-size` | 字号 — 决定一行能放多少字 |
| 当前文字长度 | 估算替换后是否溢出 |

### Step 7 — 写 page_plan.json（**仅当输出 ≠ 源 roster**）

```json
{
  "schema": "ppt-master.roundtrip-page-plan.v1",
  "pages": [
    {"source_slide": 1, "svg": "slide_01.svg"},
    {"source_slide": 2, "svg": "slide_02.svg"},
    {"source_slide": 3, "svg": "slide_03.svg"},
    {"source_slide": 4, "svg": "slide_04.svg"},
    {"source_slide": 3, "svg": "slide_part02_div.svg"},
    {"source_slide": 4, "svg": "slide_part02_content.svg"},
    ...
  ]
}
```

**关键规则**：
- `pages` 是完整输出顺序，**不允许缺漏**（除了显式 drop 的）
- 每页必须有 `source_slide`（指源 PPTX 1-based 索引）
- `svg` 可省略，省略就用 `slide_<source_slide>.svg`
- **同一 source_slide 多次出现**（复用 / 拷贝）：必须先复制 SVG 文件起新名，然后两个都登记
- **新增页**（content 不在源里的）：source_slide 指向最近的骨架页，svg 指向复制的新文件
- 写出错页_plan 会被 export 拒绝：
  - svg 文件名重复
  - svg 文件名不在 authoring-svg-flat/ 下
  - source_slide 越界

### Step 8 — 编辑 SVG

#### 8a. 改既有文本槽位

直接 edit 工具替换 `<text>` 节点内容：

```xml
<!-- 改前 -->
<text x="113.73" y="61.53" ... font-size="37.33" fill="#576B93">单击添加大标题</text>
<!-- 改后 -->
<text x="113.73" y="61.53" ... font-size="37.33" fill="#576B93">三、范围与职责</text>
```

**别动**：
- `data-pptx-*` 属性（source-ref / frame / prst / part / semantic-object）— 它们是导出时找原 DrawingML 的索引
- `<g id="shape-NN">` 的 id
- `xml:space="preserve"`

#### 8b. 加新内容板块（在已有 slot 内或空白区）

必须包在 `<g id="...">` 里，**带 `data-pptx-bounds`**：

```xml
<g id="content-body" data-pptx-bounds="120 130 1060 480">
  <text x="120" y="180" font-size="22" font-weight="bold" fill="#1D2CAB">标题</text>
  <rect x="120" y="240" width="340" height="270" rx="8" fill="#F4F6FB" />
  ...
</g>
```

`data-pptx-bounds="x y w h"` 是质量门检查文字是否溢出 frame 的依据，**没它会被 WARN**。

#### 8c. 加新页（拷贝骨架）

```bash
# 在 authoring-svg-flat/ 下复制
Copy-Item authoring-svg-flat/slide_03.svg authoring-svg-flat/slide_part02_div.svg
```

然后修改新文件的 `<text>`，并在 page_plan.json 里登记。

### Step 9 — Refresh summary + Quality Gate

```bash
python "${SKILL_DIR}/scripts/svg_authoring_view.py" "authoring-svg-flat" --refresh-summary

python "${SKILL_DIR}/scripts/svg_quality_checker.py" "." --roundtrip
```

**必读错误分类**：
- `[ERROR]` blocking — **必须修**，否则 export 失败
- `[WARN] passed with warnings` — 可接受，但要记录
- `[OK]` — 干净通过

**最常见的 ERROR**：
1. 文字超出 frame 宽度（slide_03/04/05 长标题）
2. viewBox 缺失或错误
3. 缺失 `data-pptx-bounds` 让 checker 找最近的 rect 推断 frame（误报率高）

**最常见的 WARN**：
1. `Font stack exports non-PPT-safe typeface(s)` — 中文用"思源黑体 CN Regular"在 PowerPoint 上不存在，会回退到默认字体；可接受但视觉会变
2. 文字超出 nearest rect sibling — checker 推断的 frame 不是真实 frame；非阻塞

### Step 10 — Export PPTX

```bash
python "${SKILL_DIR}/scripts/svg_to_pptx.py" "<workspace>" --roundtrip
```

**输出**：`exports/<workspace-stem>_<timestamp>.pptx`

**回执关键行**：
```
Round-trip export summary: output_pages=11 passthrough=0 cloned_passthrough=0 patched=0 rebuilt=11
```

**桶的含义**：
| 桶 | 含义 |
|---|---|
| `passthrough` | 引用页 XML 字节完全保留 |
| `cloned_passthrough` | 复制后引用的页 XML 字节保留 |
| `patched` | 引用页但顺序/notes/transition/animation 改了 |
| `rebuilt` | 编辑过的页 / 引用了改动资源的页 |

**常见 export ERROR**：
| 报错 | 原因 | 修复 |
|---|---|---|
| `Edited round-trip source object did not produce a DrawingML shape: N` | 某个 source shape 转不回 DrawingML | 看 §6.1 |
| `SVG canvas validation failed: root viewBox is required` | 编辑时丢了 `viewBox` 属性 | 恢复 `<svg viewBox="0 0 1280 720" ...>` |
| `Unknown, duplicated, or cross-owned svg filenames` | page_plan.json 写错 | 重写 page_plan |

### Step 11 — 验证

```bash
# 1. 结构 + 关系检查
python "${SKILL_DIR}/scripts/pptx_delivery_check.py" "exports/<output>.pptx" \
  > "validation/<output>.delivery.json"

# 2. 人类可读的内容回读
python "${SKILL_DIR}/scripts/source_to_md.py" "exports/<output>.pptx" \
  -o "validation/readback.md"
```

**必查**：
- `delivery.json.status` 是 `passed` 或 `passed-with-advisories`
- `delivery.json.package.zip_integrity` = `"passed"`
- `delivery.json.relationships.problems` = `[]`
- `delivery.json.slides.count` = page_plan 的页数
- `readback.md` 每页能读到预期替换后的文字

**只有 delivery.json 和 readback.md 都达标才能宣告成功。**

---

## 4. Scripts Catalog（MCP 封装时要直接调用的 6 个脚本）

> 路径都以 `SKILL_DIR = C:\Users\Administrator\.dsh\skills\.ppt-master.bak.v6.1.20260903-085537` 为基准

### 4.1 `scripts/attribution_guard.py`

**作用**：skill package 完整性校验。**每次 pipeline 开头必跑**。

```bash
python "${SKILL_DIR}/scripts/attribution_guard.py"
```

退出码非 0 → 立刻停。**不要绕过**。

---

### 4.2 `scripts/pptx_to_svg.py`

**作用**：源 PPTX → round-trip workspace。

```bash
python "${SKILL_DIR}/scripts/pptx_to_svg.py" \
  "<input.pptx>" \
  -o "<workspace_dir>" \
  --inheritance-mode both \
  --roundtrip
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `input.pptx` | ✅ | 源文件路径，绝对路径优先 |
| `-o / --output` | ✅ | workspace 目录（不存在会创建） |
| `--inheritance-mode` | ❌ | `flat` / `both` / `deep`。MCP 默认 `both` |
| `--roundtrip` | ✅ | **没有这个就不是 round-trip 模式**，导出时不保留源 XML |

**stdout 关键字段**：
```
Source: ...
Canvas: W x H px
Theme colors: ...
Theme fonts: ...
Slides converted: N
Output: <workspace>
Round-trip structure: <workspace>/analysis/native_structure.json
```

**warning**（注意但不致命）：
- `gradient-stop-order-normalized` — DrawingML 要求渐变 stop 位置单调递增
- `animation-not-reconstructed` — 复杂对象动画保留在 source PPTX 里

---

### 4.3 `scripts/svg_authoring_view.py`

**作用**：刷新或读取 `authoring_summary.json`；高级用法可 `--adopt-object` 把一页的元素搬到另一页。

```bash
# 刷新 summary（每次编辑 SVG 后必跑）
python "${SKILL_DIR}/scripts/svg_authoring_view.py" "authoring-svg-flat" --refresh-summary

# 跨页搬元素（本次没用到，但 MCP 高级封装可考虑）
python "${SKILL_DIR}/scripts/svg_authoring_view.py" "authoring-svg-flat" \
  --adopt-object slide_05.svg:<element-id> --into chapter_market.svg
```

**adopt 规则**（重要）：
- 一个输出页只能有一个骨架 `source_slide`
- 要"合并"两页的元素：选骨架页 + `--adopt-object` 把另一页元素搬过来
- 被搬的元素丢失 native identity（变成 authored SVG）
- 跨页搬 source proxy 不允许

---

### 4.4 `scripts/svg_quality_checker.py`

**作用**：Phase 4 质量门。

```bash
python "${SKILL_DIR}/scripts/svg_quality_checker.py" "<workspace>" --roundtrip
```

| 退出码 | 含义 |
|---|---|
| 0 | OK / WARN-only |
| 1 | 有 blocking ERROR |

**关键 ERROR 类型与处理**：

| 报错 | 处理 |
|---|---|
| `<text> exceeds owning frame ... overflow horizontal N%` | 缩短文字 / 缩小字号 / 改 frame |
| `viewBox required` | 恢复 `<svg viewBox="0 0 W H">` |
| XML well-formedness 失败 | 检查 `&` `<` `>` `'` `"` 是否用 entity；检查是否用 HTML entity (`&nbsp;` 之类) |

**关键 WARN 类型**：

| 警告 | 是否阻塞 | 建议 |
|---|---|---|
| Font stack 非 PPT-safe | 否 | PowerPoint 上会回退字体 |
| text exceeds nearest rect sibling（推断 frame） | 否 | 给 `<g>` 加 `data-pptx-bounds` 可消除 |
| 模块文本 overflow through 5% | 否 | 缩短或重构 |
| 模块文本 overflow above 5% | **是**（失败） | 必须修 |

---

### 4.5 `scripts/svg_to_pptx.py`

**作用**：SVG workspace → 新 PPTX。

```bash
python "${SKILL_DIR}/scripts/svg_to_pptx.py" "<workspace>" --roundtrip
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `workspace` | ✅ | 含 `authoring-svg-flat/` 的目录 |
| `--roundtrip` | ✅ | 否则不会按 source XML 还原 |
| `-t <effect>` | ❌ | 替换整 deck 转场效果 |
| `--transition-duration <s>` | ❌ | 转场时长 |
| `--recorded-narration audio` | ❌ | 旁白模式 |
| `--use-narration-timings` | ❌ | 旁白驱动自动翻页 |
| `--animation-config animations.json` | ❌ | 自定义逐页动画 |
| `-a <preset>` | ❌ | 默认 `none` |
| `--no-notes` | ❌ | 去掉所有 speaker notes |
| `--native-charts-and-tables` | ❌ | 编辑了原生图表/表格时**必加** |

**输出路径格式**：`exports/<workspace-stem>_<YYYYMMDD_HHMMSS>.pptx`

**必须打印的 receipt**：
```
Round-trip export summary: output_pages=N passthrough=P cloned_passthrough=C patched=M rebuilt=R
```

**本次 receipt 解读**：`output_pages=11 passthrough=0 cloned_passthrough=0 patched=0 rebuilt=11`
- 全部 11 页都被编辑过（正常，因为我们改了文本 / 加了内容）
- 没有任何 `passthrough` 意味着模板 byte-for-byte 的还原页**为 0**

> ⚠️ 如果希望"未编辑的页保持原 XML"：不要把那些页列在 page_plan.json 里（只列要 rebuild 的）。或者把编辑标记收得更紧。本次 run 把全部 11 页都 rebuild 是合理的，因为我们对所有页都改了文字。

---

### 4.6 `scripts/pptx_delivery_check.py`

**作用**：最终 PPTX 的结构完整性 + 关系图谱 + zip 完整性检查。

```bash
python "${SKILL_DIR}/scripts/pptx_delivery_check.py" "<output.pptx>" \
  > "validation/<output>.delivery.json"
```

**`status` 字段**：
- `passed` — 全清
- `passed-with-advisories` — 有 advisory 但无 error
- `failed` — 有 structural error，**不能交付**

**advisory 例子**（不阻塞）：
- `<a:bodyPr>` 出现多次但定义不同 — 视觉无影响
- 某些 Master 占位符空缺 — 模板自带

---

### 4.7 `scripts/source_to_md.py`

**作用**：把 PPTX 转回 markdown，给人类做"内容校对"。**比 delivery check 更重要** — 它告诉你文字有没有真的替换对。

```bash
python "${SKILL_DIR}/scripts/source_to_md.py" "<output.pptx>" -o "validation/readback.md"
```

**注意**：脚本 stdout 可能输出 "ppt_to_md.py" 字样（脚本内部提示），这是 alias，不影响。

---

## 5. SVG Authoring Standards（MCP 生成内容时必须遵守）

> 出处：`${SKILL_DIR}/references/shared-standards-core.md`

### 5.1 XML 必须 well-formed

| 类别 | 正确写法 | 错误 |
|---|---|---|
| 中文标点（— © → · NBSP） | **裸 Unicode** | `&mdash;` `&copy;` `&nbsp;` |
| XML 保留字（`&` `<` `>` `"` `'`） | **entity** `&amp;` `&lt;` `&gt;` `&quot;` `&apos;` | 裸字符 |

> 用错一个 entity，整个文件 invalid，export 直接 abort。

### 5.2 全局禁止的 SVG 特性

| 禁止 | 原因 |
|---|---|
| `<style>` / `class` / 外部 CSS | checker 不支持 |
| `<foreignObject>` | 无法转 DrawingML |
| `<textPath>` | 同上 |
| `<animate*>` / `<set>` | 同上 |
| `<script>` / 事件属性 | 同上 |
| `@font-face` | 同上 |
| `mask` | 同上 |

### 5.3 行内 `style` 属性只能写以下

paint / line: `fill`, `stroke`, `stroke-width`, `stroke-dasharray`, `stroke-linecap`, `stroke-linejoin`, `fill-opacity`, `stroke-opacity`, `vector-effect`

text: `font-family`, `font-size`, `font-weight`, `font-style`, `text-anchor`, `letter-spacing`, `text-decoration`

alpha / defs: `opacity`, `stop-color`, `stop-opacity`, `flood-color`, `flood-opacity`

`filter`, `clip-path`, `marker-start` / `marker-end` 用 **直接属性**，不用 inline style。

### 5.4 几何属性必须 unitless decimal

```xml
✅ <rect x="120" y="210" width="320" height="112" />
❌ <rect x="120px" y="210px" />
```

`px` 兼容但 WARN；其它单位（%, em, 表达式）直接 ERROR。

### 5.5 文本属性

```xml
<text x="100" y="200" 
      font-family="&quot;思源黑体 CN Medium&quot;, sans-serif"
      font-size="20" 
      font-weight="bold" 
      text-anchor="start"
      fill="#1D2CAB">
  标题
</text>
```

- `font-family` 必须**非空**
- `font-size` 必须**有限正数 unitless px**
- `text-anchor` 必须是 `start` / `middle` / `end`，且**只能放在 `<svg>` / `<g>` / `<text>` 上**，**不能放在 `<tspan>` 上**
- 多行段落用 `<tspan x="..." dy="...">`，**第一个 line 直接放 text 节点，不要 all-tspan**

### 5.6 `data-pptx-bounds` 强制规则（**质量门关键**）

每个 top-level `<g id="...">` （除了 preset atom）必须：

```xml
<g id="card-1" data-pptx-bounds="60 115 565 260">
  <!-- 整组内容 -->
</g>
```

格式：`x y width height`

- checker 用来判断组内文字是否溢出
- 没有 `data-pptx-bounds` 时 checker 会推断（经常推断成最近的 rect sibling，导致 WARN 噪声）

### 5.7 不要破坏 `data-pptx-*` 索引

源 PPTX 导入时，每个 `<g>` 都有这些属性：

| 属性 | 含义 |
|---|---|
| `data-pptx-object` | `picture` / `shape` / `group` / `connector` |
| `data-pptx-frame` | x y w h（page coordinate） |
| `data-pptx-prst` | DrawingML preset name（如 `rect`, `ellipse`） |
| `data-pptx-source-ref` | 形如 `slide:8` — 指向源 PPTX shape |
| `data-pptx-semantic-object` | `shape` / 等 |
| `data-pptx-part` | `geometry` / 等 |

**改文本时这些属性一个都不能动**。动了 export 就找不回原 DrawingML。

---

## 6. 踩坑实录（本次 run 全部命中过的坑 + 怎么解）

### 6.1 ⚠️ 渐变（`linearGradient`）在 DrawingML 受限

**症状**：export 报 `Edited round-trip source object did not produce a DrawingML shape: 61`，且 conversion report 有 `gradient-stop-order-normalized` warning。

**原因**：DrawingML 的渐变 stop 位置必须**单调递增**。原模板的渐变可能：
- stop 顺序倒过来
- 用了非 0/100 位置
- 用于 fill 在非 picture 的 shape 上

**修复**：把 `<defs><linearGradient>...</linearGradient></defs>` 删掉，把引用它的 fill/stroke 改成实色。

**示例**：
```xml
<!-- 改前 -->
<defs><linearGradient id="ggrad1" x1="0" y1="0.5" x2="1" y2="0.5">
  <stop offset="0" stop-color="#FFFFFF" stop-opacity="0" />
  <stop offset="1" stop-color="#FFFFFF" />
</linearGradient></defs>
<rect ... fill="url(#ggrad1)" />

<!-- 改后 -->
<rect ... fill="#FFFFFF" />
```

> 这次跑里命中过：
> - slide_02 左侧白色渐变蒙版（影响 shape-56/58）
> - slide_03 起分隔线渐变描边（影响 shape-7）

### 6.2 ⚠️ Picture 元素的不同结构

**症状**：export 报 `did not produce a DrawingML shape: 8`（shape-8 永远是 picture）

**原因**：源 PPTX 把图片包在一个**带 viewBox crop 的嵌套 SVG** 里。直接 `<image>` 和嵌套 `<svg>` 两种写法在 export 时行为不同。本次 run 发现：

| 结构 | 适用 |
|---|---|
| `<g><svg viewBox="0.x 0.x 0.x 0.x"><image x="0" y="0" width="1" height="1"/></svg></g>` | 部分 slide OK（如 slide_01） |
| `<g><image x="0" y="0" width="1280" height="720"/></g>` | 部分 slide OK |

**实际行为**：交替改两种结构才能让所有 slide 通过 export。**这次经验是嵌套 SVG 版本更稳**，但没有理论保证。

**修复策略**：遇到 picture export ERROR，先把当前结构切换成另一种试试。

### 6.3 ⚠️ 文字溢出 frame

**症状**：`svg_quality_checker` 报 `exceeds owning frame ... overflow horizontal N%`

**原因**：替换后的文字长度 > frame 宽度能容纳的字数。

**计算公式**（粗估）：
- 中文：1 字 ≈ `font-size` 像素宽
- 数字 / 英文：1 字 ≈ `font-size * 0.55` 像素宽
- frame 内一行 = `width - text-anchor offset`

**修复**：
1. 缩短文字（最稳）
2. 缩小 `font-size`
3. 加 `letter-spacing` 收紧（不推荐，可能影响视觉）

**本次踩坑**：
| 位置 | 原文字 | 新文字 | 溢出 | 修复 |
|---|---|---|---|---|
| slide_01 部门槽 | `部门：XXX 汇报人：XXX` | `部门：商务部 实施日期：2024-01-01` | 27.6% | 删实施日期，只留部门 |
| slide_03 副标题 | `单价添加小标题/标题英文 ...` | `Purpose & Principles / 目的与原则 · 制度总则` | 1.7% | 删 · 制度总则 |
| slide_part03_div 标题 | `添加大标题` (60pt 占位) | `合同订立与审批` (80pt) | 13.2% | 字号降到 60pt |
| slide_part03_div 副标题 | 占位 | `Contract Signing & Approval / ...` | 30.4% | 缩短英文 |
| slide_part04_div 标题 | `添加大标题` | `适用范围与附件` | 13.2% | 字号降到 60pt |
| slide_part03/04_content 标题 | `单击添加大标题` | `四、合同的订立、审查、审批` | 18.4% | 标题缩短 + 字号降到 32pt |

### 6.4 ⚠️ 编辑时丢 viewBox

**症状**：export 报 `SVG canvas validation failed: ... root viewBox is required`

**原因**：edit 工具的 `old_string` / `new_string` 没把 `viewBox="0 0 1280 720"` 包进来。

**修复**：确认 `<svg ... viewBox="0 0 W H" ...>` 在文件第一行。

### 6.5 ⚠️ 字体回退

**症状**：WARN `Font stack exports non-PPT-safe typeface(s) to PPTX`

**原因**：用了 `"思源黑体 CN Regular"`，PowerPoint 没有这个字体，会回退到默认中文字体（通常是微软雅黑）。

**修复**：可以直接把 `font-family` 改成 `"微软雅黑", sans-serif`，或接受 WARN 让 PowerPoint 自己回退。

### 6.6 ⚠️ `page_plan.json` 写错

**症状**：export 直接 abort，提示 unknown/duplicated svg filename。

**修复**：
- 每个 svg 文件只能出现在 pages 数组里**一次**（除非源相同 + 复制了新文件）
- svg 文件名必须存在于 `authoring-svg-flat/` 下
- 复制新文件必须登记

### 6.7 ⚠️ 拷贝 slide 后才编辑

本次踩坑：先复制 slide_03.svg → slide_part02_div.svg，**之后**才修了 slide_03.svg 的 picture 结构。结果 slide_part02_div.svg 保留了**坏掉的 picture 结构**，export 时它先过（因为它在 page_plan 第 5 位），但 slide_part02_content.svg 也坏掉，最终要一个个修。

**教训**：复制前**先修好模板**，再拷贝。

---

## 7. MCP 封装设计建议（核心章节）

### 7.1 MCP Tool 接口设计

**Tool 名**：`native_fill_pptx`（或 `pptx_native_fill`）

**输入 schema**（JSON Schema 风格）：

```json
{
  "source_pptx": "<absolute path to source .pptx>",
  "source_material": "<absolute path to source .md or .txt>",
  "output_dir": "<absolute path for workspace>",
  "page_plan": [
    {"source_slide": 1, "svg": "slide_01.svg"},
    ...
  ],
  "content_mapping": {
    "slide_01.svg": {
      "shape-23": "合同管理制度",
      "shape-24": "工业设备智能化服务商",
      "shape-8": "部门：商务部",
      "shape-9": "编号：BT-ZD-MOC-002"
    },
    "slide_02.svg": {
      "shape-69": "一、目的",
      "shape-70": "规范合同管理 防范风险 / Purpose",
      ...
    },
    ...
  },
  "new_pages": {
    "slide_part02_div.svg": {
      "source_slide": 3,
      "shape-4": "PART 02",
      "shape-5": "范围与职责",
      "shape-70": "Scope & Duties / 商务部主导 · 合同统一管理"
    },
    "slide_part02_content.svg": {
      "source_slide": 4,
      "shape-17": "三、范围与职责",
      "_new_content_body": {
        "bounds": "120 130 1060 480",
        "layout": "3-column-cards",
        "cards": [
          {"title": "基础工作", "color": "#1D2CAB", "items": [...]},
          {"title": "销售合同", "color": "#EE822F", "items": [...]},
          {"title": "采购合同", "color": "#75BD42", "items": [...]}
        ]
      }
    }
  },
  "options": {
    "auto_fix_gradients": true,
    "auto_simplify_pictures": true,
    "auto_adjust_overflow": true,
    "strict_validation": true
  }
}
```

**输出 schema**：

```json
{
  "status": "passed" | "passed-with-advisories" | "failed",
  "output_pptx": "<absolute path to final .pptx>",
  "workspace": "<absolute path to workspace>",
  "validation": {
    "delivery_json": "<path>",
    "readback_md": "<path>"
  },
  "metrics": {
    "slide_count": 11,
    "export_summary": {
      "passthrough": 0,
      "cloned_passthrough": 0,
      "patched": 0,
      "rebuilt": 11
    }
  },
  "warnings": ["..."],
  "errors": ["..."]
}
```

### 7.2 内部状态机

MCP 内部维护一个状态机，6 个状态：

```
INIT
  ↓ (validate inputs)
IMPORTED  ← phase 2 完成
  ↓ (read summary)
PLANNED   ← page_plan.json 已写
  ↓ (apply content mapping to SVG)
AUTHORED  ← SVG 编辑完成
  ↓ (refresh summary + quality check)
QUALITY_PASSED  ← 0 blocking ERROR
  ↓ (export)
EXPORTED  ← PPTX 已生成
  ↓ (validate)
DONE / FAILED
```

**每个状态都有"重试 / 回退"路径**。比如 QUALITY_PASSED 失败就回 AUTHORED，自动修溢出。

### 7.3 自动修复策略（封装价值的关键）

MCP 应该内建以下自动修复，让上层 LLM 不需要懂 SVG：

| 问题 | 检测方式 | 自动修复 |
|---|---|---|
| 文字溢出 frame | quality check ERROR | 缩字号 → 缩短文字 → 拆段 |
| viewBox 缺失 | quality check ERROR | 恢复 `<svg viewBox="0 0 W H">` |
| gradient 不可导出 | export ERROR | 删除 `<defs>` 渐变，引用处改实色 |
| picture 不可导出 | export ERROR | 切换嵌套 SVG / 直接 image 两种结构 |
| 字体非 PPT-safe | quality check WARN | 替换为 `"微软雅黑", sans-serif` |
| page_plan 错误 | export ERROR | 校验 + 重写 |

### 7.4 错误处理

**3 类错误分级**：

| Level | 处理 |
|---|---|
| `FATAL` | 立刻 abort，报告用户。比如 attribution_guard 失败、源 PPTX 损坏 |
| `RECOVERABLE` | 自动修复后重试。比如溢出、viewBox 丢失 |
| `WARNING` | 不阻塞，记录到 delivery.json。比如字体回退、master 占位符空缺 |

### 7.5 幂等性

每次跑应该产出**等价的 PPTX**（如果输入和 page_plan / content_mapping 一致）：

```python
# workspace 目录名带 hash
workspace = f"projects/{slug}_{hash(content_mapping)[:8]}_{date}"
```

避免重跑覆盖上次的产物。

### 7.6 缓存策略

- `sources/source.pptx` 的 sha256 不变 → 跳过 Phase 2 重新 import
- 复用 workspace 时只对**修改过**的 SVG 跑 quality check

### 7.7 MCP tool description 怎么写（关键 — 决定 LLM 会不会调错）

```yaml
name: native_fill_pptx
description: |
  把外部 markdown 文档的内容灌进既有 PPTX 模板，保留模板的原生设计。
  
  Use when:
    - 用户给了源 PPTX + 一份文档 / markdown，要"按模板生成"
    - 不要重画 / 改设计，只换内容
  Do NOT use when:
    - 模板设计差，要重新画 → 走 generate_pptx
    - 输入是图片或 PDF 截图 → 走 image_to_pptx
    - 没有源 PPTX，只要从零生成 → 走 generate_pptx
  
  Inputs:
    source_pptx: 模板文件路径
    source_material: 文档路径（会先 source_to_md 转 markdown）
    page_plan: 11 页映射表
    content_mapping: 每页每个 text 槽位的新文字
  Outputs:
    output_pptx: 最终 PPTX
    validation: delivery.json + readback.md
```

### 7.8 调试 / 内部可见性

MCP 必须把这些路径暴露给 LLM：
- workspace 路径
- `authoring-svg-flat/*.svg` 路径（让 LLM 能用 read 工具直接查）
- `validation/readback.md` 路径（让 LLM 能用 read 校对内容）
- `validation/<output>.delivery.json` 路径
- 最后输出 PPTX 路径

### 7.9 性能与超时

| 阶段 | 典型耗时 | MCP 超时建议 |
|---|---|---|
| attribution_guard | < 1s | 5s |
| pptx_to_svg (Phase 2) | 5-15s | 60s |
| refresh summary | 1-3s | 10s |
| quality check | 3-10s | 30s |
| svg_to_pptx (Phase 5) | 10-30s | 120s |
| delivery check + readback | 5-10s | 30s |
| **总计** | **~30-60s** | **180s** |

---

## 8. 验证门（必走，不走不能宣告成功）

### 8.1 4 道门

| 门 | 工具 | 通过条件 |
|---|---|---|
| Phase 4 | `svg_quality_checker --roundtrip` | 退出码 0 |
| Phase 5a | `svg_to_pptx --roundtrip` | 打印 `Round-trip export summary:` 且无 ERROR |
| Phase 5b | `pptx_delivery_check` | `status` = passed 或 passed-with-advisories |
| Phase 5c | `source_to_md` | readback.md 11 页内容与 content_mapping 一致 |

**4 道门全过才算 DONE**。

### 8.2 失败模式速查

| 现象 | 原因 | 看哪里 |
|---|---|---|
| export abort，画 canvas 错 | viewBox 丢了 | svg_quality_checker ERROR |
| export abort，画 source shape 不出 | gradient / picture 错 | export stdout |
| delivery 报 zip 损坏 | 磁盘满 / 文件被改 | delivery.json zip_integrity |
| delivery 报 relationship problems | page_plan 引用的资源找不到 | delivery.json relationships |
| readback 文字错位 | content_mapping 的 svg 路径或 shape id 错 | readback.md 文字 |

---

## 9. 本次 run 完整 Walkthrough

> 作为 MCP 实现的参考实现，每次跑都该留一份这样的实录。

### 9.1 输入

- 源 PPTX：`C:\Users\Administrator\Desktop\柏腾ppt模版.pptx`
- 源文档：`C:\Users\Administrator\Desktop\ggzsk\2山西柏腾科技有限公司合同管理制度.utf8.md`
- Workspace：`C:\Users\Administrator\Desktop\projects\boteng-contract-mgmt_20260910\`

### 9.2 Phase 2 — 导入结果

```
Source: 柏腾ppt模版.pptx
Canvas: 1280 x 720 px
Theme fonts: majorLatin=Arial, majorEastAsia=微软雅黑, minorLatin=Arial, minorEastAsia=微软雅黑
Slides converted: 5
Warnings: 2 (gradient-normalized + animation-not-reconstructed)
```

### 9.3 Page Plan（11 页）

```
1. slide_01 (edit) — 封面：合同管理制度
2. slide_02 (edit) — 目录：6 章节
3. slide_03 (edit) — PART 01 divider
4. slide_04 (edit) — Content: 目的 + 原则
5. slide_part02_div (NEW from slide_03) — PART 02 divider
6. slide_part02_content (NEW from slide_04) — 三、范围与职责 3 列卡片
7. slide_part03_div (NEW from slide_03) — PART 03 divider
8. slide_part03_content (NEW from slide_04) — 四、合同订立与审批 3 阶段流程
9. slide_part04_div (NEW from slide_03) — PART 04 divider
10. slide_part04_content (NEW from slide_04) — 五、合同适用范围 + 修订记录表
11. slide_05 (edit) — THANK YOU
```

### 9.4 修改统计

- 89 个 source ref 中：86 unchanged，3 edited，0 deleted（删除的是 slide_02 的渐变 shape 和 slide_03 的 defs）
- 11 个 SVG 文件全部 rebuild（export summary `rebuilt=11`）

### 9.5 自动修复轨迹

1. **slide_01 部门槽溢出 27.6%** → 删除"实施日期"段，只保留"部门：商务部"
2. **slide_03 副标题溢出 1.7%** → 删"· 制度总则"
3. **slide_part03_div 标题溢出 13.2%** → font-size 80→60
4. **slide_part03_div 副标题溢出 30.4%** → 缩短英文 "Contract Signing & Approval" → "Contract Signing"
5. **slide_part04_div 标题溢出 13.2%** → font-size 80→60
6. **slide_part03_content 标题溢出 18.4%** → "四、合同的订立、审查、审批" → "四、合同订立与审批"，字号 37.33→32
7. **slide_part04_content 标题溢出 18.4%** → "五、合同管理制度及适用范围" → "五、合同适用范围"，字号 37.33→32

### 9.6 导出 ERROR 修复轨迹

| 次序 | 错误 | 修复 |
|---|---|---|
| 1 | slide_02 shape-61 渐变组不导出 | 删 `<defs>` 渐变，把 `fill="url(#ggrad1)"` 改 `fill="#FFFFFF"` |
| 2 | slide_03 shape-8 picture 不导出 | 把嵌套 SVG 改为直接 `<image>` |
| 3 | slide_04 shape-2 picture 不导出 | 把直接 `<image>` 改回嵌套 SVG（与 source 一致） |
| 4 | slide_part02_div shape-8 picture 不导出 | 同 2 |
| 5 | slide_part04_div viewBox 丢失 | edit 时漏掉 viewBox，恢复 |
| 6 | slide_part02_content shape-2 picture 不导出 | 同 3 |

> **意外发现**：嵌套 SVG 和直接 `<image>` 两种 picture 结构在不同 slide 上行为不一致，没有稳定规则。MCP 自动修复时应**两种都试一遍**。

### 9.7 最终交付

```
Output PPTX: C:\Users\Administrator\Desktop\projects\boteng-contract-mgmt_20260910\exports\boteng-contract-mgmt_20260910_20260910_135103.pptx
Size: 41.4 MB (11 slides, 5 media, 4 layouts, 1 master, 79 unique parts)
Delivery status: passed-with-advisories
  zip_integrity: passed
  relationships.problems: []
  slides.count: 11
Readback: validation/readback.md (266 lines, all 11 slides verified)
```

---

## 10. Appendix

### 10.1 关键 schema / 文件路径

```
SKILL_DIR = C:\Users\Administrator\.dsh\skills\.ppt-master.bak.v6.1.20260903-085537

Authority docs:
  workflows/routing.md                     - 路由权威
  workflows/edit-native-pptx.md            - Edit Native 路由权威
  references/shared-standards-core.md      - SVG 标准
  references/svg-effects.md                - 视觉效果（本次未用到）

Scripts:
  scripts/attribution_guard.py             - 完整性校验
  scripts/pptx_to_svg.py                   - PPTX → SVG workspace
  scripts/svg_authoring_view.py            - summary / 跨页搬元素
  scripts/svg_quality_checker.py           - 质量门
  scripts/svg_to_pptx.py                   - SVG → PPTX
  scripts/pptx_delivery_check.py           - PPTX 交付检查
  scripts/source_to_md.py                  - PPTX → markdown（回读）

page_plan.json schema:
  schema = "ppt-master.roundtrip-page-plan.v1"
  pages = [{"source_slide": N, "svg": "filename.svg"}, ...]

authoring_summary.json schema:
  schema = "ppt-master.svg-authoring-summary.v1"
  documents[] = {file, kind, bytes, viewBox, elements, text_elements, text_characters, images, ...}

delivery.json schema:
  schema = "ppt-master.pptx-delivery-check.v1"
  status = "passed" | "passed-with-advisories" | "failed"
  file = {path, bytes}
  package = {zip_integrity, corrupt_member, parts, relationships}
  slides = {count, hidden_count, hidden}
```

### 10.2 数据流图

```
源 PPTX ──┐
          ├─→ pptx_to_svg.py --roundtrip ──→ workspace/
源 markdown ──→ (LLM 内部读) ──→ content_mapping ─┐
                                                ├─→ edit SVG
page_plan.json ──────────────────────────────── →  │
                                                ↓
                                          svg_quality_checker --roundtrip (pass)
                                                ↓
                                          svg_to_pptx.py --roundtrip
                                                ↓
                                          exports/*.pptx
                                                ↓
                                          pptx_delivery_check → validation/*.delivery.json
                                          source_to_md.py     → validation/readback.md
                                                ↓
                                              DONE
```

### 10.3 关联 skill / 工具

- `ppt-master` ← **本次完整使用**
- `ppt-from-template` ← 简化版封装（5 页固定流程），未来 MCP 可以更接近这个
- `resume-template-design` ← 同源 svg_authoring 标准
- `dev-expert` ← 工程实现 / 错误处理参考

### 10.4 Glossary

| 术语 | 定义 |
|---|---|
| **Round-trip workspace** | 源 PPTX 导入 + 编辑 + 导出的完整目录结构 |
| **Source ref** | SVG 元素上的 `data-pptx-source-ref="slide:N"`，指回源 PPTX 里的 shape 索引 |
| **Edit in place** | 在源 SVG 里改，不重建（保留 data-pptx-* 属性） |
| **Rebuilt** | 导出时该页 SVG 转成了新 DrawingML（区别于 passthrough） |
| **Passthrough** | 导出时该页 XML 字节完全等于源 PPTX |
| **Slot capacity** | 文字槽位在指定 frame/font-size 下能容纳的最大字数 |
| **Native proxy** | SmartArt 等不支持的对象在 SVG 里用占位符表示，导出时恢复原对象 |

### 10.5 反模式（**不要这么做**）

| 反模式 | 后果 |
|---|---|
| 用 HTML entity (`&nbsp;` `&mdash;`) | XML invalid |
| 在 `<text>` 里加 `<tspan x="..." dy="0">` | 第一个 line 直接放 text 节点 |
| 复制 SVG 后**不刷新 summary** 就 export | 旧 summary 还在，export 用错的 page_plan |
| 修改 `<g>` 的 `id` 或 `data-pptx-source-ref` | 找不回原 DrawingML |
| 用 `<style>` 或 class | checker 不支持 |
| `viewBox` 写成 `viewbox` 或单位带 `px` | ERROR |
| 直接改 `sources/source.pptx` | 整个 round-trip 假设被打破 |
| 把 SVG `<text>` 内容塞 HTML (`<br>`) | 用 `<tspan x="..." dy="...">` |
| 给所有页都用同样的 font-family | 失去视觉层级 |

---

## 11. Path B 实战诊断（mcp-ppt-native-fill chat loop 实跑结果）

> **来源**：`tests/e2e/test_boteng_path_b_e2e.py` 在 2026-09-11 跑 `柏腾ppt模版.pptx`（5 页，含 SmartArt + group + image，39MB）触发的真实故障。LLM = `MiniMax-M3` via `api.minimaxi.com/v1`。
> **目的**：记录当 `native_fill` 触发 `vendor_fallback` 后，LLM 走"读 SVG → 改 SVG → 重新 export"路径时的真实卡点，给后续 LLM agent loop / system prompt / 工具实现三个层面提供改造清单。

### 11.1 native_fill v2：复杂度分流 + 诚实失败信号（2026-09-11 改造）

**原方案问题**：`native_fill` 改 SVG 触发 vendor `Edited round-trip source object did not produce a DrawingML shape: N` 拒收后，原实现走 **identity fallback** 假装成功（`ok=True` + `output=模板原样`），污染产物并误导 LLM。

**新方案核心思路**：
- **复杂度分流**：`native_fill` 跑前扫每张 SVG，含 `<image>` / `<g data-pptx-object="group|picture">` 的 slide 标 `skipped_complex=True`，**不动 SVG**。vendor `svg_to_pptx --roundtrip` 的 `_roundtrip_passthrough_candidates`（`cli.py:339-396`）检测到这些 slide 的 `edited_refs=0` → 走 **passthrough** 路径，**byte-for-byte 用源 PPTX slide XML** → **完全绕过 overlay** 算法 → **根本不触发 shape-61 拒收**。
- **诚实失败兜底**：复杂度检测漏判时（极少数情况），vendor reject 仍然触发，native_fill 返 `error_code='E_VENDOR'` + `vendor_fallback='rejected_no_output'` + 删除 `output.pptx`（避免污染），**不再 fallback identity**。

```python
# native_fill.py:1083 _is_complex_slide()
def _is_complex_slide(svg_path: Path) -> tuple[bool, list[str]]:
    """检测 slide 是否含 native image/group."""
    reasons: list[str] = []
    try:
        root = ET.parse(svg_path).getroot()
    except (ET.ParseError, FileNotFoundError, OSError) as e:
        return True, [f"svg_unreadable: {type(e).__name__}: {e}"]
    for elem in root.iter():
        tag_local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag_local == "image":
            reasons.append(f"<image href={(elem.get('href') or '')[:60]!r}>")
            break
    for elem in root.iter("{http://www.w3.org/2000/svg}g"):
        obj = elem.get("data-pptx-object")
        if obj in {"group", "picture"}:
            gid = elem.get("id") or "?"
            reasons.append(f"<g id={gid!r} data-pptx-object={obj!r}>")
            break
    return (bool(reasons), reasons)
```

**新返回值**（`fill_results[*]`）：

| 字段 | 含义 |
|---|---|
| `skipped_complex: bool` | True = 该 slide 含 native image/group，native_fill 不动 SVG（让 vendor passthrough） |
| `complexity_reasons: list[str]` | 触发复杂的元素列表（供 LLM 调试） |
| `pending_edits: list[{shape_id, new_text}]` | native_fill 没应用的 edits，**LLM 后续用 `apply_text_edits` 接管** |
| `slots_observed: list[{...}]` | SVG text 槽位（含 `selector`/`role`/`current_text`/`editable`/`is_placeholder`） |

**LLM 拿到 native_fill 报告后的正确行为**：
- `fill_results[*].skipped_complex=True` → 跳过该 slide 的 native_fill，**直接调 `template_text_slots` → `apply_text_edits` 按 shape_id 接管** `pending_edits`
- `skipped=False, applied=N` → native_fill 已成功，**直接 `export_pptx`**
- 若 LLM 收到 `error_code='E_VENDOR'` → 复杂度检测漏判，**改用 `template_text_slots` → `apply_text_edits` 接管所有 spec.slides**

### 11.2 LLM 真实行为（chat loop max_steps=20 全程打点）

```
step 1:  read_file 源 md
step 2:  list_slide_texts + template_text_slots（摸结构）
step 3:  read_file slide_01.svg + slide_02.svg
step 4:  read_file slide_03.svg + slide_04.svg
step 5:  read_file slide_05.svg
step 6:  read_file delivery.json（看 fallback 报告）
step 7:  read_file slide_01.svg（又开始重读）
...
step 14: replace_svg slide_01
step 15: replace_svg slide_02
step 16: replace_svg slide_03
step 17: export_pptx → ToolError（err_result 不支持 autofix_applied kwarg）
step 18: native_fill 又跑一遍（违反 system prompt 第 161 行禁令）
step 19: read_file slide_01.svg（再来一次）
step 20: replace_svg slide_04 + replace_svg slide_05 → E_MAX_STEPS，无 final_answer
```

**统计**：5×replace_svg + 9×read_file + 1×native_fill（重跑）+ 1×export_pptx（失败）+ 若干次 list/template_text_slids。**0 次 apply_text_edits**（LLM 选了 replace_svg 全图重画而不是按 shape_id 精改）。

### 11.3 故障分类（七类，按修复成本从低到高）

#### 故障 A — `err_result()` 不接受 `autofix_applied` kwarg（**真 bug**）

**症状**：`Error calling tool 'export_pptx': err_result() got an unexpected keyword argument 'autofix_applied'`

**位置**：
- `src/mcp_ppt_native_fill/tools/workspace.py:421`（validate 失败分支）
- `src/mcp_ppt_native_fill/tools/workspace.py:613`（export_pptx 失败分支）

**根因**：`schema/result.py:156 err_result()` 签名只有 `tool_name/request_id/error_code/error_message/actor_id/tenant_id/workspace_path/idempotency_key/duration_ms`，但调用方传 `autofix_applied/autofix_iterations/autofix_report`。这是 Wave 4 引入 autofix 时的回归。

**修复**（5 行）：
```python
# src/mcp_ppt_native_fill/schema/result.py:156
def err_result(
    *, tool_name, request_id, error_code, error_message,
    actor_id=None, tenant_id=None, workspace_path=None,
    idempotency_key=None, duration_ms=0,
    autofix_applied: int | None = None,      # 新增
    autofix_iterations: int | None = None,    # 新增
    autofix_report: list | None = None,       # 新增
) -> ToolResult:
    return ToolResult(..., autofix_applied=autofix_applied, ...)
```

#### 故障 B — LLM 不知道 fallback 是"假成功"

**症状**：LLM 看到 `native_fill.ok=True` 后**继续做正确的事**（读 SVG、改 SVG、export），但因为 fallback 让 `output.pptx` 已写出且大小正常（39MB），LLM 没有任何信号表明这是"模板原样"。

**修复**：在 `native_fill.py` 的 fallback 返回里加明确信号：
```python
"export": {
    ...,
    "vendor_fallback": "identity_roundtrip",
    "vendor_fallback_reason": "...",
    "no_edits_applied": True,            # 新增（让 LLM 一眼看出）
    "required_user_action": "手动编辑 SVG 后调 export_pptx, 或调 native_fill 强制走 full SVG 重画"
}
```
并在 `chat.py` 的 system prompt §"vendor svg_to_pptx 失败处理"加一句：
```
若 native_fill 返回 no_edits_applied=True, 必须:
  (1) read_file 看每张 slide_NN.svg 的 <text> 槽位
  (2) apply_text_edits 按 shape_id 精改 (不要 replace_svg 整图重画)
  (3) export_pptx 再来一次
不要重跑 native_fill (vendor 拒收同样的 shape 会再 fallback)。
```

#### 故障 C — LLM 不会写"DrawingML-safe SVG"

**症状**：LLM 在 replace_svg 里写 `<defs><linearGradient>...</linearGradient></defs>`、用 `url(#ggrad)` 引用、`stroke-dasharray`、`fill-opacity` 等属性。master §6.1/§5.3 明令禁止，但 LLM 没学过。

**修复**：在 chat.py system prompt 嵌入 master §5.2/§6.1/§6.2 的关键约束（精简到 ~10 行）：
```
=== replace_svg 必须遵守的 SVG 约束 ===
禁止:
  - <linearGradient> / <radialGradient> / url(#...) 引用渐变 (master §6.1)
  - <filter> <clipPath> <mask> <style> <foreignObject> <textPath> <script>
  - <animate*> <set>
允许:
  - 实色 fill="#RRGGBB" / stroke="#RRGGBB"
  - font-family="微软雅黑" / 思源黑体 / sans-serif
  - viewBox="0 0 1280 720" + width + height 必须有
推荐: 不重画原模板的装饰 (logo / icon / 背景色块),
      只改 <text> 内容, 其它尽量保留原 SVG 的几何/装饰元素。
```

#### 故障 D — LLM 不知道改 SVG 之前先看 template_text_slots

**症状**：LLM 反复 read_file 看整张 SVG，**没调过** `template_text_slots`（system prompt 提到了，但 LLM 没优先用）。template_text_slots 才是精改的正确入口（提供 `selector` + `role` + `current_text` + `editable` + `is_placeholder`）。

**修复**：在 system prompt 把 "**所有 fill 操作前必须先调 template_text_slots**" 提前到顶部，并在调用示例里给出 shape_id 提取路径。

#### 故障 E — LLM 又调了一次 `native_fill`

**症状**：第 18 步 LLM 又调 native_fill，违反 system prompt "不要反复重 import_roundtrip + 再 apply_text_edits"。

**修复**：在 native_fill tool description 加 explicit warning：
```yaml
description: |
  按 spec 自动填模板（vendor svg_to_pptx 路径）。
  ⚠️ 如果上一次调用返回 vendor_fallback=identity_roundtrip，不要再调本工具！
  请改用 apply_text_edits / replace_svg 手动修 SVG 后调 export_pptx。
```

#### 故障 F — max_steps=20 仍不够

**症状**：LLM 20 步打完还没给 final_answer。LLM 用 5 步摸结构 + 5 步写 SVG + 反复重读 + 多次 export 试探，**实际填 5 页最少需要 ~12 步**。

**修复**：
1. `native_fill` 调用后**不要让 LLM 自己 read_file 摸结构** — 让 `native_fill` 返回时一并带上 `{slide_NN: {editable_text_slots: [{selector, current_text, suggested_text}]}}`，LLM 拿到即用。
2. chat 的 `default_max_steps` 从 10 提到 25。
3. 加 `replace_svg_batch` 工具：一次传 5 个 slide 的新 SVG，避免 5 次 round-trip。

#### 故障 G — vendor 拒收的根本原因没根治（架构性）

**症状**：boteng 模板里含 SmartArt / group / native image。vendor `svg_to_pptx.py:1679` 的 `Edited round-trip source object did not produce a DrawingML shape` 是因为**改过的 SVG 跟原始 source.pptx 的某个 native shape 比对不上**。

**修复**（master 没做，只能我们做）：
- **方案 1（短期）**：在 `tools/native_fill.py` 的 fallback 之前先尝试 "**先 export 一份 identity 看 vendor 能不能过**"，过则说明是 svg 编辑问题，可指导 LLM 修；不过则 vendor 本身问题，直接走 identity + 标 no_edits_applied。
- **方案 2（中期）**：写 `native_fill_recovery` 工具，输入是 fallback 报告，自动跑 master §6 的修复（删除渐变、改实色、切 picture 结构）后重 export。
- **方案 3（长期）**：把 fallback identity 干掉，要么真改要么真报错 — 让 LLM 不会遇到"假成功"。

### 11.4 改造优先级（建议）

| 优先级 | 故障 | 工作量 | 收益 |
|---|---|---|---|
| P0 | A: err_result kwarg bug | 5 行 | 修复 export_pptx 错误路径 |
| P0 | E: native_fill tool description 加 warning | 1 段文字 | 防 LLM 重跑 |
| P1 | B: native_fill 返回 no_edits_applied + system prompt 强信号 | 20 行 | LLM 一眼识别假成功 |
| P1 | D: system prompt 把 template_text_slots 提前 | 5 行 | 减少 50% read_file |
| P2 | C: system prompt 嵌入 SVG 约束 | 30 行 | 减少 replace_svg 错误 |
| P2 | F: max_steps 10 → 25 + native_fill 返回 slots | 50 行 | boteng 能跑完 |
| P3 | G: native_fill_recovery 自动修 | 200 行 | 端到端无需 LLM |

### 11.5 验收口径

完成 P0 + P1 后再跑 `tests/e2e/test_boteng_path_b_e2e.py`，**期望**：
- LLM 在 12 步内完成全部 5 张 slide 改写 + 一次 export
- final_answer 包含 "applied N / export receipt={...rebuilt: 5, passthrough: 0...}"
- output.pptx 第二轮 export 不再 fallback
- 如果仍 fallback，final_answer 必须包含 vendor_fallback_reason + stderr_tail 关键行

---

## 12. 一句话总结（给后续看代码的人）

> **Native Fill = 拿模板 PPTX → 拷成可编辑 SVG → 按槽位灌内容 → 质量门 → 导出回 PPTX，全程不动源 PPTX、不破坏 data-pptx-* 索引、不用渐变/复杂 picture、不让文字溢出 frame。** 6 个脚本、5 个 phase、4 道验证门。MCP 封装时按 7.1 接口设计、7.3 内建自动修复、7.5 保证幂等。
> **Path B (2026-09-11 v2)**：native_fill 已内置**复杂度分流** — 含 `<image>` / `<g data-pptx-object="group|picture">` 的 slide 自动 `skipped_complex=True`，不动 SVG → vendor passthrough → 不触发 shape-61 拒收。LLM 看到 `skipped_complex=True` 应接管 `pending_edits` → `apply_text_edits` → `export_pptx`。若收到 `error_code='E_VENDOR'` 说明复杂度检测漏判，按 §11.1 诚实失败信号走全量接管。详见 §11。
