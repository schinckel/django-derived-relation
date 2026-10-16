# Django API compatibility plans

These are separate implementation tasks, not a release commitment.

1. [Combined sources](2026-10-13-combined-sources.md): first priority; preserve existing set-operation APIs and fix branch correlation detection.
2. [Annotate registration](2026-10-13-annotate-registration.md): small, independent change matching FilteredRelation registration.
3. [Deferred fields](2026-10-13-deferred-field-sources.md): preserve only()/defer() selection without model hydration.
4. [Locking sources](2026-10-13-locking-sources.md): independent transaction/locking work; optional compound-source integration after plan 1.
5. [Queryset combinations](2026-10-13-queryset-combinations.md): semantic checkpoint required before implementing join reuse.

Plans 1–4 can be implemented separately. Plan 5 should follow 1 and 2 and must settle the recorded cardinality questions first. New backends, raw sources, table functions, join types, root replacement and dependency sorting are outside these plans.

## DEP 24 replacement

The [design](2026-10-16-dep24-backport-design.md) and
[implementation plan](2026-10-16-dep24-backport.md) replace mandatory
`DerivedRelation` usage with bare sources and an optional `Join` wrapper.
The initial checkpoint is Django 5.2; Django 4.2 is assessed afterwards.
The scalar-subquery reuse experiment remains separate.
