# Phase 13 v2: chrome/archetype opt-out — 精简灵活性方案

**日期**: 2026-09-17
**前置**: Phase 11 (commit `bc09ae5`) + Phase 12 (commit `27937ef`) 已落地 chrome topbar/footer + 4 个 archetype 几何（statement-caption / hero_statement / procedural-steps / revision-table）。当前未提交改动：chrome.py +31 行, pipeline.py +227 行, block_renderer.py +49 行（含 4 个 `_legacy_*` 引用但函数未实现）。
**修正原因**: 原 Phase 13 plan 8 option + 2 新 archetype + 4 个 `_legacy_*` 提取过度工程化。重读 `D:\mcp-ppt-native-fill\docs\ppt-master-native-fill-internals.md` §3.3 + 对照 `projects/boteng_ppt_20260916/svg_final/` 真品后定稿。

---

## Context: 真品调研修正了什么

| 原 Phase 13 plan 假设 | 真品验证结果 | 影响 |
|---|---|---|
| `section_divider` = 暗背景 full-bleed + 280px 金 numeral | 真品 `02_preface.svg` 是 **左 240px gradient rail + 右 876px 白 panel**——和 `statement-caption` 几何重叠 | ❌ 删除新 archetype；`statement-caption` 已覆盖 |
| `closing` = 84px THANK YOU 卡片 | 真品 `01_cover.svg` 是**封面**不是"谢谢"；ppt-master 无 closing archetype | ❌ 删除新 archetype |
| 需要 4 个 `_legacy_*` 函数（从 git history 2838ff6^ 提取） | Phase 11 是**几何重塑**不是"新 archetype + legacy 切换"——`git show 2838ff6^:block_renderer.py` 也没现成 legacy 实现 | ❌ 删除 _legacy_*；改"还原默认 Phase 10 几何" = `git revert block_renderer.py` 不现实，简单方案是 gate=False 时 raise ValueError |
| 8 个 boolean/option（`enable_chrome_topbar` / `enable_chrome_footer` / `enable_ppt_master_archetypes` / `enable_section_divider` / `enable_closing_archetype` / `chrome_meta` / `palette` / `archetype_override`） | 用户原话是"并不是全部页面都要 svg 我们需要**灵活性**"——只需要 2 个开关 | ✅ 简化到 2 个：`enable_chrome` (合并 top+footer) + `enable_ppt_master_archetypes` |

**vendor 边界澄清**（来自 `ppt-master-native-fill-internals.md` §2）：
> vendor `svg_to_pptx --roundtrip` 对含 native image/group 的**非结构化模板有根本限制**，但 ppt-master 真品 svg_output/ 几乎等于 svg_final/——说明 **ppt-master 自己的 fill 输出 vendor 能接受**。我们的 phase 11/12 抄的就是真品几何，所以**对 boteng 模板也兼容**。

→ 不需要担心 phase 11/12 几何触发 vendor reject。

---

## 目标（修正后）

1. **chrome 可关**：`enable_chrome=False` → 整个 `_inject_content_chrome` 跳过；模板自带 chrome 的 deck 保留原 design language
2. **archetype 可关**：`enable_ppt_master_archetypes=False` → 4 个 archetype 走 Phase 10 旧几何（raise 明确报错 + fallback 文档说明，不强行实现 legacy）
3. **boteng 行为不变**：boteng_demo 不传新选项 → 默认 True → 与 Phase 12 完全一致
4. **删除未提交的过度设计**：撤回 _legacy_* 4 处引用 + 8 option 中未用的 6 个

---

## 改动清单（最小化）

### 1. `src/mcp_ppt_native_fill/block_renderer.py`

**删除**: 4 处 `return _legacy_*(spec, payload)` (line 240, 692, 926, 1189) + 模块级 `_ALLOW_PPT_MASTER_ARCHETYPES` `_PALETTE_OVERRIDE` `_ppt_master_archetypes_enabled` `_palette_get`（共 ~25 行）

**新增**: 单个开关 — `_ALLOW_PPT_MASTER_ARCHETYPES: bool = True`（保 Phase 12 默认）

**4 个 archetype 分支头部简化**：
```python
if not _ALLOW_PPT_MASTER_ARCHETYPES:
    raise NotImplementedError(
        f"{layout} requires enable_ppt_master_archetypes=True; the "
        f"Phase 10 fallback geometry was retired in Phase 11. See "
        f"docs/PHASE11_NATIVE_FILL_SVG_REFORM_2026-09-17.md for the "
        f"new geometry."
    )
```

