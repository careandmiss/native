# ppt-master 生成 PPT 完整流程记录

**日期**：2026-09-17
**目的**：把本次"用 ppt-master Edit Native 路由，依据模板 + 文档生成 PPT"的全过程写清楚，供后续改进 `mcp_ppt_native_fill` 内容页排版时复用。
**产物**：`C:\Users\Administrator\.claude\projects\baiteng_purchase_20260917\exports\baiteng_purchase_20260917_20260917_152736.pptx`（13 页，~41 MB）

---

## 0. 输入与输出

| 项 | 值 |
|---|---|
| 内容源 | `D:\Code\tst\native_fill\3山西柏腾科技有限公司采购制度.md`（前言 + 一、目的 + 二、适用范围 + 三、基本原则 + 四、工作程序（含 一-七 子节）+ 附件） |
| 模板 | `D:\Code\tst\native_fill\柏腾ppt模版.pptx`（5 张原生 slide，蓝色科技风，思源黑体 CN） |
| Skill | `C:\Users\Administrator\.claude\skills\ppt-master` |
| 路由 | **Edit Native PPTX**（保留模板原生视觉，填入新内容） |
| 输出 PPTX | 13 张 slide，3.7 MB 内容 + 38 MB 字体备份 = 41 MB |

---

## 1. 路由选择 — 为什么是 Edit Native 而不是 Generate

两条路由对比：

| 路由 | 行为 | 适用场景 |
|---|---|---|
| **Generate PPTX** | 自由设计：从内容生成全新 PPT，模板只是风格参考 | 模板只是审美参考，不在乎模板元素 |
| **Edit Native PPTX** | 原生保留：模板的图形、装饰、字体、颜色 100% 保留，仅替换文字内容 | 需要保留模板的"工业感"设计语言 |

用户原话："我希望是依据模板+文档生成ppt 不是简单的生成模板风格的ppt"——明确要求保留模板装饰、颜色、字体。所以选 **Edit Native**。

**关键决策**：模板用了蓝色科技背景图（image1.png / image3.png）+ 椭圆装饰 + 虚线分割线 + Logo（image2.png）+ 思源黑体 CN Bold/Medium/Regular/Light 四档。Edit Native 的 roundtrip 模式把这些"非文字"资产全部保留为不可变字节层，只把 `<text>` 内容交给我们编辑。

---

## 2. 工作区建立 — `pptx_to_svg.py --roundtrip`

```bash
python "C:/Users/Administrator/.claude/skills/ppt-master/scripts/pptx_to_svg.py" \
    "C:/Users/Administrator/.claude/projects/baiteng_purchase_20260917" \
    --inheritance-mode both --roundtrip
```

### 2.1 产物清单

| 文件 | 作用 |
|---|---|
| `authoring-svg-flat/*.svg` | 13 张可编辑 SVG（5 张原生 + 8 张克隆） |
| `sources/source.pptx` | 原模板的字节级备份（不可变） |
| `animations.json` | 动画时间轴（保留） |
| `authoring_summary.json` | 每页的 text / image / placeholder 计数 |
| `conversion-report.json` | 转换诊断报告 |
| `images/image{1,2,3}.png` | 模板里抽出的 3 张图（背景、Logo、TOC 装饰） |

### 2.2 SVG 的元数据层

每个 SVG 元素带 `data-pptx-*` 属性，这些是 roundtrip 的"身份证"：

| 属性 | 作用 |
|---|---|
| `data-pptx-object` | 类型：`picture` / `shape` / `group` / `connector` |
| `data-pptx-frame` | 原 PPTX 中的位置 `x y w h`（px） |
| `data-pptx-source-ref` | `slide:N` 引用原 PPTX 的 cNvPr id |
| `data-pptx-prst` | 预设形状：`rect` / `ellipse` / `line` |
| `data-pptx-semantic-object` | 标记为"原生 shape with native text body" |
| `data-pptx-shape-id` / `data-pptx-shape-scope` | 显式保留 PowerPoint 的 shape id |

