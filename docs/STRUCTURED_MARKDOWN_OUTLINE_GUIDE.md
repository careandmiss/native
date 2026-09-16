# Structured Markdown Outline Guide

**Status**：active (Phase 2 + Phase 4, 2026-09-16)
**Audience**：caller writing markdown that feeds `native_fill` MCP server

This guide describes the markdown conventions `native_fill` understands.
It is the caller-side companion to
[`CONTENT_PAGE_FILL_PLAN_2026-09-16.md`](./CONTENT_PAGE_FILL_PLAN_2026-09-16.md)
and [`PHASE0_INTEGRATION_NOTES_2026-09-16.md`](./PHASE0_INTEGRATION_NOTES_2026-09-16.md).

---

## 1. 哲学

`native_fill` 走 **ppt-master 风格的"约束输入"**——caller 提供的 markdown 是大纲权威源，渲染器只负责忠实呈现。LLM 不参与大纲生成（可调用，但失败时降级为 warning 继续跑）。

**两个核心动作**：

- **大纲切分**：TOC 顺序 = markdown H1 顺序
- **章节路由**：每个 H1 自动选择 layout（A 路径启发式 / E 路径显式）

---

## 2. 大纲切分：H1 是 section 边界

`split_markdown_sections` (`toc_detection.py:582`) 用 `^# ` 正则切分 markdown。
每个 H1 = 一个 section：

- section 标题 = H1 文本（去 inline markdown 加粗/斜体/代码标记）
- section body = H1 到下一个 H1 之间所有内容
- TOC 顺序 = section 出现顺序

**关键约束**：

- 想进 TOC 的章节 → 必须用 `# ` (H1)
- 子章节 → 用 `## ` (H2)，**不进 TOC，只作为该 section 的子内容**
- 多级（`### `、`#### `）→ 同样不进 TOC

> ppt-master 用 **H2** 作 section 边界；本仓库用 **H1**。
> 这是设计差异，不是 bug。

### 2.1 升格 H2 → H1 的实战

如果 caller 原本写了 H2 子章节，但希望它们都进 TOC，**直接升格为 H1**：

```markdown
# 第一章 项目管理
# 1.1 项目立项        ← 升格前: ## 1.1 项目立项
正文...
# 1.2 项目执行        ← 升格前: ## 1.2 项目执行
正文...
```

升格后 TOC 出现 3 项：`第一章项目管理 / 1.1 项目立项 / 1.2 项目执行`。
注意：原本的父章节（如"第一章 项目管理"）如果升格后**没有自己的 body**，会被解析成空 section——`cards_for_section` 会用 section title 作为 fallback 占位。

### 2.2 父章节 + 子章节 的两种写法

| 写法 | 含义 | TOC | 推荐场景 |
|---|---|---|---|
| `# 父` + `## 子1` + `## 子2` | 父进 TOC，子不进 | 1 项 | 父概念明确，子是细节展开 |
| `# 父` + `# 子1` + `# 子2` | 全部进 TOC | 3 项 | 每章独立内容 |

---

## 3. 章节路由：A 路径 + E 路径

每个 H1 section 在 `expand_workspace_from_markdown` (`workspace_expand.py:188-247`) 中根据 metadata 选择 layout。

### 3.1 A 路径：自动选择（默认）

caller 不写任何 meta → 走启发式：

| body 形状 | layout | 视觉效果 |
|---|---|---|
| 单段长文本（≥1 个段落，无列表） | `simple-text` | 1 张半透明矩形 + 多行自动换行 |
| 1 段 + 多 item 列表 | `bullet-list` | 左侧色块 + 垂直编号项 |
| 多段 / 多 H2 子章节 | `3-column-cards` | 1-4 个并排卡片 |

**适用场景**：caller 懒得声明 meta，让 native_fill 自动决定。

### 3.2 E 路径：caller 显式声明

caller 在 H1 body 顶部加 `>` blockquote 元数据：

```markdown
# 一、目的

> **layout**: hero-number
> **value**: 3
> **caption**: 大目标

正文内容...
```

可选字段（按 `render_new_block` 支持）：

