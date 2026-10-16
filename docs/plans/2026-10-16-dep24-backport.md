# DEP 24 Backport Implementation Plan

**Goal:** Replace mandatory `DerivedRelation` usage with DEP-compatible sources and an optional `Join` wrapper for explicit LEFT/INNER joins and ON conditions.

**Architecture:** Keep source registration, typed output resolution, lazy join creation and compilation in the existing opt-in queryset/query/compiler adapters. Adapt the upstream implementation locally for Django 4.2 onward; share that machinery between bare sources and explicit joins, with backend-specific execution checks.

**Tech Stack:** Python 3.10+, Django 4.2 through current supported releases and development Django, PostgreSQL first, existing tox/GitHub Actions backend matrix.

---

Approved design: [DEP 24 backport design](2026-10-16-dep24-backport-design.md).

This is a planning artifact, not an instruction to start implementation, merge
other PRs, publish a release or enable the scalar-subquery reuse experiment.
Paths below are relative to a fresh implementation checkout of
`django-derived-relation`. Do not modify the separate Django core checkout.

## Execution rules and evidence

- Branch from the then-current `main`, independently of the additional-backends,
  combined-source and SQL Server branches. Preserve outstanding local work.
- Read applicable repository instructions and Git rules before state changes.
- Inspect upstream PR 21636 at the design's recorded revision. Record any newer
  revision used. Check licensing/attribution for any copied implementation.
- Keep existing API tests during development; migrate them deliberately in task
  10. Intermediate implementation support is not a public compatibility promise.
- Use @superpowers:test-driven-development and
  @superpowers:verification-before-completion during implementation.
- All new Python functions, methods, test helpers and fixtures have complete
  annotations compatible with Python 3.10. Avoid globally patching Django.
- Each task follows red test, minimal implementation, green test, review, commit.
  Do not bypass installed commit hooks. Do not commit unrelated experiments.
- Run database tests only against disposable development/test databases. The
  runner creates/drops `test_django_derived_relation`; check configuration first.
- No type checker or complexity analyser was found in the current project.
  Recheck when execution starts; run configured tools if present, otherwise
  manually review and disclose the missing automated checks.

Local feedback commands, with the chosen environment's Python activated:

```bash
PYTHONPATH=. python tests/runtests.py --sqlite
PYTHONPATH=. python tests/runtests.py
```

The first checks ordinary sources without a server; the second runs PostgreSQL,
including correlation. Add the new modules under the existing test application
so the current discovery runner includes them. Test expectations below are
expected outcomes, not results already observed.

## Task 1: Establish the upstream compatibility cases

**Files:**
- Create: `tests/derived_relations/base.py`
- Create: `tests/derived_relations/test_table_sources.py`
- Modify: `tests/derived_relations/tests.py`
- Reference: `tests/derived_relations/models.py`

1. Move `DerivedRelationTestCase`'s fixture setup and its `source()` helper to
   `base.py`; update existing imports. Preserve Yugabyte transaction behaviour.
   Run existing tests to establish this refactor preserves results.
2. Add the following ordinary-source tests using the existing Parent and Child
   models. They should fail against the current adapter because it does not
   implement the bare multi-column path:

```python
from django.db.models import Q

from .base import DerivedRelationTestCase
from .models import Child, Parent


class TableSourceTests(DerivedRelationTestCase):
    def test_unused_source_does_not_multiply_rows(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.values("amount", "sequence"),
        )
        self.assertEqual(query.count(), 3)

    def test_selected_empty_source_is_required(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.filter(pk=-1).values("amount", "sequence"),
        ).values_list("pk", "rows__amount")
        self.assertEqual(list(query), [])

    def test_optional_empty_source_preserves_other_or_branch(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.filter(pk=-1).values("amount", "sequence"),
        ).filter(Q(name="empty") | Q(rows__amount=10))
        self.assertEqual(list(query.values_list("pk", flat=True)), [self.empty.pk])
```

3. Add unchanged-native cases for a scalar queryset, scalar `Subquery`, `Exists`,
   and an ordinary non-table `Func`. Do not use the opt-in source manager in the
   inner queryset for at least one test.
4. Record which upstream tests cover these cases and the known difference
   between the PR's selected-empty-source behaviour and the accepted DEP.
   Do not change expected results just to match the PR snapshot.
5. Commit the fixture refactor separately from the red compatibility cases if
   required by repository policy. Leave red cases uncommitted until task 3 if
   the project requires passing commits.

## Task 2: Backport output metadata and source normalisation

**Files:**
- Modify: `django_derived_relation.py`
- Create: `tests/derived_relations/test_composite_output.py`