**没有 `data-pptx-shape-id` 的图片会在导出时丢失 identity**（这是后面踩坑的关键）。

---

## 3. 页面计划 — `page_plan.json`

5 张源 slide → 13 张输出 slide 的映射：

```json
{
  "schema": "ppt-master.roundtrip-page-plan.v1",
  "pages": [
    {"source_slide": 1, "svg": "slide_01.svg"},          // P1  封面
    {"source_slide": 2, "svg": "slide_02.svg"},          // P2  目录
    {"source_slide": 3, "svg": "slide_03.svg"},          // P3  PART 01 分隔
    {"source_slide": 4, "svg": "slide_04.svg"},          // P4  一、前言
    {"source_slide": 4, "svg": "p05_general.svg"},       // P5  二、总则
    {"source_slide": 3, "svg": "p06_divider_p2.svg"},    // P6  PART 02 分隔（复用 slide 3 模板）
    {"source_slide": 4, "svg": "p07_basic_app.svg"},     // P7  (一)(二)采购基本事项与申请
    {"source_slide": 4, "svg": "p08_responsibility.svg"},// P8  (三)采购人职责
    {"source_slide": 4, "svg": "p09_methods.svg"},       // P9  (四)采购方式
    {"source_slide": 4, "svg": "p10_implementation.svg"},// P10 (五)采购实施 (六)付款
    {"source_slide": 4, "svg": "p11_conduct.svg"},       // P11 (七)行为规范
    {"source_slide": 4, "svg": "p12_attachment.svg"},    // P12 附件·修订记录
    {"source_slide": 5, "svg": "slide_05.svg"}           // P13 结尾
  ]
}
```

### 3.1 复用策略

- **slide 4（内容页模板）用了 9 次**——所有正文页都基于它（1112.4 × 530.4 px 的内容区 + 顶部 chrome + 右侧 EN_LABEL）
- **slide 3（分隔页模板）用了 2 次**——PART 01 / PART 02（仅替换大标题和副标）
- **slide 1/2/5 各 1 次**——封面、目录、结尾

**关键决策**：模板只有 1 张内容页骨架，所有内容页都基于它改，意味着内容区大小、chrome 高度、字号上限都被这张骨架定死。如果想做出更好的内容页排版，要么改模板本身，要么在 SVG 编辑时改 `<g id="shape-3">` 的 frame（1124.53 × 530.4 → 自由调整）。

---

## 4. 内容编辑——13 张逐页记录

### 4.1 P1 封面（slide_01.svg）

| 元素 | 原模板 | 编辑后 |
|---|---|---|
| 主标题 | "汇报/主题 单击添加大标题"（font-size=77.33） | "山西柏腾科技有限公司 / 采购制度"（font-size=44，两行） |
| 副标 | "添加副标题"（font-size=32） | "秉公办事 · 维护公司利益"（font-size=32） |
| meta | 编号位 | "编号：BT-ZD-MOC-001" |
| 日期 | 日期位 | "2023 年发布 · 2023 年实施" |

**踩坑 1**：原标题字号 77.33 在容量门里报 69.9% 溢出（"汇报/主题 单击添加大标题" 13 字 vs 我们的"山西柏腾科技有限公司" 9 字）。降到 44 后通过。

### 4.2 P2 目录（slide_02.svg）

模板原生 3×2 = 6 槽硬布局（3 行 × 2 列），分别填：

| 左列 | 右列 |
|---|---|
| 前言 / Preface · 制度背景与适用范围 | 总则 / General Principles · 目的·范围·基本原则 |
| 采购基本事项与申请 / Basic & Application · （一）（二） | 采购人职责 / Buyer Responsibilities · （三） |
| 采购方式与实施 / Methods & Implementation · （四）（五） | 付款与行为规范 / Payment & Conduct · （六）（七）附件 |

### 4.3 P3 PART 01 分隔（slide_03.svg）

只改 2 个 text：标题 "添加大标题" → "前言与总则"，副标 → "Preface · General Principles / 制度背景 · 目的 · 适用范围 · 基本原则"。