### 2. `src/mcp_ppt_native_fill/pipeline.py`

**`_inject_content_chrome`**: 把 `enable_chrome_topbar` + `enable_chrome_footer` 合并为单个 `enable_chrome: bool = True`。逻辑：

```python
opts = state.context.get("phase13_options") or {}
enable_chrome = bool(opts.get("enable_chrome", True))
if not enable_chrome:
    return  # 模板自带 chrome，保留
# 走默认 Phase 12 路径（chrome_meta / palette 暂不暴露）
```

**删除**: `_resolve_chrome_meta`（80 行）+ `chrome_meta_defaults()` 引用 1 处

**`realize_plan`**: 把 2 个 flag 设置改成 1 个：
```python
_br_mod._ALLOW_PPT_MASTER_ARCHETYPES = bool(
    opts.get("enable_ppt_master_archetypes", True))
# 删除 _PALETTE_OVERRIDE 设置（无 palette option 了）
```

**`run_native_fill` / `run_with_mapping` 签名**: 8 个 kwarg 缩到 2 个：
```python
enable_chrome: bool = True,
enable_ppt_master_archetypes: bool = True,
```

### 3. `src/mcp_ppt_native_fill/chrome.py`

**删除**: `chrome_meta_defaults()` 函数（15 行）—— 无 caller 用

### 4. `src/mcp_ppt_native_fill/server.py`

**`TOOL_NATIVE_FILL.inputSchema.options`**: 8 个 option 缩到 2 个：
```python
"enable_chrome": {
    "type": "boolean",
    "default": True,
    "description": "Phase 13: inject chrome topbar + footer into content "
                   "slides. Set False for templates with their own chrome "
                   "(Phase 12 default keeps True for backward compat)."
},
"enable_ppt_master_archetypes": {
    "type": "boolean",
    "default": True,
    "description": "Phase 13: use ppt-master geometry for 4 archetypes. "
                   "Set False to raise NotImplementedError — the Phase 10 "
                   "legacy geometry was retired in Phase 11."
}
```

### 5. `tests/test_native_fill.py`

**新增 4 个测试**（精简版）：
- `test_chrome_off_preserves_template_chrome`: `enable_chrome=False` → SVG 无 `<g id="slide-topbar">` / `<g id="slide-footer">`
- `test_archetype_off_raises_not_implemented`: `enable_ppt_master_archetypes=False` + layout=statement-caption → NotImplementedError
- `test_default_true_matches_phase12`: 不传 option → 行为 = Phase 12（回归）
- `test_chrome_off_does_not_strip_shape_22`: chrome 关 → template 的 shape-22 不被 strip

### 6. `examples/boteng_demo.py`

**不修改**（默认 True 即保 Phase 12 视觉）。可在文档注释一行说明 phase 13 默认行为。

### 7. **不新增** `docs/PHASE13_PPT_MASTER_BORROW_2026-09-17.md`

