=====================================
Standalone derived relations backport
=====================================

``DerivedRelation`` joins the result of a queryset and exposes its named,
typed columns through ``alias__column`` lookups. Its source can group rows,
calculate columns, or select an ordered subset before joining them to an outer
query. With ``lateral=True``, the source can use ``OuterRef`` to refer to the
outer row.

Why this is needed
==================

Sometimes the thing you need to join is a query result: the latest exchange
rate for each invoice, or sales totals grouped by product and warehouse.
Django's relationship joins start from model tables. ``FilteredRelation``
restricts which related rows match by adding an ON condition. It can even
select the latest related row using a scalar ``Subquery`` in that condition.
However, it cannot directly replace the joined table with a queryset that
applies ``ORDER BY ... LIMIT``, grouping, or a custom projection.

A scalar ``Subquery`` works when you need one column. If you need several
columns from the same selected row, you often end up repeating that subquery.
``DerivedRelation`` lets you define the source once, join its result, and use
its columns in the outer query. LATERAL joins allow that source to depend on
each outer row; ordinary derived joins handle independent results.

This gives the ORM another way to express the query. Whether it is faster
depends on the data and execution plan.

This backport targets Django 4.2 and later releases before the core feature is
available. Python 3.10 or later and PostgreSQL are required. Python 3.11 is
supported with Django 4.2 through 5.2.

Installation
============

Install from PyPI with ``python -m pip install django-derived-relation``.

Import ``DerivedRelation`` and ``DerivedQuerySet`` from ``django_derived_relation``.

The outer model's manager needs the derived-query adapter. Use
``DerivedQuerySet.as_manager()`` for a new queryset. Source querysets can use
ordinary Django managers.

If you already have a custom queryset, add ``DerivedQuerySetMixin`` before its
existing base so the adapter's methods run before delegating through ``super()``:

.. code-block:: python

    from django_derived_relation import DerivedQuerySetMixin


    class InvoiceQuerySet(DerivedQuerySetMixin, ExistingInvoiceQuerySet):
        pass


    class Invoice(models.Model):
        objects = InvoiceQuerySet.as_manager()

``Manager.from_queryset(InvoiceQuerySet)`` also works. Existing queryset
overrides must cooperate with ``super()``. ``DerivedQuerySet`` is just the
convenience class combining the mixin with Django's ``QuerySet``.

The descriptor alone cannot integrate with older Django's alias registration,
column selection, aggregation, and resolution paths. The module therefore
includes query and compiler subclasses attached to the opt-in queryset. It
does not monkey-patch Django classes or backend features globally. If/when you
upgrade to a version of django that includes ``DerivedRelation``, you must
uninstall this package and use the django supplied one.

Registering a derived relation
==============================

Use ``alias()`` when registering the join on its own. ``annotate()`` also
accepts ``DerivedRelation``, like Django's ``FilteredRelation``, and lets you
select ordinary expressions alongside it:

.. code-block:: python

    source = Child.objects.filter(parent_id=OuterRef("pk")).values("amount")
    parents = Parent.objects.annotate(
        rows=DerivedRelation(source, lateral=True),
        amount=F("rows__amount"),
    )

Each instance has an ``amount`` attribute. The descriptor does not create a
``rows`` attribute or hydrate a related model instance. Registering the join
alone retains its effect on row counts through either method; ordinary
``alias()`` expressions remain unselected and ordinary ``annotate()``
expressions are selected.

Choosing between FilteredRelation and DerivedRelation
=====================================================

Use Django's ``FilteredRelation`` when you want an ON condition for a declared
model relationship. The joined source remains the related model's table; each
matching model row can produce a result row.

Use ``DerivedRelation`` when a queryset needs to define the joined rows and
columns. The source can use ``values()`` to select columns,
``values(...).annotate(...)`` to group and calculate aggregates, and
``order_by(...)[:1]`` to select its first row. ``FilteredRelation`` does not
accept such a queryset as its joined source; it adds an ON condition to a
model-table join. Ordering and slicing the outer queryset sort and limit the
complete result, not the related rows separately for every outer row.

Some results can be obtained either way. For example, a scalar ``Subquery``
in a ``FilteredRelation`` condition can choose the newest child's primary key,
and the relationship join can retrieve that child's fields. A LATERAL derived
query expresses the ordered selection directly and can expose several columns
from it. For a single value, a scalar ``Subquery`` may be sufficient.

The following examples show two other reasons to use a derived source: a match
computed without a declared relationship, and summaries produced by grouping.
Neither form guarantees a faster execution plan.