### 4.4 P4 前言（slide_04.svg）

模板原生只有"标题 + 1 段文字"。我们扩展为：
- 标题（22px Bold 蓝）："一、前言"
- 小节 1 标题（22px Bold 蓝）："制定背景"
- 小节 1 内容（18px Medium 灰）：公司规章制度…
- 小节 2 标题："动态管理"
- 小节 2 内容：山西柏腾…
- 小节 3 标题："制度定位"
- 3 条 bullets（17px + 蓝方块标记）：规范流程 / 降低成本 / 保障资源
- 引言带（14px Light 斜体灰）："—— 摘自山西柏腾科技有限公司《采购制度》前言"

**踩坑 2**：右侧 `data-pptx-frame="827.4 36.33 346.33 28.07"` 的 EN_LABEL 在 18.67pt 时报 12.2% 溢出，降到 16pt 通过。

### 4.5 P5 总则（p05_general.svg）

- 顶部 3 张卡片（320 × 155，stroke #4874CB）："一、目的" / "二、适用范围" / "三、基本原则" + 各 3 行说明
- 中部 3 基石（圆形 1/2/3 + 标题 + 描述）："规范流程" / "明确职责" / "质优价廉"
- 底部 callout（1014 × 80，#F2F5FB）："原则要点 / 采购是一项重要、严肃的工作…"

**踩坑 3**：原始 SVG 把 3 个基石 group 都叫 `pillar-item`（duplicate id）——后面单独讲。

### 4.6 P6 PART 02 分隔（p06_divider_p2.svg）

和 P3 同结构，标题"工作程序"，副标"Working Procedures / 基本事项 · 申请 · 职责 · 方式 · 实施 · 付款 · 行为规范"。

### 4.7 P7 采购基本事项与申请（p07_basic_app.svg）

2-column：
- 左列"（一）采购基本事项"——4 条 numbered（22px 蓝方块 + 14px 标题 + 12px 描述）
- 右列"（二）采购申请"——4 张卡片（500 × 50，#F2F5FB 背景 + 圆形编号）

**内容密度高的关键页**——一个元素数量决定了视觉是否"塞满"。

### 4.8 P8 采购人职责（p08_responsibility.svg）

- 标题"六大核心职责"
- 6 张职责卡（335 × 78，3×2 grid）：每张含编号圆 + 标题 + 2 行描述
- 底部"职责细化 · 6.1 - 6.8"（1014 × 155，#F2F5FB）——4 列 × 2 行 = 8 条 bullets（12px Medium）

### 4.9 P9 采购方式（p09_methods.svg）

- 标题"四种采购方式"
- 4 张方式卡（2×2 grid）：编号 + 中英标题 + 2-3 行说明 + EN tag（LONG-TERM / ANNUAL / R&D / PRODUCTION）
- 底部核心原则带

###  10 P10 采购实施与付款（p10_implementation.svg）

- 标题（合并）
- 上半：3 步流（"下单 → 盘点入库 → 研发物料特殊"）
- 下半：2 张付款卡（"经办人报销" + "对公付款"）

### 4.11 P11 行为规范（p11_conduct.svg）

- 标题"采购经办人行为规范"
- 2 张规则卡："质优价廉的基本原则"（PRINCIPLE） + "尽职尽责 · 廉洁从业"（RED LINE）
- 红色警示 banner（1014 × 50，#E54C5E / #FCE6E9）："馈赠 · 回扣 · 贿赂 · 严重失职 · 违反原则 — 均触发辞退 / 司法处理"

### 4.12 P12 附件·修订记录（p12_attachment.svg）

- 标题"附件 · 修订记录"
- 6 列表格（1014 × 230，4 行）：日期 / 修订状态 / 修改内容 / 修改人 / 审核人 / 批准人
- 2 张 meta-block："制定人 / 审核批准人" + "发布日期 / 实施日期"

### 4.13 P13 结尾（slide_05.svg）

仅把"XXX部门" → "采购部"。

---

## 5. 容量门 — `svg_quality_checker.py --roundtrip`

