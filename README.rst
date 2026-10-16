========================
Django table expressions
========================

Use querysets and table functions as named, typed sources in another query.
The public queryset syntax follows `accepted Django DEP 24
<https://github.com/django/deps/blob/main/accepted/0024-table-value-expression.rst>`_.
An optional ``Join`` wrapper adds explicit LEFT/INNER joins and ON conditions.
Correlation is inferred; there is no ``lateral=True`` switch.

This API and project name are under development. No release has been published
as ``django-table-expressions``; version 0.2.0 was published as
``django-derived-relation`` with the earlier API.
The adapter uses Django's private query/compiler APIs and requires compatibility
tests as Django evolves. It supports Python 3.10+ with compatible Django
versions from 4.2 onward. Python 3.11 works with Django 4.2 through 5.2.

Why this is needed
==================

Sometimes the thing you need to join is a query result: the latest exchange
rate for each invoice, or sales totals grouped by product and warehouse.
``FilteredRelation`` adds an ON condition to a declared model relationship.
It can choose a particular related row using a scalar subquery in that
condition, but it cannot replace the joined table with an ordered, sliced,
grouped or calculated queryset result.

A scalar ``Subquery`` works for one value. To select several columns from the
same chosen row, a derived source lets you define the query once and reference
its columns. This is another way to express the query; performance depends on
the data and execution plan.

Installation and managers
=========================

After the first release under the new name, install from PyPI:

.. code-block:: shell

    python -m pip install django-table-expressions

The outer queryset needs the opt-in adapter. Source querysets can use ordinary
Django managers. Existing custom querysets can add ``DerivedQuerySetMixin``
before their current base:

.. code-block:: python

    from django_table_expressions import DerivedQuerySet, DerivedQuerySetMixin


    class InvoiceQuerySet(DerivedQuerySetMixin, ExistingInvoiceQuerySet):
        pass


    class Invoice(models.Model):
        objects = InvoiceQuerySet.as_manager()

``DerivedQuerySet.as_manager()`` and ``Manager.from_queryset()`` also work.
Overrides must cooperate with ``super()``. No Django classes or global lookup
registries are patched. Import backported types from ``django_table_expressions``.

Bare queryset sources
=====================

Register a multi-column source with ``alias()`` and select its components:

.. code-block:: python

    latest = (
        Child.objects.filter(parent_id=OuterRef("pk"))
        .order_by("-sequence", "-pk")
        .values("amount", "sequence")[:1]
    )
    parents = Parent.objects.alias(latest=latest).values(
        "pk", "latest__amount", "latest__sequence",
    )

This correlated source uses LATERAL. Selecting its columns makes it required:
parents without a matching child produce no row. A bare multi-column source
requires an explicit projection. Bare single-column querysets and scalar
``Subquery`` expressions retain their existing scalar behaviour.

Sources are lazy. Merely registering ``alias(latest=latest)`` does not change
``count()`` or multiply rows. Filters, selection and other references bring the
source into the query. Repeated references reuse its join. A source referenced
only on an optional side of an OR can use LEFT OUTER; selecting its columns
makes it required, as specified by the DEP.

Multi-column sources cannot be selected wholesale with ``annotate()`` or
``values("latest")``. Select their components, or annotate individual ``F``
references. Component lookups and transforms retain their field types.
Complete aliases can be used in tuple comparisons and tuple ordering.

Explicit joins
==============

Use ``Join`` when unmatched rows or an ON predicate matter:

.. code-block:: python

    from django_table_expressions import Join

    parents = Parent.objects.alias(
        latest=Join(latest, join_type="left"),
    ).annotate(amount=F("latest__amount"))

This retains parents without children, with ``None`` for ``amount``.
``Join(source, *, join_type="left" | "inner", condition=None)`` is a backport
extension; these explicit controls are not specified by the accepted DEP.
``join_type`` must be supplied. It works with querysets and opted-in functions.

