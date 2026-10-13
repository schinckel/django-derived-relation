=====================================
Standalone derived relations backport
=====================================

``DerivedRelation`` joins the result of a queryset and exposes its named,
typed columns through ``alias__column`` lookups. Its source can group rows,
calculate columns, or select an ordered subset before joining them to an outer
query. With ``lateral=True``, the source can use ``OuterRef`` to refer to the
outer row.

This backport targets Django 5.2 and later releases before the core feature is
available. PostgreSQL is required. Future Django releases require regression
verification; the target range does not mean unreleased versions have been
tested.

Installation
============

Install from PyPI with ``python -m pip install django-derived-relation``. Import
``DerivedRelation`` and ``DerivedQuerySet`` from ``derived_relation_backport``.
The module is self-contained and does not require a patched Django installation.

The outer model's manager must use ``DerivedQuerySet``. Source querysets may
use ordinary Django managers. If you already have a custom queryset, make
``DerivedQuerySet`` its base and expose it with ``as_manager()`` or
``Manager.from_queryset()``. The examples below show the manager on each outer
model.

The descriptor alone cannot integrate with older Django's alias registration,
column selection, aggregation, and resolution paths. The module therefore
includes query and compiler subclasses attached to the opt-in queryset. It
does not monkey-patch Django classes or backend features globally.

Choosing between FilteredRelation and DerivedRelation
=====================================================

Use Django's ``FilteredRelation`` when you want an ON condition for a declared
model relationship. The joined source remains the related model's table; each
matching model row can produce a result row.

Use ``DerivedRelation`` when a queryset needs to define the joined rows and
columns. ``FilteredRelation`` cannot itself apply a projection, grouping,
ordering, or limit to that source. Ordering and slicing the outer queryset
sort and limit the complete result, not the related rows separately for every
outer row.

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
reports. There is no fixed invoice-to-rate foreign key::

    from django.db import models
    from django.db.models import F, OuterRef

    from derived_relation_backport import DerivedQuerySet, DerivedRelation


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
and warehouse; it is a query result, not a sales-line model instance::

    from datetime import date

    from django.db import models
    from django.db.models import F, Q, Sum

    from derived_relation_backport import DerivedQuerySet, DerivedRelation


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

Restrictions
============

* PostgreSQL is required; other backends raise ``NotSupportedError``.
* Register the relation with ``alias()`` on ``DerivedQuerySet``. Registering it
  with ``annotate()`` is unsupported.
* Updating or deleting a queryset containing a derived relation is unsupported.
* Relation and projected column names must be nonempty and cannot contain
  periods or ``__``. Rename traversed fields with
  ``values(name=F("related__field"))``.
* Sources using ``select_related()``, ``only()``, or ``defer()`` require an
  explicit values projection. Combined querysets and row-locking queries cannot
  be used as sources.
* Derived relations do not hydrate related model instances through
  ``select_related()``. Table-valued functions, raw SQL sources, right/full
  joins, and root FROM replacement are unsupported.
* Boolean combinations of outer querysets with ``&``, ``|``, or ``^`` are
  unsupported; combine filter conditions with ``Q`` instead.
* Explicit source and outer database aliases must match.

Moving to Django's core implementation
======================================

The backport raises ``ImportError`` at import time when Django exposes
``django.db.models.DerivedRelation``, so applications must migrate to the core
implementation. Once your installed Django provides ``DerivedRelation``,
import it from
``django.db.models``. For an application queryset with custom methods, change
its base from the backport's ``DerivedQuerySet`` to Django's ``QuerySet`` and
retain those methods and manager configuration. An outer model using only
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

The 52 regression tests cover ordinary and LATERAL joins, typed projections,
aggregation, null semantics, nesting, cloning, serialization, and validation.
The backport has been verified on Django 5.2.0, 5.2.17, 6.0.0, 6.0.9, 6.1.0,
and 6.1.2. CI tests each supported series and Django's development branch.
Support is intended to continue from Django 5.2 until the feature reaches core;
future versions require compatibility testing and may need adapter updates.

This implementation originated in an experimental Django core implementation.
It uses internal query/compiler APIs, with adapters scoped to DerivedQuerySet.
Code derived from Django retains Django's BSD license and attribution.

Publishing releases
===================

The ``release.yml`` workflow runs the regression matrix, verifies the version
tag, builds a wheel and source distribution, validates their metadata, and
publishes through PyPI Trusted Publishing. It runs when a GitHub release is
published. Routine pushes only run tests.

PyPI Trusted Publishing is configured for this repository’s ``release.yml``
workflow and the GitHub ``pypi`` environment. No PyPI API token is needed.

For each release, update ``project.version`` in ``pyproject.toml``, commit and
push the change, then publish a GitHub release with the corresponding
``v<version>`` tag (for example ``v0.1.0``). PyPI versions cannot be replaced;
use a new version for subsequent uploads. Download the built distributions
from the workflow artifacts if publication needs investigation.