```bash
python "C:/Users/Administrator/.claude/skills/ppt-master/scripts/svg_quality_checker.py" \
    "C:/Users/Administrator/.claude/projects/baiteng_purchase_20260917" --roundtrip
```

### 5.1 溢出 13 处清单（已修 13 处）

| Slide | 元素 | 原始字号 | 溢出率 | 修复 |
|---|---|---|---|---|
| slide_01 | 标题 | 77.33 | 69.9% | 改内容 + 降至 44 |
| slide_01 | 副标 | 32 | 7.6% | 改内容为"秉公办事 · 维护公司利益" |
| slide_01 | meta | 21.33 | 20.9% | 改内容为"编号：BT-ZD-MOC-001" |
| slide_04 | EN_LABEL | 18.67 | 12.2% | 降至 16pt |
| p05 | EN_LABEL | 18.67 | 6.9% | 降至 16pt |
| p07 | EN_LABEL | 18.67 | 20.5% | 改内容 + 16pt |
| p10 | EN_LABEL | 18.67 | 15.9% | 改内容 + 16pt |
| ... | ... | ... | ... | ... |

### 5.2 容量门只检查文本溢出，不检查 ID 重复 / 结构合法

**这是关键认知**：svg_quality_checker 只看 text-vs-frame 的尺寸关系。重复 ID、wrapper group、semantic marker 这类结构问题它不报。

---

## 6. 导出踩坑 — `svg_to_pptx.py --roundtrip`

整轮导出踩了 **4 类共 5 次硬错**，每个都暴露了一个 ppt-master Edit Native 的硬约束。

### 6.1 错误 1：duplicate SVG id

```
Error: p05_general.svg: duplicate SVG id(s) are not allowed in explicit Layout mode: pillar-item-1
```

**根因**：3 个基石都叫 `pillar-item`（最初是 `pillar-item`、`pillar-item`、`pillar-item`），合并成 `pillar-item-1` 后仍 3 个相同 id。SVG explicit Layout mode 不允许同 id 多次出现。

**修复**：手动改 3 个基石 id 为 `pillar-item-1` / `pillar-item-2` / `pillar-item-3`。

**对 native_fill 的启示**：
- 我们渲染多个相同 archetype（如 6 张职责卡、4 步流程）时，**每个实例必须有唯一 id**。
- 渲染器生成 SVG 时应当用 `archetype-name + counter` 模式（`duty-card-1`、`duty-card-2`…），而不是全部用同一 id。

### 6.2 错误 2：top-level wrapper group 丢失 identity

```
Error: slide_02.svg: Edited round-trip source object did not produce a DrawingML shape: 61
```

**根因**：slide_02（TOC）的顶层有 `<g id="shape-61" data-pptx-object="group">` 包了背景图 + 渐变矩形 + 白板。**单子 group 在显式 Layout mode 下被 flatten**——wrapper group 消失，子元素被提升为 top-level，但 `shape-61` 这个 id 在输出中找不到对应。

**修复**：删除 wrapper `shape-61`，把 `shape-55`（picture）、`shape-56`（rect 渐变）、`shape-58`（rect 白板）直接作为 top-level。

**对 native_fill 的启示**：
- 编辑 SVG 时不要保留单子 wrapper group——它会被 flatten 并丢失 identity。
- 如果一定要分组，应当让 group 有 ≥2 个视觉子元素，且 `data-pptx-object="group"`。

### 6.3 错误 3：嵌套 SVG picture 丢失 shape id

```
Error: slide_03.svg: Edited round-trip source object did not produce a DrawingML shape: 8
```

**根因**：模板的 background picture 长这样：
```xml
<g id="shape-8" data-pptx-object="picture" data-pptx-source-ref="slide:8">
  <svg viewBox="..." preserveAspectRatio="none">
    <image href="../images/image1.png" .../>
  </svg>
</g>
```
single-child group 被 flatten，`<svg>` 直接变 top-level picture。**但 picture 的 shape_id 来自内部 `<svg>` 的 `data-pptx-shape-id`（不存在）→ 输出是 fresh id，不是 8**。merge 阶段找 shape-8 找不到。

