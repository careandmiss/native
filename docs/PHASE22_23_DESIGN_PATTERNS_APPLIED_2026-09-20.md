# Phase 22 + 23 Design Patterns Applied — Deep Dive (2026-09-20)

> **Status**: Living document. Updated with every Phase 22 + 23 commit.
> **Purpose**: For engineers learning design patterns by reading production code.
> **Companion**: `docs/PIPELINE_PATTERN_DESIGN_2026-09-20.md` (Pipeline Pattern theory).
> **This file**: Phase 22 (commits 1-6) + Phase 23 (commits 1-2) **applied** patterns.

---

## 1. Read this first: the full pattern list

| # | Pattern | Where (file:line) | Phase 22 commit | Why |
|---|---------|------------------------|-------|-----|
| 1 | **Pipeline Pattern** | `pipeline/orchestrator.py:Pipeline.run` | 2 | Core sequence control |
| 2 | **Template Method** | `pipeline/context.py:PipelineHandler.run/skip/on_failure` | 1 | Algorithm skeleton, varying steps |
| 3 | **Context Object** | `pipeline/context.py:PipelineContext` | 1 | Parameter Object — bundle 40+ args |
| 4 | **Façade** | `pipeline/_internal.py` + `handlers/*.py` | 5,6 | Hide 700+ line vendor logic |
| 5 | **Strategy** | `pipeline/handlers/markdown_expand.py:_auto_fill_template_options` | 4 | CallerSupplied vs Inspected adapter |
| 6 | **Factory Method** | `pipeline/orchestrator.py:Pipeline.DEFAULT_HANDLERS` | 2 | Registry factory |
| 7 | **Chain of Responsibility** (degenerate) | `pipeline/orchestrator.py:Pipeline.run` skip checks | 2 | Skip Handlers |
| 8 | **State** | `pipeline/_internal.py:PipelineState` | 1 | Persistent per-pipeline state |
| 9 | **Null Object** | `pipeline/_phase_helpers.py:PipelineState` (default factory) | 6 | Avoid `_missing`-like state checks |
| 11 | **Adapter Pattern** | `pipeline/handlers/*.py` (handler → _internal) | 5,6 | Free functions → PipelineHandler interface |
| 12 | **Retry-with-Backoff** | `vendor/.../converter.py:_copytree_with_retry` | Phase 23 commit 1 | OS-level handle race workaround |

Phase 23 commit 2 (`32e14db`) added **Strategy + Composite heuristics** to `_infer_slot_role`.

---

## 2. Each pattern in detail (with code excerpts)

### 2.1 Pipeline Pattern (GoF "Chain of Responsibility" cousin)

**Intent**: A sequence of operations on data; data flows through each
operation in turn. Each operation is independent and configurable.

**Real code** — `src/mcp_ppt_native_fill/pipeline/orchestrator.py`:

```python
class Pipeline:
    DEFAULT_HANDLERS = [Phase2Import, MarkdownExpand,
                         Phase3Author, Phase4Quality, Phase5Export]

    def __init__(self, handlers=None):
        self.handlers = handlers or [h() for h in self.DEFAULT_HANDLERS]

    def run(self, ctx: PipelineContext) -> PipelineContext:
        for h in self.handlers:
            if h.name in ctx.skip_handlers:
                continue
            if h.skip(ctx):
                continue
            try:
                ctx = h.run(ctx)
            except PipelineError as exc:
                ctx.state.stage = "failed"
                ctx.state.errors.append(f"{h.name}: {exc}")
                h.on_failure(ctx, exc)
                break
            if ctx.stop_after == h.name:
                break
        return ctx
```

**Why this is Pipeline Pattern**:
- Each handler does one thing (`indent_executor.run` style)
- Data flows via `ctx` (mutable, passed through)
- Each handler is independent + reorderable
- Failure in one handler stops the chain (vs Chain-of-Responsibility which dispatches one event to one handler)

**Trade-offs**:
- ✅ Add new phase without modifying orchestrator (Phase 23 just adds `_infer_slot_role` inside `Phase2ImportHandler`)
- ✅ Test each phase independently (we have `TestPhase2ImportHandler`, `TestMarkdownExpandHandler`, etc.)
- ❌ Indirection cost (5 classes instead of 5 functions in 1 module)

---

### 2.2 Template Method (GoF p. 325)

**Intent**: Define the skeleton of an algorithm in a base class;
let subclasses override specific steps.

