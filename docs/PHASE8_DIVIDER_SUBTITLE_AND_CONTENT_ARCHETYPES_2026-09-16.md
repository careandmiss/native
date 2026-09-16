# Phase 8: Divider 副标题翻译 + 内容页 archetype 升级

**日期**：2026-09-16
**分支**：`integration/sink-generate-local-ppt-2026-09-14`
**Commit**：`0346e0c` (Phase 7) 之后的两个新 commit

---

## 解决的问题

Phase 7 (commit `0346e0c`) 新增 statement-caption / procedural-steps / three-thesis-cards 三个 archetype，但用户反馈两个独立问题：

1. **章节页底部出现 "单价添加小标题/标题英文..."** —— 模板 `slide_03.svg` 的 `<g id="shape-70">` 里硬编码的中文占位符，被 `shutil.copy2` 完整克隆下来，6 个章节页都出现这段垃圾文本。
2. **内容页排版可进一步优化** —— boteng slide 6/8/10 (一、目的 / 二、适用范围 / 三、基本原则) 是单短句，目前走 simple-text 之外的空白路径；slide 12 (四、工作程序) 走 procedural-steps fallback 而非真正渲染 macro phase 圆点。

## 用户决策

| 决策 | 选择 |
|------|------|
| Bug 修法 | **不能清空** —— 必须保留模板原 `<g>` 结构（字体/字号/颜色/位置都不变），把对应英文填到 `<text>` 里去。翻译策略走 caller 端。 |
| 新 archetype | **hero_statement + kpi_row**（不只加 kpi_row，不加 comparison / matrix_2x2） |
| slide 12 fallback | 顺带修 `_classify_h2_to_phase` 关键词覆盖（"基本事项" → "申请"） |

---

## 改动清单

### 1. Bug 修复：英文副标题填充（不破坏模板）

**核心思路**：boteng 模板的 shape-70 是一个完整 `<g>`，含字体 `思源黑体 CN Regular` 21.33px 灰色，位置 `(85.8, 492.8)`。用户的明确要求是**保留这个结构**，只把 `<text>` body 替换成英文（如 "Purpose" / "Working Procedure"）。

**实现路径**：
- `expand_workspace_from_markdown` 新增 `section_title_en_map` 参数（`{section_title: english_title}`）
- `_format()` helper 加 `title_en` 关键字
- divider clone 阶段，第二轮 `divider_subtitle_template` 应用时会用 `{title_en}` 替换为查表得到的英文
- 4 个 caller (`boteng_demo.py` / `structured_demo.py` / `smart_toc_fill.py` / `test_toc_4_vs_7.py`) 各自维护 `SECTION_TITLE_EN` 字典并传给 `expand_section_title_en_map`

**为什么不在 native_fill 内做翻译**：native_fill 是通用工具，不应该 hardcode 中文→英文映射。每种语言 / 每种业务文档都需要不同的翻译策略。caller 全权决定是合理的。

**Server forwarding 修复**：`server.py` 漏转发 `expand_section_title_en_map`，已补上。

### 2. 新 archetype `hero_statement`

**几何**（默认 `body_bounds = "120 130 1060 480"`）：
- 全 bounds 软色背景 `<rect fill="#F4F6F8" rx="12">`
- 可选 eyebrow：14px 灰色 `#64748B` letter-spaced
- Headline 居中 68px bold ink `#1E293B`，自动 shrink 到 14px min
- 可选 subline：18px 灰色 `#64748B`

**触发**：单 card + 单 item + item 长度 20-79 chars。

### 3. 新 archetype `kpi_row`

**几何**：
- N=2-5 tiles 顶 y=by，宽 `(bw - gap*(N-1))/N`，gap=20px
- 每个 tile 高 200px, rx=8, fill `#FFFFFF` stroke `#D6DCE3`
- Tile 内：keyword 40px ink + descriptor 14px body + value 18px muted
- 下方 evidence panel (full bw)，200px 高，rx=12 fill `#F4F6F8`

**触发**：单 card + 2-5 items + 每个 item ≤12 chars。

### 4. A-path dispatch 改造（`workspace_expand.py`）

新增 priority order：

```
1. revision-table sentinel (Phase 6.4, unchanged)
2. n_h2 >= 3 → procedural-steps (with synthesized macro phases)
3. single long paragraph (≥80 chars) → statement-caption
4. single medium paragraph (20-79 chars) → hero_statement  [NEW]
5. single short paragraph (<20 chars) → simple-text
6. 1 card, 2-5 items each ≤12 chars → kpi_row            [NEW]
7. 1 card ≥2 items → bullet-list
8. 2 H2 → three-thesis-cards
9. ≥2 cards → 3-column-cards
```