``condition`` is a ``Q`` tree or boolean expression compiled in ON. A condition
inside an ordered, sliced source changes which rows reach LIMIT. An ON condition
tests the rows already chosen by that source. A subsequent outer ``filter()``
is a WHERE condition and can remove unmatched rows from a LEFT join.

Explicit joins are lazy too. Use a column reference when the join must affect
row counts. Independent sources are not matched by a foreign key automatically;
without a condition, each outer row joins every source row. ``Join`` also makes
a one-column queryset a table source. For bare
model querysets inside ``Join``, native ``only()``/``defer()`` root-model
selection is preserved, including implicit primary keys and selected
annotations. Omitted columns cannot be fetched through the alias later.


Historical rates without a declared relationship
================================================

An invoice stores its currency and issue date. A separate feed supplies
historical rates identified by currency codes. The reporting currency is chosen
by the caller, so the appropriate rate for an invoice can change between
reports. There is no fixed invoice-to-rate foreign key:

.. code-block:: python

    from django.db import models
    from django.db.models import F, OuterRef

    from django_table_expressions import DerivedQuerySet, Join


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
        conversion=Join(rates, join_type="left"),
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

    from django_table_expressions import DerivedQuerySet, Join


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
        sales=Join(totals, condition=Q(
                sales__product_code=F("product_code"),
                sales__warehouse_code=F("warehouse_code"),
            ), join_type="left"),
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

This source is independent of the outer snapshot and therefore uses an ordinary derived join. The ON condition matches its two projected grouping keys.
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

    from django_table_expressions import DerivedQuerySet, Join


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
        nearest=Join(nearest, join_type="left"),
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
        region=Join(regions, condition=Q(region__boundary__covers=F("location")), join_type="left"),
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

Table functions
===============

Functions explicitly opt in with ``table_source=True``. Existing
``set_returning=True`` expressions keep their current behaviour unless they
also opt in. ``output_field`` describes the named, typed outputs:

.. code-block:: python

    from django.db.models import Func, IntegerField, JSONField, TextField, Value
    from django_table_expressions import CompositeField


    class GenerateSeries(Func):
        function = "generate_series"
        output_field = IntegerField()
        table_source = True


    class JsonEach(Func):
        function = "jsonb_each"
        output_field = CompositeField(key=TextField(), value=JSONField())
        table_source = True


    parents = Parent.objects.annotate(
        series=GenerateSeries(Value(1), Value(3)),
    ).filter(series__gt=1)

These functions are PostgreSQL examples. A single-column function can be
selected with ``annotate()``. Register multi-column functions with ``alias()``
and select components such as ``item__key`` and ``item__value``. ``db_column``
maps an output field name to its physical function column. ``CompositeField``
describes expression output and cannot be installed as a model field.

Function SQL and output conversions depend on the backend. In particular,
SQLite ``json_each`` exposes numeric values as SQL numbers, whereas PostgreSQL
``jsonb_each`` exposes JSON. Declare appropriate output types for each function.
The suite executes PostgreSQL scalar/composite functions and SQLite numeric
JSON outputs, including implicit correlation. It does not claim arbitrary
function portability. Function joins on other vendors currently raise
``NotSupportedError`` until their compilation and execution are verified.

Database support
================

Queryset sources support ordinary joins on PostgreSQL, SQLite, MySQL, MariaDB
and Oracle. Correlated queryset sources require PostgreSQL, Oracle, or MySQL
8.0.14+. SQLite and MariaDB reject those correlated sources with
``NotSupportedError``. SQLite's implicitly correlated table functions are a
separate capability.

MySQL and Oracle use ``NO_MERGE`` hints where necessary to preserve NULL
extension for constant/cast projections. This can change materialisation and
execution plans. Oracle's character NULL conversions, source-locking errors
and native spatial/ordering restrictions still apply.

Source ``select_for_update()`` retains native transaction checks and locking
options when that source is used. Evaluate inside ``transaction.atomic()``.
Registering an unused lazy source does not execute it or acquire its locks.
Sources explicitly bound with ``using()`` cannot join another database.

