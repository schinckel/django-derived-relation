# DEP 24 backport design

Date: 2026-10-16

Status: Implemented on `codex/dep24-backport`; see the
[verification record](2026-10-16-dep24-backport-verification.md).

Execution update: start with Django 5.2, then assess Django 4.2 compatibility.
The user changed this order after approving the design.

## Problem

The existing backport requires `DerivedRelation` to register queryset sources.
Accepted DEP 24 instead uses bare multi-column querysets and explicitly opted-in
function expressions. We want to build on that implementation, replace the
mandatory descriptor, and retain explicit joins when their behaviour matters.

References:

- [Accepted DEP 24](https://github.com/django/deps/blob/main/accepted/0024-table-value-expression.rst)
- [Upstream implementation PR 21636](https://github.com/django/django/pull/21636),
  inspected at `f7a2507e6834bd8deda629ac2e512f108d662419`.
- [Function-source experiment](https://github.com/p-r-a-v-i-n/django/tree/feature/set-returning-functions-v2),
  inspected at `e4040fb20f95bd69e31e555353ebe250275b8ee4`.
- [DEP discussion of optional join wrappers](https://github.com/django/deps/pull/113#discussion_r3414132647).

These revisions are reference points, not evidence that their tests pass. The
upstream query-source PR currently rejects correlation. The function experiment
is not a clean stack on that PR. The accepted DEP governs intended public
behaviour; implementation discrepancies need explicit tests and investigation.

## Approved direction

Normal usage follows the DEP:

```python
parents = Parent.objects.alias(
    rows=Child.objects.values("amount", "sequence"),
).values("pk", "rows__amount", "rows__sequence")
```

An optional wrapper supplies explicit join controls:

```python
parents = Parent.objects.alias(
    rows=Join(
        totals,
        join_type="left",
        condition=Q(rows__parent_id=F("pk")),
    ),
).values("pk", "rows__total")
```

`Join` is the working public name. It describes a table source with join
instructions and delegates to the same source resolution and compilation path
as a bare source. It must not become another implementation of that path.
Supported explicit join kinds are LEFT and INNER. No other join kinds are
approved. Examples specify `join_type`; this design does not invent a default.

`DerivedRelation` is replaced in the intended public API. The package name,
module name, and existing opt-in queryset/mixin names can stay unchanged.
This is a breaking API change and requires migration documentation. No release
number, deprecation period, publication, or removal commit is authorised here.

## Behaviour

### Ordinary sources

- Register multi-column querysets with `alias()` and an explicit `values()`
  projection. Select their component columns individually.
- Alias registration is lazy. An unused source does not multiply rows or change
  `count()`/`exists()`. Repeated references reuse its join.
- Required sources use CROSS joins; a source needed only on an optional side of
  an OR uses LEFT OUTER. Selecting an output makes the source required, as the
  DEP specifies. Test this explicitly against the upstream revision, whose
  existing empty-source behaviour does not establish full DEP conformance.
- A multi-column source cannot be selected wholesale through `annotate()`.
- Ordinary bare single-column querysets and scalar `Subquery` expressions keep
  their existing meaning. This work does not promote or deduplicate them.
- Component names resolve using the projected names, including names containing
  `__`; longest matching projected name wins before applying transforms.
- Complete-source references needed by tuple operations use internal typed tuple
  expressions. This does not imply model hydration or selecting an entire row
  into one annotation.

### Explicit joins

- `Join` forces table-source interpretation, including a one-column queryset.
- `condition` is an ON predicate. It is not moved into WHERE or into an ordered,
  sliced source. Those rewrites can change the result.
- An explicitly requested LEFT join preserves unmatched rows when its output is
  selected. A subsequent WHERE predicate can still remove those rows.
- Explicit controls do not imply eager registration: unused sources remain lazy.
  This differs from the old descriptor's retained-join semantics and must be
  documented and tested.
- ON predicates may refer to the outer query, this source, and previously
  registered sources. Reject dependency cycles or unsupported forward references
  clearly; dependency sorting is not part of this change.
- A source referenced only by another active source or ON condition is itself
  required for compilation and must not be pruned.

### Correlation and backends

Infer correlation from resolved outer bindings, including nested expressions
and supported combined branches. Do not require a public `lateral=True` flag.
Use the existing backend rendering and capability handling where applicable.

Capability depends on source kind and join kind. Correlated querysets and
correlated table functions are different capabilities. Some functions have
implicit correlation syntax; this does not establish support for correlated
derived querysets. Retain the tested TiDB INNER/LEFT distinction. Preserve
ordinary-query support on backends without correlated-query support.

Reject unsupported execution with `NotSupportedError`; do not silently rewrite
away correlation, ON conditions, or null extension. Backend claims require real
execution tests. Snowflake remains outside the matrix. SQL Server work remains
separate unless merged before implementation starts.

### Table functions

Follow the DEP's `table_source=True` opt-in and `output_field` metadata. Do not
reinterpret existing `set_returning=True` expressions automatically.

Single-column functions expose a typed scalar alias. Multi-column functions
expose named components through a lightweight `CompositeField`; physical names
can use `db_column`. An optional `Join` can wrap either source kind.

Use backend-specific compilation for function syntax, correlation syntax and
column aliases. This design does not promise arbitrary functions on every
backend or infer their row shape from database catalogues.

## Architecture and reuse

Keep adaptation local to `DerivedQuerySetMixin`, its Query subclass and its
compiler subclass. Source managers can remain ordinary Django managers. Do not
patch Django's global classes, expression attributes, managers, or field lookup
registries.

Reuse the current projected-column inspection, typed column references,
nullability handling, source relabelling, backend adaptations, custom compiler
preservation, database checks, and safe empty-source compilation where tests
confirm they remain necessary. Replace eager descriptor registration and
immutable join types for inferred joins.

Adapt the upstream implementation by responsibility rather than copying its
entire composite-field changes. On Django 4.2, intercept a multi-column queryset
before its native `resolve_expression()` rejects it. Supply compatible output
metadata and tuple expressions locally; do not backport concrete composite
primary keys or migrations. On newer Django, use available native primitives
when their behaviour is compatible.

Integrate source dependencies into Django's join voting and alias/reference
lifecycle. Compiler-only rendering cannot provide correct OR, negation,
selection, count, or cloning semantics.

Use package-local imports for backported public types. The present import guard
for native `models.DerivedRelation` no longer detects this API. Replace it only
after defining which native capabilities make the adapter redundant; the mere
presence of `CompositeField` is insufficient while core implementation is
partial. Native-package handoff must preserve optional explicit join controls.

## Errors and compatibility boundaries

Preserve normal scalar errors and name-conflict checks. Give targeted errors
for missing projections, unsupported whole-row selection, invalid join kinds,
unknown output components, unsafe joined updates, and cross-database sources.
An explicitly selected `.using()` binding must survive normalisation.

Do not merge unrelated open PRs as part of this redesign. Start production work
from the then-current main in its own branch. Include only merged backend and
source capabilities; keep outstanding work separately reviewable.

The private scalar-subquery reuse experiment is separate. Its demonstrated
volatility counterexample remains unresolved; this redesign does not enable it.

## Testable acceptance criteria

1. Bare multi-column queryset examples work on Django 4.2 through the supported
   current Django versions, using compatible Python versions.
2. Bare scalar querysets, scalar `Subquery`, `Exists`, legacy functions, and
   ordinary managers retain their native behaviour.
3. Lazy joins, selection requirements, OR, negation, null checks, clone/relabel,
   alias reuse/replacement, tuple operations, and aggregation match tested DEP
   behaviour rather than existing descriptor assumptions.
4. Explicit LEFT joins retain unmatched rows; ON conditions after LIMIT are
   observably distinct from filtering before LIMIT or in WHERE.
5. One-column explicit queryset sources work without changing bare scalar use.
6. Correlated PostgreSQL query sources execute without an explicit lateral flag;
   unsupported backends fail clearly. Nested references and dependencies are
   checked, not inferred from a single happy-path SQL string.
7. PostgreSQL scalar/composite table functions execute through both bare and
   explicit paths. Any further backend/function combination has execution
   evidence before it is advertised.
8. No global monkey patches are introduced. Public types and compiled/uncompiled
   query lifecycle receive appropriate cloning and pickle coverage.
9. Existing behaviour tests are migrated deliberately, with documented changes
   to eager joins, bare projections, and multi-column `annotate()` registration.
10. A wheel installed independently of a Django checkout passes the matrix.
    Existing hooks and formatting checks pass. New functions are fully typed.
    No type checker or cognitive-complexity analyser is currently configured;
    record that limitation and manually review until one is configured.

## Remaining implementation checkpoints

- Confirm the exact `CompositeField` constructor against the chosen upstream
  revision. DEP examples and the current PR use different spellings.
- Prove local tuple integration on Django 4.2 without global changes before
  promising complete upstream operation coverage.
- Resolve any further upstream/DEP behavioural discrepancies explicitly.
- Decide release migration timing and final optional-wrapper naming before
  publishing, not as an implicit consequence of this design approval.
