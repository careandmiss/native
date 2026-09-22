# 2026-09-20 Daily Log

> 项目:mcp_ppt_native_fill (D:\Code\DSH\native_fill)
> 主线:Phase 19 续作 — content page density 修复
> 今日活跃 phases:Phase 5 svg_to_pptx 修复 (handoff) / Phase 19 P0-B 计划评审

---

## [review-time] - code-review: CONTENT_PAGE_DENSITY_PLAN_2026-09-20.md 合理性评审

- **文件**:
  - 评审对象:`docs/CONTENT_PAGE_DENSITY_PLAN_2026-09-20.md`(Phase 19 P0-B 续作,Claude 起草)
  - 交叉参照:`src/mcp_ppt_native_fill/pipeline.py:1130-1230`、`src/mcp_ppt_native_fill/workspace_expand.py:1034-1130`、`src/mcp_ppt_native_fill/toc_detection.py:655-690`
  - 上游 plan:`docs/CONTENT_LAYOUT_PLAN_2026-09-20.md`、教训:`docs/PHASE18_POSTMORTEM_2026-09-20.md`

- **决策**:
  - 整体判定:**方向正确,1 处 P0 事实错误 + 1 处 P1 路径错误 + 3 处 P2 改进建议**,必须修订为 v2 后才能落地
  - P0-1:P0-B-1 改动段写反数据流向——`_derive_default_chrome_plan` 是生产 chrome_plan 的阶段(读 markdown 构造 lookup),不是消费 `page_plan.json` 的阶段;且 `pipeline.py:1144-1151` 已经有 hardcoded `titles_by_index` 字典,plan 完全没提到。正确改法是改 `pipeline.py:1195` 那一行,而非动 page_plan.json schema
  - P1-1:P0-B-3 文件路径错误——`cards_from_body` 在 `toc_detection.py:681`,不在 `block_renderer.py`;且 toc 路径已有空 body fallback(`toc_detection.py:662-677`),content page 走的是另一条调用链,需先 grep 定位
  - P2-1:每个 commit 后未写回滚条件,与 Phase 18 教训对齐不足
  - P2-2:验证清单只覆盖结构异常的 boteng MD,缺少正常 MD 的反例验证
  - P2-3:P0-B-2 标"待定位"但未给定位步骤

- **验证**:
  - 验证方式:静态代码 grep + 上游 plan 对照(未跑 `python examples/boteng_demo.py` 实测,plan §1 现状描述已足够)
  - grep 命令:
    - `grep -rn "_EN_LABELS" src/` → 5 处,确认仅 `pipeline.py:1202` 调用旧逻辑
    - `grep -rn "def cards_from_body" src/` → 仅 `toc_detection.py:681`,确认 plan 路径错误
    - `grep -rn "_fallback_synthesize" src/` → 0 处,确认 plan 提到的函数不存在
    - `grep -n "section_title" src/mcp_ppt_native_fill/pipeline.py` → 3 处,line 1144 已硬编码 titles_by_index
  - 关键证据:`pipeline.py:1144-1151` 已有 6 个 hardcoded 标题(前言/目的/适用范围/基本原则/工作程序/附件),P0-1 修正版改动量 ≤ 10 行
  - 用户反馈:**同意我的建议**(作者需出 v2 plan,本评审再做事实复核)

- **后续**:
  - 等作者出 v2 plan 再做 30 分钟事实复核
  - 复核通过后再按"单 commit per fix"逐项落地
  - 不建议按 v1 直接落地(浪费 1-2 小时)

---

## [plan-author-time] - plan-draft: CONTENT_PAGE_DENSITY_PLAN_v2_2026-09-20.md (接手 v2)

- **文件**:
  - 新建:`docs/CONTENT_PAGE_DENSITY_PLAN_v2_2026-09-20.md`(417 行,基于 v1 评审结论起草)
  - 关键交叉引用:`src/mcp_ppt_native_fill/pipeline.py:1195/1202/835-851/651-659`、`src/mcp_ppt_native_fill/toc_detection.py:952-1013`、`src/mcp_ppt_native_fill/workspace_expand.py:1034-1130`、`tests/test_native_fill.py:2305-2399 (ContentBlockFallbackTests 已存在)`

