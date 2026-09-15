# phase5 svg_to_pptx 失败诊断与修复方案

**日期**: 2026-09-15
**作者**: lrq + Claude
**影响范围**: `mcp_ppt_native_fill.pipeline.normalize_export_artifacts`
**关联 PR / commit**: (待提交)

---

## 1. 问题

### 1.1 现象

`examples/smart_toc_fill.py` 跑完整 E2E 时:

```
phase5 svg_to_pptx failed exit=1
stderr_tail=Error: Edited round-trip source object did not produce a DrawingML shape: 2
```

诊断脚本进一步定位到失败发生在 **slide 4/17 = `slide_04.svg`**,而非最初疑似的 `slide_02.svg`(TOC slide)。

### 1.2 受影响命令

```bash
python examples/smart_toc_fill.py        # 主 demo,失败
python examples/test_toc_4_vs_7.py      # 4 vs 7 章节场景,同样失败
```

---

## 2. 根因分析

### 2.1 误判与拨乱

第一直觉是 vendor `svg_to_pptx.py` 把 "shape 2" 当作 SVG `<g id="shape-2">`。**错的**。

读完 vendor `C:\Users\Administrator\.claude\skills\ppt-master\scripts\svg_to_pptx\pptx_package\builder.py:1677-1684`:

```python
missing_authored = (
    authored_top_ids - deleted_top_ids - set(authored_order)
)
if missing_authored:
    raise TemplateStructureError(
        "Edited round-trip source object did not produce a DrawingML shape: "
        + ", ".join(sorted(missing_authored, key=_roundtrip_shape_sort_key))
    )
```

`_shape_id(elem)` (line 570-573) 返回 `cNvPr.attrib.get("id")`,**"shape 2" 实指 source PPTX spTree 里某个 cNvPr.id="2" 的 top-level shape,在 generated SVG 里没有对应 DrawingML 元素**。

### 2.2 source slide 4 spTree 形态

| pos | tag   | cNvPr.id | name         | 说明 |
|-----|-------|----------|--------------|------|
| 0   | pic   | 2        | 图片 1       | 背景大图 1280×720 |
| 1   | grpSp | 14       | 组 13        | 左侧装饰 |
| 2   | sp    | 17       | 文本框 16    | 单击添加大标题 |
| 3   | grpSp | 18       | 组 17        | 右侧装饰 |
| 4   | cxnSp | 21       | 直线连接符 20| 虚线 |
| 5   | sp    | 22       | 文本框 21    | 高效协同... |
| 6   | sp    | 3        | 文本框 2     | 内容占位框 |

### 2.3 edited 后 slide_04.svg 形态

```
slide_04.svg 里 data-pptx-source-ref 共 2 处:
  shape-2 -> slide:2     (pic,背景,位置 0)
  shape-3 -> slide:3     (注意:实际指向 grpSp,不是 sp cNvPr.id=3)
```

但 framework 在 `expand_skeleton_content` 路径下,只改了 `shape-17` 标题文本,**没有给 7 个 source shape 中的其他 5 个补 source-ref**,vendor 期望它们在 generated SVG 里出现,找不到就 abort。

### 2.4 framework 设计漏洞

`pipeline.py:646-714` 的 `normalize_export_artifacts` 已经设计了"edited slide 全剥 source-ref"逻辑(见 `_apply_source_ref_normalization`),关键判断在:

```python
edited_slides = {
    e.split(":", 1)[0].strip()
    for e in state.context.get("edited_svg_paths", [])
}
if not edited_slides:
    cm = state.context.get("content_mapping", {}) or {}
    edited_slides = set(cm.keys())
```

**全文 grep `edited_svg_paths`**:除了这一处**没有任何写入点**。`content_mapping` 在 `examples/smart_toc_fill.py` 里是 `{}`(用 smart TOC 填充)。结果:

- `edited_svg_paths` → 空
- `content_mapping` → 空
- `edited_slides` → 空
- `_apply_source_ref_normalization` 走 UNEDITED 路径,只剥"指向不存在 source slide"的 invalid refs,合法 `slide:2`/`slide:3` 全部保留
- vendor 报 "Edited round-trip source object did not produce a DrawingML shape: 2"

### 2.5 这是 boteng 模板特性引发的设计漏洞

- `slide_04.svg` 是 content skeleton,被 framework clone 多份(6 份,`slide_part01_content.svg` ... `slide_part06_content.svg`),每份都被改了 `shape-17` 标题
- framework 的 `_seed_original_roster` 只把 source PPTX 的 5 张原始 slide 写到 `authoring-svg-flat/`(`slide_01` ~ `slide_05`),**克隆出来的 `slide_part*.svg` 完全没被任何机制标记为 edited**
- vendor 的 round-trip 路径会重新跑一遍 slide_04.svg(它也注册在 page_plan_pages),用 source slide 4 的 spTree 作为 byte-rehydrate 蓝图,但 SVG 里只有 2 个 shape 带 source-ref → 失败

---

## 3. 修复方案

### 3.1 设计选择

#### 候选 A:侵入 svg_edits / phase2c / phase3 写入点

