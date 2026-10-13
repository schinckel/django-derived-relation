# Queryset Combinations Implementation Plan

**Goal:** Support &, |, and ^ when derived joins can be combined with Django-compatible semantics, while retaining explicit errors for ambiguous cases.

**Architecture:** Preserve Django's QuerySet operator checks and Query.combine alias/where handling. Add only the derived-join compatibility and identity handling proven necessary by tests. This plan begins with an execution-path design checkpoint because join reuse differs for AND and OR and can change row multiplicity.

**Tech Stack:** Django 5.2 onward, PostgreSQL, Django TestCase, tox.

---

## Task 1: Decide semantics before implementation

Files: django_derived_relation.py (DerivedQueryMixin.combine, DerivedJoin.identity and relabeled_clone); Django's installed query.py/sql/query.py as read-only references.

1. Trace QuerySet.__and__/__or__/__xor__, _merge_sanity_check, sliced OR/XOR PK-subquery fallback, and Query.combine on Django 5.2, 6.0, 6.1 and main.
2. Record native behaviour for scalar annotation filters and FilteredRelation joins with zero/one/many related rows. AND currently recreates joins; OR attempts reuse. The adapter's fixed join types and retained unused joins make simply deleting its guard unsafe.
3. Define cases separately: branches cloned from one registered derived source; independently registered sources with the same alias; different alias sets; a derived branch combined with an ordinary queryset. Include whether predicates apply to one derived row or independently joined rows and how duplicates are counted.
4. Present a concrete fixture/output table for any choice that native Django examples do not settle. Obtain approval for that semantic choice before changing behaviour. Keep ambiguous alias/source definitions rejected; do not claim full general support from a narrow implementation.

## Task 2: Prove a safe initial subset

Files: tests/derived_relations/tests.py.

1. Once the checkpoint settles semantics, add typed failing execution tests for shared-source clones under AND, OR and XOR, with predicates on outer fields and projected fields. Use duplicate matching child rows to expose accidental join duplication.
2. Test independently registered aliases and mismatched source definitions against the agreed rejection/acceptance rules. Assert input queries remain unchanged.
3. Test unused-source count(), sum(), left unmatched rows, inner joins, explicit ON conditions, empty/full predicates, and values()/ordering selection. Preserve native distinct/model/slice compatibility checks.
4. Where the agreed meaning is a single shared join with boolean predicates, compare actual rows and counts against one queryset filtered with the equivalent Q tree. Where native Django uses distinct join copies, use a separately specified expected row bag; do not force Q equivalence.
5. Run tox and confirm the present adapter restriction fails these tests before writing implementation.

## Task 3: Join integration

Files: django_derived_relation.py (DerivedQueryMixin.combine, DerivedJoin identity/relabel helpers).

1. Preserve Query.combine for ordinary joins and native operator rules. Determine how to carry derived namespaces and projected annotations across the relabel map without overwriting a conflicting source.
2. If equivalent shared-source joins require stable identity, define it from registration lineage preserved by clone/relabel and pickle, not rendered SQL or mutable Query object identity. Do not deduplicate merely because aliases or SQL text happen to match. Implement only if the checkpoint proves it necessary.
3. Preserve fixed left/inner types and retained cardinality. Do not replace combinations with pk__in Subquery wholesale: that loses joined-row multiplicity and projected values.
4. Verify nested derived sources, OuterRef dependencies, parameter order, serializer round trips, and explicit source database restrictions. Leave unsupported compatibility cases with a useful error.
5. Run the agreed test set and commit only after the row-bag/cardinality evidence passes.

## Task 4: Document the exact boundary

Files: README.rst, CHANGES.rst.

1. Replace the blanket operator restriction with the implemented cases, including concrete incompatible-alias examples. Do not document ambiguous cases as supported.
2. Add an Unreleased entry and run the four-series tox check, lint/docs, strict distribution checks, and full CI.

Acceptance: supported combinations preserve their agreed native semantics and cardinality; incompatible combinations fail explicitly; cloning, selected columns, and source dependencies remain intact. This is the highest-risk plan and should follow combined sources and annotate registration.

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