Restrictions
============

Currently unsupported
---------------------

* Combined queryset sources using ``union()``, ``intersection()`` or
  ``difference()``; their separate implementation is not included here.
* Boolean queryset combinations using ``queryset_a | queryset_b`` or ``&``
  when table sources are registered. Use a single queryset with ``Q`` conditions.
* ``update()`` and ``delete()`` after resolving table-source references.
  An unused alias alone does not prevent a normal update.
* Forward or circular source dependencies. Register dependencies first.
* Projecting a composite primary key as one named output. Select its individual
  columns on Django versions with native composite primary keys.
* General tuple-expression RHS comparisons on Django before 5.2. Literal tuple
  comparisons and tuple membership subqueries are covered; other shapes can
  require newer native tuple infrastructure.

Operations that cannot have the requested meaning
-------------------------------------------------

* Joining querysets bound to different databases in one SQL statement.
* Selecting several source columns into one scalar annotation. Select component
  columns, or explicitly encode a scalar value such as JSON instead.
* Referring to an output that the source has not projected, or hydrating a
  related model instance through a derived alias.

For queryset sources, ``values()``/``values_list()`` define columns,
``values(...).annotate(...)`` defines grouping and aggregates, and
``order_by(...)[:n]`` chooses source rows. ``aggregate()`` returns a Python
dictionary rather than a source queryset.

Migration from DerivedRelation
==============================

The distribution is now ``django-table-expressions`` and the import package is
``django_table_expressions``. Replace imports from ``django_derived_relation``
with the new package. Recreate stored queryset/query pickles after upgrading;
the old import namespace is not included.

Replace ordinary multi-column descriptors with bare querysets. Replace explicit
LEFT/INNER and ON controls with ``Join`` and supply ``join_type``. Remove
``lateral=``; correlation determines that automatically. Register multi-column
sources with ``alias()``, then annotate or select their individual components.

Unlike 0.2.0's retained joins, unused aliases no longer change row counts.
Selecting a bare source makes it required and can remove unmatched rows; use
``Join(..., join_type="left")`` to retain those rows. Single-column row sources
also need an explicit ``Join`` to distinguish them from scalar subqueries.

The adapter remains necessary for this extension even if Django starts to
provide part of DEP 24. Presence of ``CompositeField`` alone does not establish
that native Django supports every source or explicit join control. The old
``models.DerivedRelation`` import guard is removed.

Development
===========

Run ``python tests/runtests.py`` against a disposable PostgreSQL database, or
``python tests/runtests.py --sqlite`` for ordinary sources and SQLite functions.
The runner creates and drops ``test_django_table_expressions``. PostgreSQL
connection settings use ``PG*`` environment variables.

``tox`` tests installed wheels across compatible Python/Django versions.
For example, ``tox -e py311-django52`` runs PostgreSQL and SQLite. Append
``-mysql`` or ``-oracle`` to select their service jobs. Avoid parallel tox
environments against a shared test database.

The CI matrix covers Django 4.2 through development Django, plus MySQL, MariaDB
and Oracle jobs. Test backend behaviour through execution before extending the
support table. No typing or cognitive-complexity analyser is currently
configured; automated formatting/linting does not verify those properties.

This project is licensed by Matthew Schinckel under the BSD 3-Clause license.
The copyright notice for code derived from Django is retained in ``LICENSE``.

Publishing releases
===================

The ``release.yml`` workflow runs the regression matrix, verifies the version
tag, builds a wheel and source distribution, validates their metadata, and
publishes through PyPI Trusted Publishing when a ``v<version>`` tag is pushed.
Routine branch pushes only run tests.

Before the first release under the new name, configure a PyPI pending publisher
with these settings:

* Project: ``django-table-expressions``
* Repository owner: ``schinckel``
* Repository: ``django-table-expressions``
* Workflow: ``release.yml``
* Environment: ``pypi``

The previous project's publisher does not cover the renamed project and
repository. No PyPI API token is needed.

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