### 5. `_classify_h2_to_phase` 关键词覆盖

boteng 7 个 H2 标题：

| H2 标题 | 期望 phase |
|---|---|
| 采购基本事项 | 申请 |
| 采购申请 | 申请 |
| 采购人职责 | 审批 |
| 采购方式 | 采购 |
| 采购实施 | 采购 |
| 采购付款 | 审批 |
| 行为规范 | 验收 |

修复：`_PHASE_KEYWORDS` 元组首位追加 `("基本事项", "申请")`（必须在 `("采购", "采购")` 之前，因为 `if kw in title` 是 first-match-wins）。

### 6. 副作用修复：revision-table 列数泛化

boteng 附件章节 markdown 是 6 列 revision table，但 `revision-table` renderer 写死 4 列 `("date", "status", "content", "author")`。Phase 7 没暴露这个问题（之前 attachment 走 sentinel 但不渲染），Phase 8 让它真进入渲染路径后报错 `'list' object has no attribute 'get'`（因为 row 是 list of dict 而不是 dict of lists）。

修复 `block_renderer.py:revision-table`：从第一行 dict keys 推断列数，header 文本通过 `header_map` 配置或 default-from-key。

---

## 关键文件

| 路径 | 改动 |
|---|---|
| `src/mcp_ppt_native_fill/workspace_expand.py` | `_format()` 扩展支持 `{title_en}` placeholder + 新增 `section_title_en_map` 参数 + 调用点传 `title_en`；A-path dispatch 插 hero_statement / kpi_row 分支；`_PHASE_KEYWORDS` 追加 `("基本事项", "申请")` |
| `src/mcp_ppt_native_fill/block_renderer.py` | 末尾追加 `hero_statement` 和 `kpi_row` 两个新 layout 分支；`revision-table` 改为变列数 |
| `src/mcp_ppt_native_fill/pipeline.py` | `run_with_mapping` 新增 `expand_section_title_en_map` 参数 + 转发给 `expand_workspace_from_markdown` |
| `src/mcp_ppt_native_fill/server.py` | 接收 + 转发 `expand_section_title_en_map` |
| `examples/boteng_demo.py`、`examples/structured_demo.py`、`examples/smart_toc_fill.py`、`examples/test_toc_4_vs_7.py` | 各加 `SECTION_TITLE_EN` 字典 + `DIVIDER_SUBTITLE` + options 里加 `"expand_divider_subtitle_template"` + `"expand_section_title_en_map"` |
| `tests/test_native_fill.py` | 16 新单测（计划 12，实际 16 个新增），分布在 5 个 TestCase |

**复用 helper**：
- `toc_detection.is_toc_slot_` ` (检测toc_detection.py:135`) — 占位符检测（虽然 Phase 8 没最终用，但保留作为后续扩展点）
- `svg_edits.apply_text_edits(mark_empty_as_carrier=True)` — text 清空
- `text_width.chars_that_fit` — CJK auto-wrap
- `block_renderer.escape` (`block_renderer.py:57`) — XML escape
- `coerce_str_list` (`block_renderer.py:30`) — items coerce

---

## 验证

### 单元测试

```
PYTHONIOENCODING=utf-8 python -X utf8 -m unittest tests.test_native_fill
```

**结果**：226 测试全部通过（原 210 + Phase 8 新增 16）

新增 5 个 TestCase：
- `TestPhase8DividerSubtitleTranslation` (2)
- `TestPhase8HeroStatement` (4)
- `TestPhase8KpiRow` (4)
- `TestPhase8Dispatch` (5)
- `TestPhase8ClassifyH2` (1)

### E2E

```
PYTHONIOENCODING=utf-8 python -X utf8 examples/boteng_demo.py
```

**结果**：ok=true, errors_count=0, output_size=43MB

```
PYTHONIOENCODING=utf-8 python -X utf8 -c "
from pathlib import Path
ws = Path('projects/boteng_采购制度_v2_workspace/authoring-svg-flat')
expected = ['Preface','Purpose','Application Scope','Basic Principles','Working Procedure','Appendix']
for svg in sorted(ws.glob('slide_part*_div.svg')):
    raw = svg.read_text(encoding='utf-8')
    has_placeholder = '单价添加小标题' in raw
    has_en = any(s in raw for s in expected)
    print(f'{svg.name}: placeholder={\"YES\" if has_placeholder else \"NO\"}, english={\"YES\" if has_en else \"NO\"}')"
```

