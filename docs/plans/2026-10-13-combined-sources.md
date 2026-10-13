# Combined Sources Implementation Plan

**Goal:** Accept Django's existing union(), intersection(), and difference() querysets as derived sources.

**Architecture:** Keep Django's compound-query compiler and expression resolution. Extend the adapter's projection and external-reference handling across every branch instead of translating set operations into new SQL constructs.

**Tech Stack:** Python 3.10–3.14, Django 5.2 onward, PostgreSQL, unittest/Django TestCase, tox.

---

## Contract and evidence

```python
source = Child.objects.values(x=F("amount")).union(
    Parent.objects.values(x=F("minimum")),
)
parents = Parent.objects.alias(rows=DerivedRelation(source)).values("rows__x")
```

Output names and ORM field metadata follow the first queryset, as in Django's existing combined querysets. Corresponding columns must meet the database's set-operation requirements. Do not require identical field names on subsequent branches, invent a new projection declaration, or infer Python output types from SQL strings. Use Cast when callers need explicit common types.

A read-only compilation probe produced valid-looking SQL for the example after removing the guard. It also produced invalid non-LATERAL correlation when only the second branch used OuterRef: get_external_cols() currently omits combined branches. Compilation alone is not execution evidence.

## Task 1: Set-operation execution

Files: django_derived_relation.py (DerivedRelation._projection, DerivedQueryMixin), tests/derived_relations/tests.py.

1. Write typed failing tests using the existing Parent/Child fixtures: union DISTINCT, union ALL with duplicate rows, intersection, difference, and explicitly projected querysets from different models. Assert actual values and unused-join count, including unmatched outer rows.
2. Run `python -m tox -e py313-django52`; confirm the current NotSupportedError is the failure.
3. Remove the combinator rejection only after projection metadata follows Django's first-query convention. Preserve selected column order and aliases through Django's compiler. If bare combined sources need more handling, implement it through Django's selected fields/select mask; do not silently reshape operands.
4. Add ordering/slicing on the compound result, compatible renamed projections on subsequent branches, an empty first/later/all branch, and source-immutability tests. Check that known-empty aggregate handling does not manufacture a union row.
5. Run the same suite, then commit the coherent set-operation change with normal hooks.

## Task 2: Correlation and nesting

1. Add failing tests for OuterRef in only the second branch and in a nested compound branch. Ordinary joins must reject these; lateral=True must execute per outer row. Test a branch containing an existing DerivedRelation and a union nested inside a scalar Subquery.
2. Adapt combined branches to DerivedQueryMixin before resolution where required. Collect external columns recursively from combined_queries as well as ordinary expressions and DerivedJoin sources. Resolve/relabel each branch using Django's normal clone path.
3. Verify alias-prefix collisions, parameter ordering, unchanged original operands, and pickling. Do not evaluate sources to inspect their schema.
4. Re-run the PostgreSQL suite on each Django series and commit the correlation fix separately if it is independently reviewable.

## Task 3: Documentation and release

Files: README.rst, CHANGES.rst.

1. Replace the combined-source restriction with a union example and Django's first-query naming/type convention. Keep actual backend ordering/locking restrictions.
2. Add one user-facing Unreleased entry; do not bump or tag a release as part of implementation.
3. Run `python -m tox -e py313-django52,py313-django60,py313-django61,py313-djangomain` sequentially, lint/docs checks, and strict distribution checks. Require the existing 14-combination CI matrix before release.

Acceptance: all three set operators and union ALL execute correctly; later-branch correlation cannot bypass lateral validation; inputs remain unchanged. No global patches or new public API.

## Execution requirements

Work in the existing project checkout; preserve unrelated edits. Use @superpowers:test-driven-development and @superpowers:verification-before-completion. Add full type annotations to new Python functions/helpers. Run normal commit hooks without bypasses. No type checker or cognitive-complexity analyzer is currently configured: inspect changed functions manually and report that automated verification is unavailable.

Run tests sequentially against an isolated PostgreSQL test database; the runner's fixed database name makes parallel local tox runs unsafe. The standard checks are:

```sh
python -m flake8 .
python -m black --check .
python -m isort --check-only .
python -m sphinxlint README.rst CHANGES.rst
python -m unittest discover -s tests -p test_release.py
python -m build
python -m twine check --strict dist/*
git diff --check
```

Do not execute these plans, publish, bump versions, or move tags merely because the plan exists. The Django core prototype in the sibling checkout is outside these implementation tasks; upstream porting needs its own request.
