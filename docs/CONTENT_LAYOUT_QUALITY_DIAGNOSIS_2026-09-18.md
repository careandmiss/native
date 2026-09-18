# Content Page SVG Layout Quality Diagnosis (2026-09-18)

## 背景

用户在完成 Round-3 (Layer 1+2) + Phase 15 (内容分页) 后,继续反馈:

> "内容页面svg的排版质量太差了 分析可以怎么样改进"

通过检查 `workspace/3山西柏腾科技有限公司采购制度_auto/authoring-svg-flat/`
下的 6 张 content SVG (`slide_part02..06_content.svg`) + `slide_part05b_content.svg`,
发现 6 类系统性质量问题。

## 证据

| Slide | layout | key issues |
|---|---|---|
| slide_part02 | statement-caption | body 文本同时出现在蓝底白字 rail (32pt) 与白底黑字 panel (22pt) — 视觉重复 |
| slide_part03 | callout-box | 仅 1 句 + `"` glyph,下半页 350px 空白 |
| slide_part04 | (raw→hero_statement?) | rail+panel 渲染,仅 1 句口号 |
| slide_part05 | (空模板) | "本节要点 / (待补充)" — LLM 漏填 spec.body |
| slide_part05b | 3-column-cards | 三卡 fill-opacity=0.12 同质,无层次 |
| slide_part06 | revision-table | 表头列分隔线 385/650/915 ≠ 文本中心 252.5/517.5/782.5/1047.5 |

## 6 类根因

### R1. 顶部 chrome 三层堆叠 (template + injected)

每个 content slide 同时承载:

- `shape-14/15/16` 左椭圆 (template 原生, y=29-78)
- `shape-17` 章节标题框 (y=25-82)
- `shape-21` 虚线 (y=68-69)
- `shape-3` 外框 (y=102-633)
- `slide-topbar` 注入 (y=32-68) ← **与 `shape-17` 垂直重叠**
- `slide-footer` 注入 (y=680-710)

→ 即使 archetype 不同也很难看出视觉差异。

### R2. body_bounds 全部硬编码 `120 130 1060 480`

LLM SYSTEM_PROMPT (llm_planner.py:304) 的 JSON 示例把 `bounds` 写死:

```json
{
  "id": "content-body",
  "bounds": "120 130 1060 480",
  ...
}
```

`block_renderer.render_new_block` (line 278-280) 优先用 `spec.bounds`,
**无视 `archetype_meta.body_bounds`**。

→ 17 种 archetype **body frame 完全一致**, archetype 只改变内部布局。

### R3. `chrome_suppress_for` 没真正生效

`chrome.py` 定义了 `chrome_suppress_for(archetype)`,但实际只有 chrome.py 自己调用。
`pipeline._inject_content_chrome` (pipeline.py:850) **从未调用它**。

→ hero archetypes (hero_statement / callout-box) 该出现的"无 topbar 大字宣言"页
**根本没机会出现**。

### R4. statement-caption 误用于普通段落 + caption/body 同串

SYSTEM_PROMPT (llm_planner.py:191):

```
single long paragraph (≥80 chars) / chapter manifesto → statement-caption
```

但 statement-caption 设计是 **rail 装短标语 + panel 装正文**,
LLM 经常把同一段 body 同时塞进 rail 和 panel,
导致"一句话重复两次"。

→ `slide_part02` "为了提高公司采购效率..." 同时出现在蓝底白字 rail 与
白底黑字 panel。

### R5. raw 兜底导致"一句话 + 空白"

SYSTEM_PROMPT 提到 raw 兜底,但 raw 是 caller-supplied SVG,block_renderer:
```python
if layout == "raw":
    inner = payload.get("svg", "") or spec.get("svg", "")
    if not inner:
        raise ValueError("layout='raw' requires spec.svg")
```

LLM 没传 `svg` 时:**直接选 raw 但不传 body**,渲染失败或留白。
`slide_part04` / `slide_part05` 都是这个问题。

### R6. 3-column-cards / revision-table 几何细节错

- 三列卡片 fill-opacity=0.12 同质,主色块/副色块无层次区分
- revision-table 表头 text-anchor=middle 的 x 没用
  `col_x_center = col_x + col_w/2` 公式,LLM 随便给导致列分隔线与文本中心错位

## 改进路线图 (Tier 1 优先)

#### Tier 1 — 高 ROI,小改动

1. **`pipeline._inject_content_chrome` 接入 `chrome_suppress_for`** (R3 修复)
2. **从 LLM JSON 示例删掉 `"bounds": "120 130 1060 480"`** 改 archetype_meta 接管 (R2 修复)
3. **`statement-caption` 强制 `caption` ≠ `body`** 在 normalizer 校验 (R4 修复)
4. **`raw` 不允许作为内容 layout** — 强制 fallback 到 `simple-text` (R5 修复)

#### Tier 2 — 中 ROI,中等改动

5. LLM prompt 加 archetype 选择决策树
6. `3-column-cards` 主卡 fill-opacity=0.85 + 副卡 0.06
7. `revision-table` 列对齐公式 fix
8. 新增 `outline-toc` archetype

#### Tier 3 — 大改动

9. 顶部 chrome 重构 (走 v2 模板,route 到 slide_06/07/08)
10. `slide-topbar` y=32 与 `shape-17` y=25 不再重叠
11. 画布利用率:body 自适应 shrink bounds
12. 全局字号 8-tier 强制

## 建议

本轮先做 Tier 1 (1-2h 投入) → 跑测试看 6 张 SVG 视觉变化 → 再决定 Tier 2。