Historical rates without a declared relationship
================================================

An invoice stores its currency and issue date. A separate feed supplies
historical rates identified by currency codes. The reporting currency is chosen
by the caller, so the appropriate rate for an invoice can change between
reports. There is no fixed invoice-to-rate foreign key:

.. code-block:: python

    from django.db import models
    from django.db.models import F, OuterRef

    from django_derived_relation import DerivedQuerySet, DerivedRelation


    class Invoice(models.Model):
        objects = DerivedQuerySet.as_manager()

        currency = models.CharField(max_length=3)
        issued_at = models.DateTimeField()
        amount = models.DecimalField(max_digits=12, decimal_places=2)


    class ExchangeRate(models.Model):
        base_currency = models.CharField(max_length=3)
        quote_currency = models.CharField(max_length=3)
        effective_at = models.DateTimeField()
        rate = models.DecimalField(max_digits=18, decimal_places=8)


    reporting_currency = "USD"
    rates = (
        ExchangeRate.objects.filter(
            base_currency=OuterRef("currency"),
            quote_currency=reporting_currency,
            effective_at__lte=OuterRef("issued_at"),
        )
        .order_by("-effective_at", "-pk")
        .values("rate", "effective_at")[:1]
    )
    invoices = Invoice.objects.alias(
        conversion=DerivedRelation(rates, lateral=True),
    ).annotate(
        reporting_amount=F("amount") * F("conversion__rate"),
        rate_effective_at=F("conversion__effective_at"),
    )

The source selects the latest rate effective on or before each invoice's issue
date. Ordering by primary key breaks ties in effective dates. Both projected
columns come from that selected rate. ``FilteredRelation`` requires a declared
relationship path; these models store scalar codes and have no such path.

The default left join preserves invoices without suitable rates and returns
``None`` for their calculated amount and rate timestamp. Use
``join_type="inner"`` to exclude invoices without a match. Only ``Invoice``
needs the backport manager here; ``ExchangeRate`` can use its ordinary manager.

Joining grouped results
=======================

Suppose an inventory report joins a stock snapshot with sales totals for a
caller-selected period. Each total combines several sales lines for one product
and warehouse; it is a query result, not a sales-line model instance:

.. code-block:: python

    from datetime import date

    from django.db import models
    from django.db.models import F, Q, Sum

    from django_derived_relation import DerivedQuerySet, DerivedRelation


    class StockSnapshot(models.Model):
        objects = DerivedQuerySet.as_manager()

        product_code = models.CharField(max_length=40)
        warehouse_code = models.CharField(max_length=40)
        as_of = models.DateField()
        quantity = models.IntegerField()


    class SalesLine(models.Model):
        product_code = models.CharField(max_length=40)
        warehouse_code = models.CharField(max_length=40)
        sold_on = models.DateField()
        quantity = models.IntegerField()
        line_total = models.DecimalField(max_digits=12, decimal_places=2)


    period_start = date(2026, 10, 5)
    period_end = date(2026, 10, 12)
    totals = (
        SalesLine.objects.filter(sold_on__gte=period_start, sold_on__lt=period_end)
        .order_by()
        .values("product_code", "warehouse_code")
        .annotate(units=Sum("quantity"), revenue=Sum("line_total"))
    )
    snapshots = StockSnapshot.objects.filter(as_of=period_end).alias(
        sales=DerivedRelation(
            totals,
            condition=Q(
                sales__product_code=F("product_code"),
                sales__warehouse_code=F("warehouse_code"),
            ),
        ),
    )
    report = snapshots.values(
        "product_code",
        "warehouse_code",
        "quantity",
        "sales__units",
        "sales__revenue",
    )

Use ``values(...).annotate(...)`` to keep a grouped queryset as the source.
``aggregate()`` returns a dictionary and cannot be used here. Clearing ordering
prevents default ordering from changing the grouping.

There is no persisted summary model or row for a foreign key to reference.
Relationships to product or warehouse models would not define the grouped
query result either. ``FilteredRelation`` adds conditions to a model-table
join; it cannot replace that table with this grouping and projection.

This source is independent of the outer snapshot and therefore uses the default
``lateral=False``. The ON condition matches its two projected grouping keys.
Grouping by those keys produces at most one matching source row per snapshot.
Snapshots without sales remain present with ``None`` for the totals; use
``Coalesce`` if your report needs zero. Later joins can repeat those summary
rows, so aggregating their values over an expanded result can count a summary
more than once.

