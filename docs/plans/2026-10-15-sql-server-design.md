# SQL Server derived relations

Approved in the conversation on 2026-10-15.

Support ordinary and correlated derived relations through Microsoft's
`mssql-django` backend, with the existing `DerivedRelation` API. Keep this
work in a separate PR against main, before releasing 0.3.0.

Ordinary sources use the existing JOIN rendering. Correlated left joins use
OUTER APPLY; correlated inner joins use CROSS APPLY. APPLY has no ON clause,
so a condition filters a wrapper around the compiled source. The source's
ordering, slicing, grouping and projection take effect before this filter.
Reuse the public relation alias inside the wrapper to preserve resolved column
references without adding an alias that could shadow outer references.

Retain the backend's compiler, conversions, locking behaviour and native errors.
Do not patch Django or the backend globally. Explicit capability overrides and
cross-database guards continue to apply.

Run existing ordinary/LATERAL tests plus regressions for conditions after
slicing, constant false/true conditions, empty sources, parameter ordering,
dependent sources and alias scope. Use SQL Server service containers on x86-64
Linux CI, with Django 4.2 and 6.1 and compatible backend versions. Do not claim
local database execution on this ARM64 host.