**结果**：6 个 div.svg 全部 `placeholder=NO, english=YES`

### 回归

```
PYTHONIOENCODING=utf-8 python -X utf8 examples/test_toc_4_vs_7.py
```

**结果**：4-chapter scenario ok=true, 7-chapter scenario ok=true

### Pass / fail 判定

- ✅ 226 单测全过（原 210 + 16 新）
- ✅ boteng_demo ok=true；新 PPT 43MB
- ✅ 所有 6 个 div.svg 不含 "单价添加小标题"，且含对应英文（Preface / Purpose / Application Scope / Basic Principles / Working Procedure / Appendix）
- ✅ shape-70 的 `<g>` 外壳、字号、字体、颜色、位置全保持模板原样
- ✅ test_toc_4_vs_7 双 ok=true

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| caller 翻译 map 漏 key → `{title_en}` 展开为空 | 文档明示；空字符串作为降级，不阻断主流程 |
| `_format()` 新加 `title_en` 参数破现有签名 | `**kwargs` 兼容；测试覆盖回退到空字符串 |
| `hero_statement` 68pt 在窄 bounds 溢出 | `chars_that_fit` + auto-shrink 到 14px min |
| `kpi_row` 与 `three-thesis-cards` 触发冲突 | `kpi_row` 需 item ≤12 chars (keyword)；`three-thesis-cards` 需 n_h2==2。互斥 |
| revision-table 列数推断出错 | 测试覆盖 4 列 + 6 列两种场景；header_map 提供 caller 覆盖点 |
| 新 archetype 触发 LLM new_blocks 双层堆叠 | 复用 Phase 7.5 anti-double-stack guard (`pipeline.py:755`) |
| boteng markdown 已恢复 H1+H2 | `_original.md` 备份存在 |

**回滚**：两个 commit 各自独立可 `git revert`。

---

## 实施步骤回顾

| Step | 任务 | 实际耗时 |
|---|---|---|
| 1 | workspace_expand.py: `_format()` 加 `title_en` 支持 + `section_title_en_map` 参数 | 20min |
| 2 | 4 个 caller 加 SECTION_TITLE_EN + 传 `expand_divider_subtitle_template` | 20min |
| 3 | block_renderer.py 加 hero_statement 分支 | 30min |
| 4 | block_renderer.py 加 kpi_row 分支 | 45min |
| 5 | workspace_expand.py A-path dispatch 插 hero_statement/kpi_row | 30min |
| 6 | _PHASE_KEYWORDS 补基本事项 | 5min |
| 7 | tests/test_native_fill.py 16 新测试 | 60min（含修测试 fixture 的 namespace 问题） |
| 8 | e2e + inspect_pptx 验证（含 server forwarding 修复 + revision-table 列数泛化 + 附件 markdown 冒号处理） | 90min |
| 9 | docs/ 实施文档 + 清理 debug 文件 + commit | 10min |

总计：~5 小时

---

## 不在范围

- ❌ 不动现有 layout 的颜色 / 字体 / 圆角
- ❌ 不重写 LLM planner
- ❌ 不修改 boteng markdown
- ❌ 不加 `comparison` / `matrix_2x2` archetype
- ❌ 不实现「父 H1 自动分页到子 H2」

---

## 用户后续可选项

1. **Hero statement 触发阈值调整** — boteng 一、目的 "提高采购效率、明确各岗位职责…" 实际超过 79 chars，走 statement-caption 而非 hero_statement。如果想让 hero_statement 更广泛触发，可以把 80-char 阈值下移到 50 chars。
2. **kpi_row 触发扩展** — 当前只触发于 ≤12 chars 的 keyword-style items。可以放宽到 ≤20 chars，让普通短句也走 kpi_row。
3. **comparison archetype** — 如果未来有 "新旧制度对比" / "原则 vs 实施" 类内容，可加 comparison (ppt-master presentation_core.comparison)。
4. **LLM 路径覆盖 A-path dispatch** — 当前 boteng_demo 启用 `llm_plan: True`，LLM 会覆盖我们的 hero_statement / kpi_row 选择。可以让 caller 在 LLM prompt 里明确告诉 LLM 优先用新 archetype。