Geographic matches without a foreign key
========================================

An address might need its nearest service centre and the regions that cover
it. Neither match needs a stored foreign key. These examples require GeoDjango,
PostGIS, and ``django.contrib.gis.db.backends.postgis``.

.. code-block:: python

    from django.contrib.gis.db import models
    from django.contrib.gis.db.models.functions import Distance
    from django.db.models import ExpressionWrapper, F, OuterRef, Q

    from django_derived_relation import DerivedQuerySet, DerivedRelation


    class Address(models.Model):
        objects = DerivedQuerySet.as_manager()

        location = models.PointField(srid=28354)


    class ServiceCentre(models.Model):
        name = models.CharField(max_length=100)
        active = models.BooleanField(default=True)
        location = models.PointField(srid=28354)


    class Region(models.Model):
        name = models.CharField(max_length=100)
        active = models.BooleanField(default=True)
        boundary = models.MultiPolygonField(srid=28354)

These models use a projected coordinate system suitable for the Adelaide
area, with distances in metres. Choose a coordinate system appropriate for
your data; longitude/latitude geometry coordinates are not metres.

Nearest active service centre
-----------------------------

.. code-block:: python

    address_location = ExpressionWrapper(
        OuterRef("location"),
        output_field=models.PointField(srid=28354),
    )
    nearest = (
        ServiceCentre.objects.filter(
            active=True,
            location__dwithin=(OuterRef("location"), 5000),
        )
        .annotate(distance=Distance("location", address_location))
        .order_by("distance", "pk")
        .values("id", "name", "distance")[:1]
    )
    addresses = Address.objects.alias(
        nearest=DerivedRelation(nearest, lateral=True),
    ).annotate(
        service_centre_id=F("nearest__id"),
        service_centre_name=F("nearest__name"),
        service_centre_distance=F("nearest__distance"),
    )

The source finds active centres within five kilometres, then chooses the
nearest for each address. The primary key breaks distance ties. All three
columns come from that selected centre. The wrapper supplies the geometry
type needed by ``Distance`` while the outer reference is unresolved.

The left join retains addresses without a nearby centre, with ``None`` for
the three annotations. ``Distance`` returns GeoDjango distance objects; use
``address.service_centre_distance.m`` to read metres when it is present.
This measures straight-line distance, not travel time.

Regions covering each address
-----------------------------

.. code-block:: python

    regions = Region.objects.filter(active=True).values("id", "name", "boundary")
    matches = Address.objects.alias(
        region=DerivedRelation(
            regions,
            condition=Q(region__boundary__covers=F("location")),
        ),
    ).values("id", "region__id", "region__name")

This source is independent of the address, so it uses an ordinary derived
join. The ON condition performs the spatial match using its projected
boundary. ``covers`` includes points on the polygon boundary. An address
covered by overlapping regions appears once per matching region; an address
without a match still appears with ``None`` for the region columns.

For a simple model-table spatial join, a derived source may not be necessary.
It becomes useful when the region queryset also needs calculated columns or
other source operations. See GeoDjango's
`spatial lookups <https://docs.djangoproject.com/en/5.2/ref/contrib/gis/geoquerysets/>`_
and `distance functions <https://docs.djangoproject.com/en/5.2/ref/contrib/gis/functions/>`_.

ON conditions and result rows
=============================

``DerivedRelation(queryset, condition=None, join_type="left", lateral=False)``
accepts a ``Q`` tree or boolean expression as its condition. It preserves that
condition for both ordinary and LATERAL joins. No condition means ``ON TRUE``.

A condition can use the outer model's fields, this source's columns, or columns
from a previously registered derived relation. Register dependencies first;
forward and circular references are unsupported. Conditions can combine ``Q``
objects using ``&`` and ``|``.

An ON condition on a left join retains outer rows when no source row satisfies
it. A subsequent ``filter()`` on a projected column can remove those rows. A
filter inside an ordered, sliced source changes which rows are eligible before
the slice; an ON condition tests the rows chosen by that source.

Several matching source rows multiply each outer row. Registration retains
the join even when none of its columns are selected, so ``count()`` and
aggregates still reflect that multiplication. The alias does not make the join
optional, and joined rows are not collected into a related-object list.

An ordinary source with no condition joins every source row to each outer row.
The source's model does not supply automatic joining keys. Use a condition to
match projected keys, as in the sales-totals example.

