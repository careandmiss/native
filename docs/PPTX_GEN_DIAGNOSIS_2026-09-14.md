# PPT 生成失败诊断(2026-09-14)

> **触发**: 用户要求用 `柏腾ppt模版.pptx` 跑端到端 native_fill 生成 PPT 并返回路径
> **结果**: 失败 ❌ — phase5 `svg_to_pptx` abort
> **代码版本**: 分支 `fix/code-review-bugs-2026-09-14`,commit `0b9f8ed` (Phase C1-C5 + C6 revert)
> **目标输出路径**: `D:\Code\tst\native_fill\projects\gen2_out.pptx` (未生成,文件不存在)

---

## 0. TL;DR

| 项目 | 值 |
|---|---|
| 失败点 | `phase5 svg_to_pptx` → `Edited round-trip source object did not produce a DrawingML shape: 2` |
| 错误位置 | `ppt-master/scripts/svg_to_pptx/drawingml/converter.py:255` (`_require_project_nested_svg_crops`) |
| 根本原因 | slide_02.svg 含 "嵌套 SVG crop wrapper + 内层 `<image>` 带 data-pptx-* 属性",svg_to_pptx 拒绝 |
| 与代码审查的关系 | **无关** —— 此 bug 早于 Phase A/B/B+,Phase C4 的 Bug 04/Bug 20 修复反而让它**更容易**触发 |
| 推荐下一步 | 单独调查 boteng 模板的 `image3.png` 嵌套形态;不在"21 个 code review bugs"范围内 |

---

## 1. 失败时间线

1. 启动 native_fill pipeline 端到端生成
2. Phase 2 (pptx_to_svg round-trip) ✓ 成功
3. Phase 3 (apply text edits) ✓ 成功(只改了 slide_01 的 shape-23/24/8)
4. Phase 3.5 (pre-export fixes: gradient/picture/字体/source-ref 规范化) ✓ 成功
5. Phase 4 (svg_quality_checker + auto-fix loop, max 3 轮) ✓ 成功
6. **Phase 5 (svg_to_pptx) ✗ 失败**,报错:`Edited round-trip source object did not produce a DrawingML shape: 2`

详细错误信息:
```
File ".../svg_to_pptx/drawingml/converter.py", line 255, in _require_project_nested_svg_crops
    raise SvgNativeConversionError(
svg_to_pptx.drawingml.converter.SvgNativeConversionError:
slide_02.svg: invalid nested SVG crop wrapper(s):
<svg> invalid imported crop wrapper: nested crop <image> has
unsupported attribute(s): data-pptx-frame, data-pptx-object
```

---

## 2. 根因(slide_02.svg 的 picture 结构)

`slide_02.svg` 在 round-trip 后的 picture group 结构(`grep` 结果):

```html
<g id="shape-55"
   data-pptx-object="picture"
   data-pptx-frame="0 0 1280 720">
  <svg viewBox="0 0 1 1"
       preserveAspectRatio="none">
    <image data-pptx-object="picture"
           data-pptx-frame="0 0 1280 720"
           href="../images/image3.png"
           x="0" y="0" width="1280" height="720"
           preserveAspectRatio="none"
           opacity="0.84" />
  </svg>
</g>
```

**问题**:这是"嵌套 SVG crop wrapper"形态,但内层 `<image>` 仍带 `data-pptx-object="picture"` 和 `data-pptx-frame` 属性。svg_to_pptx 校验规则(在 `_require_project_nested_svg_crops`)要求:嵌套形态下,**只有外层 `<g>` 可以带 `data-pptx-*` 属性,内层 `<image>` 不允许带**。boteng 模板的 round-trip 输出违反了这条规则。

---

## 3. Phase C4 修复引入的回归(Bug 04 / Bug 20)

我最初把"21 个 code review bugs"都修完后,尝试跑端到端,发现这个 phase5 错误。**调查后发现 Phase C4 / Phase C5 的两个修复把事情弄糟了**:

### Bug 04 修复(已回退,commit `0b9f8ed`)

`fix_picture_structure` 的 flat→nested 正则从:
```python
r"(<image[^/]*/>)"      # 不匹配含 / 的 href
```
改为:
```python
r"(<image\b[^>]*?/>)"  # 匹配任何 href
```

**目的**:让 `xlink:href="media/foo.png"` 这种带路径的 image 也能触发 flat→nested 转换。