1. Add red tests for generated output fields: projection order, aliases,
   `db_column`, annotated outputs, nullability, field copying, and invalid whole
   source selection. Include a projected name containing `__`.
2. Introduce package-local composite output metadata compatible with the chosen
   upstream public constructor. Test constructor spelling explicitly; current
   DEP examples and PR implementation differ. Do not promise both without need.
3. Normalise bare queryset sources before Django 4.2's QuerySet-level
   `resolve_expression()` multi-field rejection. Clone rather than mutate the
   source, preserve `_db` and write intent, and use its normal Query resolution
   to bind outer references. Preserve custom Query subclasses and compilers.
4. Distinguish multi-column querysets from scalar expressions by actual output
   shape. A one-column queryset remains scalar unless explicitly marked as a
   table source. Do not repurpose arbitrary `Subquery.extra` keywords.
5. Return typed component references; resolve the longest matching projected
   name before applying lookups/transforms. Avoid per-component fake public
   annotations if they break alias replacement or lazy dependencies.
6. Run focused metadata tests and the unchanged scalar suite on Django 4.2 and
   the newest supported release. Commit after the task's tests pass.

## Task 3: Materialise ordinary sources lazily

**Files:**
- Modify: `django_derived_relation.py`
- Modify: `tests/derived_relations/test_table_sources.py`

1. Extend red cases to cover repeated field references, same-model nested
   queries, aliases resembling generated SQL aliases, projection masks, and
   source `.none()`.
2. Store an unresolved table source in the annotation namespace. Materialise
   one join when a field or tuple reference needs it; reuse the join thereafter.
3. Integrate join use with native reference counts, alias maps, cloning and
   selection. Registering an unused source alone must leave `count()` and
   `exists()` unaffected. Preserve a referenced source dependency even if its
   own columns are not selected.
4. Render required ordinary sources as CROSS joins. Reuse safe empty-source
   compilation and the existing typed column/alias machinery where appropriate.
5. Get the unused and selected-empty task 1 cases passing on SQLite and
   PostgreSQL. Leave optional OR handling to task 4 rather than introducing an
   ad hoc SQL shortcut. Commit the passing ordinary-source slice.

## Task 4: Integrate Boolean join semantics and query lifecycle

**Files:**
- Modify: `django_derived_relation.py`
- Modify: `tests/derived_relations/test_table_sources.py`

1. Add red tests for OR, nested AND/OR, negation, `exclude()`, nullable output,
   `isnull`, expressions containing references, and selection combined with OR.
2. Feed source dependencies into native join voting. Annotation lookups return
   early in older Django's `build_filter()`; account for that explicitly.
   Creating a lazy LEFT join without returning its dependencies is insufficient.
3. Enforce the DEP's selection requirement independently of filter voting.
   Verify optional OR preserves unmatched rows until a selected source makes
   the join required. Handle annotations built from component expressions too.
4. Test alias replacement on clones, aggregation, `count()`, `exists()`,
   `values()` masks, ordering/transforms and distinct/grouped queries. Preserve
   scalar alias behaviour, including scalar aliases containing `__`.
5. Check unsupported writes fail for actual joined references, while unused
   alias registration does not cause unnecessary blanket rejection. Confirm
   update/delete behaviour by native Query compilation rather than guessing.
6. Run all ordinary-source tests on both version endpoints and commit.

## Task 5: Add the optional explicit Join wrapper

**Files:**
- Modify: `django_derived_relation.py`
- Create: `tests/derived_relations/test_explicit_joins.py`

1. Add the following regression for a grouped source and ON condition. Unlike a
   WHERE rewrite, the result must retain the parent with no children:

```python
from django.db.models import F, Q, Sum

from django_derived_relation import Join

from .base import DerivedRelationTestCase
from .models import Child, Parent


class ExplicitJoinTests(DerivedRelationTestCase):
    def test_grouped_left_join_preserves_unmatched_parent(self) -> None:
        totals = (
            Child.objects.order_by()
            .values("parent_id")
            .annotate(total=Sum("amount"))
            .values("parent_id", "total")
        )
        query = Parent.objects.alias(
            rows=Join(
                totals,
                join_type="left",
                condition=Q(rows__parent_id=F("pk")),
            ),
        )
        self.assertEqual(
            list(query.order_by("name").values_list("name", "rows__total")),
            [("empty", None), ("many", 50), ("one", 10)],
        )
```

2. Implement `Join` as source plus explicit instructions, delegating output
   schema, normalisation, dependency tracking and compilation to tasks 2–4.
   Require LEFT/INNER values and reject invalid values; no new join kinds.