Source columns can be used in ``F`` expressions, filters, ``values()``,
``values_list()``, ordering, and aggregates. An explicit projection is
recommended, although bare model querysets are accepted. Registration clones
the source without evaluating it. It does not change scalar ``Subquery``
semantics.

Bare model sources preserve Django's ``only()`` and ``defer()`` selection,
including implicitly selected primary-key columns and selected annotations.
For example, ``Child.objects.only("amount")`` exposes ``rows__id`` and
``rows__amount`` when registered as ``rows``. Filters, ordering, and LATERAL
correlation can still use fields omitted from that projection. Native call-order
rules apply: ``only()`` replaces the selected field set, ``defer()`` adds
exclusions, and ``defer(None)`` clears them. A subsequent ``values()`` supplies
its own explicit projection.

This selects columns from the root model; it does not hydrate deferred model
instances or joined related objects. An omitted column such as
``rows__sequence`` raises ``FieldError`` when referenced and cannot be lazily
fetched through the derived alias. Foreign keys use their attnames, such as
``parent_id``, and composite primary keys expose their individual columns on
Django 5.2 and later.
Use explicit ``values()`` when output names matter.

Locking source rows
===================

Sources can use ``select_for_update()`` where Django and PostgreSQL support
row locking. Evaluate the outer queryset inside ``transaction.atomic()``;
registration alone does not execute SQL or acquire locks.

.. code-block:: python

    from django.db import transaction

    with transaction.atomic():
        candidates = (
            Child.objects.filter(parent_id=OuterRef("pk"))
            .order_by("sequence")
            .select_for_update(skip_locked=True)
        )
        rows = Parent.objects.alias(
            locked=DerivedRelation(
                candidates.values("id", "amount"), lateral=True
            )
        )
        locked_rows = list(rows.values("pk", "locked__id", "locked__amount"))

Source locks apply to the underlying source rows. They do not lock the outer
model merely because it joins the source. Calling ``select_for_update()`` on
the outer queryset is a separate operation, subject to Django's and
PostgreSQL's normal restrictions on locking joined tables.

A locking source routes the outer queryset through Django's write router,
including when the lock is inside a nested derived source. Explicit outer
``using()`` keeps its normal precedence; an explicit source database must
still match the outer query's chosen database.

Django compiles the source's ``nowait``, ``skip_locked``, ``of``, and ``no_key``
options normally. ``nowait=True`` reports contention as a database error;
``skip_locked=True`` omits locked source rows. A LEFT join still returns an
outer row with NULL source columns when no source row can be locked. For
``of=("self",)``, include at least one model field in the source's
``values()`` projection. Django requires a selected field from each model
named in ``of`` and validates which related field paths the query supports.

Locks last until the transaction commits or rolls back. Predicates, slicing,
query execution, and lock options determine which rows PostgreSQL visits;
do not assume that every candidate row is locked. Known-empty sources preserve
LEFT join rows through an empty SQL source and still require a transaction.
Transaction checks, unsupported-option errors, and PostgreSQL errors propagate
normally. PostgreSQL rejects locking grouped, aggregate, distinct, and
set-operation results; adding a locking clause does not make those shapes
lockable. Combined sources remain unsupported by this adapter.

Restrictions
============

Currently unsupported
---------------------

These are implementation limits, not claims that the SQL is impossible.
Listing them here does not promise future support.

* Backends other than PostgreSQL.
* Combined sources using ``union()``, ``intersection()``, or ``difference()``.
  A union of compatible ``values()`` projections is a valid derived-table
  shape, but the adapter does not yet support it.
* Updating or deleting an outer queryset containing a derived relation, or
  combining outer querysets with ``&``, ``|``, or ``^``. Use ``Q`` trees for
  combined filter conditions.
* Hydrating joined model instances through ``select_related()``, table-valued
  functions, raw SQL sources, right/full joins, and replacing the root FROM
  source.
* Forward references to derived aliases. Register dependencies first.

Registration requires ``DerivedQuerySet`` or ``DerivedQuerySetMixin``.
Sources using ``select_related()`` need an explicit values projection.
Relation and column names must be nonempty,
cannot contain periods or ``__``, and must not create ambiguous aliases.
Rename traversed columns with ``values(name=F("related__field"))``. Explicit
source and outer database aliases must match, even if two aliases happen to
point to the same database.

Will never work
---------------

These require a different query or API, rather than lifting an adapter check.

* An ordinary, non-LATERAL source referring to another FROM item in the same
  query. That correlation requires ``lateral=True``.
* Circular dependencies between derived sources. LATERAL sources can depend
  on preceding sources, not on each other recursively.