**Real code** — `pipeline/context.py`:

```python
class PipelineHandler(ABC):
    name: ClassVar[str]

    @abstractmethod
    def run(self, ctx: PipelineContext) -> PipelineContext: ...

    def skip(self, ctx): return False  # null-object default

    def on_failure(self, ctx, exc): return None  # null-object default
```

**Why this is Template Method**:
- `Pipeline.run` is the template (fixed algorithm: skip → run → check stop)
- Each handler subclass fills in `run()` (the only abstract method)
- `skip()` and `on_failure()` have sensible defaults; subclasses override only if needed

**Trade-offs**:
- ✅ Subclasses only write what differs (`Phase2ImportHandler.run()` is 20 lines, not 60)
- ✅ Default impls (`on_failure`, `skip`) reduce subclass boilerplate
- ❌ Inheritance coupling (changing `Pipeline.run` affects all handlers)

---

### 2.3 Context Object (Fowler *Parameter Object*)

**Intent**: Bundle related parameters into one object passed throughout
a long method chain.

**Real code** — `pipeline/context.py`:

```python
@dataclass
class PipelineContext:
    state: PipelineState
    source_pptx: Path
    workspace: Path
    output_pptx: Path
    skill_dir: Path
    options: dict[str, Any]
    handler_outputs: dict[str, Any]
    skip_handlers: set[str]
    stop_after: str | None
```

**Before this refactor**, `pipeline.run_native_fill()` took **40+ keyword-only
arguments** (we measured during commit 1). Adding a new option meant
touching every call site.

**Trade-offs**:
- ✅ Adding `expand_toc_slot_grid` to `ctx.options` doesn't break existing call sites
- ✅ `ctx.handler_outputs["vendor_result"]` is the canonical cross-handler data channel
- ❌ Mutable state can hide data flow (the `state` field is mutated by every handler)

---

### 2.4 Façade (GoF p. 185)

**Intent**: Provide a unified interface to a set of interfaces in a
subsystem. Hide subsystem complexity from the caller.

**Real code** — Phase 22 commit 6:

```
pipeline/
├── handlers/              # THIN PipelineHandler wrappers (~80 lines each)
│   ├── phase2_import.py    # calls vendor (Façade boundary)
│   ├── markdown_expand.py  # calls workspace_expand.expand
│   ├── phase3_author.py    # calls _internal.phase3_author
│   └── ...
└── _internal.py           # 700+ lines of vendor coordination (Façade implementation)
```

`Phase3AuthorHandler` (Façade) hides:
- `block_renderer.render_new_block` (vendor SVG generation)
- `archetype_router.route_archetype` (LLM layout selection)
- `chrome.render_chrome_topbar/footer` (chrome injection)
- `autofix.repair_nested_picture_attrs` (picture attribute repair)
- `_remove_existing_new_content_group` (Phase 19 sibling dedup)

**Why Façade**:
- Each handler is ~80 lines (lifecycle only)
- Business logic lives in `_internal.py` (Façade implementation)
- Test handler independently without booting vendor / LLM

**Trade-offs**:
- ✅ Easy to add new phase (new handler wrapping new internal function)
- ✅ Handler file is short and readable
- ❌ Two-file structure (`handlers/*.py` + `_internal.py`) is more files to navigate

---

### 2.5 Strategy (GoF p. 315) — Phase 23 commit 2

**Intent**: Define a family of algorithms, encapsulate each one, and
make them interchangeable.

**Real code** — `template_adapter.py:235-300`:

```python
def _infer_slot_role(shape, current_text: str) -> str:
    # 1. Placeholder type (authoritative)
    if getattr(shape, "is_placeholder", False):
        try:
            from pptx.enum.shapes import PP_PLACEHOLDER
            ph_type = shape.placeholder_format.type
            if ph_type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
                return "title"
            ...
    # 2. Shape-name keyword heuristic (legacy)
    name_lower = (shape.name or "").lower()
    if any(kw.lower() in name_lower for kw in _TITLE_KEYWORDS):
        return "title"
    # 3. Geometry fallback
    if y_ratio < 0.15 and current_text:
        return "title"
    # 4. Text-content heuristic
    if "PART " in current_text:
        return "part_label"
```

This is Strategy via **priority chain**: 4 algorithms in priority order,
first match wins. Cleaner than a parameter-switch because:
- New strategies add at the end (open/closed)
- Order of priority is explicit (the chain)
- Each strategy is independent (no shared state)

