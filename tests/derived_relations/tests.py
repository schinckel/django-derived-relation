import pickle
from datetime import date
from decimal import Decimal
from importlib import reload
from unittest import skipUnless
from unittest.mock import patch

from django import VERSION
from django.core.exceptions import FieldDoesNotExist, FieldError
from django.db import DatabaseError, NotSupportedError, connection, models, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import (
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Cast
from django.db.models.lookups import GreaterThan
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.query import Query
from django.test import SimpleTestCase, skipUnlessDBFeature

import django_table_expressions
from django_table_expressions import DerivedQuerySetMixin, Join

from .base import DerivedRelationTestCase
from .models import Child, CompositeChild, InheritedChild, Parent


class PostgreSQLSourceQuery(Query):
    def as_postgresql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        query = self.clone()
        query.add_q(Q(amount__gte=30))
        sql, params = query.as_sql(compiler, connection)
        return sql, tuple(params)


@skipUnless(
    connection.vendor in {"postgresql", "oracle"}
    or (
        connection.vendor == "mysql"
        and not connection.mysql_is_mariadb
        and connection.mysql_version >= (8, 0, 14)
    ),
    "Requires LATERAL derived joins.",
)
class DerivedRelationTests(DerivedRelationTestCase):
    def test_computed_nullable_projection_in_inner_join(self) -> None:
        source = (
            Child.objects.filter(parent_id=OuterRef("pk"))
            .order_by()
            .values(total=Subquery(Child.objects.filter(pk=-1).values("amount")))
        )
        joined = Parent.objects.alias(rows=Join(source, join_type="inner"))
        self.assertEqual(joined.exclude(rows__total=1).count(), 3)

    def test_mixin_preserves_custom_queryset_and_manager(self) -> None:
        class ExistingQuerySet(QuerySet):
            def named(self, name: str) -> QuerySet:
                return self.filter(name=name)

        class CustomQuerySet(DerivedQuerySetMixin, ExistingQuerySet):
            pass

        manager = CustomQuerySet.as_manager()
        manager.model = Parent
        source = Child.objects.filter(parent_id=OuterRef("pk")).values("amount")
        joined = manager.named("many").alias(
            floor=F("minimum"),
            rows=Join(source, join_type="left"),
            chosen=F("rows__amount"),
        )
        self.assertIsInstance(joined.all(), CustomQuerySet)
        self.assertEqual(
            list(
                joined.named("many")
                .order_by("rows__amount")
                .values_list("rows__amount", flat=True)
            ),
            [20, 30],
        )
        annotated = (
            manager.alias(rows=Join(source, join_type="left"))
            .annotate(chosen=F("rows__amount"))
            .named("many")
        )
        self.assertIsInstance(annotated.all(), CustomQuerySet)
        self.assertEqual([row.chosen for row in annotated.order_by("chosen")], [20, 30])
        with self.assertRaises(NotSupportedError):
            joined.update(minimum=1)
        with self.assertRaises(NotSupportedError):
            joined.delete()

    def test_alias_registration_is_lazy(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        self.assertEqual(joined.count(), 3)
        self.assertEqual(joined.aggregate(total=Count("pk")), {"total": 3})
        self.assertEqual(
            [row.name for row in joined.order_by("name")], ["empty", "many", "one"]
        )
        with self.assertRaisesRegex(ValueError, "alias.*annotate"):
            Parent.objects.annotate(rows=Join(self.source(), join_type="left"))

    def test_annotate_mixed_expressions_preserve_selection_and_order(self) -> None:
        source = Child.objects.filter(
            parent_id=OuterRef("pk"), amount__gte=OuterRef("floor")
        ).values("amount")
        annotated = (
            Parent.objects.annotate(floor=F("minimum") + Value(15))
            .alias(rows=Join(source, join_type="left"))
            .annotate(chosen=F("rows__amount"))
        )
        self.assertEqual(
            [
                (row.name, row.floor, row.chosen)
                for row in annotated.order_by("name", "chosen")
            ],
            [
                ("empty", 15, None),
                ("many", 15, 20),
                ("many", 15, 30),
                ("one", 15, None),
            ],
        )
        aliased = Parent.objects.alias(
            floor=F("minimum") + Value(15),
            rows=Join(source, join_type="left"),
            chosen=F("rows__amount"),
        ).annotate(chosen_value=F("chosen"))
        self.assertEqual(
            [
                (row.name, row.chosen_value)
                for row in aliased.order_by("name", "chosen_value")
            ],
            [("empty", None), ("many", 20), ("many", 30), ("one", None)],
        )
        self.assertFalse(hasattr(aliased.first(), "floor"))
        self.assertFalse(hasattr(aliased.first(), "chosen"))

    def test_annotate_registration_resolves_preceding_relation(self) -> None:
        newest = self.source().order_by("-sequence").values("amount")[:1]
        dependent = Child.objects.filter(
            parent_id=OuterRef("pk"), amount=OuterRef("newest__amount")
        ).values("sequence")
        joined = (
            Parent.objects.alias(newest=Join(newest, join_type="left"))
            .alias(match=Join(dependent, join_type="left"))
            .annotate(chosen=F("newest__amount"))
            .annotate(sequence=F("match__sequence"))
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("name", "chosen", "sequence")),
            [("empty", None, None), ("many", 30, 2), ("one", 10, 1)],
        )

    def test_left_join_retains_cardinality_without_column_references(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).values("amount")
        parents = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(parents.count(), 3)
        self.assertEqual(Parent.objects.count(), 3)
        self.assertEqual(
            list(
                parents.order_by("name", "rows__amount").values_list(
                    "name", "rows__amount"
                )
            ),
            [("empty", None), ("many", 20), ("many", 30), ("one", 10)],
        )

    def test_top_one_columns_have_distinct_identity(self) -> None:
        Child.objects.create(parent=self.many, amount=40, sequence=2)
        source = (
            Child.objects.filter(parent_id=OuterRef("pk"))
            .order_by("-sequence", "-pk")
            .values("amount", "sequence")[:1]
        )
        parents = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertNotEqual(
            parents.query.resolve_ref("rows__amount"),
            parents.query.resolve_ref("rows__sequence"),
        )
        self.assertEqual(
            list(
                parents.order_by("name")
                .annotate(amount=F("rows__amount"), sequence=F("rows__sequence"))
                .values_list("name", "amount", "sequence")
            ),
            [("empty", None, None), ("many", 40, 2), ("one", 10, 1)],
        )

    def test_registration_does_not_select_columns_or_mutate_inputs(self) -> None:
        source = self.source()
        parents = Parent.objects.all()
        original = source.query.clone()
        joined = parents.alias(rows=Join(source, join_type="left"))
        self.assertEqual(list(joined.query.annotation_select), [])
        self.assertFalse(getattr(parents.query, "_source_joins", {}))
        self.assertEqual(source.query.alias_map, original.alias_map)
        self.assertIsNone(source._result_cache)
        self.assertFalse(hasattr(joined.first(), "rows__amount"))

    def test_inner_join_and_unreferenced_count(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="inner"))
        self.assertEqual(joined.count(), 3)
        self.assertEqual(
            list(joined.order_by("name").values_list("name", flat=True)),
            ["empty", "many", "one"],
        )

    def test_bare_model_source(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).order_by("-sequence")[
            :1
        ]
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__amount", flat=True)),
            [None, 30, 10],
        )

    def test_deferred_source_retains_filters_ordering_and_annotations(self) -> None:
        source = (
            Child.objects.filter(parent_id=OuterRef("pk"), sequence__gte=1)
            .only("amount")
            .annotate(doubled=F("amount") * 2)
            .alias(hidden=F("sequence"))
            .order_by("-sequence")[:1]
        )
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(
            tuple(
                "rows__" + name for name in joined.query.annotations["rows"].projection
            ),
            ("rows__id", "rows__amount", "rows__doubled"),
        )
        self.assertIsInstance(
            joined.query.resolve_ref("rows__amount").output_field, models.IntegerField
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__amount", "rows__doubled")),
            [(None, None), (30, 60), (10, 20)],
        )
        restored = joined.all()
        restored.query = pickle.loads(pickle.dumps(joined.query))
        self.assertEqual(
            list(restored.filter(name="many").values_list("rows__doubled", flat=True)),
            [60],
        )
        self.assertEqual(source.query.deferred_loading, (frozenset({"amount"}), False))
        self.assertIsNone(getattr(source.query, "selected", None))
        self.assertIsNone(source._result_cache)
        with self.assertRaises(FieldError):
            restored.values("rows__sequence")

    def test_nested_on_condition_preserves_left_join(self) -> None:
        condition = (Q(rows__amount__gt=25) & Q(rows__sequence=2)) | (
            Q(rows__amount=10) & Q(name="one")
        )
        joined = Parent.objects.alias(
            rows=Join(self.source(), condition=condition, join_type="left")
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("name", "rows__amount")),
            [("empty", None), ("many", 30), ("one", 10)],
        )
        filtered = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        self.assertEqual(filtered.filter(condition).count(), 2)

    def test_boolean_on_expression(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(
                self.source(),
                condition=GreaterThan(F("rows__amount"), Value(25)),
                join_type="left",
            )
        )
        self.assertEqual(joined.filter(rows__amount__isnull=False).count(), 1)

    def test_on_expression_can_transform_its_own_columns(self) -> None:
        source = self.source().annotate(day=Value(date(2026, 10, 13))).values("day")
        joined = Parent.objects.alias(
            rows=Join(
                source,
                condition=GreaterThan(F("rows__day__year"), Value(2020)),
                join_type="left",
            )
        )
        self.assertEqual(joined.count(), 3)
        self.assertEqual(joined.filter(rows__day__isnull=False).count(), 3)

    def test_parameter_order(self) -> None:
        source = (
            self.source()
            .filter(amount__gt=5)
            .annotate(mark=Value("source"))
            .values("amount", "mark")
        )
        joined = (
            Parent.objects.annotate(label=Value("select"))
            .alias(
                rows=Join(source, condition=Q(rows__amount__gt=15), join_type="left")
            )
            .filter(name="many")
        )
        joined = joined.annotate(amount=F("rows__amount"))
        sql, params = joined.query.sql_with_params()
        self.assertEqual(params, ("select", "source", 5, 15, "many"))
        self.assertEqual(sql.count("%s"), len(params))
        self.assertEqual(joined.count(), 2)

    def test_empty_sources_and_false_true_on(self) -> None:
        for source in (
            Child.objects.none().values("amount"),
            self.source().filter(pk__in=[]),
        ):
            for kind, count in (("left", 3), ("inner", 0)):
                with self.subTest(kind=kind):
                    joined = Parent.objects.alias(rows=Join(source, join_type=kind))
                    self.assertEqual(joined.values("rows__amount").count(), count)
        for condition, count in (
            (Q(rows__amount__in=[]), 3),
            (~Q(rows__amount__in=[]), 4),
        ):
            joined = Parent.objects.alias(
                rows=Join(self.source(), condition=condition, join_type="left")
            )
            self.assertEqual(joined.values("rows__amount").count(), count)

    def test_filter_order_aggregate_distinct_slice_and_clone(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        self.assertEqual(joined.aggregate(total=Sum("rows__amount")), {"total": 60})
        self.assertEqual(joined.values("name").distinct().count(), 3)
        self.assertEqual(joined.order_by("rows__amount")[:2].count(), 2)
        self.assertEqual(joined.filter(rows__amount__gt=15).count(), 2)
        self.assertEqual(joined.exclude(rows__amount=20).count(), 3)
        self.assertEqual(joined.count(), 3)
        grouped = joined.values("name").annotate(
            n=Count("rows__sequence"), total=Sum("rows__amount")
        )
        self.assertEqual(
            list(grouped.order_by("name").values_list("name", "n", "total")),
            [("empty", 0, None), ("many", 2, 50), ("one", 1, 10)],
        )

    def test_preceding_derived_source(self) -> None:
        first = self.source().order_by("-sequence")[:1]
        second = Child.objects.filter(amount=OuterRef("first__amount")).values(
            "sequence"
        )
        joined = Parent.objects.alias(first=Join(first, join_type="left")).alias(
            second=Join(second, join_type="left")
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("second__sequence", flat=True)),
            [None, 2, 1],
        )

    def test_correlation_does_not_mutate_preceding_queryset_columns(self) -> None:
        original = Parent.objects.alias(first=Join(self.source(), join_type="left"))
        source = Child.objects.filter(amount=OuterRef("first__amount")).values(
            "sequence"
        )
        original.alias(second=Join(source, join_type="left"))
        self.assertFalse(
            original.query.resolve_ref("first__amount").possibly_multivalued
        )

    def test_nested_subquery_and_exists(self) -> None:
        joined = Parent.objects.filter(pk=OuterRef("pk")).alias(
            rows=Join(self.source(), join_type="left")
        )
        parents = Parent.objects.annotate(
            amount=Subquery(
                joined.order_by("-rows__amount").values("rows__amount")[:1]
            ),
            found=Exists(joined),
        )
        self.assertEqual(
            list(parents.order_by("name").values_list("amount", "found")),
            [(None, True), (30, True), (10, True)],
        )

    def test_explicit_join_types_survive_or_filters(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(self.source(), join_type="inner")
        ).filter(Q(name="empty") | Q(rows__amount__gt=0))
        self.assertEqual(joined.count(), 3)

    def test_grouping_projected_primary_key_does_not_assume_table_constraints(
        self,
    ) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).values("id", "amount")
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        grouped = joined.values("rows__id", "rows__amount").annotate(n=Count("pk"))
        self.assertEqual(len(list(grouped)), 4)

    @skipUnlessDBFeature("can_distinct_on_fields")
    def test_distinct_on_projected_column(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        self.assertEqual(
            len(list(joined.order_by("rows__amount").distinct("rows__amount"))), 4
        )

    def test_correlated_relationship_dependency(self) -> None:
        source = Parent.objects.filter(name=OuterRef("parent__name")).values("minimum")
        joined = Child.objects.alias(info=Join(source, join_type="left"))
        self.assertEqual(
            list(joined.order_by("amount").values_list("info__minimum", flat=True)),
            [0, 0, 0],
        )

    def test_derived_alias_collision_with_base_table(self) -> None:
        alias = Parent._meta.db_table
        joined = Parent.objects.alias(**{alias: Join(self.source(), join_type="left")})
        self.assertEqual(joined.count(), 3)
        self.assertEqual(joined.filter(**{f"{alias}__amount__gt": 25}).count(), 1)

    def test_nested_outerref(self) -> None:
        source = (
            Child.objects.filter(parent_id=OuterRef(OuterRef("pk")))
            .order_by("sequence", "pk")
            .values("amount")
        )
        middle = (
            Parent.objects.alias(rows=Join(source, join_type="left"))
            .order_by("pk", "rows__amount")
            .values("rows__amount")[:1]
        )
        parents = Parent.objects.annotate(amount=Subquery(middle))
        self.assertEqual(
            list(parents.order_by("name").values_list("amount", flat=True)),
            [None, 20, 10],
        )

    def test_distinct_definitions_and_repeated_descriptor(self) -> None:
        descriptor = Join(self.source(), join_type="left")
        joined = Parent.objects.alias(a=descriptor, b=descriptor)
        self.assertEqual(joined.count(), 3)
        joined = Parent.objects.alias(
            a=descriptor,
            b=Join(self.source().filter(amount__gt=25), join_type="left"),
        )
        self.assertEqual(joined.count(), 3)
        filtered = joined.filter(b__amount=30)
        self.assertEqual(filtered.count(), 1)
        self.assertEqual(filtered.values("a__amount").count(), 2)

    def test_null_rhs_in_negated_comparison(self) -> None:
        joined = Parent.objects.alias(
            a=Join(self.source(), join_type="left"),
            b=Join(self.source().filter(amount__gt=25), join_type="left"),
        )
        self.assertEqual(joined.exclude(a__amount=F("b__amount")).count(), 3)

    def test_negated_in_including_none(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        self.assertEqual(joined.exclude(rows__amount__in=[20, None]).count(), 2)

    def test_scalar_alias_can_replace_unused_source(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        replaced = joined.alias(rows=Value(7)).annotate(chosen=F("rows"))
        self.assertEqual(list(replaced.values_list("chosen", flat=True)), [7] * 3)
        self.assertEqual(joined.count(), 3)

    def test_forward_reference_rejected(self) -> None:
        source = Child.objects.filter(amount=OuterRef("later__amount")).values("amount")
        with self.assertRaises(FieldError):
            Parent.objects.alias(first=Join(source, join_type="left"))

    def test_correlation_in_nested_source_is_inferred(self) -> None:
        inner = Parent.objects.filter(pk=OuterRef("pk")).alias(
            rows=Join(self.source(), join_type="left")
        )
        outer = Parent.objects.alias(
            nested=Join(inner.values(amount=F("rows__amount")), join_type="left")
        )
        self.assertEqual(
            sorted(
                outer.values_list("nested__amount", flat=True),
                key=lambda value: value or 0,
            ),
            [None, 10, 20, 30],
        )

    def test_mixed_alias_registration_order(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(self.source(), join_type="left"),
            amount=F("rows__amount"),
        ).annotate(selected=F("amount"))
        self.assertEqual(
            list(
                joined.order_by(F("selected").asc(nulls_last=True)).values_list(
                    "selected", flat=True
                )
            ),
            [10, 20, 30, None],
        )

    def test_nested_on_outerref(self) -> None:
        inner = (
            Parent.objects.filter(pk=OuterRef("pk"))
            .alias(
                rows=Join(
                    self.source(),
                    condition=Q(rows__amount__gt=OuterRef("minimum")),
                    join_type="left",
                )
            )
            .order_by("rows__amount")
            .values("rows__amount")[:1]
        )
        parents = Parent.objects.annotate(amount=Subquery(inner))
        self.assertEqual(
            list(parents.order_by("name").values_list("amount", flat=True)),
            [None, 20, 10],
        )

    def test_values_list_source_and_expression_projection(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).values_list(
            "amount", "sequence"
        )
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(joined.aggregate(total=Sum("rows__amount")), {"total": 60})
        computed = self.source().annotate(double=F("amount") * 2).values("double")
        joined = Parent.objects.alias(rows=Join(computed, join_type="left"))
        self.assertEqual(joined.filter(rows__double__gte=40).count(), 2)

    @skipUnless(connection.vendor == "postgresql", "Uses an as_postgresql() override.")
    def test_source_vendor_compiler_dispatch(self) -> None:
        source = self.source()
        source.query.__class__ = PostgreSQLSourceQuery
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(joined.count(), 3)
        self.assertEqual(joined.filter(rows__amount__isnull=False).count(), 1)

    def test_column_types_and_transforms(self) -> None:
        source = (
            self.source()
            .annotate(
                day=Cast(Value(date(2026, 10, 13)), models.DateField()),
                price=Value(Decimal("1.25")),
            )
            .values("day", "price")
        )
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(joined.filter(rows__day__year=2026).count(), 3)
        self.assertEqual(joined.filter(rows__price__gt=Decimal("1.20")).count(), 3)
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__day", "rows__price")),
            [
                (None, None),
                (date(2026, 10, 13), Decimal("1.25")),
                (date(2026, 10, 13), Decimal("1.25")),
                (date(2026, 10, 13), Decimal("1.25")),
            ],
        )
        self.assertEqual(
            list(
                joined.filter(name="one")
                .annotate(year=F("rows__day__year"))
                .values_list("year", flat=True)
            ),
            [2026],
        )

    def test_unbound_same_type_projection_identity_through_aggregate_wrapper(
        self,
    ) -> None:
        source = self.source().annotate(x=Value(1), y=Value(2)).values("x", "y")[:1]
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertNotEqual(
            joined.query.resolve_ref("rows__x"), joined.query.resolve_ref("rows__y")
        )
        self.assertEqual(
            joined.distinct().aggregate(x=Sum("rows__x"), y=Sum("rows__y")),
            {"x": 2, "y": 4},
        )

    def test_pickled_query_preserves_projection_constraints(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).values("id", "amount")
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        restored = Parent.objects.all()
        restored.query = pickle.loads(pickle.dumps(joined.query))
        self.assertEqual(restored.count(), 3)
        grouped = restored.values("rows__id", "rows__amount").annotate(n=Count("pk"))
        self.assertEqual(len(list(grouped)), 4)

    def test_projected_column_correlation(self) -> None:
        source = (
            Child.objects.filter(parent_id=OuterRef("pk"))
            .annotate(parent_name=OuterRef("name"))
            .values("parent_name")
        )
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(
            list(joined.order_by("name").values_list("name", "rows__parent_name")),
            [
                (
                    "empty",
                    (
                        ""
                        if connection.features.interprets_empty_strings_as_nulls
                        else None
                    ),
                ),
                ("many", "many"),
                ("many", "many"),
                ("one", "one"),
            ],
        )

    def test_materialized_source_does_not_shadow_outer_alias(self) -> None:
        source = self.source().values(
            marker=Cast(Value(date(2026, 10, 13)), models.DateField())
        )
        outer = Parent.objects.all()
        outer_alias = outer.query.get_initial_alias()
        # Django emits custom query aliases unquoted. Oracle requires an
        # alphabetic first character; MySQL still exercises the former
        # wrapper alias that could shadow a correlated outer table.
        renamed = (
            "DERIVED_SOURCE" if connection.vendor == "oracle" else "__derived_source"
        )
        outer.query.change_aliases({outer_alias: renamed})
        joined = outer.alias(rows=Join(source, join_type="left"))
        self.assertEqual(joined.filter(rows__marker__year=2026).count(), 3)
        self.assertEqual(
            list(joined.order_by("pk").values_list("rows__marker", flat=True)),
            [None, date(2026, 10, 13), date(2026, 10, 13), date(2026, 10, 13)],
        )

    def test_outer_select_related_cannot_hydrate_derived_relation(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        with self.assertRaises(FieldError):
            joined.select_related("rows").query.sql_with_params()

    def test_queryset_boolean_combination_rejected(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        with self.assertRaises(NotSupportedError):
            joined | joined.filter(name="one")

    def test_outer_values_does_not_allow_model_field_collision(self) -> None:
        with self.assertRaises(ValueError):
            Parent.objects.values("pk").alias(
                name=Join(self.source(), join_type="left")
            )

    def test_explicit_source_database_mismatch_rejected(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(self.source().using("other"), join_type="left")
        )
        with self.assertRaises(ValueError):
            joined.values("rows__amount").query.sql_with_params()

    def test_relation_predicates_cannot_hide_required_source(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        nested = Parent.objects.filter(pk__in=joined.values("pk"))
        self.assertEqual(nested.count(), 3)

    def test_select_related_source_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Parent.objects.alias(
                rows=Join(Child.objects.select_related("parent"), join_type="left")
            )

    def test_unsupported_source_shapes(self) -> None:
        with self.assertRaises(NotSupportedError):
            Parent.objects.alias(
                rows=Join(
                    Child.objects.all().union(Child.objects.all()), join_type="left"
                )
            )

    def test_correlation_does_not_require_a_flag(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        self.assertEqual(joined.values("rows__amount").count(), 4)

    def test_source_replacement_and_unknown_columns(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        replaced = joined.alias(
            rows=Join(self.source().filter(amount=10), join_type="left")
        )
        self.assertEqual(
            list(replaced.order_by("name").values_list("rows__amount", flat=True)),
            [None, None, 10],
        )
        with self.assertRaises(FieldError):
            joined.filter(rows__unknown=1)

    def test_used_sources_reject_writes_but_unused_aliases_allow_update(self) -> None:
        joined = Parent.objects.alias(rows=Join(self.source(), join_type="left"))
        self.assertEqual(joined.update(minimum=1), 3)
        used = joined.filter(rows__amount=10)
        with self.assertRaises(NotSupportedError):
            used.update(minimum=2)
        with self.assertRaises(NotSupportedError):
            used.delete()


@skipUnless(
    connection.vendor in {"postgresql", "sqlite", "mysql", "oracle"},
    "Requires ordinary derived joins.",
)
class OrdinaryDerivedRelationTests(DerivedRelationTestCase):
    def test_date_projection_preserves_null_extension(self) -> None:
        source = Child.objects.filter(amount=10).values(
            "parent_id", day=Cast(Value(date(2026, 10, 13)), models.DateField())
        )
        joined = Parent.objects.alias(
            rows=Join(source, condition=Q(rows__parent_id=F("pk")), join_type="left")
        )
        self.assertEqual(joined.filter(rows__day__year=2026).count(), 1)
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__day", flat=True)),
            [None, None, date(2026, 10, 13)],
        )

    def test_constant_projection_preserves_null_extension(self) -> None:
        source = Child.objects.filter(amount=10).values("parent_id", marker=Value(7))
        joined = Parent.objects.alias(
            rows=Join(source, condition=Q(rows__parent_id=F("pk")), join_type="left")
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__marker", flat=True)),
            [None, None, 7],
        )
        self.assertEqual(
            list(joined.filter(rows__marker=7).values_list("name", flat=True)), ["one"]
        )
        self.assertEqual(
            list(
                joined.exclude(rows__marker=7)
                .order_by("name")
                .values_list("name", flat=True)
            ),
            ["empty", "many"],
        )

    def test_left_and_inner_join_cardinality_without_column_references(self) -> None:
        source = Child.objects.values("parent_id", "amount")
        for kind, expected in (("left", 4), ("inner", 3)):
            with self.subTest(kind=kind):
                joined = Parent.objects.alias(
                    rows=Join(
                        source, condition=Q(rows__parent_id=F("pk")), join_type=kind
                    )
                )
                self.assertEqual(joined.count(), 3)
                self.assertEqual(joined.values("rows__amount").count(), expected)
                self.assertEqual(joined.aggregate(total=Count("pk")), {"total": 3})
                self.assertIsNone(source._result_cache)

    def test_empty_sources_preserve_left_rows(self) -> None:
        for source in (
            Child.objects.none().values("amount"),
            Child.objects.filter(pk__in=[]).values("amount"),
            Child.objects.values("amount")[:0],
        ):
            for kind, expected in (("left", [None, None, None]), ("inner", [])):
                with self.subTest(kind=kind, source=source.query.is_empty()):
                    joined = Parent.objects.alias(rows=Join(source, join_type=kind))
                    self.assertEqual(
                        list(joined.values_list("rows__amount", flat=True)), expected
                    )

    def test_constant_on_conditions_preserve_left_rows(self) -> None:
        source = Child.objects.values("parent_id", "amount")
        for condition, expected in (
            (Q(rows__amount__in=[]), [None, None, None]),
            (~Q(rows__amount__in=[]), [None, 20, 30, 10]),
        ):
            with self.subTest(condition=condition):
                joined = Parent.objects.alias(
                    rows=Join(
                        source,
                        condition=Q(rows__parent_id=F("pk")) & condition,
                        join_type="left",
                    )
                )
                self.assertEqual(
                    list(
                        joined.order_by("name", "rows__amount").values_list(
                            "rows__amount", flat=True
                        )
                    ),
                    expected,
                )

    def test_ordered_slice_applies_to_source(self) -> None:
        source = Child.objects.order_by("-amount").values("amount", "sequence")[:1]
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__amount", "rows__sequence")),
            [(30, 2), (30, 2), (30, 2)],
        )

    def test_selected_annotations_and_bare_columns(self) -> None:
        source = Child.objects.filter(amount=10).annotate(doubled=F("amount") * 2)
        joined = Parent.objects.alias(
            rows=Join(source, condition=Q(rows__parent_id=F("pk")), join_type="left")
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__amount", "rows__doubled")),
            [(None, None), (None, None), (10, 20)],
        )

    def test_nested_ordinary_sources(self) -> None:
        first = Child.objects.values("parent_id", "amount")
        source = Parent.objects.alias(
            lines=Join(first, condition=Q(lines__parent_id=F("pk")), join_type="left")
        ).values(parent_name=F("name"), amount=F("lines__amount"))
        joined = Parent.objects.alias(
            rows=Join(
                source, condition=Q(rows__parent_name=F("name")), join_type="left"
            )
        )
        self.assertEqual(
            list(
                joined.order_by("name", "rows__amount").values_list(
                    "rows__amount", flat=True
                )
            ),
            [None, 20, 30, 10],
        )

    def test_negation_and_parameter_order(self) -> None:
        source = (
            Child.objects.filter(amount__gte=10)
            .annotate(mark=Value("from-source"))
            .values("parent_id", "amount", "mark")
        )
        joined = Parent.objects.alias(
            rows=Join(
                source,
                condition=Q(rows__parent_id=F("pk"), rows__mark="from-source"),
                join_type="left",
            )
        )
        self.assertEqual(
            list(
                joined.exclude(rows__amount=20)
                .order_by("name")
                .values_list("name", "rows__amount")
            ),
            [("empty", None), ("many", 30), ("one", 10)],
        )

    @skipUnlessDBFeature("has_select_for_update")
    def test_source_locking_uses_native_backend_compiler(self) -> None:
        source = Child.objects.values("id", "parent_id").select_for_update()
        joined = Parent.objects.alias(
            rows=Join(source, condition=Q(rows__parent_id=F("pk")), join_type="left")
        )
        joined = joined.values("rows__id")
        self.assertIn("FOR UPDATE", joined.query.sql_with_params()[0])
        self.assertTrue(joined._for_write)
        self.assertFalse(joined.query.select_for_update)
        if connection.vendor == "oracle":
            # Oracle permits FOR UPDATE only in a top-level SELECT. Preserve
            # its error rather than dropping the source's requested lock.
            with (
                transaction.atomic(),
                self.assertRaises(DatabaseError) as derived_error,
            ):
                joined.count()
            source_sql, params = source.query.sql_with_params()
            with transaction.atomic(), self.assertRaises(DatabaseError) as native_error:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT * FROM ({source_sql})", params)
            self.assertEqual(
                derived_error.exception.args[0].code,
                native_error.exception.args[0].code,
            )
        else:
            self.assertEqual(joined.count(), 4)

    def test_annotate_registration_matches_alias_on_conditions(self) -> None:
        source = Child.objects.values("parent_id", "amount")
        for join_type, expected in (
            ("left", [("empty", None), ("many", 30), ("one", None)]),
            ("inner", [("many", 30)]),
        ):
            with self.subTest(join_type=join_type):
                relation = Join(
                    source,
                    join_type=join_type,
                    condition=Q(rows__parent_id=F("pk"), rows__amount__gt=20),
                )
                for register in (Parent.objects.alias,):
                    joined = register(rows=relation)
                    self.assertEqual(
                        list(
                            joined.order_by("name").values_list("name", "rows__amount")
                        ),
                        expected,
                    )

    def test_deferred_sources_match_native_root_columns(self) -> None:
        cases: tuple[tuple[QuerySet, tuple[str, ...]], ...] = (
            (Child.objects.only("amount"), ("id", "amount")),
            (Child.objects.defer("sequence"), ("id", "parent_id", "amount")),
            (Child.objects.only("parent"), ("id", "parent_id")),
            (Child.objects.only("parent_id"), ("id", "parent_id")),
            (Child.objects.defer("parent"), ("id", "amount", "sequence")),
            (Child.objects.defer("parent_id"), ("id", "amount", "sequence")),
            (Child.objects.only("parent__name"), ("id", "parent_id")),
            (
                Child.objects.defer("parent__name"),
                ("id", "parent_id", "amount", "sequence"),
            ),
            (Child.objects.defer("id"), ("id", "parent_id", "amount", "sequence")),
            (Child.objects.only("amount").only("sequence"), ("id", "sequence")),
            (
                Child.objects.only("amount", "sequence").defer("sequence"),
                ("id", "amount"),
            ),
            (
                Child.objects.defer("sequence").only("amount", "sequence"),
                ("id", "amount"),
            ),
            (
                Child.objects.defer("sequence").defer("amount"),
                ("id", "parent_id"),
            ),
            (
                Child.objects.only("amount").defer(None),
                ("id", "parent_id", "amount", "sequence"),
            ),
        )
        for source, names in cases:
            with self.subTest(deferred_loading=source.query.deferred_loading):
                source = source.filter(parent=self.one)
                if VERSION[:3] == (4, 2, 0) and source.query.deferred_loading == (
                    frozenset({"parent_id"}),
                    True,
                ):
                    # Django #34458: deferring an FK attname crashes in 4.2.0.
                    with self.assertRaises(AttributeError):
                        source.query.get_compiler(connection=connection).get_select()
                    with self.assertRaises(AttributeError):
                        Parent.objects.alias(rows=Join(source, join_type="left"))
                    continue
                native_select = source.query.get_compiler(
                    connection=connection
                ).get_select()[0]
                self.assertEqual(
                    tuple(
                        expression.target.attname for expression, _, _ in native_select
                    ),
                    names,
                )
                joined = Parent.objects.filter(pk=self.one.pk).alias(
                    rows=Join(source, join_type="inner")
                )
                self.assertEqual(
                    tuple(
                        "rows__" + name
                        for name in joined.query.annotations["rows"].projection
                    ),
                    tuple(f"rows__{name}" for name in names),
                )
                self.assertEqual(
                    list(joined.values_list(*(f"rows__{name}" for name in names))),
                    list(source.values_list(*names)),
                )
                for omitted in {"id", "parent_id", "amount", "sequence"} - set(names):
                    with self.assertRaises(FieldError):
                        joined.values(f"rows__{omitted}")
                    with self.assertRaises(FieldError):
                        joined.filter(**{f"rows__{omitted}": 1})

    def test_explicit_values_replace_deferred_source_selection(self) -> None:
        for source in (
            Child.objects.only("amount").values("sequence"),
            Child.objects.defer("sequence").values("sequence"),
        ):
            with self.subTest(source=source):
                joined = Parent.objects.filter(pk=self.one.pk).alias(
                    rows=Join(source.filter(parent=self.one), join_type="left")
                )
                self.assertEqual(
                    tuple(
                        "rows__" + name
                        for name in joined.query.annotations["rows"].projection
                    ),
                    ("rows__sequence",),
                )
                self.assertEqual(
                    list(joined.values_list("rows__sequence", flat=True)), [1]
                )
                with self.assertRaises(FieldError):
                    joined.values("rows__amount")

    def test_invalid_deferred_source_fields_use_native_validation(self) -> None:
        for source in (
            Child.objects.only("unknown"),
            Child.objects.defer("unknown"),
            Child.objects.only("parent__unknown"),
        ):
            with self.subTest(deferred_loading=source.query.deferred_loading):
                with self.assertRaises(FieldDoesNotExist):
                    source.query.get_compiler(connection=connection).get_select()
                with self.assertRaises(FieldDoesNotExist):
                    Parent.objects.alias(rows=Join(source, join_type="left"))

    def test_inherited_deferred_source_preserves_parent_columns(self) -> None:
        child = InheritedChild.objects.create(
            parent=self.one, amount=40, sequence=3, label="extra"
        )
        source = InheritedChild.objects.only("amount", "label").filter(pk=child.pk)
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=Join(source, join_type="left")
        )
        self.assertEqual(
            tuple(
                "rows__" + name for name in joined.query.annotations["rows"].projection
            ),
            ("rows__amount", "rows__child_ptr_id", "rows__label"),
        )
        self.assertEqual(
            list(
                joined.values_list("rows__amount", "rows__child_ptr_id", "rows__label")
            ),
            [(40, child.pk, "extra")],
        )
        with self.assertRaises(FieldError):
            joined.values("rows__sequence")

    @skipUnless(
        hasattr(models, "CompositePrimaryKey"), "Requires composite primary keys."
    )
    def test_composite_primary_key_deferred_source_has_individual_columns(self) -> None:
        CompositeChild.objects.create(parent=self.one, amount=40, sequence=3)
        source = CompositeChild.objects.only("amount").filter(parent=self.one)
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=Join(source, join_type="left")
        )
        self.assertEqual(
            tuple(
                "rows__" + name for name in joined.query.annotations["rows"].projection
            ),
            ("rows__parent_id", "rows__amount", "rows__sequence"),
        )
        self.assertEqual(
            list(
                joined.values_list("rows__parent_id", "rows__amount", "rows__sequence")
            ),
            [(self.one.pk, 40, 3)],
        )
        with self.assertRaisesMessage(ValueError, "individually named columns"):
            Parent.objects.alias(rows=Join(source.values("pk"), join_type="left"))

    def test_ordinary_grouped_source(self) -> None:
        source = Child.objects.values("parent_id").annotate(total=Sum("amount"))
        joined = Parent.objects.alias(
            totals=Join(
                source, condition=Q(totals__parent_id=F("pk")), join_type="left"
            )
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("totals__total", flat=True)),
            [None, 50, 10],
        )

    def test_empty_aggregate_source_has_no_rows(self) -> None:
        source = (
            Child.objects.none()
            .order_by()
            .values(one=Value(1))
            .annotate(total=Count("pk"))
            .values("total")
        )
        left = Parent.objects.alias(rows=Join(source, join_type="left"))
        inner = Parent.objects.alias(rows=Join(source, join_type="inner"))
        self.assertEqual(inner.values("rows__total").count(), 0)
        self.assertEqual(
            list(left.values_list("rows__total", flat=True)), [None, None, None]
        )

    def test_on_relationship_dependency(self) -> None:
        source = Parent.objects.values("name", "minimum")
        joined = Child.objects.alias(
            info=Join(
                source, condition=Q(info__name=F("parent__name")), join_type="left"
            )
        )
        self.assertEqual(joined.count(), 3)

    def test_null_rhs_from_ordinary_outer_join(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(Child.objects.values("amount"), join_type="inner")
        )
        self.assertEqual(joined.exclude(rows__amount=F("children__amount")).count(), 9)


class DerivedRelationValidationTests(SimpleTestCase):
    def test_mysql_lateral_version_boundary(self) -> None:
        for version, mariadb, supported in (
            ((8, 0, 13), False, False),
            ((8, 0, 14), False, True),
            ((11, 4, 0), True, False),
        ):
            with (
                self.subTest(version=version, mariadb=mariadb),
                patch.object(connection, "vendor", "mysql"),
                patch.object(connection, "mysql_version", version, create=True),
                patch.object(connection, "mysql_is_mariadb", mariadb, create=True),
            ):
                joined = Parent.objects.alias(
                    rows=Join(
                        Child.objects.filter(parent_id=OuterRef("pk")).values("amount"),
                        join_type="left",
                    )
                )
                if supported:
                    self.assertIn(
                        " LATERAL ",
                        joined.values("rows__amount").query.sql_with_params()[0],
                    )
                else:
                    with self.assertRaisesMessage(NotSupportedError, "LATERAL"):
                        joined.values("rows__amount").query.sql_with_params()

    def test_backend_can_disable_derived_joins(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(Child.objects.values("id"), join_type="left")
        )
        with patch.object(
            connection.features, "supports_derived_joins", False, create=True
        ):
            with self.assertRaisesMessage(NotSupportedError, "derived joins"):
                joined.values("rows__id").query.sql_with_params()

    def test_backend_can_disable_lateral_joins(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(
                Child.objects.filter(parent_id=OuterRef("pk")).values("id"),
                join_type="left",
            )
        )
        with patch.object(
            connection.features, "supports_lateral_joins", False, create=True
        ):
            with self.assertRaisesMessage(NotSupportedError, "LATERAL"):
                joined.values("rows__id").query.sql_with_params()

    def test_annotation_only_source_does_not_export_unselected_fields(self) -> None:
        source = Child.objects.values(renamed=F("amount"))
        joined = Parent.objects.alias(rows=Join(source, join_type="left"))
        self.assertEqual(
            joined.query.resolve_ref("rows__renamed").output_field.get_internal_type(),
            "IntegerField",
        )
        for field in ("id", "parent_id", "amount", "sequence"):
            with self.subTest(field=field), self.assertRaises(FieldError):
                joined.values(f"rows__{field}")

    def test_registration_rejects_model_field_collisions(self) -> None:
        relation = Join(Child.objects.values("amount"), join_type="left")
        for queryset, names in (
            (Parent.objects.all(), ("name", "pk", "id")),
            (Parent.objects.values("pk"), ("name", "pk", "id")),
            (Child.objects.all(), ("parent", "parent_id")),
        ):
            for method in ("alias", "annotate"):
                for name in names:
                    with self.subTest(model=queryset.model, method=method, name=name):
                        with self.assertRaisesRegex(ValueError, "conflicts.*field"):
                            getattr(queryset, method)(**{name: relation})

    def test_source_replaces_existing_expression_without_mutating_original(
        self,
    ) -> None:
        original = Parent.objects.alias(rows=Value(7))
        joined = original.alias(
            rows=Join(Child.objects.values("amount"), join_type="left")
        )
        self.assertEqual(original.query.annotations["rows"].value, 7)
        self.assertIn("rows", joined.query.annotations)

    def test_registration_rejects_earlier_expression_in_same_call(self) -> None:
        relation = Join(Child.objects.values("amount"), join_type="left")
        for method in ("alias", "annotate"):
            with self.subTest(method=method):
                with self.assertRaisesMessage(ValueError, "conflict with aliases"):
                    getattr(Parent.objects, method)(
                        **{"rows__amount": Value(7), "rows": relation}
                    )

    def test_partial_core_composite_support_does_not_disable_backport(self) -> None:
        with patch.object(models, "CompositeField", object(), create=True):
            module = reload(django_table_expressions)
        self.assertIs(module.Join, Join)

    def test_empty_relation_and_projection_names_rejected(self) -> None:
        with self.assertRaisesMessage(ValueError, "nonempty"):
            Parent.objects.alias(
                **{"": Join(Child.objects.values("amount"), join_type="left")}
            )
        with self.assertRaisesMessage(ValueError, "nonempty"):
            Parent.objects.alias(
                rows=Join(Child.objects.values(**{"": F("amount")}), join_type="left")
            )

    def test_source_and_argument_validation(self) -> None:
        with self.assertRaises(TypeError):
            Join(Value(1), join_type="left")
        with self.assertRaises(ValueError):
            Join(Child.objects.all(), join_type="right")
        with self.assertRaises(TypeError):
            Join(Child.objects.all(), condition=Value(1), join_type="left")
        relation = Join(Child.objects.all(), join_type="left")
        with self.assertRaises(TypeError):
            Parent.objects.annotate(relation)
        with self.assertRaisesMessage(TypeError, "registered"):
            Parent.objects.annotate(value=F("minimum") + relation)

    def test_unsupported_backend(self) -> None:
        joined = Parent.objects.alias(
            rows=Join(Child.objects.values("amount"), join_type="left")
        )
        with (
            patch.object(connection, "vendor", "unknown"),
            self.assertRaises(NotSupportedError),
        ):
            joined.values("rows__amount").query.sql_with_params()
