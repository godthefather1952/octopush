# Static analysis

## In force

**ruff** (lint + import sorting), configured in `pyproject.toml`:
`select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]`, line length 100.

```bash
ruff check .
```

Clean as of Phase 0 remediation. It is a gate: a lint error is a build
failure, not a warning.

## Evaluated and deliberately not yet a gate

**mypy 2.3.1**, run across the eleven platform packages:

```bash
mypy --ignore-missing-imports agents apps core execution monitoring \
     replay risk simulation storage strategies venues
```

Result when first run during Phase 0 remediation: **48 errors in 10 of 101
files.** Each was read rather than counted, not merely tallied. Seven were
fixed (see below), leaving **41 errors in 7 files**, and `core/` — the
package everything else depends on — at **zero**.

They fall into three groups:

### 1. Optional not narrowed through a filter (the large majority)

mypy cannot see that a comprehension has already excluded the `None`s:

```python
usable = [s for s in states if s.quality.is_usable and s.metrics.mid is not None]
...
sum(s.metrics.mid * w for s, w in zip(usable, weights, strict=True))
#     ^ mypy: Unsupported operand types for * ("None" and "float")
```

`agents/tidal/agent.py:226,243`, `agents/okapi/agent.py:95,96` and
`apps/api/app.py:71` are all this shape. Every one was traced to its guard
and confirmed unreachable. **No live bug was found by mypy.**

### 2. A protocol narrower than its only implementation

`apps/orchestrator/orchestrator.py` calls `self.veska.executor.account`,
`.execution_disabled` and `.update_market`, none of which are on the
`Executor` interface — they exist on `PaperExecutor`, which is the only
implementation and is asserted to be so
(`tests/unit/test_venues.py::test_paper_executor_is_the_only_executor_implementation`).
Fixing this properly means deciding what belongs on the execution interface,
which is an interface design change and therefore Phase 1 work, not Phase 0
remediation.

### 3. Fields a `__post_init__` guarantees

`barrier: ResponseBarrier | None` is `None` only between `__init__` and
`__post_init__`, which no caller can observe. mypy flags every use.

### Fixed rather than filed

Seven of the 48 were genuine typing defects, not false positives, and
were corrected (three distinct causes, across three files):

- `Orchestrator.recorder` was annotated `object | None`, which silenced the
  checker by discarding the type altogether. Now `Recorder | None` behind
  `TYPE_CHECKING`, since `storage` imports `core` and a runtime import would
  close the cycle.
- `ZephrConfig.size_ladder` was declared `list[float]` with an `int` default
  factory — the annotation and the value disagreed.
- `InMemoryEventBus.subscribe` and `RedisEventBus.subscribe` passed
  `getattr(handler, "__qualname__", "handler")` (typed `Any | None`) into a
  `str` parameter.

Those three files were the whole of `core/`'s error count, which is why it
now stands at zero.

## Recommendation

Do not make mypy a gate yet, and do not silence it with a blanket ignore —
that produces a check that passes while proving nothing.

Adopting it as a gate today would require either 41 further fixes, most of them in
agent logic that Phase 0 remediation is explicitly not permitted to change,
or a suppression broad enough to be worthless. Neither is a good trade for a
tool that, on this codebase, found no live bug.

The sequence that does pay:

1. Enable mypy on `core/` alone — now at zero, verified with
   `mypy --ignore-missing-imports core` — and gate that. It is the package
   everything else depends on, so it is where a regression costs most.
2. Decide the execution interface question during Phase 1, when changing
   `Executor` is in scope. That clears group 2 as a side effect.
3. Extend the gate package by package as each reaches zero, rather than
   turning it on everywhere and living with a permanent baseline of noise.

The full run should be repeated at the start of Phase 1 so the count is
tracked rather than rediscovered.

## Not evaluated

**pyright** was not run. It would likely report the same three groups —
its Optional narrowing through comprehensions is somewhat better than
mypy's, so the group-1 count would fall — but the conclusion above turns on
groups 2 and 3, which are structural and which any checker will flag.
