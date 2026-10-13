# Locking Sources Implementation Plan

**Goal:** Allow source select_for_update() where Django and PostgreSQL permit it, preserving transaction and lock-option behaviour.

**Architecture:** Remove the adapter's blanket source-lock rejection and delegate SQL generation, transaction checks, and supported options to Django's compiler. PostgreSQL remains responsible for rejecting SQL shapes that cannot lock identifiable rows. Verify locking effects with independent connections, rather than inspecting SQL alone.

**Tech Stack:** Django 5.2 onward, PostgreSQL, TransactionTestCase, psycopg, tox.

---

## Contract and limits

```python
with transaction.atomic():
    candidates = Child.objects.filter(parent_id=OuterRef("pk")).select_for_update()
    rows = Parent.objects.alias(
        locked=DerivedRelation(candidates.values("amount"), lateral=True),
    )
    list(rows.values("locked__amount"))
```

Locks belong to the underlying source rows, not to synthetic derived rows. Evaluation must occur within the required transaction. Do not promise locks on every candidate row: source predicates, slicing, optimizer behaviour, nowait and skip_locked affect which rows are visited. Outer-query select_for_update() is a distinct operation, not a replacement for source locking.

## Task 1: Real lock tests

Files: create tests/derived_relations/test_locking.py; reuse tests/derived_relations/models.py.

1. Add a PostgreSQL-only typed TransactionTestCase, without relying on TestCase's implicit transaction.
2. Test successful ordinary and LATERAL source locking inside atomic(), and Django's normal TransactionManagementError outside atomic(). Confirm list() actually executes the query.
3. Use a second psycopg connection to the runner-created test database, with the same database identity, to hold a known Child row lock. Assert source nowait=True reports lock contention; source skip_locked=True omits that row. Include release after commit/rollback and a LEFT join with no lockable match.
4. Derive connection settings from the test connection. Use context managers/finally cleanup and deterministic lock acquisition; no sleeps, production database names, global settings changes, or leaked connections. Set a bounded timeout on the test connection to prevent hangs.
5. Run `python -m tox -e py313-django52`; confirm initial failures come from the blanket source rejection.

## Task 2: Preserve native compilation

Files: django_derived_relation.py (DerivedRelation._projection, DerivedJoin.as_sql).

1. Remove source.select_for_update rejection. Verify source flags survive clone, resolve_expression(), and normal compiler dispatch.
2. Test nowait, skip_locked, of=("self",), and no_key=True using native backend feature guards. Keep transaction and database exceptions intact; do not convert them to success or silently drop a locking clause.
3. Cover a sliced source and known-empty source, including existing EmptyResultSet fallback. Compare lock behaviour with equivalent native subquery SQL.
4. Add grouped-source rejection at execution and combined-source rejection if that feature has landed. PostgreSQL's error belongs to the database; do not attempt a universal Python classifier for locking shapes.
5. Commit only once independent-connection tests pass without timeout flakes.

## Task 3: Docs and matrix

Files: README.rst, CHANGES.rst.

1. Replace blanket rejection with transaction requirements, option behaviour, and PostgreSQL's grouped/set-operation restrictions. Warn specifically that source locks are not outer-model locks.
2. Add an Unreleased entry. Run the four-series tox check sequentially and confirm the new TransactionTestCase is discovered by tests/runtests.py. Check SQLite skips and full CI.
3. Validate lint/docs and distributions. Do not tag a release in this task.

Acceptance: contention tests prove source-row locking; native transaction/option errors survive; no lock is silently removed, and no additional global adapter hooks are introduced.

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