**修复**：给 `<g>` 加 `data-pptx-shape-id="8" data-pptx-shape-scope="slide"`，强制保留 id 8。

**对 native_fill 的启示**：
- **每个 picture / group / shape 元素必须有显式 `data-pptx-shape-id` 和 `data-pptx-shape-scope`**，否则 roundtrip 时会被分配 fresh id 而丢失与原 PPTX 的对应关系。
- 这是 hidden requirement——文档没明说，但 merge 阶段必须靠这个 id。

### 6.4 错误 4：semantic shape 多 text

```
Error: slide_04.svg: Failed to convert <g id="shape-3">: Semantic shape text must be one direct SVG text component
```

**根因**：模板的 `<g id="shape-3" data-pptx-semantic-object="shape">` 是"原生 shape + 单一 native text body"的载体，要求**只有一个 direct `<text>` 子元素**。我们往里塞了 8+ 个 text / bullet g，converter 拒绝。

**修复**：删掉 `data-pptx-semantic-object="shape"` 属性（8 个内容页全改了）。

**对 native_fill 的启示**：
- `data-pptx-semantic-object="shape"` 只用于"和原 PPTX 一字不差的简单文本框"。**多段落 / 多 bullets / 多卡片的内容区不能用这个标记**。
- 内容页（archetype-rich 页面）应当把这个属性从主内容容器上拿掉。
- 我们的内容页 archetype（hero_statement / procedural-steps / revision-table）全都是多 text 容器 → **必须拿掉这个标记**。

### 6.5 错误汇总

| 错误 | 触发条件 | 修复 | 是否会再次出现 |
|---|---|---|---|
| duplicate id | 多个相同 archetype 实例共享 id | 用 counter 后缀 | 是（每次多 archetype 时） |
| wrapper group flatten | 单子 `<g data-pptx-object="group">` | 删 wrapper，让子元素直接 top-level | 罕见 |
| picture id 丢失 | 嵌套 svg 的 picture 缺 `data-pptx-shape-id` | 加 `data-pptx-shape-id="N" data-pptx-shape-scope="slide"` | **几乎所有 picture** |
| semantic shape 多 text | 主内容容器带 `data-pptx-semantic-object="shape"` 且 >1 text | 删属性 | **几乎所有内容页** |

**经验**：每次换模板，第一轮导出大概率踩 4 类错误。准备一份"修复脚本"——根据上面 4 条模式批量改 SVG。

---

## 7. 最终导出命令与产物

```bash
python "C:/Users/Administrator/.claude/skills/ppt-master/scripts/svg_to_pptx.py" \
    "C:/Users/Administrator/.claude/projects/baiteng_purchase_20260917" --roundtrip
```

最终输出：
- **PPTX**：`C:\Users\Administrator\.claude\projects\baiteng_purchase_20260917\exports\baiteng_purchase_20260917_20260917_152736.pptx`
- **Postflight**：status=passed-with-warnings, 13 slides, 2 warning categories（quality_gate=not-provided, unsafe_exported_font_faces=9）
- **Report**：`C:\Users\Administrator\.claude\projects\baiteng_purchase_20260917\validation\baiteng_purchase_20260917_20260917_152736.report.json`

---

## 8. 验证

### 8.1 delivery check

```bash
python "C:/Users/Administrator/.claude/skills/ppt-master/scripts/pptx_delivery_check.py" \
    "C:/.../baiteng_purchase_20260917_20260917_152736.pptx"
```

结果：
- ✅ errors: []
- ⚠️ advisories: 1 条（font_portability，9 个字体不在常见 Office 集合：`OPPO Sans 4.0 Light` / `微软雅黑` / `思源黑体 CN Bold/Medium/Regular/Light` / `Source Han Sans Bold/Regular`）
- ✅ motion: 1 张 slide 有 timing（slide 2 TOC），其余无动画

### 8.2 read-back