- **决策**:
  - 用户授权:**直接由我出 v2 plan**(不再等原作者),并自审合理性
  - P0-B-1 修复:在 `pipeline.py:1195` 后插入 `en_label, _body_label = intent_label_pair(title)`,删除 `pipeline.py:1202` 的 `_EN_LABELS.get(idx)` 调用;保留 import `_EN_LABELS`(以防未来 use)。**改动量:净 1 行**
  - P0-B-2 修复:在 `pipeline.py:835-840` 后(line 841 if 前)插入 sibling group 清理循环——无论 shape_id 是 content-body 还是 body_cards,都同时删除另一个 id;删除 line 841-851 旧的单向清理。**改动量:净 +1 行**
  - P0-B-3 修复:在 `pipeline.py:651-659` 的 `_fill_missing_content_blocks` 调用前,合并 LLM-identified svgs + final_pages 中的所有 *_content.svg entry,确保 LLM 漏识别的 Part 04 也被兜底;修正了一处关键逻辑错误——line 651 时 `state.context["page_plan_pages"]` 尚未写入(line 688 才写),改为读 `final_pages` 局部变量。**改动量:16 行替换**
  - P0-B-4 重定位:从"独立 test commit"改为"集成验证步骤",避免歧义;单测 case 拆分到 P0-B-1/2/3 各自的 commit 中(8+2+3=13 case)
  - 总 commit 数从 v1 的 4 个修正为 3 个(每个 commit 自带单测)

- **验证**:
  - 验证方式:grep 复核所有行号 + 代码内容匹配 + 内部一致性自审
  - grep 命令:
    - `grep "titles_by_index\[1\]\|en_label = _EN_LABELS.get" src/mcp_ppt_native_fill/pipeline.py` → 命中 line 1146 + 1202,与 v2 plan §3.1 引用一致
    - `grep "if shape_id == \"content-body\":" src/mcp_ppt_native_fill/pipeline.py` → 命中 line 841 + 860,与 v2 plan §3.2 引用一致
    - `grep "_fill_missing_content_blocks(" src/mcp_ppt_native_fill/pipeline.py` → 命中 line 651,与 v2 plan §3.3 引用一致
    - `grep "page_plan_pages\|final_pages" src/mcp_ppt_native_fill/pipeline.py` → 27 处,确认 line 688 才写入 context(关键发现,触发 P0-B-3 修正)
  - 自审发现并修正 2 处:
    1. P0-B-3 改动初稿误用 `state.context.get("page_plan_pages") or final_pages`——line 651 时 context 还没写入,改用 `final_pages` 局部变量
    2. P0-B-4 描述歧义——既像验证又像 commit,改为"集成验证,无独立 commit"
  - v2 plan 总长度 417 行,8 个章节 + 3 个根因 + 4 个 P0-B 子任务 + 5 节验证清单 + v1→v2 修订记录表

- **后续**:
  - 等用户对 v2 plan 做最终确认
  - 确认后按 §4 实施顺序:先 P0-B-1 (1 commit)→P0-B-2 (1 commit)→P0-B-3 (1 commit),每步后跑 §5.1 boteng_demo baseline + §5.2 反例 MD + §5.3 单测
  - 任何 commit 触发 §4 步骤 3 的 5 条回滚条件之一 → `git revert HEAD`,不叠加
  - v2 plan 评审中如发现新事实错误,走 §8 修订记录流程追加

---

## [phase20-execution-time] - Phase 20 rule-based layout + merge-to-divider

- **文件**:
  - 新建:`src/mcp_ppt_native_fill/layout_rules.py` (159 行,7 rule + 共享 regex)
  - 新建:`docs/PHASE20_CONTENT_PAGE_LAYOUT_PLAN_2026-09-20.md` (417 行,8 章节)
  - 改动:`src/mcp_ppt_native_fill/archetype_router.py:198-227` (pre-check 入口)
  - 改动:`src/mcp_ppt_native_fill/workspace_expand.py:213-237` + `:272` (merge-to-divider flag + content clone guard)
  - 测试:`tests/test_native_fill.py` 新增 3 个 test class 共 21 个 case (15 TestLayoutRule + 4 TestArchetypeRouterRulePrecheck + 2 TestWorkspaceExpandMergeToDivider)

- **决策**:
  - 用户确认 strategy: **B + B**(rule-based layout + 简化 chrome),但用户原话"最主要是内容页的排版 其他的封面什么的我感觉都正常"把 chrome 简化从范围移除
  - 实际策略: **只 B**(rule-based layout),chrome 简化不在范围
  - Plan 文档已交付: docs/PHASE20_CONTENT_PAGE_LAYOUT_PLAN_2026-09-20.md(旧版 PHASE20_LAYOUT_STRATEGY 删除,因 over-include chrome)
  - commit 粒度(每个独立 commit,符合"一次只改一个文件" Phase 18 教训):
    - 52eee78 P0-A: layout_rules 模块 + 15 test(纯基础设施,不集成)
    - aa9b0b2 P0-B: archetype_router 接入 rule pre-check(rule 命中 override LLM)
    - 86518f1 P0-C: workspace_expand merge-to-divider(skip content clone for short body)