**Where Strategy is also used** — `MarkdownExpandHandler._auto_fill_template_options`:
```python
if (options["expand_divider_edits_template"] is None):
    # Use InspectedAdapter (Phase 21 inspect_template auto-fill)
    profile = inspect_template(...)
    options["expand_skeleton_divider"] = profile.divider_skeleton
else:
    # Use CallerSuppliedAdapter (legacy caller-supplied)
    pass  # do nothing, use options as-is
```

Two concrete strategies (InspectedAdapter vs CallerSuppliedAdapter),
selected at runtime based on whether options are None. Cleaner than
a single if/elif chain because each has a different discovery path.

---

### 2.6 Factory Method (GoF p. 107)

**Intent**: Define an interface for creating an object, but let
subclasses decide which class to instantiate.

**Real code** — `pipeline/orchestrator.py`:

```python
class Pipeline:
    DEFAULT_HANDLERS: ClassVar[list[type[PipelineHandler]]] = [
        Phase2ImportHandler,
        MarkdownExpandHandler,
        Phase3AuthorHandler,
        Phase4QualityHandler,
        Phase5ExportHandler,
    ]

    def __init__(self, handlers=None):
        self.handlers = handlers or [h() for h in self.DEFAULT_HANDLERS]
```

This is a **registry factory**: the canonical handler chain is declared
as a class attribute, and `Pipeline()` instantiates each class. Adding
a new handler is `DEFAULT_HANDLERS.append(NewHandler)`.

**Trade-offs**:
- ✅ Single source of truth for canonical chain
- ✅ Subclass can override DEFAULT_HANDLERS to customize
- ❌ Class-name strings in DEFAULT_HANDLERS — typo risk

---

### 2.7 Chain of Responsibility (degenerate) — GoF p. 223

**Intent**: Pass a request along a chain of handlers until one handles it.

**Real code** — `pipeline/orchestrator.py:Pipeline.run`:

```python
for h in self.handlers:
    if h.name in ctx.skip_handlers:    # explicit skip list
        continue
    if h.skip(ctx):                     # handler-decided skip
        continue
    ctx = h.run(ctx)                    # ← all handlers run (not just one)
```

