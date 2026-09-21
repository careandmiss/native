# Pipeline Pattern — Design Deep-Dive (2026-09-20)

> **Status**: Living document. Updated with each commit of Phase 22 (Pipeline Pattern refactor).
> **Purpose**: Deep-dive into every design pattern used by the refactor, with GoF references, code excerpts, and trade-offs.
> **For**: Engineers learning design patterns by reading real production code.

---

## Reading Order

| # | Pattern | Where used in Phase 22 | Commit |
|---|-----|-----|-----|
| 1 | **Pipeline Pattern** | Phase 1-5 handlers | commit 1-5 |
| 2 | **Template Method** | `PipelineHandler.run()` as template, subclasses fill in | commit 1 |
| 3 | **Context Object** | `PipelineContext` carries state between handlers | commit 1 |
| 4 | **Façade** | Each handler wraps vendor/internal complexity | commit 3, 5 |
| 5 | **Strategy** | `inspect_template` (Phase 21) chooses auto vs hardcoded adapter | commit 4 |
| 6 | **Factory** | `Pipeline.DEFAULT_HANDLERS` builds the default handler chain | commit 2 |
| 7 | **Chain of Responsibility** | `Pipeline.run()` skips handlers via `skip_handlers` / `skip()` | commit 2 |
| 8 | **State** | `PipelineState` is the persistent state object | commit 1, 6 |
| 9 | **Null Object** | `_legacy.py` is a no-op shim during transition | commit 1, 6 |

---

## 1. Pipeline Pattern

**GoF reference**: Not in the original GoF book (1994). Often attributed
to Unix shell pipelines and the Pipes-and-Filters EAA pattern
(Fowler 2002). GoF's closest analog is **Chain of Responsibility**
(behavioral, p. 223), but Chain dispatches one event to one handler,
while Pipeline transforms data through every handler.

**Intent**: A sequence of operations on data, with data flowing through
each operation in turn. Each operation is independent and
configurable.

**When to use**:
- You have a clear, ordered set of phases (init → import → author
  → quality → export).
- Each phase can be implemented independently.
- You want to add, remove, or reorder phases without rewriting the
  orchestrator.
- You want to test each phase in isolation.

**When NOT to use**:
- Phases have complex inter-dependencies that don't fit "data flows
  through handlers".
- The order of operations varies per request (use Strategy instead).
- Each phase needs a different runtime (e.g., async vs sync).

### Real code in this refactor

`src/mcp_ppt_native_fill/pipeline/orchestrator.py` (commit 2):

```python
class Pipeline:
    def __init__(self, handlers: list[PipelineHandler] | None = None):
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
                h.on_failure(ctx, exc)
                break
            if ctx.stop_after == h.name:
                break
        return ctx
```

### Trade-offs (this project)