- **验证**:
  - 用户规则:**commit 前生成 ppt 给用户看效果**(用户在前面几轮明确的 hard rule)
  - demo 路径:
    - P0-A: D:\Code\DSH\native_fill\projects\structured_md_phase20_p0a.pptx (43,375,940 bytes,无功能变化)
    - P0-B: D:\Code\DSH\native_fill\projects\structured_md_phase20_p0b.pptx (43,373,768 bytes,-462 bytes vs baseline)
    - P0-C: D:\Code\DSH\native_fill\projects\structured_md_phase20_p0c.pptx (43,374,535 bytes,slide 数 15→14)
  - 量化验收:
    - structured_md slide 数: 15 → **14** ✓(二、适用范围 25 chars 触发 Rule 1 merge-to-divider,content slide skipped)
    - 单测: 21 个新 case 全过,整体 292/7 failures(无新增)
    - 用户每次 ack 后才进入下一个 commit(push 前用 git stash 暂存未 commit 改动)

- **后续**:
  - Phase 20 收官,dsh 分支累计 7 个 commit
  - 下一步候选:Phase 21 archetype 几何布局重设计(layout 多样性真正视觉化),但用户未明确需要
  - 等用户决定下一步方向(可能继续做 archetype 几何,或收尾,或回滚某些 commit)


## [phase22-23-time] - Pipeline Pattern refactor + vendor retry + template role inference

- **文件**:
  - 新建:`src/mcp_ppt_native_fill/pipeline/__init__.py` (~70 lines — sub-package public exports)
  - 新建:`src/mcp_ppt_native_fill/pipeline/context.py` (~140 lines — PipelineContext + Handler ABC + PipelineError)
  - 新建:`src/mcp_ppt_native_fill/pipeline/orchestrator.py` (~250 lines — Pipeline class + run_with_pipeline entry)
  - 新建:`src/mcp_ppt_native_fill/pipeline/_internal.py` (~1190 lines — phase3/4/5 + helpers, from _legacy.py line ranges)
  - 新建:`src/mcp_ppt_native_fill/pipeline/handlers/` (phase2_import.py, markdown_expand.py, phase3_author.py, phase4_quality.py, phase5_export.py — thin Adapter wrappers)
  - 删除:`src/mcp_ppt_native_fill/pipeline.py` (2474 lines — moved into pipeline/_internal.py)
  - 改动:`src/mcp_ppt_native_fill/template_adapter.py` (_infer_slot_role: add placeholder type + geometry fallback)
  - 改动:`src/mcp_ppt_native_fill/runner.py` (add stdin=DEVNULL — Fix B for vendor PowerShell stdio race)
  - 改动:`vendor/pptx_master/scripts/pptx_to_svg/converter.py` (add _copytree_with_retry — Phase 23 commit 1)
  - 文档:`docs/PIPELINE_PATTERN_DESIGN_2026-09-20.md` (already in 9219e56)
  - 文档:`docs/PHASE22_23_DESIGN_PATTERNS_APPLIED_2026-09-20.md` (new — 536 lines deep-dive)
  - 文档:`docs/PHASE23_VENDOR_RETRY_BACKOFF_PLAN_2026-09-20.md` (new)

- **决策**:
  - 用户原话:"我们一定要mcp自实现的" — 全部在 mcp_ppt_native_fill 内实现,不依赖外部 skill
  - 用户原话:"是不是所有的模板都通用化了?" — 诚实回答:vendor 能跑通(Phase 23 commit 1 retry),role 推断不需要 caller-supplied shape ID(Phase 23 commit 2 placeholder type),但视觉一致性仍因模板而异
  - 用户原话:"开始吧" — 推进通用化,做了 5 个 commit(Phase 22 commit 6 + Phase 23 commit 1 + commit 2 + docs)
  - 用户原话:"深入学习设计模式" — 写了 2 个深度文档(Pipeline Pattern theory + Applied deep-dive with code excerpts)

- **验证**:
  - boteng demo 跑通 (43,379,607 bytes, ok=true, stage=done) — commit 6 baseline preserved
  - template_v2 demo 仍 ok=false (PermissionError) — vendor 内部 race fix 不解决 PowerShell 子进程 stdio race;留 Phase 23+ 后续
  - 单测:297 tests / 7 failures preserved (baseline)
  - vendor retry:5 attempts × 0.2/0.4/0.8/1.6/3.2s backoff,worst case 6.2s;happy path 无延迟
  - 16 commit (Phase 22: 6 commits, Phase 23: 2 commits + 2 docs) dsh 分支完整 Pipeline Pattern 重构

- **后续**:
  - Phase 23+ 修 PowerShell stdio 子进程 race(让 template_v2 demo 跑通)
  - 实施 layer 3 layout choice(用 archetype_dedup loop heuristic,不再基于 pp_rule)
  - 写后即记 + 设计模式文档已 push (ee0dc53)