3. Test an explicit one-column queryset source using `rows__amount`, contrasted
   with an unchanged bare single-column scalar queryset. Test single-column
   sources with more than one row to establish table-source semantics.
4. Keep explicit LEFT joins LEFT when selecting output; WHERE predicates remain
   WHERE predicates. Bare inferred joins still follow normal promotion rules.
5. Resolve ON references without treating them as WHERE join votes. Ensure
   related-model and prior-source dependencies appear before their consumer.
   Test missing/forward dependencies and cycles fail predictably.
6. Test that an unused explicit source remains lazy, and that an ON-only
   dependency of a used source survives pruning. Run ordinary backend tests;
   commit once green.

## Task 6: Infer and execute correlated queryset joins

**Files:**
- Modify: `django_derived_relation.py`
- Create: `tests/derived_relations/test_correlated_sources.py`

1. Add PostgreSQL execution tests for bare correlated multi-column sources and
   explicit LEFT/INNER sources. Use the existing actual model columns:

```python
source = (
    Child.objects.filter(parent_id=OuterRef("pk"))
    .order_by("-sequence")
    .values("amount", "sequence")[:1]
)
```

2. Assert bare selected-source results omit `empty`; explicit LEFT results
   contain `empty` with NULL outputs and choose `(30, 2)` for `many`.
3. Add `condition=Q(rows__amount__lte=20)` to the explicit LEFT join. Assert
   `many` has NULL outputs. Pushing that condition before LIMIT would incorrectly
   choose `(20, 1)`, so this must be an executed regression.
4. Infer external bindings after native expression resolution. Cover nested
   scalar expressions, nested `OuterRef`, references to earlier source columns,
   alias relabelling and custom source Query classes. Examine combined branches
   only if combined sources are present on the implementation base.
5. Reuse existing backend rendering and capability checks. Fail on unsupported
   correlated querysets without disabling independent ordinary sources.
   Preserve the distinction between INNER and LEFT on TiDB.
6. Run both version endpoints on PostgreSQL. Run ordinary SQLite cases and
   explicit unsupported-correlation error cases. Commit the verified slice.

**Feasibility checkpoint:** Tasks 1–6 must prove the intended API on Django 4.2
without global patches before expanding tuple/function support. If they expose
a requirement to globally alter Django or alter approved semantics, report the
specific counterexample and revisit that decision. Do not claim the whole
backport complete on this evidence alone.

## Task 7: Complete composite and tuple operation compatibility

**Files:**
- Modify: `django_derived_relation.py`
- Modify: `tests/derived_relations/test_composite_output.py`
- Modify: `tests/derived_relations/test_table_sources.py`

1. Select the upstream tuple lookup tests applicable to composite expression
   outputs. Port assertions for exact, membership, null tests, comparisons,
   ordering and negation. Include nullable components and empty RHS values.
2. On older Django provide package-local tuple expressions/lookups, scoped to
   the backported output field. Do not globally register or rebase native
   fields. Use native tuple primitives on newer versions where compatible.
3. Use backend tuple capabilities where they exist; for older Django establish
   supported SQL through execution. Do not advertise every tuple operation just
   because individual column references work.
4. Preserve concrete composite-primary-key support only on versions that
   already implement it. Do not add composite model fields to Django 4.2.
5. Test explicit projection order, renamed combined-source outputs if supported,
   unsupported whole-row selection, and ambiguous component paths.
6. Run endpoint suites and relevant backend tuple cases. Commit once supported
   operations and specific capability errors have execution evidence.

## Task 8: Add table functions through the same source path

**Files:**
- Modify: `django_derived_relation.py`
- Create: `tests/derived_relations/test_table_functions.py`
- Modify: `tests/derived_relations/models.py` only if a JSON fixture is needed

1. Add a fully typed PostgreSQL `GenerateSeries` test expression using native
   `Func`, `IntegerField()` output and `table_source=True`. Add a JSON table
   function exposing two named fields with physical column metadata.
2. Test lazy aliasing, scalar alias filters, selected scalar sources, composite
   components, correlated arguments expressed with `F`, and a legacy
   `set_returning=True` expression that has not opted in.
3. Generalise source normalisation to expression-like table sources. Preserve
   scalar metadata for scalar function outputs; do not wrap every output in a
   composite field. Use native expression compilation for vendor methods.
4. Add function-specific join rendering and alias/column declaration handling.
   Do not copy the experimental function branch wholesale or treat its tests
   as previously executed evidence.
