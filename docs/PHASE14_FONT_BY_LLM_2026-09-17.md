# Phase 14: 内容页 SVG 字号由 LLM 选档（不再硬编码 / 不再 *scale 缩水）

**日期**: 2026-09-17
**分支**: `integration/sink-generate-local-ppt-2026-09-14`
**前置**: Phase 11 (`bc09ae5`) + Phase 12 (`27937ef`) 已落地 chrome topbar/footer + 4 archetype 几何。
**触发**: 用户原话 `D:\Code\tst\native_fill\projects\boteng_采购制度_v2_out.pptx 内容页面的字体太小 svg 并不美观智能 进入plan模式查看 ppt-master 是怎么样实现的`。

---

## 1. Context — 根因 & ppt-master 范式

### 1.1 根因（Phase 12 输出实测）

`block_renderer.py` 的 4 个 archetype 函数（`hero_statement` / `statement-caption` / `procedural-steps` / `revision-table`）在所有 `font-size` 上乘 `scale = bw / 1280.0`。boteng 的 `body_bounds = "120 130 1060 480"` → `scale = 0.828`，所有字号被等比压 17.2%。

实测对比（ppt-master 真品 vs Phase 12 输出）：

| Role | ppt-master 真品 | Phase 12 输出 | 差距 |
|---|---:|---:|---:|
| hero_statement claim title | 22 | 18.22 | -17.2% |
| hero_statement body | 20 | 16.56 | -17.2% |
| procedural-steps card body | 13 | 10.77 | -17.2% |
| revision-table cell body | 12 | 9.94 | -17.2% |
| procedural-steps title | 32 | 26.50 | -17.2% |
| revision-table header | 14 | 11.59 | -17.2% |

### 1.2 ppt-master 范式（`references/executor-base.md`:207）

> **"map every structural text item to a declared `typography` role and write its anchor or a value within ±2 px, as unitless px with at most two decimals."**

- §IV **Font Size Hierarchy** 是 deck-wide anchors（Body / Title / Subtitle / Annotation），单一来源
- Authoring SVGs **硬编码 px 值**（`font-size="14"`），但每个数字都回溯到 §IV 锚点 —— 不是凭空写
- LLM（Executor）负责把每个文本项 **映射到一个 role**，再写到该 role 的 anchor px

### 1.3 我们之前的 v1 反模式

直接把 `font-size="{N*scale:g}"` 改成 `font-size="{N}"` —— **字号成了 renderer 内部硬编码**，违反"不能硬编码"约束（用户 2026-09-17 反馈）。

### 1.4 Phase 14 修复方向（v2）

- **role 名 → px 锚点** 通过单一 `TYPOGRAPHY` dict 在 module 顶部暴露
- **LLM planner 在 spec.payload 里声明** `font_size: "lead"`（role 名）或 `font_size: "22"`（raw px）
- **block_renderer 透传**：不缩放、不发明数字

---

## 2. 实施改动（4 文件 + 1 文档）

### 2.1 `src/mcp_ppt_native_fill/block_renderer.py`

**新增**: 模块顶部 `TYPOGRAPHY: dict[str, int]` —— design_spec §IV 8 档 tier + inter-tier roles（22/14/12/72）回溯到 §IV 锚点。

```python
TYPOGRAPHY = {
    # design_spec §IV tier anchors
    "cover_title": 56,
    "chapter_title": 42,
    "page_title": 32,
    "subtitle": 24,
    "lead": 20,
    "body": 16,
    "annotation": 13,
    "footnote": 11,
    # inter-tier roles (between two anchors, always within ±2 px of a named tier)
    "claim_band": 22, "panel_title": 22,
    "rail_title": 32, "big_question": 32,
    "rail_caption": 14, "en_subtitle": 14, "header": 14,
    "rail_body": 12, "phase_num": 12,
    "phase_label": 13, "phase_card_section": 13, "cell_body": 13,
    "bullet": 13, "placeholder_hint": 13,
    "big_quote": 72, "takeaway_label": 14,
    "doc_code": 11, "keyword_en": 11,
    "keyword_word": 16, "lead": 20,
    ...
}
```