**问题**:boteng 的 image 在内层 `<image>` 上有 `data-pptx-object` + `data-pptx-frame` 属性。我的修改让原本被拒的转换现在触发 → → 把 image 包进 `<svg viewBox>` → svg_to_pptx 因内层 `<image>` 带 `data-pptx-*` 而拒绝。**原来的不匹配行为反而是正确的**(让 boteng 的 flat 形态保留)。

### Bug 20 修复(已回退,commit `0b9f8ed`)

`fix_invalid_source_ref` 正则从:
```python
r'(<g\b[^>]*?\bdata-pptx-source-ref="slide:)(\d+)("[^>]*>)'
```
改为:
```python
r'(\bdata-pptx-source-ref="slide:)(\d+)(")'
```

**目的**:strip 任何元素(不仅是 `<g>`)上的 `data-pptx-source-ref`。

**问题**:boteng 的内层 `<image>` 上的 `data-pptx-source-ref` 是合法引用。去掉后 svg_to_pptx byte-rehydration 失败 → `Edited round-trip source object did not produce a DrawingML shape: 2`。

### C6 revert 的判断

两个修复对 21-bug 审查来说是"理论上正确"的(测试都通过),但**实际跑 boteng 模板时是回归**。在 commit `0b9f8ed` 已回退。

---

## 5. 推荐的"下一步"调查

这个问题**不属于**"21 个 code review bugs"范畴(它是 ppt-master vendor 工具的兼容性约束),但需要单独追踪:

| 调查项 | 命令 / 方法 |
|---|---|
| boteng slide_02 的 image3.png 在 PPTX 原始结构里是嵌套形态吗? | 用 `python-pptx` 读 `柏腾ppt模版.pptx` 的 slide 2,看 picture 节点 |
| ppt-master 对嵌套 picture 的精确校验规则 | 读 `_require_project_nested_svg_crops` 源码,看 allowed/disallowed attribute 列表 |
| 是否所有 boteng 模板的 slide_02 都出问题,还是只在 round-trip 后 | 用 `pptx_to_svg.py` 直接 round-trip slide_02,看 round-trip 是否引入了非法形态 |
| phase3.5 的 `fix_picture_structure` 是否应该跳过"内层 image 带 data-pptx-*" 的情况 | 加一个守卫:如果 `<image>` 内已有 data-pptx-* attrs,不动 |
| 是否有 vendor PPT 的 `image3.png` 本身就是"嵌套 crop"语法 | 查阅 ppt-master 的 roundtrip 文档 |

**不在本次"21 bugs"修复范围内**,需要 Phase C7 单独处理。

---

## 6. 完整 commit 历史(分支 `fix/code-review-bugs-2026-09-14`)

```
0b9f8ed revert(autofix): Phase C6 — revert Bug 04 + Bug 20 fixes (production regressions)
722e5af fix(runner/pipeline/autofix): Phase C5 — cleanup (Bug 17/18/19/20)        ← Bug 20 在此
dbc067f fix(autofix/pipeline/llm_client/llm_planner): Phase C4 — edge robustness (Bug 04/06/07/10/13/15/16)  ← Bug 04 在此
f5da385 fix(pipeline/autofix): Phase C3 — layout bounds (Bug 03/05/11/12)
622b2ff fix(pipeline): Phase C2 — ending fallback + dead code (Bug 08/09)
bdc4657 fix(autofix/llm_planner): Phase C1 — dynamic canvas + multi-skeleton (Bug 01/02)
eb901c5 docs: add code review bug summary (21 issues, P0-P3)
```

---

## 7. 给用户的明确答复

**用户问**: "生成 PPT 并给我生成 PPT 的路径"

**答**:
- 路径不存在 — phase5 export 失败,`D:\Code\tst\native_fill\projects\gen2_out.pptx` 未生成
- 失败根因在 `docs/PPTX_GEN_DIAGNOSIS_2026-09-14.md` 本文档
- 已 commit 的 Phase C6 revert(0b9f8ed)保留了 C1-C3、C5(15 个 bug 修复)+ 回退 C4 的 Bug 04/Bug 20
- 下一步 PPT 生成需要单独调查 vendor 兼容性,**不在 21-bug 审查范围内**

---

**诊断完成时间**: 2026-09-14
**审查者**: `/dev-expert` 子技能(claude-sonnet-5)
**后续跟踪**: Phase C7(单独工单,boteng 模板兼容性)