**改用**这个文件 (`PHASE13_REVISED_PLAN_2026-09-17.md`）作为 Phase 13 文档。PHASE11/PHASE12 doc 保留；这个文件加一段"phase 13 = opt-out 灵活性"。

---

## 验证

### 单元测试

```bash
cd D:\Code\tst\native_fill
PYTHONIOENCODING=utf-8 python -X utf8 -m pytest tests/test_native_fill.py -x -q
# 期望: 现有 249 测试 + 4 新测试 = 253 PASS，旧测试零破
```

### E2E: boteng_demo 视觉 = Phase 12

```bash
cd D:\Code\tst\native_fill
PYTHONIOENCODING=utf-8 python -X utf8 examples/boteng_demo.py
# 期望: ok=true, 0 errors, slide_part01-06 SVG 含 <g id="slide-topbar"> + <g id="slide-footer">
```

### E2E: 灵活性 demo（chrome off）

新建 `examples/example_chrome_off.py`，传 `enable_chrome=False`，跑 6 slide，验证 SVG 无 slide-topbar / slide-footer。

### 静态渲染检查

```bash
PYTHONIOENCODING=utf-8 python -X utf8 -c "
from pathlib import Path
ws = Path('projects/boteng_采购制度_v2_workspace/authoring-svg-flat')
for svg in sorted(ws.glob('slide_part0*_content.svg')):
    raw = svg.read_text(encoding='utf-8')
    assert 'id=\"slide-topbar\"' in raw, f'{svg.name}: topbar missing'
print('Phase 13 default check passed (chrome on)')
"
```

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| `_legacy_*` 引用导致 crash（当前未提交状态） | **修复方法就是本 plan**——删 4 处引用 + raise NotImplementedError |
| boteng_demo 默认行为变化 | 不动 boteng_demo options dict；默认 True 即保 Phase 12 |
| 删 `chrome_meta_defaults()` 导致外部调用方破 | 内部函数，无外部 caller（grep 确认） |
| 删 `_PALETTE_OVERRIDE` 丢掉部分覆盖能力 | 当前 pipeline.py 里只 2 处引用（realize_plan + render 时）；用 _PALETTE_OVERRIDE['_PALETTE_OVERRIDE = opts.get("palette") or {}`] 替代由 caller 改 chrome.BRAND_BLUE 单例更危险；删除 = 强制 Phase 12 palette，不留 caller 路径 |
| chrome_meta dict/list 形态被砍 | 用户原始反馈只要"灵活性"，chrome_meta 是 Phase 13 原 plan 过度工程化产物；不砍这个的话 _resolve_chrome_meta 80 行 + 测试 4 个全是冗余 |

**回滚**: `git restore src/mcp_ppt_native_fill/block_renderer.py pipeline.py chrome.py` 即可。所有改动是删除 + 简化，无破坏性新增。

---

## 实施步骤

| Step | 任务 | 验证 |
|---|---|---|
| 1 | block_renderer.py: 删 4 处 `_legacy_*` 引用 → 改为 raise NotImplementedError；删 `_PALETTE_OVERRIDE` / `_palette_get` / `_ppt_master_archetypes_enabled` 包装函数；保留 `_ALLOW_PPT_MASTER_ARCHETYPES` 单标志 | grep 确认 4 处 legacy 引用消失 |
| 2 | pipeline.py: `_inject_content_chrome` 把 2 option 合并为 `enable_chrome`；删 `_resolve_chrome_meta` + `chrome_meta_defaults` 引用；`realize_plan` 删 `_PALETTE_OVERRIDE` 设置；`run_native_fill` / `run_with_mapping` 签名缩到 2 option | grep 确认 palette/chrome_meta 引用消失 |
| 3 | chrome.py: 删 `chrome_meta_defaults()` 函数 + `__all__` 入口 | grep 确认零引用 |
| 4 | tests/test_native_fill.py: 加 4 个 TestPhase13OptOut 测试 | pytest 253 PASS |
| 5 | 跑 boteng_demo E2E + 静态检查 | ok=true, chrome on |
| 6 | 跑 examples/example_chrome_off.py（新建一个最小 demo） | ok=true, chrome off |
| 7 | commit `feat(content-pages): Phase 13 — chrome/archetype opt-out (simplified)` | git log |

**总计**: 7 步约 1 小时（原 plan 估 5 小时；修正后 80% 缩减）。

---

## 不在范围（已确认）

- ❌ **section_divider / closing 新 archetype**（ppt-master 真品无对应几何）
- ❌ **chrome_meta dict/list 形态**（per-slide override 过度工程化）
- ❌ **palette 全覆盖**（保持 Phase 12 默认，caller 改 chrome.BRAND_BLUE 不安全）
- ❌ **archetype_override**（markdown `> **layout**:` + A-path 启发式已够）
- ❌ **暗背景 full-bleed section_divider Master**（PHASE10 §"open gaps"，模板工程）
- ❌ **boteng 模板 chat loop 替换 native_fill**（不在本轮；vendor 边界清晰后 boteng 可走原生 `native_fill`）

---

## 用户后续可选项

1. 加 `palette` 选项（但需重构 chrome 模块避免单例篡改）
2. 加 `chrome_meta` dict 形态（按 section_idx 覆盖 chapter_label）
3. 改 boteng 模板自带的 chrome design language（不抄 ppt-master）
4. 加 section_divider 真品 archetype（左 rail + 右 panel，但用专门的 PREFACE / CHAPTER X 命名）

---

## 一句话总结

> Phase 13 修正后 = **2 个 opt-out 开关**（chrome on/off + ppt-master archetypes on/off），默认全开保 Phase 12 视觉。删掉原 plan 8 option + 2 新 archetype + 4 个 `_legacy_*` 提取——这些是过度工程化，对照真品调研后无证据支撑。