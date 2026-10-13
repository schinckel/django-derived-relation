# Deferred Field Sources Implementation Plan

**Goal:** Preserve Django's only()/defer() field selection when a bare model queryset becomes a derived source.

**Architecture:** Read Django's get_select_mask() before normalizing the source to named columns. Select concrete model fields through that mask, including Django's implicit primary key, and retain selected annotations. This is column projection, not deferred model-instance hydration.

**Tech Stack:** Python 3.10–3.14, Django 5.2 onward, PostgreSQL, Django TestCase, tox.

---

## Contract

```python
source = Child.objects.only("amount")
# Named output includes id and amount, matching Django's implicit PK selection.
parents = Parent.objects.alias(rows=DerivedRelation(source)).values("rows__amount")
```

Derived output exposes only selected columns. Referring to rows__sequence above must fail. Filter/order/correlation expressions may still use nonprojected fields inside the source. Follow native call-order semantics: only() replaces the immediate-load set, defer() adds exclusions, defer(None) clears exclusions, and values() supplies its own projection.

select_related() model hydration stays unsupported. Do not flatten related-object columns or generate additional public column names as part of this change.

## Task 1: Establish the projection contract

Files: tests/derived_relations/tests.py.

1. Split the existing unsupported-source-shapes test so select_related() rejection remains independently tested.
2. Add typed failing tests for only("amount"), defer("sequence"), implicit PK inclusion, selected annotations, only()/defer() call ordering, and defer(None).
3. Verify excluded projected lookups fail and that an OuterRef filter using an unprojected parent_id still executes with lateral=True.
4. Add FK name/attname, invalid field, and explicit values() interaction tests. Compare the selected model fields against a native QuerySet baseline, without evaluating deferred model attributes.
5. Run `python -m tox -e py313-django52`; expect current deferred-loading rejection.

## Task 2: Use Django's mask

Files: django_derived_relation.py (DerivedRelation._projection).

1. On a cloned bare source, call get_select_mask() before set_values(). Iterate model._meta.concrete_fields in model order and choose fields included in the mask, or all concrete fields when the mask is empty. Project attname for concrete FK columns and append existing selected annotation names.
2. Let Django report invalid field selections. Do not reimplement field-path parsing or hardcode primary-key names. Review inherited models and composite PK handling; preserve the existing individually named column requirement.
3. Keep select_related() rejected. A nested defer/only path without hydrated related objects must follow native root-column selection, rather than inventing related output columns.
4. Clear deferred loading through Django's normal set_values() normalization on the clone. Check output_field metadata, source immutability, cloning and serialization.
5. Run the suite and commit the projection change with normal hooks.

## Task 3: Integration and documentation

Files: README.rst, CHANGES.rst, tests/derived_relations/tests.py.

1. If the combined-sources plan has landed, add a union test with matching deferred projections and an invalid width mismatch. Do not make that plan a dependency for ordinary deferred sources.
2. Explain that omitted fields cannot be lazily fetched through a derived column alias. Recommend explicit values() when names matter; retain the select_related() limitation.
3. Add an Unreleased entry and run the four-series tox check, lint/docs, and strict package checks. Require full CI before release.

Acceptance: root-model projections agree with native only()/defer() selection; excluded columns remain inaccessible; existing explicit projections and source filters are unchanged.

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