```bash
python "C:/Users/Administrator/.claude/skills/ppt-master/scripts/source_to_md.py" \
    "C:/.../baiteng_purchase_20260917_20260917_152736.pptx"
```

产出 `*.md` + `*_files/` 提取的 3 张图片。13 张 slide 文字内容均完整保留（中文 + 英文标 + bullet 列表全在）。

---

## 9. 内容页排版改进清单 — 给 native_fill 用

基于本次流程沉淀的可落地改动，按优先级排序：

### P0 — 立即做（影响所有内容页）

1. **picture 强制加 shape-id**：native_fill 渲染任何 `<g data-pptx-object="picture">` 时，自动注入 `data-pptx-shape-id="<unique>"`、`data-pptx-shape-scope="slide"`。否则 roundtrip 必踩坑 6.3。
2. **多 text 容器去 semantic 标记**：任何 `<g>` 里 ≥2 个 `<text>`，强制去掉 `data-pptx-semantic-object="shape"`。否则 roundtrip 必踩坑 6.4。
3. **archetype 实例 id 加 counter**：6 张卡 → `duty-card-1`/`duty-card-2`/…，4 步 → `step-1`/`step-2`/…。否则 roundtrip 必踩坑 6.1。

### P1 — 模板结构性改进（影响版式）

4. **理解内容页骨架**：模板 slide 4 的 `<g id="shape-3">` 是 1124.53 × 530.4 的内容区，外加 64px 顶部 chrome + 28px EN_LABEL。我们的所有 9 张内容页都受这个 530px 高度限制。**想多塞内容就得改这个 frame 或做分页**。
5. **字号档位**（依据容量门实测）：
   - 标题（22pt Bold 蓝）—— `<g id="shape-3">` 内主标题
   - 小标题（18pt Bold 蓝）—— 卡片 / 小节标题
   - 正文（14pt Medium 灰）—— 卡片描述
   - 副描述（12pt Regular 浅灰）—— 二级说明
   - EN_LABEL（16pt Medium）—— 右上角英文标（不能再降，否则太小）
   - Bullet（11-12pt Medium）—— 列表项
6. **配色**：深蓝 `#1D2CAB`（标题）/ 中蓝 `#4874CB`（accent）/ 橙 `#EE822F`（强调）/ 红 `#E54C5E`（警示）/ 浅蓝 `#F2F5FB`（callout 底）/ 灰 `#576B93`（描述）/ `#404040`（正文）。模板原色，不要改。

### P2 — 内容密度与排版美学（用户感知）

7. **卡片宽度公式**：单列 335px / 双列 320px / 三列 320px / 满宽 1014px。这是基于 1124.53px 内容区减去 50px×2 边距得出的。
8. **卡片高度节奏**：标题卡 78-155px / 描述卡 50px / callout 80-155px。
9. **bullet 设计**：用 6×6 蓝方块（`#4874CB`）+ 17pt 文字，比单纯文字 bullet 更有节奏感。
10. **EN_LABEL 必须存在**：右上角的英文副标（"Preface · 前言" / "Methods · （四）"）是模板的"画眼"，**不要去掉**。

### P3 — 工程纪律（预防性）

11. **导出前自检脚本**：写一个 `preflight_check.py`，对每个 SVG 检查：
    - 所有 id 唯一
    - 所有 `<g data-pptx-object="picture">` 有 shape-id
    - 所有 `<g data-pptx-semantic-object="shape">` 最多 1 个 direct `<text>` 子元素
    - 任何 `data-pptx-object="group"` 的 `<g>` 至少有 2 个视觉子
13. **失败重试基线**：导出失败时按 6.1-6.4 自动诊断 + 自动修复，而不是手动 5 轮 debug。

---

## 10. 一句话总结

**Edit Native PPTX 的本质 = 模板字节级保留 + 文字内容替换**。但隐藏 4 类结构硬约束（id 唯一 / shape-id 必备 / 嵌套 group flatten / semantic shape 限制）。第一次跑每个新模板都要踩一遍这 4 个坑，第二次以后写 preflight 脚本批量预防。