5. Wrap both scalar and composite functions in `Join`. Test empty function
   output under explicit LEFT and required bare use, ON filters, OR, selection,
   cloning and count/exists. Follow native scalar alias naming for one-column
   table functions; queryset components continue using projected field names.
6. Execute PostgreSQL tests on version endpoints. Add SQLite `json_each`
   execution only with its actual implicit correlation semantics; ordinary
   SQLite support does not prove correlated queryset support.
7. Commit after function and queryset sources demonstrably share the same
   resolution/lifecycle machinery.

## Task 9: Verify backend integration and version compatibility

**Files:**
- Modify: `tox.ini` only as needed
- Modify: `.github/workflows/tests.yml` only as needed
- Modify: `.github/workflows/additional-backends.yml` only if present on base
- Modify: source-kind capability tests in the new test modules

1. Run the installed-wheel PostgreSQL/SQLite matrix, not only source-path runs:

```bash
python -m tox -e py311-django42,py311-django50,py311-django51,py311-django52,py313-django60,py313-django61,py313-djangomain
```

2. Keep the configured CI matrix for all compatible Python versions, including
   Python 3.10 and 3.11 on the supported Django versions. Do not construct an
   invalid Python/Django pair to satisfy an oversimplified cross product.
3. Execute MySQL/MariaDB and Oracle queryset-source cases already supported by
   the base. For merged third-party backend work, execute that existing matrix
   with the new API; record database, backend package, Django and join kind.
4. Verify inferred joins preserve empty/null output and parameter order. Check
   backend query merging workarounds still protect constants under LEFT joins.
5. Test table functions per backend/function combination, rather than carrying
   over queryset-source support claims. Snowflake remains excluded.
6. Keep SQL Server separate unless its work is merged, and do not pull other
   open PRs into this branch to make the matrix appear complete.

## Task 10: Migrate public examples and retire the old descriptor

**Files:**
- Modify: `django_derived_relation.py`
- Modify: `tests/derived_relations/tests.py`
- Modify: `tests/derived_relations/test_locking.py`
- Modify: `tests/gis_derived_relations/tests.py` if present on base
- Modify: `README.rst`
- Modify: `CHANGES.rst`

1. Migrate ordinary multi-column sources to bare `.alias()` registrations and
   explicit LEFT/ON examples to `Join`. Convert one-column row sources to an
   explicit wrapper; do not accidentally turn them into scalar subqueries.
2. Rewrite eager-count and multi-column `annotate()` tests to assert the
   documented new API behaviour. Preserve tests for name collisions against
   regular fields, scalar annotations and other source aliases.
3. Retain grouped-sales and geographic examples, demonstrating why their ON
   predicates and explicit LEFT joins matter. Verify spatial operations through
   the actual GIS model definitions and existing execution tests.
4. Document breaking changes: lazy registration, explicit projections,
   multi-column `annotate()` rejection, automatic correlation and preservation
   of scalar use. Explain which imports come from the backport.
5. Remove `DerivedRelation` only after the replacement tests cover its retained
   capabilities. Remove obsolete eager-registration internals; do not keep two
   separate compiler implementations for the same sources.
6. Replace the native-DerivedRelation import guard with an explicit native
   integration policy. Test partial core support, full core support where
   available, and the ongoing optional wrapper requirements. Do not fail merely
   because a native field class exists.
7. Keep release number/tag/publishing decisions separate. Put a concise entry
   under the project's existing unreleased changelog section.

## Task 11: Final verification and review

**Files:** all changed implementation, tests and documentation files.

1. Run the full suite after removal of the old descriptor, including source
   locking, custom queryset subclasses, dependency reuse, nested queries,
   cloning/relabel and applicable pickle cases. Distinguish any pre-existing
   evaluated-query pickle issue from regressions introduced here.
2. Run the repository's configured hooks and formatting/lint commands. With
   current tooling the explicit commands are:

```bash
python -m black --check django_derived_relation.py tests
python -m isort --check-only django_derived_relation.py tests
python -m flake8 django_derived_relation.py tests
python -m build
python -m twine check dist/*
```

3. Run any configured typing/complexity checks found at execution time. If none
   exist, review signatures and changed control flow manually and disclose the
   automation gap. Do not claim either automated check passed.
4. Use semantic diff/impact analysis where available. Check that opt-in usage
   remains scoped and normal Django querysets are unaffected.
5. Summarise execution evidence by API behaviour, Django/Python version and
   backend. Identify any unsupported operations specifically; do not advertise
   full DEP parity while an acceptance criterion is still failing.
6. Prepare a separately reviewable branch/PR only when requested, with a concise
   description of the public API and intentional behaviour changes. Follow the
   Git push guardrail and attach any created PR to this chat.
