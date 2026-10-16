# Package module design

Status: Approved on 2026-10-16.

Replace the single `django_derived_relation.py` file with a package of the same
name. Preserve the documented imports of `CompositeField`, `Join`,
`DerivedQuerySet`, and `DerivedQuerySetMixin`. This is a structural refactor:
query behaviour, supported versions, and backend capabilities stay unchanged.

## Boundaries

- `__init__.py`: public exports and compatibility imports needed by pickles.
- `fields.py`: the metadata-only `CompositeField`.
- `lookups.py`: tuple lookups, including the older-Django fallback.
- `expressions.py`: typed projected columns and lazy table-source expressions.
- `joins.py`: explicit `Join` controls, query/function join nodes and backend SQL.
- `compiler.py`: compiler adaptation and its cached class factory.
- `query.py`: source interpretation, registration, resolution, query mixin and
  cached class factory.
- `queryset.py`: opt-in queryset mixin and convenience class.
- `compat.py`: projection inspection and version-dependent column types.

Dependencies flow from queryset to query, then compiler/joins/expressions, then
fields/lookups/compat. Source interpretation stays in query.py because it needs
the custom Query factory. This avoids circular imports and deferred imports.
SQL type aliases stay beside their compiler consumers.

The alternative of three broad modules leaves source registration, SQL
compilation and compatibility mixed together. The approved split separates
existing responsibilities without adding an abstraction layer.

## Compatibility and verification

Use explicit setuptools package discovery. Preserve the public deconstruction
path `django_derived_relation.CompositeField`. Adapt the import compatibility
test to reload a package instead of executing a module file directly. Keep
serialization round trips, import order independence, and installed-wheel
behaviour covered by execution rather than file-placement tests.

Run the existing PostgreSQL and SQLite suites across the available Django
environments, release tests, formatting/linting, build and metadata validation.
Exercise the built wheel from outside the checkout. CI supplies the remaining
Python/backend combinations. No type or complexity analyser is configured;
review changed functions manually and state that limitation.