| 字段 | 说明 | 适用 layout |
|---|---|---|
| `layout` | 必须 — 渲染器名 | 任意 |
| `value` / `number` | 大数字 | `hero-number` |
| `caption` / `text` | 说明文字 | `hero-number` / `simple-text` |
| `items` | 列表（逗号/顿号/分号分隔） | `bullet-list` |
| `quote` / `attribution` | 引言 + 出处 | `callout-box` |
| `left` / `right` | A/B 对比 | `two-column-compare` |
| `steps` / `nodes` | 步骤列表 | `timeline` / `flow-steps` |

**宽松正则**（`_META_RE` in `toc_detection.py:551`）：

- `>` 必须紧跟 `**字段名**`
- 字段名与值之间：全角冒号 `：` 或半角冒号 `:` 都接受
- 字段名无白名单——任何 `**xxx**` 都会进 meta，渲染器只取认识的字段

### 3.3 完整 E 路径示例

```markdown
# 前言
> **layout**: simple-text
> **text**: 为了规范公司采购行为, 降低采购成本...

# 一、目的
> **layout**: hero-number
> **value**: 3
> **caption**: 大目标

# 二、范围
> **layout**: bullet-list
> **items**: 设备采购、物料采购、服务采购、研发采购、生产采购

# 四、流程
> **layout**: flow-steps
> **steps**: 需求申请 → 采购审批 → 供应商选择 → 合同签订 → 验收入库 → 付款结算
```

---

## 4. Cover title backfill

`native_fill` 自动把 markdown H1（或文档标题）写到封面大标题形状，**不依赖 LLM**：

- 取文档第一个非空 H1 作为标题候选
- fallback：markdown 文件名 → 大写转空格
- 在 `llm_planner.backfill_cover_title()` 确定性触发

caller 无需配置——但若 caller 已在 `content_mapping` 显式覆盖封面标题，**caller 优先**。

---

## 5. TOC 槽位与溢出克隆

TOC 模板默认 6 槽（3 行 × 2 列，`TOC_TOP = {"rows": 3, "cols": 2}`）。

- **N ≤ 6**：TOC 一张，多余槽清空
- **N > 6**：TOC 第一张填 6 项，超出部分自动克隆为 `slide_part02_toc.svg`、`slide_part03_toc.svg`... 每张填 6 项

caller 无需关心——`expand_workspace_from_toc` 自动处理。

---

## 6. 完整 boteng 案例

源 markdown (`3山西柏腾科技有限公司采购制度.md`) 经过升格后：

```
13 个 H1: 前言 / 一、目的 / 二、适用范围 / 三、基本原则 /
          四、工作程序 / （一）-（七） 采购... / 附件:
```

生成 PPT:
- slide 1：封面（"采购制度"）
- slide 2：TOC 第一张（6 项）
- slide 3：TOC 克隆（6 项：二-七章子节）
- slide 4：TOC 克隆（1 项：附件）
- slide 5-17：13 张正文（每节 1 张 div + 1 张 content）
- slide 18：致谢页

共 18+ 张幻灯片，TOC 自动溢出，cover title backfill 生效。

---

## 7. 不要做的事

| ❌ 不要 | 原因 |
|---|---|
| 不要用 `### ` H3 当 section | `split_markdown_sections` 只识别 H1 |
| 不要在同一段落里混 `> **layout**: simple-text` 和正文 | meta 行被剥离后正文可能错位 |
| 不要依赖 LLM 帮 caller 写大纲 | LLM 失败时会跳过整页（已降级为 warning 但仍可能丢内容）|
| 不要假设 LLM 一定可达 | `network error reaching https://...` 是已知 advisory |

---

## 8. 相关文件

| 路径 | 角色 |
|---|---|
| `src/mcp_ppt_native_fill/toc_detection.py` | `split_markdown_sections` + `_extract_section_meta` + `cards_for_section` |
| `src/mcp_ppt_native_fill/workspace_expand.py` | `expand_workspace_from_markdown` (A/E 路径 dispatch) |
| `src/mcp_ppt_native_fill/block_renderer.py` | `render_new_block` (layout 渲染器) |
| `src/mcp_ppt_native_fill/llm_planner.py` | `backfill_cover_title` (cover title 确定性回填) |
| `docs/CONTENT_PAGE_FILL_PLAN_2026-09-16.md` | 实现计划 |
| `docs/PHASE0_INTEGRATION_NOTES_2026-09-16.md` | 集成说明 |