| Pro | Con |
|---|---|
| Each handler is ~150 lines, easy to read | Indirection cost: 5 classes instead of 5 functions |
| Mock-friendly: test one handler without instantiating the others | Adds `PipelineContext` boilerplate |
| Easy to add new phase (e.g. Phase 23's "test_allocation"): just write `class TestAllocationHandler(PipelineHandler)` | First failure short-circuits — can hide partial progress |

---

## 2. Template Method

**GoF reference**: GoF (1994), Template Method pattern (behavioral,
p. 325).

**Intent**: Define the skeleton of an algorithm in a base class,
letting subclasses override specific steps without changing the
algorithm's structure.

**When to use**:
- Multiple classes share an algorithm structure but differ in
  specific steps.
- You want to enforce that the algorithm's structure isn't changed
  by subclasses.

### Real code in this refactor

`src/mcp_ppt_native_fill/pipeline/context.py`:

```python
class PipelineHandler(ABC):
    name: ClassVar[str]

    @abstractmethod
    def run(self, ctx: PipelineContext) -> PipelineContext:
        raise NotImplementedError

    def skip(self, ctx: PipelineContext) -> bool:
        return False  # default: never skip

    def on_failure(self, ctx, exc):
        return None  # default: no cleanup
```

Subclass example (`Phase2ImportHandler`, commit 3):

```python
class Phase2ImportHandler(PipelineHandler):
    name = "phase2_import"

    def run(self, ctx):
        res = runner.run_pptx_to_svg(...)  # actual work
        if not res.ok:
            raise PipelineError(...)
        ctx.set("vendor_result", res.parsed)
        return ctx

    def on_failure(self, ctx, exc):
        # Vendor may have left .publish-{hash}/ temp dirs; clean up
        for p in ctx.workspace.parent.glob(f".{ctx.workspace.name}.publish-*"):
            shutil.rmtree(p, ignore_errors=True)
```

The template is: `name` + `run` (must implement) + `skip` (default
never) + `on_failure` (default no-op). The orchestrator calls them
in a fixed order (skip → run → on_failure-on-error). Subclasses only
override what they need.

### Trade-offs

| Pro | Con |
|---|---|
| Algorithm structure enforced by base class | Inheritance coupling: changing the template affects all subclasses |
| Subclasses only write the parts that differ | Hard to extend with non-default sequence (use Strategy instead) |
| Default impls (`skip`/`on_failure`) reduce subclass boilerplate | Subtle bugs from forgot-to-override (mitigated by `name: ClassVar[str]`) |

---

## 3. Context Object

**GoF reference**: Not in GoF. Common in EAA (Fowler) as
*Parameter Object* (refactoring) and *Data Transfer Object*.

**Intent**: Bundle related parameters into one object passed
throughout a long method chain. Reduces parameter-list sprawl.

**When to use**:
- A method takes 5+ parameters.
- The parameters are related (all about one operation).
- You want to add new parameters without changing every call site.

**When NOT to use**:
- The parameters are unrelated (use multiple Parameter Objects).
- The object would just be a glorified dict (consider TypedDict).

### Real code in this refactor

`PipelineContext` (commit 1):

```python
@dataclass
class PipelineContext:
    # Inputs (one-shot, set before run())
    state: PipelineState
    source_pptx: Path
    workspace: Path
    output_pptx: Path
    skill_dir: Path
    options: dict[str, Any]

    # Inter-handler outputs
    handler_outputs: dict[str, Any] = field(default_factory=dict)

    # Lifecycle
    skip_handlers: set[str] = field(default_factory=set)
    stop_after: str | None = None
```

Before this refactor, `pipeline.run_native_fill()` took **40+
keyword-only arguments**:

```python
def run_native_fill(*, source_pptx, workspace, output_pptx, page_plan,
                     content_mapping, new_content_blocks, skill_dir,
                     auto_fix, max_fix_iterations, validate_strict,
                     inheritance_mode, content_markdown, enable_llm_planner,
                     skip_phase3_5, disabled_autofixes, quality_strict,
                     render_previews, preflight_strict, llm_layout_hints,
                     enable_chrome_topbar, enable_chrome_footer,
                     enable_ppt_master_archetypes, ...  # 20+ more):
    ...
```

After: `run_with_mapping()` builds a `PipelineContext` once, hands
it to `Pipeline.run()`. Adding a new option doesn't ripple through
every call site.

### Trade-offs

| Pro | Con |
|---|---|
| Adding params doesn't change call sites | Type-checking less granular (all in one dict) |
| Centralized docstring (`PipelineContext` describes all params) | Mutable state can hide data flow |
| Handlers share state via `ctx.handler_outputs` | Handlers can accidentally clobber each other's keys |

---

## 4. Façade

**GoF reference**: GoF (1994), Façade pattern (structural, p. 185).

**Intent**: Provide a unified interface to a set of interfaces in a
subsystem. Define a higher-level interface that makes the subsystem
easier to use.

**When to use**:
- A subsystem has many classes with awkward relationships.
- You want to decouple client code from subsystem complexity.
- You want to layer your system (subsystem can evolve independently).

### Real code in this refactor

`Phase2ImportHandler` (commit 3) wraps vendor's
`pptx_to_svg.py --roundtrip` complexity:

```python
class Phase2ImportHandler(PipelineHandler):
    name = "phase2_import"

    def run(self, ctx):
        # Façade hides: vendor spawn args, stdout parsing, attribution
        # guard, temp-dir lifecycle, slide-count regex.
        res = runner.run_pptx_to_svg(
            ctx.skill_dir, ctx.source_pptx, ctx.workspace,
            inheritance_mode=ctx.options.get("inheritance_mode", "both"),
            roundtrip=True,
        )
        if not res.ok:
            raise PipelineError(f"phase2 vendor failed: {res.stderr}")
        ctx.set("vendor_result", res.parsed)
        return ctx
```

The handler hides:
- subprocess.Popen spawn details (managed by `runner.run_pptx_to_svg`)
- Attributed-guard script invocation
- `Slides converted: N` stdout regex
- `workspace/published/original source.pptx` path resolution
- workspace cleanup

Client code (the orchestrator) just sees "handler runs, ctx has
vendor_result, or PipelineError raised".

### Trade-offs

| Pro | Con |
|---|---|
| Client code simpler (1 method call) | Façade can become a god-object if it grows |
| Subsystem can change without breaking clients | Façade adds indirection; debugging is one hop deeper |
| Easier to mock for testing | Over-facading hides too much (e.g. progress callbacks) |

---

## 5. Strategy

**GoF reference**: GoF (1994), Strategy pattern (behavioral, p. 315).

**Intent**: Define a family of algorithms, encapsulate each one, and
make them interchangeable. Strategy lets the algorithm vary
independently from clients that use it.

**When to use**:
- Many related classes differ only in their behavior.
- You need different variants of an algorithm at runtime.
- An algorithm uses data that clients shouldn't know about.

### Real code in this refactor

`MarkdownExpandHandler` (commit 4) chooses between hardcoded
template config (legacy boteng demo) and auto-inspect
(Phase 21 inspect_template):

```python
class MarkdownExpandHandler(PipelineHandler):
    name = "markdown_expand"

    def _auto_fill_template_options(self, ctx):
        # Strategy: caller-supplied options win (preserve compat);
        # otherwise inspect the template.
        if (ctx.options.get("expand_divider_edits_template") is None
                or ctx.options.get("expand_content_edits_template") is None):
            from .. import template_adapter
            inspect = template_adapter.inspect_template(
                template_adapter.ensure_ascii_path(ctx.source_pptx)
            )
            if ctx.options.get("expand_skeleton_divider") is None:
                ctx.options["expand_skeleton_divider"] = inspect.divider_skeleton
            # ... more auto-fills ...
```

This is Strategy via **conditional strategy selection** — the
choice (caller's options vs inspect_template) is made at runtime
based on what's None.

A future improvement (Phase 23+) could formalize this:

```python
class TemplateAdapter(ABC):
    @abstractmethod
    def fill_options(self, ctx) -> dict[str, Any]: ...

class CallerSuppliedAdapter(TemplateAdapter):
    def fill_options(self, ctx):
        return {}  # user supplies everything

class InspectedAdapter(TemplateAdapter):
    def fill_options(self, ctx):
        inspect = inspect_template(ctx.source_pptx)
        return {"expand_skeleton_divider": inspect.divider_skeleton, ...}

class TemplateAdapterFactory:
    @staticmethod
    def pick(ctx) -> TemplateAdapter:
        if ctx.options.get("expand_divider_edits_template"):
            return CallerSuppliedAdapter()
        return InspectedAdapter()
```

But commit 1-6 ship the simpler conditional form. Strategy is
*implicit* in the dispatch.

### Trade-offs

| Pro | Con |
|---|---|
| Algorithm selection at runtime | Client must know about strategies (or use a factory) |
| Easy to add new strategies | More classes than conditional logic |
| Strategies can be tested independently | Communication overhead between context and strategy |

---

## 6. Factory

**GoF reference**: GoF (1994), Factory Method pattern (creational,
p. 107). Simple Factory (non-GoF) is just a function that returns
objects.

**Intent**: Define an interface for creating an object, but let
subclasses decide which class to instantiate.

### Real code in this refactor

`Pipeline.DEFAULT_HANDLERS` (commit 2):

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

This is a **registry factory**: the canonical handler chain is
declared as a class attribute, and `Pipeline()` instantiates each
class. Callers override via `Pipeline(handlers=[...])` for testing.

### Trade-offs

| Pro | Con |
|---|---|
| Single place to change the default chain | Class-name strings are not type-checked |
| Easy to add new handler (just append to list) | Order in list = order of execution (silent bug if misordered) |
| Testing: pass custom handlers | Registry vs import cycle: handlers must be importable at module load |

---

## 7. Chain of Responsibility

**GoF reference**: GoF (1994), Chain of Responsibility (behavioral,
p. 223).

**Intent**: Avoid coupling the sender of a request to its receiver
by giving more than one object a chance to handle it. Chain the
receiving objects and pass the request along the chain until an
object handles it.

### Real code in this refactor

`Pipeline.run()` (commit 2) checks two ways to skip a handler:

```python
for h in self.handlers:
    if h.name in ctx.skip_handlers:        # explicit skip list
        continue
    if h.skip(ctx):                         # handler-decided skip
        continue
    try:
        ctx = h.run(ctx)
    except PipelineError as exc:
        ...
```

Two skip mechanisms:
1. **`ctx.skip_handlers`** — explicit set of names (caller-set; for
   resume from middle, debugging, partial pipelines).
2. **`h.skip(ctx)`** — handler-decided based on context (e.g.
   `MarkdownExpandHandler.skip()` returns `True` when no
   `content_markdown`).

This is a degenerate form of Chain of Responsibility (Pipeline is
not really CoR because **every** handler runs in order). But the
skip mechanisms borrow CoR's "handler decides whether to act".

### Trade-offs

| Pro | Con |
|---|---|
| Caller can skip specific phases | Two skip APIs — which wins? (current: skip_handlers first) |
| Handler can be conditional without leaking decision | `stop_after` is ad-hoc (could be a Strategy) |
| Easy to add new skip conditions | Hard to debug: which handler skipped? (need logging) |

---

## 8. State

**GoF reference**: GoF (1994), State pattern (behavioral, p. 305).

**Intent**: Allow an object to alter its behavior when its internal
state changes. The object will appear to change its class.

**When to use**:
- An object's behavior depends on its state, and it must change
  behavior at runtime depending on that state.
- Operations have large, multipart conditional statements that depend
  on the object's state.

### Real code in this refactor

`PipelineState` (existing in `pipeline.py`, kept):

```python
@dataclass
class PipelineState:
    stage: str = "init"
    workspace: Path | None = None
    output_pptx: Path | None = None
    skill_dir: Path | None = None
    fix_iterations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # ...
```

Before Pipeline Pattern: state was passed implicitly via
**closure-captured locals** in `run_native_fill`:

```python
def run_native_fill(*, ...):
    state = PipelineState()
    state.stage = "import"
    res = runner.run_pptx_to_svg(...)
    state.stage = "imported"
    # ... 10 more state mutations ...
    return state
```

After: state is **passed explicitly** in `PipelineContext.state`.
Each handler does `ctx.state.stage = "phase2_done"` and the next
handler reads it. State is no longer hidden in closures.

Note: this is **NOT the GoF State pattern** in the strict sense
(we don't have `State` subclasses with polymorphic `handle()`
methods). It's a **plain state object** (the common interpretation
in modern Python codebases).

### Trade-offs

| Pro | Con |
|---|---|
| State is inspectable (can log, can debug) | Mutation is non-atomic (concurrent handlers could race) |
| Easy to add fields without breaking signatures | Hard to enforce state transitions (would need FSM) |
| Serializable for debugging | Mutable state in unexpected places |

---

## 9. Null Object

**GoF reference**: Not in GoF. Common in Fowler's *Refactoring*
book and many modern codebases.

**Intent**: Provide an object with appropriate neutral ("null")
behavior instead of using `None`. Simplifies client code by
removing the need for `None` checks.

**When to use**:
- A method would otherwise return `None`.
- The caller would have to check for `None` before using the result.
- A neutral default behavior is acceptable.

### Real code in this refactor

**`PipelineHandler.skip()` default = `False`** (commit 1):

```python
class PipelineHandler(ABC):
    @abstractmethod
    def run(self, ctx): ...
    
    def skip(self, ctx):
        return False  # null-object: default is "never skip"

    def on_failure(self, ctx, exc):
        return None  # null-object: default is "no cleanup"
```

Subclasses override only when they need different behavior.
`Phase2ImportHandler` does NOT override `skip` (always runs).

**`pipeline/_legacy.py`** is itself a Null-Object-style transition
shim (commits 5-6):

```python
# Before commit 6, the legacy module is still imported. After
# commit 6, it's deleted and all callers use Pipeline.run().
# In the meantime, _legacy.py provides the OLD API as a no-op
# wrapper around the NEW pipeline.
```

### Trade-offs

| Pro | Con |
|---|---|
| No `None` checks needed | Can hide bugs (caller might not realize it got a null) |
| Default impls are obvious | Subclasses may forget to override (mitigated by docstring) |
| Easier to extend | Identity vs behavior: when does "no-op" become "wrong default"? |

---

## Cross-Pattern Relationships

```
                    ┌──────────────────────┐
                    │   Pipeline (Factory)  │
                    │  - DEFAULT_HANDLERS    │
                    └──────────┬───────────┘
                               │ builds
                               ▼
        ┌─────────────────────────────────────────────┐
        │       Pipeline.run() (Chain of Resp.)        │
        │  for h in handlers: skip → run → on_failure │
        └──────┬──────────────────────────────────────┘
               │ delegates
               ▼
┌─────────────────────────────────────────────────────────┐
│   PipelineHandler (Template Method)                     │
│   name / run* / skip / on_failure                        │
└──────┬──────────────────────────────────────────────────┘
       │ extended by
       ▼
┌──────────────────────────────────────────────┐
│  Concrete handlers (Strategy + Façade)         │
│  - Phase2ImportHandler   (Façade + State)       │
│  - MarkdownExpandHandler (Strategy: inspect?) │
│  - Phase3AuthorHandler   (Façade)              │
│  - Phase4QualityHandler  (Chain step)          │
│  - Phase5ExportHandler   (Façade)              │
└──────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────┐
│  PipelineContext (Context Object + State)      │
│  - inputs (source_pptx, workspace, ...)        │
│  - handler_outputs (inter-handler data)        │
│  - skip_handlers / stop_after (lifecycle)       │
│  - state: PipelineState (State pattern)        │
└──────────────────────────────────────────────┘
```

---

## Lessons Learned (Phase 22 retrospective)

1. **Commit 1's biggest friction**: Python's package-vs-module
   resolution made `pipeline.py` shadow `pipeline/` sub-package.
   Solution: rename `pipeline.py` → `pipeline/_legacy.py` and rewrite
   all 14 relative imports from `from .` to `from ..`. Took 3
   iterations to find all of them.

2. **Don't pre-export everything from `__init__.py`**: `from
   ._legacy import *` doesn't export underscore-prefixed names
   unless `__all__` is set. Solution: explicit re-export list.

3. **`TYPE_CHECKING` import for forward references**: `context.py`
   needed `PipelineState` for type annotation but importing it at
   runtime caused a circular import. `if TYPE_CHECKING:` solved
   it. Mypy/pyright see the type; runtime doesn't trigger the cycle.

4. **Sub-package boundary costs 1 PowerShell move**: Moving
   `pipeline.py` → `pipeline/_legacy.py` is a `Move-Item` away in
   PowerShell, but git rename detection picks it up as 94% match.
   We get a clean rename in the diff.

---

## Further Reading

- GoF (1994): *Design Patterns* — Chain of Responsibility (p. 223),
  Template Method (p. 325), Strategy (p. 315), State (p. 305),
  Façade (p. 185), Factory Method (p. 107).
- Fowler (2002): *Patterns of Enterprise Application Architecture* —
  Pipeline (Pipes-and-Filters), Data Transfer Object, Null Object.
- Martin Fowler's *Refactoring* book (2018) — Parameter Object,
  Replace Conditional with Polymorphism.

---

**Author**: Claude (with extensive design-mode documentation requested by user)
**Date**: 2026-09-20
**Status**: Living document. Updated as Phase 22 commits land.