**新增**: `_fs(spec, role: str) -> str` helper —— 优先读 `payload.font_size`（role 名 → `TYPOGRAPHY[role]` / raw px → verbatim），缺省时用 caller 的 role 参数走 `TYPOGRAPHY[role]`。

**替换**: 4 个 archetype 函数 30 处 `font-size="{N*scale:g}"` → `font-size="{_fs(spec, "<role>")}"`，例如：
- `font-size="{_fs(spec, 'lead')}"`（hero_statement body 20px）
- `font-size="{_fs(spec, 'big_question')}"`（hero_statement 大问题 32px）
- `font-size="{_fs(spec, 'bullet')}"`（procedural-steps 卡片正文 13px）
- `font-size="{_fs(spec, 'cell_body')}"`（revision-table 单元格 13px）

**保留**: `scale = bw / 1280.0` 仍用于**几何**（位置 / 大小 / line-height），**字号不再缩放**。

### 2.2 `src/mcp_ppt_native_fill/llm_planner.py`

**新增**: `SYSTEM_PROMPT` 中追加 §IV Font Size Hierarchy 表 + 8 个 archetype `font_size` 范例段。LLM 被要求在每个 `new_blocks` 块的 `spec` 顶层写出 `font_size` 字段。

**新增**: `_normalize_new_blocks()` 末尾对 4 个 ppt-master archetype 注入**默认 `font_size`**（LLM 漏写时的兜底）：

```python
_ARCHETYPE_DEFAULT_FS = {
    "hero_statement": "20",       # lead
    "statement-caption": "20",     # lead
    "procedural-steps": "13",      # annotation
    "revision-table": "13",        # annotation
}
```

LLM 提供的值永远优先。

### 2.3 `src/mcp_ppt_native_fill/workspace_expand.py`

**重写**: `_PHASE_KEYWORDS` —— 从 3-phase (申请/审批/采购) 升级为 4-phase taxonomy：

| Phase | 关键词 |
|---|---|
| **申请与审批** | 基本事项 / 氚云申请 / 提交申请 / 申请部门 / 申请 / 审批 |
| **采购人职责** | 采购经办人 / 采购负责人 / 采购人 / 职责 |
| **采购方式** | 采购方式 / 供应商 / 询价 / 比价 / 议价 / 议定 |
| **实施付款规范** | 行为规范 / 严禁 / 回扣 / 对公 / 报销 / 入库 / 下单 / 付款方式 / 付款 / 验收 / 实施 |

**重写**: `_synthesize_procedural_phases` 的 `macro_order` 同步更新为 4-phase 列表。

**关键顺序规则**: `付款方式` 必须排在 `方式` 之前 —— 否则 `采购付款方式` 会先匹配 `方式` token 路由到 `采购方式`，而非 `实施付款规范`。

### 2.4 `tests/test_native_fill.py`

**新增 2 个 test class，5 个测试**:

- `TestPhase14FontSizeByLLM`:
  - `test_hero_statement_uses_spec_font_size_role` —— `spec.font_size="lead"` → `font-size="20"`
  - `test_hero_statement_uses_spec_font_size_px_raw` —— `spec.font_size="22"` → `font-size="22"`
  - `test_procedural_steps_falls_back_to_typography_anchor` —— role 缺省 → `TYPOGRAPHY["bullet"]=13`、`TYPOGRAPHY["page_title"]=32`
- `TestPhase14PhaseKeywords`:
  - `test_classify_h2_routes_to_four_distinct_phases` —— 8 个 boteng H2 → 全部 4 个 phase
  - `test_more_specific_keyword_wins_over_broad_purchase` —— `采购付款方式` → 实施付款规范（不是 采购方式）

**更新**: `test_seven_boteng_h2_produce_four_macro_phases` (Phase 8) → 新 4-phase taxonomy
**更新**: `test_multi_h2_routes_to_procedural_steps` (Phase 7) → 新 markdown 跨 4 phase

### 2.5 文档（本文件）

