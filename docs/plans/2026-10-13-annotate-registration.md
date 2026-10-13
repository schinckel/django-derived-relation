# Annotate Registration Implementation Plan

**Goal:** Allow DerivedRelation registration through annotate(), consistently with Django's FilteredRelation.

**Architecture:** Keep one registration path in DerivedQuerySetMixin._annotate. DerivedRelation remains a join descriptor, rather than becoming a scalar expression or a hydrated object attribute. The select flag continues to control ordinary annotations.

**Tech Stack:** Python 3.10–3.14, Django 5.2 onward, PostgreSQL, Django TestCase, tox.

---

## Contract

```python
parents = Parent.objects.annotate(
    newest=DerivedRelation(source, lateral=True),
    amount=F("newest__amount"),
)
```

Registration has the same join and cardinality effect through alias() and annotate(). Like FilteredRelation, the descriptor does not create a model attribute holding a related row. Ordinary expressions passed to annotate() are still selected; alias() expressions remain nonselected. Existing left/inner and ON semantics are unchanged.

## Task 1: Prove parity

Files: tests/derived_relations/tests.py.

1. Replace the test expecting annotate() rejection with a typed execution test comparing alias() and annotate() for the same source, explicit condition, and projected column selection.
2. Add tests for registration without selected source columns: count and duplicates must be identical. Check that no descriptor object is assigned as an instance attribute.
3. Add mixed keyword tests with an ordinary annotation before/after the descriptor, and dependent preceding derived aliases. Check selected annotation values as well as alias-only values. Preserve existing collision and unknown-column errors.
4. Run `python -m tox -e py313-django52` and confirm failures come from the current registration rejection.

## Task 2: Share registration

Files: django_derived_relation.py (DerivedQuerySetMixin._annotate, DerivedRelation.resolve_expression).

1. Remove the select-and-relations rejection. Keep descriptor handling in keyword order through add_to_query(); pass select unchanged for ordinary annotations.
2. Delete the now-unused relations comprehension if it serves no remaining purpose. Do not call Django's scalar-expression resolver for descriptors.
3. Keep positional unnamed descriptors rejected unless Django's existing default_alias contract can be satisfied without inventing an alias. Update the direct expression-use error to distinguish registration methods from unsupported scalar use.
4. Run mixed annotation, custom mixin, cloning, ordinary expression, and aggregate regressions. Inspect annotation_select_mask to ensure descriptor columns stay nonselected until requested.
5. Commit with normal hooks after the suite passes.

## Task 3: Docs and validation

Files: README.rst, CHANGES.rst.

1. Document both registration methods. Recommend alias() for readability when only registering a join, while showing annotate() with selected ordinary expressions.
2. Remove the alias-only restriction and add an Unreleased entry.
3. Run `python -m tox -e py313-django52,py313-django60,py313-django61,py313-djangomain`, normal lint/docs checks, and build/twine validation. Require the full CI matrix.

Acceptance: annotate() behaves like FilteredRelation registration without changing scalar annotations, source cardinality, or custom queryset delegation. No release tag in this task.

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