* Joining across independent database connections in one SQL statement.
  Tables exposed to one connection through database facilities are a different
  case; Django cannot merge the results of two connections into this join.
* Referencing a column the source does not project. Include it in ``values()``
  or give the expression a projected name first.
* Passing the result of ``aggregate()``, an evaluated list, or a dictionary
  as the source. This API needs a queryset that can compile into SQL; use
  ``values(...).annotate(...)`` for grouped results.

PostgreSQL also rejects row locking for shapes such as grouped and combined
results. See its
`SELECT documentation <https://www.postgresql.org/docs/current/sql-select.html>`_.

Moving to Django's core implementation
======================================

The backport raises ``ImportError`` at import time when Django exposes
``django.db.models.DerivedRelation``, so applications must migrate to the core
implementation. Once your installed Django provides ``DerivedRelation``,
import it from
``django.db.models``. For an application queryset with custom methods, change
its base from the backport's ``DerivedQuerySet`` to Django's ``QuerySet`` and
retain those methods and manager configuration. If you used
``DerivedQuerySetMixin``, remove it from the bases of your custom queryset.
An outer model using only
``DerivedQuerySet.as_manager()`` can return to its ordinary Django manager.
The relation definitions and ``alias__column`` references use the same public
API.

Development and verification
============================

Install development dependencies with ``python -m pip install -e '.[test]'``.
Run ``python tests/runtests.py`` against a local PostgreSQL server. Connection
settings use the standard ``PGDATABASE``, ``PGUSER``, ``PGPASSWORD``, ``PGHOST``,
and ``PGPORT`` environment variables. The database user needs permission to
create the isolated test database. SQLite validation can be run with
``python tests/runtests.py --sqlite``; execution tests require PostgreSQL.

The regression tests cover ordinary and LATERAL joins, typed and deferred projections,
aggregation, null semantics, nesting, cloning, serialization, and validation.
Independent PostgreSQL connections verify source lock contention, lock options,
and transaction release; database-free tests check write routing.
CI runs 24 combinations: Django 4.2 and 5.0 on Python 3.10–3.12; Django 5.1 on
Python 3.10–3.13; Django 5.2 on Python 3.10–3.14; and Django 6.0, 6.1, and
development main on Python 3.12–3.14. The same matrix runs weekly to catch
upstream changes. Composite-key tests run only where Django provides them.
Each environment installs the built wheel and runs PostgreSQL and SQLite tests.

Run the matrix locally with ``python -m pip install tox`` followed by
``tox``. All listed Python interpreters must be installed. To run one
combination, use ``tox -e py311-django42``. PostgreSQL connection settings are
passed through from the ``PG*`` environment variables. Tox runs environments
sequentially; avoid ``tox -p`` because the runner uses a shared test database.
Support is intended to continue from Django 4.2 until the feature reaches core;
future versions require compatibility testing and may need adapter updates.

This implementation originated in an experimental Django core implementation.
It uses internal query/compiler APIs, with adapters scoped to DerivedQuerySet.
This project is licensed by Matthew Schinckel under the BSD 3-Clause license.
The copyright notice for code derived from Django is retained in ``LICENSE``.

Publishing releases
===================

The ``release.yml`` workflow runs the regression matrix, verifies the version
tag, builds a wheel and source distribution, validates their metadata, and
publishes through PyPI Trusted Publishing when a ``v<version>`` tag is pushed.
Routine branch pushes only run tests.

PyPI Trusted Publishing is configured for this repository’s ``release.yml``
workflow and the GitHub ``pypi`` environment. No PyPI API token is needed.

Record user-visible changes under ``Unreleased`` in ``CHANGES.rst`` as they
are made. Before releasing, move those entries under a heading such as
``0.1.0 (2026-10-13)``, using the actual release date, and leave an empty
``Unreleased`` section for the next changes.

Update ``project.version`` in ``pyproject.toml`` in the same commit. Version
bumps are manual for now; there is only one version value to change. Check the
release metadata before committing and pushing:

.. code-block:: shell

    RELEASE_TAG=v0.1.0 python tools/check_release.py

The check requires a matching version tag and a dated changelog heading.
It also runs before publication. Once that passes, commit and push the changes,
then create and push the corresponding tag:

.. code-block:: shell

    git tag v0.1.0
    git push origin v0.1.0

The tag must match the package version. PyPI versions cannot be replaced;
use a new version for subsequent uploads. Download the built distributions
from the workflow artifacts if publication needs investigation.