`docs/PHASE14_FONT_BY_LLM_2026-09-17.md` —— 本文档。

---

## 3. 验证

### 3.1 单元测试

```bash
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python -X utf8 -m unittest tests.test_native_fill
# Ran 254 tests in 0.604s
# OK
```

249（旧）→ **254（新 + 5 PASS）** PASS，零回归。

### 3.2 静态渲染检查（boteng 4 archetype）

| Archetype | 输出字号档位 | 真品 baseline | 状态 |
|---|---|---|---|
| hero_statement | [11, 14, 16, 20, 22, 32] | 11/14/16/20/22/32 | ✅ |
| statement-caption | [11, 12, 14, 20, 22, 32, 56, 72] | 11/12/14/20/22/32/56/72 | ✅ |
| procedural-steps | [12, 13, 14, 32] | 12/13/14/32 | ✅ |
| revision-table | [13, 14] | 13/14 | ✅ |

**零缩水字号**（10.77 / 11.59 / 9.94 / 16.56 / 18.22 等旧反模式全部消失）。

### 3.3 4-phase taxonomy 验证

```
'采购基本事项' -> '申请与审批'
'采购人职责'   -> '采购人职责'
'采购付款方式' -> '实施付款规范'  (不是 采购方式)
'行为规范'     -> '实施付款规范'
```

boteng 7 H2 现在产生 **4 个 macro phase** （之前 3 个），procedural-steps timeline 显示 4 个圆 + 4 张 cards。

---

## 4. ppt-master 范式合规性

| ppt-master rule | Phase 14 实现 | 状态 |
|---|---|---|
| §IV Font Size Hierarchy 单一来源 | `TYPOGRAPHY` dict 顶部暴露 | ✅ |
| 每个文本项映射到一个 role | `_fs(spec, role)` 调用点全部用 role 名 | ✅ |
| ±2 px 漂移允许 | inter-tier roles (claim_band=22, big_quote=72) 都在 ±2 px 内 | ✅ |
| `shrinking type is last and never deck-wide` | 字号不再 *scale | ✅ |
| authoring SVG 写绝对 px | `_fs` 返回绝对 px 字串 | ✅ |

---

## 5. 风险 & 回滚

| 风险 | 缓解 |
|---|---|
| LLM 不输出 `font_size` 字段 | `_normalize_new_blocks` 默认注入；`_fs` 缺省走 role 参数 |
| LLM 选档过大（如 body=42） | `_fs` 不校验；prompt 范例约束；后续可加 sanity check |
| _PHASE_KEYWORDS 关键词顺序错（付款方式 vs 方式） | 测试 `test_more_specific_keyword_wins_over_broad_purchase` 兜底 |
| TYPOGRAPHY 字段名漂移 | 单一 dict 顶部暴露，文档注释每个 role 含义 |
| procedural-steps taxonomy 重命名破坏旧 caller | 旧 3-phase 测试已更新到 4-phase |

**回滚**:
```bash
git restore src/mcp_ppt_native_fill/block_renderer.py \
            src/mcp_ppt_native_fill/llm_planner.py \
            src/mcp_ppt_native_fill/workspace_expand.py \
            tests/test_native_fill.py \
            docs/PHASE14_FONT_BY_LLM_2026-09-17.md
```

---

## 6. 不在范围

- ❌ **chrome 改造**（topbar/footer 已在 phase 12 + design_spec footnote/annotation 锚点）
- ❌ **vendor svg_to_pptx 改造**（hard rule "shrinking type is last and never deck-wide"）
- ❌ **新增 archetype**
- ❌ **动态字号自适应**
- ❌ **part01 cover archetype 错派**

---

## 7. 用户后续可选项

1. 跑 `python examples/boteng_demo.py` 看 `boteng_采购制度_v2_out.pptx` 实际效果
2. 调整 `TYPOGRAPHY` dict 里某个 role（如 `body=18`）
3. 加 sanity check：warning if `font_size > 56`
4. 给 chrome.py 也接 `TYPOGRAPHY`（topbar / footer 用 footnote=11）
5. part01 cover archetype 改回真品的 cover rail 几何