每个 SVG 写入函数主动 push 到 `state.context["edited_svg_paths"]`。**否决**:改 3 个函数、跨 2 个模块、加 callback 接口,改动面过大。

#### 候选 B:基于 page_plan_pages 差集

```python
baseline = {f"slide_{i:02d}.svg" for i in range(1, valid_source_slides + 1)}
planned = {p["svg"] for p in state.context["page_plan_pages"]}
edited_slides = planned - baseline
```

**优点**:不依赖 mtime,语义清晰。
**缺点**:依赖 `_seed_original_roster` 必须真的写 `slide_NN.svg` 到 disk(否则 baseline 算不准);且 page_plan_pages 没去重。

#### 候选 C:mtime 启发式 ✓ **采纳**

在 `normalize_export_artifacts` 调用入口打 sentinel:

```python
import time
phase_start = time.time()
# ... rest of normalize_export_artifacts ...
# 兜底:任何晚于该函数调用时刻 0.5s 之前的 svg 都视为 edited
mtime_threshold = phase_start - 0.5
mtime_edited = {
    f.name for f in slide_files
    if f.stat().st_mtime >= mtime_threshold
}
if mtime_edited and not edited_slides:
    edited_slides = mtime_edited
```

**优点**:
- 单点改动,全部局限在 `normalize_export_artifacts`
- 0.5s slack 容忍 sub-second 写入
- 不侵入 svg_edits / phase2c / phase3
- 不依赖 page_plan_pages 的语义完整性
- 跟 framework 的"normalize 是 edits 之后"时序完全吻合

**缺点**:
- TOCTOU:写入和读 mtime 之间极短窗口(< 0.5s 容忍)
- 跨时区/挂钟回拨可能误判(0.5s slack 已覆盖)

#### 候选 D:跳过 phase5

给 phase5 加 continue-on-error,失败不阻塞。**否决**:用户期望生成 pptx,跳过是逃兵行为。

### 3.2 采纳:候选 C

代码改动锁定在 `pipeline.py:646-714`,预计 ≤ 8 行。

---

## 4. 实施计划

### 4.1 Wave 1 — 修复 + 验证

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1 | `src/mcp_ppt_native_fill/pipeline.py` | 在 `normalize_export_artifacts` 加 mtime 兜底 |
| 2 | `tests/test_native_fill.py` | 新增 `TestNormalizeEditedSlidesFallback` 类,验证 mtime 启发式 |
| 3 | (验证) | `python -m unittest tests.test_native_fill` 全 154+ 测试通过 |
| 4 | (验证) | `python examples/smart_toc_fill.py` 输出 `smart_toc_boteng_out.pptx`,exit=0 |
| 5 | (验证) | `python examples/test_toc_4_vs_7.py` 4/6 + 7/6 scenario 都输出 pptx |

### 4.2 Wave 2 — 命名规范化(独立 PR)

| 项 | 文件 | 改动 |
|-----|------|------|
| 1 | `src/mcp_ppt_native_fill/pipeline.py` | 内部 docstring 残留 "phase3.5" / "phase2.6" 字样清掉 |
| 2 | `tests/test_native_fill.py` | 10 个 `test_phase2_5_*` / `test_phase2_6_*` 方法名改为 `test_phase2b_*` / `test_phase2c_*` |
| 3 | `src/mcp_ppt_native_fill/autofix.py` | 函数 `fix_nested_picture_data_attrs` 保留(公开 API),内部 docstring 改用 `disabled_autofixes=["render_compat"]` 表达 |
| 4 | `src/mcp_ppt_native_fill/server.py` | 保留 `skip_phase3_5` / `fix_nested_picture` schema 字段(deprecated,继续触发 deprecation warning),不动 |

**注**:`skip_phase3_5` / `fix_nested_picture` 是公开 MCP schema 字段,**不在本轮清理范围**,符合用户已确认的"保留别名+警告"策略。

---

## 5. 已知风险与限制

1. **vendor 仍可能再次抛错**:mtime 启发式只解决 source-ref 这一类问题。其他类型的 round-trip 错误(connector zero-stroke、picture structure variants 等)需要 `disabled_autofixes` 显式 opt-in,行为不变。
2. **boteng 模板的形状 ID 命名不规范**:cNvPr.id 不连续(2/14/17/18/21/22/3),framework 推断算法需要兼容这种乱序,**已知问题,不修**(与本 issue 无关)。
3. **mtime 启发式在容器化/CI 跑时**:容器内文件系统时间戳可能不可靠。已加 0.5s slack,可缓解;若 CI 仍出 false-positive/negative,降级到候选 B。

---

## 6. 验证证据

(待回填:修复后 `python examples/smart_toc_fill.py` 输出文件大小 + tests/test_native_fill.py 全部通过数)

---

## 7. 相关记录

- 上一次决策:`docs/REFACTOR_NAMING_AND_MODULES_2026-09-15.md`(待提交)
- 关联 issue:(无独立 issue tracker,本文即为 issue)
- Daily log:`D:\Code\tst\native_fill\.ai-memory\20260915\daily.md`(待写)