This is a **degenerate** CoR because every handler runs (not "first
match wins"). But the skip mechanisms borrow CoR's "handler decides
whether to act" pattern.

**Where full CoR would apply** (future): content-mapping dispatch
where each edit slot has multiple candidate transformers and the
pipeline tries them in order. Not yet implemented.

---

### 2.8 State — `pipeline/_internal.py:PipelineState`

**Intent**: Allow an object to alter its behavior when its internal
state changes.

**Real code** — `_internal.py`:

```python
@dataclass
class PipelineState:
    stage: str = "init"     # → "import" → "imported" → "plan_realize"
                            # → "author" → "authored" → "quality"
                            # → "quality_passed" → "quality_advisory"
                            # → "export" → "exported" → "validate"
                            # → "done" | "failed"
    workspace: Path | None = None
    output_pptx: Path | None = None
    skill_dir: Path | None = None
    fix_iterations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_export_receipt: dict | None = None
    last_delivery: dict | None = None
    last_quality_stdout: str = ""
    context: dict[str, Any] = field(default_factory=dict)
```

This is a plain state object (not the strict GoF State pattern with
polymorphic `handle()` methods). But the lifecycle code in
`_internal.py:230-260` and `:1700-1810` does transition states based on
subroutine outcomes — that's the *behavior* part of State.

**Note (commit 6 history)**: Before commit 6, this was the only
state in `pipeline.py`. We moved it to `_internal.py` (under pipeline/
sub-package). External callers now `from mcp_ppt_native_fill.pipeline
import PipelineState` (re-exported in `__init__.py`).

---

### 2.9 Null Object (Fowler)

**Intent**: Provide an object with appropriate neutral behavior
instead of using `None`.

**Real code** — `pipeline/_internal.py:PipelineState`:

```python
@dataclass
class PipelineState:
    stage: str = "init"   # ← null-object: defaults to "init"
    workspace: Path | None = None
    output_pptx: Path | None = None
    ...
    fix_iterations: list[dict] = field(default_factory=list)  # ← empty list default
    warnings: list[str] = field(default_factory=list)  # ← empty list default
    errors: list[str] = field(default_factory=list)  # ← empty list default
    started_at: float = field(default_factory=time.time)
    last_export_receipt: dict | None = None  # ← None default (acceptable: callers check)
    last_delivery: dict | None = None
    last_quality_stdout: str = ""
    context: dict[str, Any] = field(default_factory=dict)
```

`PipelineState()` works as a "default no-op" — you can read every
field without checking for None. `warnings` is always a list (empty
or populated). `errors` is always a list. `last_export_receipt` is
None (None is the only acceptable Null here — caller checks).

`PipelineHandler.skip()` defaults to `False` (never skip).
`PipelineHandler.on_failure()` defaults to `None` (no cleanup).
Subclasses override only if needed.

---

### 2.10 Adapter Pattern — Phase 22 commits 5-6

**Intent**: Convert the interface of a class into another interface
clients expect. Adapter lets classes work together that couldn't be
otherwise because of incompatible interfaces.

**Real code** — `pipeline/handlers/*.py`:

```python
# pipeline/handlers/phase3_author.py
class Phase3AuthorHandler(PipelineHandler):
    def run(self, ctx):
        if ctx.options.get("fix_nested_picture"):
            auth = ctx.workspace / "authoring-svg-flat"
            autofix.repair_nested_picture_attrs(auth)

        page_plan_pages = ctx.get("page_plan")
        new_content_blocks = ctx.get("new_blocks")
        content_mapping = ctx.get("content_mapping") or {}

        _internal.phase3_author(
            ctx.state,
            page_plan_pages=page_plan_pages,
            content_mapping=content_mapping,
            new_content_blocks=new_content_blocks,
        )
        return ctx
```

The handler **adapts** the legacy free-function API
(`_internal.phase3_author(state, ...)`) to the PipelineHandler.run
interface (`run(ctx)`). The handler also adapts the data model:
`ctx.handler_outputs["page_plan"]` → positional arg
`page_plan_pages=`.

---

### 2.11 Retry-with-Backoff (workaround, not strictly GoF) — Phase 23 commit 1

**Intent**: When an operation may transiently fail (file lock, network
timeout, etc.), retry with increasing delays.

**Real code** — `vendor/.../converter.py:1236-1287`:

```python
def _copytree_with_retry(src, dst, **kwargs):
    last_exc = None
    for attempt in range(5):
        try:
            shutil.copytree(src, dst, **kwargs)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.2 * (2 ** attempt))   # 0.2, 0.4, 0.8, 1.6, 3.2 s
    raise last_exc


# Caller (was: shutil.copytree(output_dir, candidate_dir, symlinks=True))
_copytree_with_retry(output_dir, candidate_dir, symlinks=True)
```

**Backoff schedule**: exponential, capped at 3.2s, 5 retries, worst case 6.2s.

**Why this works** (empirically):
- boteng template (80 KB source.pptx): copytree completes in < 100ms, handle released
- template_v2 (5 MB): copytree takes 500-1500ms, retries bridge the gap

**Trade-offs**:
- ✅ No vendor behavior change on the happy path
- ❌ Worst case adds 6.2s to vendor publish for large templates
- ❌ Doesn't solve PowerShell-subprocess stdio handle races — only vendor-internal handle races

---

## 3. Cross-pattern relationships

### 3.1 Pipeline + Template Method + Context Object = the Big Three

```
Pipeline (Pipeline Pattern)
  └── iterates handlers
       ↓
PipelineHandler (Template Method — fixed lifecycle in Pipeline.run)
  └── subclass overrides run() (and optionally skip()/on_failure())
       ↓
PipelineContext (Context Object — passed through every handler)
  └── mutable state bag + cross-handler outputs
```

These three patterns work together as a unit: the Pipeline orchestrates
the algorithm; the Handler fills in the steps; the Context carries
the data. **You can't have one without the others** — Pipeline.run
needs handlers (Template Method) and a context (Context Object) to
do anything useful.

### 3.2 Façade + Adapter = handler pattern

```
handler.run() (Adapter)
  └── calls _internal.xxx (Façade)
       └── hides 700+ lines of vendor coordination
```

Every handler is an Adapter (free function → PipelineHandler.run
interface) that calls a Façade (_internal) that hides the complexity.

### 3.3 State + Null Object = default behavior

`PipelineState()` defaults (Null Object) + lifecycle transitions
(State). No `if state is None: ...` checks anywhere.

---

## 4. Patterns we explicitly DIDN'T use (and why)

| Pattern | Why we didn't use it |
|---|---|
| **Singleton** (GoF) | Pipeline is instantiated per call. Singleton adds global state that hurts testability. |
| **Observer** (GoF) | Handlers communicate via PipelineContext.handler_outputs dict. Observer would require subscription/unsubscription overhead for marginal value. |
| **Memento** (GoF) | PipelineState is itself a memento (it captures run-time state). We don't store snapshots for undo. |
| **Visitor** (GoF) | No double-dispatch use case (handlers all operate on PipelineContext). |
| **Builder** (GoF) | PipelineContext is constructed by caller; builder would add ceremony without need. |
| **Flyweight** (GoF) | No large number of similar objects (handlers are class-level, instantiated once per run). |
| **Proxy** (GoF) | `runner._run` is conceptually a proxy for vendor's main(). But the proxy here is thin (just env config + capture_output); we didn't abstract it further. |
| **Decorator** (GoF) | No layering of behavior needed; each handler is independent. |
| **Mediator** (GoF) | PipelineContext is a mediator. We didn't name it as such but it serves the role. |
| **Interpreter** (GoF) | No DSL to parse. |
| **Prototype** (GoF) | No cloning of complex objects. |

---

## 5. Anti-patterns we explicitly avoided

| Anti-pattern | How we avoided it |
|---|---|
| **God Object** | `_internal.py` (700+ lines) is large but cohesive (one function per phase). Not a god object. |
| **Spaghetti code** | Strict handler chain (Pipeline.DEFAULT_HANDLERS in order). No cross-handler conditionals. |
| **Magic numbers** | All thresholds in `_TITLE_KEYWORDS`, `_SLOT_THRESHOLD` etc. have descriptive names. |
| **Copy-paste** | `_copytree_with_retry` extracted to a named function (commit 1 of Phase 23); not inlined. |
| **Long Parameter List** | `PipelineContext` replaces the legacy 40+ arg function signature. |
| **Primitive Obsession** | `role: str` is still primitive, but the `_infer_slot_role` algorithm gives it semantic meaning via the Strategy chain. |
| **Switch Statements** | None (Pipeline Pattern replaces switch-on-phase). |
| **Speculative Generality** | Only added patterns we actually needed (e.g. Strategy for template adapter; didn't add Visitor for templates that don't need double dispatch). |
| **Inner Classes** | Handlers are top-level (not nested inside Pipeline). |
| **Circular Dependencies** | Verified: `pipeline/orchestrator.py` imports handlers, handlers import `_internal`, `_internal` doesn't import handlers. No cycle. |

---

## 6. Lessons from Phase 22 + 23

1. **Pipeline Pattern is right when phases are independent and reorderable** — adding a new handler is one line (DEFAULT_HANDLERS.append).
2. **Adapter pattern shines at boundaries** — handler/adapter adapts free functions to the Pipeline interface; vendor/facade hides 700 lines.
3. **Null Object reduces None checks dramatically** — `state: str = "init"` and `warnings: list = field(default_factory=list)` means every consumer can trust the field has a value.
4. **Strategy as priority chain is underused** — most "pick algorithm by conditions" code becomes cleaner with a priority chain of strategies, not nested ifs.
5. **Vendor modification is a real cost** — Phase 23 commit 1 (vendor retry) required editing vendor code. We mitigated via the plan doc and "Phase 18 exception" note. Smaller scope next time.

---

## 7. Reading order for new engineers

1. **Start with** `pipeline/__init__.py` (public exports)
2. **Then read** `pipeline/context.py` (the ABC + Context — small)
4. **Then read** `pipeline/orchestrator.py` (the runner)
5. **Then read** `pipeline/handlers/phase2_import.py` (a concrete handler — thin)
6. **Then read** `pipeline/_internal.py` (the Façade — large but linear)
7. **Then read** `pipeline/handlers/phase3_author.py` (a thin stub calling _internal)
8. **Then read** `template_adapter.py` (Phase 21 inspect_template + role inference)

After this tour, you understand the whole refactor.

---

**Author**: Claude
**Date**: 2026-09-20
**Status**: Living document. Updated as Phase 22-23 commits land.
**Reviewed against**: every commit from 979f9ed (commit 2) to dff5abb (commit 7, Phase 23 commit 1).