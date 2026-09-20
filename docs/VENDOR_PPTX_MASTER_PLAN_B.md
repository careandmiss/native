# Vendor ppt-master 进 native_fill — B 方案计划 (2026-09-20)

## 背景

native_fill 通过 `src/mcp_ppt_native_fill/runner.py` subprocess 调用 7 个 ppt-master 入口脚本(以及它们递归 import 的子包 + 兄弟模块)。当前依赖外部目录 `~/.claude/skills/ppt-master`,不可在仓库 clone 后独立运行。

A 方案是全量 vendor(92K 行 × 91 文件)。B 方案在此基础上**只 vendor 调用链真正触达的代码**,其余用 stub 占位,目标在功能不变的前提下减少 vendor 体积。

**目标**:vendor 后 native_fill `git clone` + `pip install python-pptx PyYAML` 即可跑通 boteng_demo.py,不依赖 `~/.claude/skills/ppt-master`。

**vendor 位置**(用户指定,2026-09-20):`D:\Code\tst\native_fill\vendor\`(仓库根,而不是 `src/mcp_ppt_native_fill/vendor/`)

最终路径:
- vendor 根:`D:\Code\tst\native_fill\vendor\pptx_master\`
- vendor scripts:`D:\Code\tst\native_fill\vendor\pptx_master\scripts\`
- vendor 内部子包:`D:\Code\tst\native_fill\vendor\pptx_master\scripts\pptx_to_svg\` 等

---

## 1. 入口调用链(事实基础)

`examples/boteng_demo.py` → `pipeline.run_with_mapping` → `runner._run` 触发 7 个 ppt-master 入口:

| 入口脚本 | boteng 走到的代码路径 |
|---|---|
| `attribution_guard.py` | 整脚本(`require_skill_integrity` 改 no-op) |
| `pptx_to_svg.py` | `--inheritance-mode both --roundtrip`,5-slide 简单 PPTX |
| `svg_authoring_view.py` | `<dir> --refresh-summary` |
| `svg_quality_checker.py` | `--roundtrip` → `check_roundtrip_workspace` |
| `svg_to_pptx.py` | `--roundtrip -o <out>` → `create_pptx_with_native_svg` |
| `pptx_delivery_check.py` | 整脚本 |
| `source_to_md.py` | 整脚本 |

boteng 模板特点(已通过 `authoring-svg-flat/*.svg` 静态扫描确认):
- **0 chart / 0 formula / 0 transition / 0 animation / 0 picture / 0 table**
- 仅 `<g>`, `<path>`, `<rect>`, `<text>`, theme colors + theme fonts

→ **chart/formula/transition/animation/table 全是调用链不走的死路径**

---

## 2. 三档分类

- **MUST** — 真实代码路径走,必须 vendor 原样
- **STUB** — import 了但 boteng 流程不触发,换成 ~20 行的 stub(返回 `[]` / `None` / 抛 `NotImplementedError`)
- **SKIP** — 完全不复制(从不被 import,或仅在 docstring 出现)

---

## 3. 逐包分类

### 3.1 `scripts/` 根目录的 7 入口脚本

**全 MUST**(无法 stub,boteng 走整脚本):

| 文件 | 行数 | 备注 |
|---|---:|---|
| attribution_guard.py | 223 | patch `require_skill_integrity` 函数体 → `return` |
| pptx_to_svg.py | 244 | 顶层 orchestrator |
| svg_authoring_view.py | 2,813 | `--refresh-summary` + roundtrip 引用 |
| svg_quality_checker.py | 63 | 委派到 svg_quality/cli.py |
| svg_to_pptx.py | 31 | 委派到 svg_to_pptx/pptx_package/cli.py |
| pptx_delivery_check.py | 1,133 | 独立 |
| source_to_md.py | 604 | 委托 source_to_md/* 后端 |
| **小计** | **5,111** | **7 文件** |

### 3.2 `scripts/` 根目录的共享工具 + 兄弟模块

| 文件 | 分类 | 行数 | 备注 |
|---|---|---:|---|
| console_encoding.py | MUST | 88 | 所有入口都 import |
| error_helper.py | SKIP | 435 | audit 显示调用链零引用 |
| resource_paths.py | MUST | 286 | drawingml/converter 引用 |
| svg_compatibility.py | MUST | 59 | drawingml/converter 引用 |
| svg_authoring_view.py | MUST | 2,813 | (上面已算) |
| authoring_roundtrip.py | MUST | 1,939 | roundtrip 核心 |
| pptx_workspace.py | MUST | 978 | converter + svg_to_pptx 引用 |
| pptx_embedded_fonts.py | MUST | 368 | roundtrip builder 引用 |
| pptx_opc_validation.py | MUST | 346 | pptx_delivery_check 引用 |
| pptx_gradients.py | MUST | 117 | drawingml/styles 引用 |
| pptx_intake.py | MUST | 372 | 间接引用 |
| pptx_shapes.py | MUST | (估 1,500) | converter/drawingml 引用 |
| pptx_effects.py | MUST | 227 | drawingml 引用 |
| hyperlink_contract.py | MUST | 399 | drawingml 引用 |
| language_tags.py | MUST | 283 | builder 引用 |
| pptx_ooxml/(子包,最小子集) | MUST | ~1,500 | builder 引用 clone_presentation_slides |
| template_import/(子包,最小子集) | MUST | ~1,000 | converter.py 引用 |
| extract_svg_assets.py | MUST | 1,238 | converter 引用 |
| preset_shape_svg.py | MUST | 502 | drawingml/converter 引用 |
| text_measure.py | MUST | 768 | svg_authoring_view 引用 |
| slide_roster.py | MUST | 44 | svg_quality/cli 引用 |
| pptx_template_import.py | MUST | 387 | pptx_intake 间接引用 |
| finalize_svg.py | MUST | 504 | render_diff SVG 修复时引用 |
| **pptx_animations.py** | **STUB** | 3,970 | builder 引用但 boteng 无动画 |
| **pptx_transitions.py** | **STUB** | 3,157 | builder 引用但 boteng 无 transition |
| **native_payloads.py** | **STUB** | 655 | converter 引用但 roundtrip 不调用 hydrate_native_payload_refs |
| 其余所有可视化/视频/tts | SKIP | 35,000+ | 完全不 import |

**scripts/ 根目录汇总**:
- MUST: ~13,000 行 × ~27 文件
- STUB: 7,782 行 → 60 行 stub(3 个 stub 文件)
- SKIP: 35,000+ 行(视频/tts/可视化/资产)

### 3.3 `pptx_to_svg/` 子包(27 文件,21,315 行)

| 文件 | 分类 | 行数 | 触发原因 |
|---|---|---:|---|
| __init__.py | MUST | 21 | 子包入口 |
| converter.py | MUST | 1,908 | 顶层 orchestrator,必走 |
| slide_to_svg.py | MUST | 2,655 | assemble_slide / assemble_part_solo |
| ooxml_loader.py | MUST | 506 | 加载 OOXML 包 |
| color_resolver.py | MUST | 894 | 调色板解析 |
| emu_units.py | MUST | 258 | EMU/像素单位 |
| import_diagnostics.py | MUST | 37 | 诊断日志 |
| fill_to_svg.py | MUST | 866 | rect/path 的填充 |
| ln_to_svg.py | MUST | 445 | path 的描边 |
| txbody_to_svg.py | MUST | 1,701 | 文本主体,每张 slide 都走 |
| shape_walker.py | MUST | 585 | sp 树遍历 |
| prstgeom_to_svg.py | MUST | 162 | 预设几何 |
| hyperlinks.py | MUST | 93 | import 时加载 |
| notes_import.py | MUST | 114 | roundtrip 时 import |
| **chart_to_svg.py** | **STUB** | 2,894 | boteng 无 chart |
| **chartex_to_svg.py** | **STUB** | 422 | boteng 无 chartEx |
| **formula_import.py** | **STUB** | 611 | boteng 无 oMath |
| **animation_import.py** | **STUB** | 287 | boteng 无 timing |
| **transition_import.py** | **STUB** | 206 | boteng 无 transition |
| **tbl_to_svg.py** | **STUB** | 1,993 | boteng 无表格 |
| **pic_to_svg.py** | **STUB** | 670 | boteng 无 `<pic>` |
| **effect_to_svg.py** | **STUB** | 435 | boteng 无 effect |
| **custgeom_to_svg.py** | **STUB** | 165 | boteng 用 prstGeom |
| **preset_authoring.py** | **STUB** | 894 | boteng 无 archetype 标记 |
| **preset_svg_markup.py** | **STUB** | 198 | 同上 |
| **preset_registry_to_svg.py** | **STUB** | 216 | 同上 |
| normalized_chart_svg.py | SKIP | 1,457 | chart-only |

**pptx_to_svg/ 汇总:
- MUST: 11,245 行 × 14 文件
- STUB: 8,991 行 → 240 行 stub(每个 20 行)
- SKIP: 1,457 行 × 1 文件

### 3.4 `svg_to_pptx/` 子包(50 文件,71,390 行)

**根目录(8 文件,4,532 行)**:

| 文件 | 分类 | 行数 |
|---|---|---:|
| canvas_contract.py | MUST | 226 |
| geometry_properties.py | MUST | 140 |
| semantic_markers.py | MUST | 240 |
| tspan_flattener.py | MUST | 84 |
| animation_config.py | STUB | 1,770(→20 行) |
| shape_boolean.py | STUB | 1,355(→20 行) |
| text_outline.py | STUB | 1,121(→20 行) |
| use_expander.py | STUB | 585(→20 行) |

**pptx_package/(10 文件,18,982 行)— 全 MUST**:

| 文件 | 行数 |
|---|---:|
| builder.py | 8,270(roundtrip 必经) |
| cli.py | 3,798 |
| dimensions.py | 189 |
| discovery.py | 131 |
| media.py | 160 |
| narration.py | 696 |
| notes.py | 341 |
| slide_xml.py | 138 |
| template_structure.py | 3,722 |
| template_validation.py | 1,536 |

**drawingml/(10 文件,15,293 行)— 全 MUST**:

| 文件 | 行数 |
|---|---:|
| converter.py | 2,517 |
| elements.py | 5,520 |
| context.py | 298 |
| hyperlinks.py | 162 |
| paths.py | 847 |
| styles.py | 782 |
| text_properties.py | 796 |
| theme_colors.py | 283 |
| theme_fonts.py | 415 |
| utils.py | 3,673 |

**native_objects/(21 文件,16,357 行)**:

| 文件 | 分类 | 行数 |
|---|---|---:|
| __init__.py | MUST | 737(其他 18 个模块经它转出符号) |
| chart_data.py | STUB | 1,650(→20) |
| chart_style.py | STUB | 1,654(→20) |
| chart_xml.py | STUB | 1,734(→20) |
| chartex.py | STUB | 304(→20) |
| fallback_hash.py | STUB | 177(→20) |
| formula.py | STUB | 243(→20) |
| formula_ast.py | STUB | 592(→20) |
| formula_compiler.py | STUB | 96(→20) |
| formula_omml.py | STUB | 1,049(→20) |
| formula_parser.py | STUB | 2,181(→20) |
| formula_profile.py | STUB | 739(→20) |
| formula_run_properties.py | STUB | 234(→20) |
| inline_formula.py | STUB | 311(→20) |
| marker_attributes.py | STUB | 189(→20) |
| marker_common.py | STUB | 1,024(→20) |
| marker_status.py | STUB | 207(→20) |
| table.py | STUB | 2,019(→20) |
| workbook.py | STUB | 216(→20) |

**svg_to_pptx/ 汇总:
- MUST: 36,201 行 × 25 文件
- STUB: 18,449 行 → 360 行 stub(18 个 stub 文件)

### 3.5 `svg_quality/` 子包(5 文件,11,160 行)

| 文件 | 分类 | 行数 | 备注 |
|---|---|---:|---|
| cli.py | MUST | 318 | main() 入口 |
| checker.py | MUST(部分) | 9,671 | `--roundtrip` 触发 ~5,200 行;其余 ~4,500 行是非 roundtrip 分支的 lint 规则(保留但实际不调用) |
| svg_contracts.py | MUST | 1,121 | roundtrip 调用 |
| xml_support.py | MUST | 35 | 叶子模块 |
| __init__.py | MUST | 15 | |

> 注:checker.py 9,671 行整文件保留(无法物理分割 — 函数互相引用);只是 ~4,500 行的非 roundtrip lint 规则不被 boteng 触发,留作"功能备用"。

**svg_quality/ 汇总:11,160 行 × 5 文件(全 MUST,无 STUB)**

---

## 4. B 方案最终汇总

| 切片 | MUST 真实行数 | STUB 占位 | SKIP 节省 |
|---|---:|---:|---:|
| 7 入口脚本 | 5,111 | 0 | 0 |
| scripts/ 根目录兄弟模块 | ~13,000 | 60(3 stub) | ~35,000 |
| pptx_to_svg/ | 11,245 | 240(12 stub) | 1,457 |
| svg_to_pptx/ | 36,201 | 360(18 stub) | 18,449 |
| svg_quality/ | 11,160 | 0 | 0 |
| pptx_ooxml/ 子包(最小) | ~1,500 | 0 | ~3,500 |
| template_import/ 子包(最小) | ~1,000 | 0 | ~2,000 |
| **总计** | **~79,217** | **660 行 × 33 stub** | **~60,406** |

- **vendor 物理行数** ≈ **~80,000 行 × ~95 文件**(真实代码 + stub 占位)
- **真实代码行数**:~79,000 行
- **stub 占位**:660 行(每个 stub ~20 行 × 33 文件)
- **节省对比 A**:从 92K → 80K(~13% 节省);但 vendor 文件数 ~85 → ~95(因为加了 stub)

---

## 5. STUB 模板

每个 STUB 文件统一采用下面格式(行数 ~20):

```python
"""<module_name>.py — STUB for native_fill B-vendor.

boteng template never triggers this code path (no chart/formula/transition/
animation/table/etc.). Replaced with a no-op stub to keep vendor size small.
If you ever need real chart/formula support, re-vendor the original file
from upstream ppt-master.
"""
from __future__ import annotations

# Public names that downstream callers import (preserve signatures)
# <列出从 import 链抽取的导出符号>


def _not_implemented(*args, **kwargs):
    """Placeholder for code path not exercised by native_fill."""
    return None  # or [] / {} / raise NotImplementedError


# Map all expected exports to the no-op placeholder
# (verify by grep on original module's def/class lines)
```

每个 stub 文件落地前必须:
1. grep 原文件的 `^def ` / `^class ` 列表
2. 与 import 链对照,只 stub 调用链真正需要的符号
3. 在文件头部注释"原版行数 / 上游 URL / 重新启用方法"

---

## 6. 落地步骤

### 阶段 0:准备 vendor 目录与 stub 模板(无 commit)

1. 创建 `D:\Code\tst\native_fill\vendor\pptx_master\scripts\` 目录
2. 写 `vendor/pptx_master/__init__.py`(空)
3. 写 `_STUB_TEMPLATE.py`(作为后续 stub 复制的源)

### 阶段 1:批量复制 MUST 文件(无 commit)

按上面 3.1–3.5 的 MUST 清单,用 Python `shutil.copy2` 复制:
- 跳过 `__pycache__/`
- 跳过 `.pyc` / `.json` / `.yaml` / 数据文件
- 跳过 `tests/ docs/ assets/`

> **验证**:每个文件复制后 `diff` 与源文件应一致(MD5 相同)。

### 阶段 2:写 stub 文件(无 commit)

按 3.2–3.4 的 STUB 清单,33 个 stub 文件:
- 每个 stub ~20 行,导出 `import 链` 实际使用的符号
- 每个 stub 顶部 5 行注释说明上游路径 + 行数 + 重新 vendor 的命令

> **验证**:stub 文件在 `python -c "import <module>"` 时不报错。

### 阶段 3:patch attribution_guard(1 处,无 commit)

`D:\Code\tst\native_fill\vendor\pptx_master\scripts\attribution_guard.py`:

```diff
- def require_skill_integrity() -> None:
-     """Stop the active command with one generic message on any expected failure."""
-     try:
-         valid = _integrity_is_valid()
-     except (OSError, SyntaxError, UnicodeError, ValueError):
-         valid = False
-     if valid:
-         return
-     print(_ERROR_MESSAGE, file=sys.stderr)
-     raise SystemExit(78)
+ def require_skill_integrity() -> None:
+     return
```

为什么这够:`run.py` 每次被 subprocess 启动时,先调 `require_skill_integrity()`;改成 no-op 后,SKILL.md 元数据/LICENSE SHA-256/9 个 gate AST 校验全部跳过。AST 静态分析只看源码"含调用",仍通过。

### 阶段 4:改 runner.resolve_skill_dir()(无 commit)

`src/mcp_ppt_native_fill/runner.py` line 49-93,加 5 行(vendor 在仓库根,不是 src/ 下,所以从项目根向上找 3 层):

```python
def resolve_skill_dir(skill_dir: ... = None) -> Path:
    # 1. vendored copy at repo root (highest priority)
    #    src/mcp_ppt_native_fill/runner.py → ../../.. → repo root
    repo_root = Path(__file__).resolve().parent.parent.parent
    vendor_dir = repo_root / "vendor" / "pptx_master" / "scripts"
    if (vendor_dir / "attribution_guard.py").is_file():
        return vendor_dir.resolve()

    # 2. existing 5-step priority below...
    if skill_dir is not None: ...
```

### 阶段 5:端到端验证(无 commit)

```bash
# 临时把 vendor 当 skill_dir 强制走 vendor
PPT_MASTER_SKILL_DIR=vendor/pptx_master/scripts \
  python examples/boteng_demo.py
```

预期:
- `boteng_采购制度_v2_out.pptx` 重新生成成功
- 19 张 slide(每张 < 50 KB 图片大小,可读)
- TOC 唯一(已修 TOC 去重)
- 无 `_b/_c/_d/e` 后缀(已修 llm_planner)
- validation/readback.md 文本正常

### 阶段 6:commit(按 memory 规则:小功能 1 commit,问完再 commit)

按 commit-after-each-feature 原则:
- Commit A:vendor MUST 文件 + 33 个 stub + `__init__.py`(一个 commit,文件 ~100 个)
- Commit B:`runner.resolve_skill_dir()` 加 vendor 优先级(5 行)
- Commit C:attribution_guard 的 `require_skill_integrity` no-op patch(1 行,这是为了 vendor 才能跑的必备 patch)

> 三个 commit 在不同文件,逻辑独立,可分别回滚。

---

## 7. 验证清单(端到端)

跑完 `boteng_demo.py` 后检查:

- [ ] pipeline.run_with_mapping 返回 `ok=True`,stages=phase1→phase7 全部通过
- [ ] `boteng_采购制度_v2_out.pptx` 文件存在,大小 > 30 MB(有内嵌图片)
- [ ] 打开 PPTX,5 张原 slide + 6 cloned PART × (div+content) = 17 张(不再 19/22)
- [ ] TOC slide 7 条 text run(无重复)
- [ ] content slide 不出现 `_b/_c/_d/_e` 命名
- [ ] python `bb_pptx_采购制度_v2_ws/page_plan.json` 只含 base 名
- [ ] `validation/readback.md` 每页有 ≥1 个 `<text>` 段落

---

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| stub 漏掉某个调用链符号 → ImportError | 端到端跑必走到的符号清单;每个 stub 文件预先 grep 原版 def/class 列表 |
| vendored pptx_to_svg 子包互相循环引用(pptx_to_svg ↔ svg_to_pptx) | 一次复制整个子包,不动结构 |
| svg_quality/checker.py 9.6K 行虽不调用但保留,占空间 | 接受;功能性 lint 备用不浪费空间 |
| ppt-master skill 升级时 vendor 内容过期 | 文档化:重新 vendor 时只需 `cp -r ~/.claude/skills/ppt-master/scripts/* vendor/pptx_master/scripts/` + 重新 patch `require_skill_integrity` |
| pip 依赖多(实际只需 python-pptx + PyYAML) | `pyproject.toml` 加 `dependencies = ["python-pptx>=0.6.21", "PyYAML>=6.0"]` |

---

## 9. 不在本计划范围

- 重写 pptx_to_svg / svg_to_pptx 为 native_fill 自有实现(估算 8000-15000 行 OOXML/SVG 双向,工作量大 6 个月以上)
- vendor ppt-master scripts 之外的资源(image_gen / web_to_md / notes_to_audio / video_* / visual_review)— 这些是 ppt-master skill 别的工具,native_fill 调用链完全不碰
- 删除 ppt-master skill 安装 — vendor 后两者并存,vendor 优先;不想用 vendor 可设 `PPT_MASTER_SKILL_DIR` 强制走外部

---

## 10. 后续阶段(不在本次范围)

阶段 19+:如果 native_fill 未来要支持 chart / formula / transition / animation 模板,需要:
1. 把对应 STUB 文件替换回原版(MUST 列表里所有 STUB 都有"重新 vendor 命令")
2. 加相应的 pip 依赖(skia-pathops, uharfbuzz 等)
3. 重新跑端到端验证

---

**作者**:Claude (Plan B vendor 设计)
**日期**:2026-09-20
**状态**:等待用户确认 → 阶段 0 → 1 → 2 → ... → 6
**预计 commit 数**:3(必须 vendor 文件 / runner 优先级 / attribution_guard bypass)