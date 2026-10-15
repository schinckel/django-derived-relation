import pickle
import runpy
from datetime import date
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django import VERSION
from django.core.exceptions import FieldDoesNotExist, FieldError
from django.db import (
    DatabaseError,
    NotSupportedError,
    connection,
    connections,
    models,
    transaction,
)
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
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    skipUnlessDBFeature,
)

import django_derived_relation
from django_derived_relation import DerivedQuerySetMixin, DerivedRelation

from .models import Child, CompositeChild, InheritedChild, Parent


class PostgreSQLSourceQuery(Query):
    def as_postgresql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        query = self.clone()
        query.add_q(Q(amount__gte=30))
        sql, params = query.as_sql(compiler, connection)
        return sql, tuple(params)


class DerivedRelationTestCase(
    TransactionTestCase if connection.vendor == "yugabyte" else TestCase
):
    def setUp(self) -> None:
        super().setUp()
        if connection.vendor == "yugabyte":
            # YugabyteDB's deferred FK triggers fail after TestCase savepoint
            # rollback. Use committed fixtures and Django's normal table flush.
            self.setUpTestData()

    @classmethod
    def setUpTestData(cls) -> None:
        cls.empty = Parent.objects.create(name="empty")
        cls.one = Parent.objects.create(name="one")
        cls.many = Parent.objects.create(name="many")
        Child.objects.create(parent=cls.one, amount=10, sequence=1)
        Child.objects.create(parent=cls.many, amount=20, sequence=1)
        Child.objects.create(parent=cls.many, amount=30, sequence=2)

    def source(self) -> QuerySet[Child]:
        return Child.objects.filter(parent_id=OuterRef("pk")).values(
            "amount", "sequence"
        )


@skipUnless(
    connection.vendor in {"postgresql", "oracle", "cockroachdb", "yugabyte", "DB2"}
    or (
        connection.vendor == "mysql"
        and not hasattr(type(connections["default"]), "tidb_version")
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
        joined = Parent.objects.alias(
            rows=DerivedRelation(source, lateral=True, join_type="inner")
        )
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
            rows=DerivedRelation(source, lateral=True),
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
        annotated = manager.annotate(
            rows=DerivedRelation(source, lateral=True), chosen=F("rows__amount")
        ).named("many")
        self.assertIsInstance(annotated.all(), CustomQuerySet)
        self.assertEqual([row.chosen for row in annotated.order_by("chosen")], [20, 30])
        with self.assertRaises(NotSupportedError):
            joined.update(minimum=1)
        with self.assertRaises(NotSupportedError):
            joined.delete()

    def test_annotate_registration_retains_unselected_join(self) -> None:
        joined = Parent.objects.annotate(
            rows=DerivedRelation(self.source(), lateral=True)
        )
        self.assertEqual(joined.count(), 4)
        self.assertEqual(joined.aggregate(total=Count("pk")), {"total": 4})
        instances = list(joined.order_by("name"))
        self.assertEqual(
            [row.name for row in instances], ["empty", "many", "many", "one"]
        )
        for row in instances:
            self.assertFalse(hasattr(row, "rows"))
            self.assertFalse(hasattr(row, "rows__amount"))

    def test_annotate_mixed_expressions_preserve_selection_and_order(self) -> None:
        source = Child.objects.filter(
            parent_id=OuterRef("pk"), amount__gte=OuterRef("floor")
        ).values("amount")
        annotated = Parent.objects.annotate(
            floor=F("minimum") + Value(15),
            rows=DerivedRelation(source, lateral=True),
            chosen=F("rows__amount"),
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
            rows=DerivedRelation(source, lateral=True),
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
        joined = Parent.objects.annotate(
            newest=DerivedRelation(newest, lateral=True),
            match=DerivedRelation(dependent, lateral=True),
            chosen=F("newest__amount"),
            sequence=F("match__sequence"),
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("name", "chosen", "sequence")),
            [("empty", None, None), ("many", 30, 2), ("one", 10, 1)],
        )

    def test_left_join_retains_cardinality_without_column_references(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).values("amount")
        parents = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(parents.count(), 4)
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
        parents = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
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
        joined = parents.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(list(joined.query.annotation_select), [])
        self.assertFalse(parents.query.derived_relations)
        self.assertEqual(source.query.alias_map, original.alias_map)
        self.assertIsNone(source._result_cache)
        self.assertFalse(hasattr(joined.first(), "rows__amount"))

    def test_inner_join_and_unreferenced_count(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(self.source(), lateral=True, join_type="inner")
        )
        self.assertEqual(joined.count(), 3)
        self.assertEqual(
            list(joined.order_by("name").values_list("name", flat=True)),
            ["many", "many", "one"],
        )

    def test_bare_model_source(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).order_by("-sequence")[
            :1
        ]
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
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
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(
            tuple(joined.query.annotations),
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
            rows=DerivedRelation(self.source(), lateral=True, condition=condition)
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("name", "rows__amount")),
            [("empty", None), ("many", 30), ("one", 10)],
        )
        filtered = Parent.objects.alias(
            rows=DerivedRelation(self.source(), lateral=True)
        )
        self.assertEqual(filtered.filter(condition).count(), 2)

    def test_boolean_on_expression(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(
                self.source(),
                lateral=True,
                condition=GreaterThan(F("rows__amount"), Value(25)),
            )
        )
        self.assertEqual(joined.filter(rows__amount__isnull=False).count(), 1)

    def test_on_expression_can_transform_its_own_columns(self) -> None:
        source = self.source().annotate(day=Value(date(2026, 10, 13))).values("day")
        joined = Parent.objects.alias(
            rows=DerivedRelation(
                source,
                lateral=True,
                condition=GreaterThan(F("rows__day__year"), Value(2020)),
            )
        )
        self.assertEqual(joined.count(), 4)
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
                rows=DerivedRelation(
                    source, lateral=True, condition=Q(rows__amount__gt=15)
                )
            )
            .filter(name="many")
        )
        sql, params = joined.query.sql_with_params()
        compiler = joined.query.get_compiler(connection=connection)
        label_params = compiler.compile(Value("select"))[1]
        mark_params = compiler.compile(Value("source"))[1]
        self.assertEqual(params, (*label_params, *mark_params, 5, 15, "many"))
        self.assertEqual(sql.count("%s"), len(params))
        self.assertEqual(joined.count(), 2)

    def test_empty_sources_and_false_true_on(self) -> None:
        for source in (
            Child.objects.none().values("amount"),
            self.source().filter(pk__in=[]),
        ):
            for kind, count in (("left", 3), ("inner", 0)):
                with self.subTest(kind=kind):
                    joined = Parent.objects.alias(
                        rows=DerivedRelation(source, lateral=True, join_type=kind)
                    )
                    self.assertEqual(joined.count(), count)
        for condition, count in (
            (Q(rows__amount__in=[]), 3),
            (~Q(rows__amount__in=[]), 4),
        ):
            joined = Parent.objects.alias(
                rows=DerivedRelation(self.source(), lateral=True, condition=condition)
            )
            self.assertEqual(joined.count(), count)

    def test_filter_order_aggregate_distinct_slice_and_clone(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        self.assertEqual(joined.aggregate(total=Sum("rows__amount")), {"total": 60})
        self.assertEqual(joined.values("name").distinct().count(), 3)
        self.assertEqual(joined.order_by("rows__amount")[:2].count(), 2)
        self.assertEqual(joined.filter(rows__amount__gt=15).count(), 2)
        self.assertEqual(joined.exclude(rows__amount=20).count(), 3)
        self.assertEqual(joined.count(), 4)
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
        joined = Parent.objects.alias(first=DerivedRelation(first, lateral=True)).alias(
            second=DerivedRelation(second, lateral=True)
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("second__sequence", flat=True)),
            [None, 2, 1],
        )

    def test_correlation_does_not_mutate_preceding_queryset_columns(self) -> None:
        original = Parent.objects.alias(
            first=DerivedRelation(self.source(), lateral=True)
        )
        source = Child.objects.filter(amount=OuterRef("first__amount")).values(
            "sequence"
        )
        original.alias(second=DerivedRelation(source, lateral=True))
        self.assertFalse(
            original.query.annotations["first__amount"].possibly_multivalued
        )

    def test_nested_subquery_and_exists(self) -> None:
        joined = Parent.objects.filter(pk=OuterRef("pk")).alias(
            rows=DerivedRelation(self.source(), lateral=True)
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
            rows=DerivedRelation(self.source(), lateral=True, join_type="inner")
        ).filter(Q(name="empty") | Q(rows__amount__gt=0))
        self.assertEqual(joined.count(), 3)

    def test_grouping_projected_primary_key_does_not_assume_table_constraints(
        self,
    ) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).values("id", "amount")
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        grouped = joined.values("rows__id", "rows__amount").annotate(n=Count("pk"))
        self.assertEqual(len(list(grouped)), 4)

    @skipUnlessDBFeature("can_distinct_on_fields")
    def test_distinct_on_projected_column(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        self.assertEqual(
            len(list(joined.order_by("rows__amount").distinct("rows__amount"))), 4
        )

    def test_correlated_relationship_dependency(self) -> None:
        source = Parent.objects.filter(name=OuterRef("parent__name")).values("minimum")
        joined = Child.objects.alias(info=DerivedRelation(source, lateral=True))
        self.assertEqual(
            list(joined.order_by("amount").values_list("info__minimum", flat=True)),
            [0, 0, 0],
        )

    def test_derived_alias_collision_with_base_table(self) -> None:
        alias = Parent._meta.db_table
        joined = Parent.objects.alias(
            **{alias: DerivedRelation(self.source(), lateral=True)}
        )
        self.assertEqual(joined.count(), 4)
        self.assertEqual(joined.filter(**{f"{alias}__amount__gt": 25}).count(), 1)

    def test_nested_outerref(self) -> None:
        source = (
            Child.objects.filter(parent_id=OuterRef(OuterRef("pk")))
            .order_by("sequence", "pk")
            .values("amount")
        )
        middle = (
            Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
            .order_by("pk", "rows__amount")
            .values("rows__amount")[:1]
        )
        parents = Parent.objects.annotate(amount=Subquery(middle))
        self.assertEqual(
            list(parents.order_by("name").values_list("amount", flat=True)),
            [None, 20, 10],
        )

    def test_distinct_definitions_and_repeated_descriptor(self) -> None:
        descriptor = DerivedRelation(self.source(), lateral=True)
        joined = Parent.objects.alias(a=descriptor, b=descriptor)
        self.assertEqual(joined.count(), 6)
        joined = Parent.objects.alias(
            a=descriptor,
            b=DerivedRelation(self.source().filter(amount__gt=25), lateral=True),
        )
        self.assertEqual(joined.count(), 4)
        self.assertEqual(joined.filter(b__amount=30).count(), 2)

    def test_null_rhs_in_negated_comparison(self) -> None:
        joined = Parent.objects.alias(
            a=DerivedRelation(self.source(), lateral=True),
            b=DerivedRelation(self.source().filter(amount__gt=25), lateral=True),
        )
        self.assertEqual(joined.exclude(a__amount=F("b__amount")).count(), 3)

    def test_negated_in_including_none(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        self.assertEqual(joined.exclude(rows__amount__in=[20, None]).count(), 2)

    def test_alias_cannot_shadow_registered_relation(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        with self.assertRaises(ValueError):
            joined.alias(rows=Value(1))

    def test_forward_reference_rejected(self) -> None:
        source = Child.objects.filter(amount=OuterRef("later__amount")).values("amount")
        with self.assertRaises(FieldError):
            Parent.objects.alias(first=DerivedRelation(source, lateral=True))

    def test_nonlateral_correlation_in_nested_derived_source_rejected(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef(OuterRef("pk"))).values(
            "amount"
        )
        middle = Parent.objects.alias(
            rows=DerivedRelation(source, lateral=True)
        ).values(amount=F("rows__amount"))
        with self.assertRaisesMessage(ValueError, "require lateral=True"):
            Parent.objects.alias(nested=DerivedRelation(middle))

    def test_mixed_alias_registration_order(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(self.source(), lateral=True),
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
                rows=DerivedRelation(
                    self.source(),
                    lateral=True,
                    condition=Q(rows__amount__gt=OuterRef("minimum")),
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
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(joined.aggregate(total=Sum("rows__amount")), {"total": 60})
        computed = self.source().annotate(double=F("amount") * 2).values("double")
        joined = Parent.objects.alias(rows=DerivedRelation(computed, lateral=True))
        self.assertEqual(joined.filter(rows__double__gte=40).count(), 2)

    @skipUnless(connection.vendor == "postgresql", "Uses an as_postgresql() override.")
    def test_source_vendor_compiler_dispatch(self) -> None:
        source = self.source()
        source.query.__class__ = PostgreSQLSourceQuery
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
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
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
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
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        self.assertNotEqual(
            joined.query.resolve_ref("rows__x"), joined.query.resolve_ref("rows__y")
        )
        self.assertEqual(
            joined.distinct().aggregate(x=Sum("rows__x"), y=Sum("rows__y")),
            {"x": 2, "y": 4},
        )

    def test_pickled_query_preserves_projection_constraints(self) -> None:
        source = Child.objects.filter(parent_id=OuterRef("pk")).values("id", "amount")
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        restored = Parent.objects.all()
        restored.query = pickle.loads(pickle.dumps(joined.query))
        self.assertEqual(restored.count(), 4)
        grouped = restored.values("rows__id", "rows__amount").annotate(n=Count("pk"))
        self.assertEqual(len(list(grouped)), 4)

    def test_projected_column_correlation(self) -> None:
        source = (
            Child.objects.filter(parent_id=OuterRef("pk"))
            .annotate(parent_name=OuterRef("name"))
            .values("parent_name")
        )
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
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
        # Django emits custom query aliases unquoted. Oracle and Db2 require an
        # alphabetic first character; MySQL still exercises the former
        # wrapper alias that could shadow a correlated outer table.
        renamed = (
            "DERIVED_SOURCE"
            if connection.vendor in {"oracle", "DB2"}
            else "__derived_source"
        )
        outer.query.change_aliases({outer_alias: renamed})
        joined = outer.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(joined.filter(rows__marker__year=2026).count(), 3)
        self.assertEqual(
            list(joined.order_by("pk").values_list("rows__marker", flat=True)),
            [None, date(2026, 10, 13), date(2026, 10, 13), date(2026, 10, 13)],
        )

    def test_outer_select_related_cannot_hydrate_derived_relation(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        with self.assertRaises(FieldError):
            joined.select_related("rows").query.sql_with_params()

    def test_queryset_boolean_combination_rejected(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        with self.assertRaises(NotSupportedError):
            joined | joined.filter(name="one")

    def test_outer_values_does_not_allow_model_field_collision(self) -> None:
        with self.assertRaises(ValueError):
            Parent.objects.values("pk").alias(
                name=DerivedRelation(self.source(), lateral=True)
            )

    def test_explicit_source_database_mismatch_rejected(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(self.source().using("other"), lateral=True)
        )
        with self.assertRaises(ValueError):
            joined.query.sql_with_params()

    def test_relation_predicates_cannot_hide_required_source(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        nested = Parent.objects.filter(pk__in=joined.values("pk"))
        self.assertEqual(nested.count(), 3)

    def test_select_related_source_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Parent.objects.alias(
                rows=DerivedRelation(Child.objects.select_related("parent"))
            )

    def test_unsupported_source_shapes(self) -> None:
        with self.assertRaises(NotSupportedError):
            Parent.objects.alias(
                rows=DerivedRelation(Child.objects.all().union(Child.objects.all()))
            )

    def test_reject_correlated_nonlateral_source(self) -> None:
        with self.assertRaisesMessage(ValueError, "require lateral=True"):
            Parent.objects.alias(rows=DerivedRelation(self.source()))

    def test_reject_duplicate_registration_and_unknown_columns(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        with self.assertRaisesMessage(ValueError, "already in use"):
            joined.alias(rows=DerivedRelation(self.source(), lateral=True))
        with self.assertRaises(FieldError):
            joined.filter(rows__unknown=1)

    def test_reject_writes(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        with self.assertRaises(NotSupportedError):
            joined.update(minimum=1)
        with self.assertRaises(NotSupportedError):
            joined.delete()


@skipUnless(
    connection.vendor
    in {
        "postgresql",
        "sqlite",
        "mysql",
        "oracle",
        "cockroachdb",
        "yugabyte",
        "firebird",
        "DB2",
    },
    "Requires ordinary derived joins.",
)
class OrdinaryDerivedRelationTests(DerivedRelationTestCase):
    def test_date_projection_preserves_null_extension(self) -> None:
        source = Child.objects.filter(amount=10).values(
            "parent_id", day=Cast(Value(date(2026, 10, 13)), models.DateField())
        )
        joined = Parent.objects.alias(
            rows=DerivedRelation(source, condition=Q(rows__parent_id=F("pk")))
        )
        self.assertEqual(joined.filter(rows__day__year=2026).count(), 1)
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__day", flat=True)),
            [None, None, date(2026, 10, 13)],
        )

    def test_constant_projection_preserves_null_extension(self) -> None:
        source = Child.objects.filter(amount=10).values("parent_id", marker=Value(7))
        joined = Parent.objects.alias(
            rows=DerivedRelation(source, condition=Q(rows__parent_id=F("pk")))
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
                    rows=DerivedRelation(
                        source,
                        condition=Q(rows__parent_id=F("pk")),
                        join_type=kind,
                    )
                )
                self.assertEqual(joined.count(), expected)
                self.assertEqual(
                    joined.aggregate(total=Count("pk")), {"total": expected}
                )
                self.assertIsNone(source._result_cache)

    def test_empty_sources_preserve_left_rows(self) -> None:
        for source in (
            Child.objects.none().values("amount"),
            Child.objects.filter(pk__in=[]).values("amount"),
            Child.objects.values("amount")[:0],
        ):
            for kind, expected in (("left", [None, None, None]), ("inner", [])):
                with self.subTest(kind=kind, source=source.query.is_empty()):
                    joined = Parent.objects.alias(
                        rows=DerivedRelation(source, join_type=kind)
                    )
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
                    rows=DerivedRelation(
                        source, condition=Q(rows__parent_id=F("pk")) & condition
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
        joined = Parent.objects.alias(rows=DerivedRelation(source))
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__amount", "rows__sequence")),
            [(30, 2), (30, 2), (30, 2)],
        )

    def test_selected_annotations_and_bare_columns(self) -> None:
        source = Child.objects.filter(amount=10).annotate(doubled=F("amount") * 2)
        joined = Parent.objects.alias(
            rows=DerivedRelation(source, condition=Q(rows__parent_id=F("pk")))
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__amount", "rows__doubled")),
            [(None, None), (None, None), (10, 20)],
        )

    def test_nested_ordinary_sources(self) -> None:
        first = Child.objects.values("parent_id", "amount")
        source = Parent.objects.alias(
            lines=DerivedRelation(first, condition=Q(lines__parent_id=F("pk")))
        ).values(parent_name=F("name"), amount=F("lines__amount"))
        joined = Parent.objects.alias(
            rows=DerivedRelation(source, condition=Q(rows__parent_name=F("name")))
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
            rows=DerivedRelation(
                source, condition=Q(rows__parent_id=F("pk"), rows__mark="from-source")
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
    @transaction.atomic
    def test_source_locking_uses_native_backend_compiler(self) -> None:
        source = Child.objects.values("id", "parent_id").select_for_update()
        joined = Parent.objects.alias(
            rows=DerivedRelation(source, condition=Q(rows__parent_id=F("pk")))
        )
        self.assertTrue(joined._for_write)
        self.assertFalse(joined.query.select_for_update)
        if connection.vendor == "DB2":
            with self.assertRaisesMessage(NotSupportedError, "source-only row locks"):
                joined.count()
            return
        self.assertIn(
            connection.ops.for_update_sql(), joined.query.sql_with_params()[0]
        )
        if connection.vendor in {"oracle", "firebird"}:
            # These engines permit FOR UPDATE only in a top-level SELECT. Preserve
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
            if connection.vendor == "oracle":
                self.assertEqual(
                    derived_error.exception.args[0].code,
                    native_error.exception.args[0].code,
                )
            else:
                self.assertEqual(
                    derived_error.exception.__cause__.sqlcode,
                    native_error.exception.__cause__.sqlcode,
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
                relation = DerivedRelation(
                    source,
                    join_type=join_type,
                    condition=Q(rows__parent_id=F("pk"), rows__amount__gt=20),
                )
                for register in (Parent.objects.alias, Parent.objects.annotate):
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
                        Parent.objects.alias(rows=DerivedRelation(source))
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
                    rows=DerivedRelation(source, join_type="inner")
                )
                self.assertEqual(
                    tuple(joined.query.annotations),
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
                    rows=DerivedRelation(source.filter(parent=self.one))
                )
                self.assertEqual(tuple(joined.query.annotations), ("rows__sequence",))
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
                    Parent.objects.alias(rows=DerivedRelation(source))

    def test_inherited_deferred_source_preserves_parent_columns(self) -> None:
        child = InheritedChild.objects.create(
            parent=self.one, amount=40, sequence=3, label="extra"
        )
        source = InheritedChild.objects.only("amount", "label").filter(pk=child.pk)
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=DerivedRelation(source)
        )
        self.assertEqual(
            tuple(joined.query.annotations),
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
            rows=DerivedRelation(source)
        )
        self.assertEqual(
            tuple(joined.query.annotations),
            ("rows__parent_id", "rows__amount", "rows__sequence"),
        )
        self.assertEqual(
            list(
                joined.values_list("rows__parent_id", "rows__amount", "rows__sequence")
            ),
            [(self.one.pk, 40, 3)],
        )
        with self.assertRaisesMessage(ValueError, "individually named columns"):
            Parent.objects.alias(rows=DerivedRelation(source.values("pk")))

    def test_ordinary_grouped_source(self) -> None:
        source = Child.objects.values("parent_id").annotate(total=Sum("amount"))
        joined = Parent.objects.alias(
            totals=DerivedRelation(source, condition=Q(totals__parent_id=F("pk")))
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
        left = Parent.objects.alias(rows=DerivedRelation(source))
        inner = Parent.objects.alias(rows=DerivedRelation(source, join_type="inner"))
        self.assertEqual(inner.count(), 0)
        self.assertEqual(
            list(left.values_list("rows__total", flat=True)), [None, None, None]
        )

    def test_on_relationship_dependency(self) -> None:
        source = Parent.objects.values("name", "minimum")
        joined = Child.objects.alias(
            info=DerivedRelation(source, condition=Q(info__name=F("parent__name")))
        )
        self.assertEqual(joined.count(), 3)

    def test_null_rhs_from_ordinary_outer_join(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(Child.objects.values("amount"), join_type="inner")
        )
        self.assertEqual(joined.exclude(rows__amount=F("children__amount")).count(), 9)


@skipUnless(
    connection.vendor == "mysql"
    and hasattr(type(connections["default"]), "tidb_version")
    and connection.tidb_version >= (8, 5, 7),
    "Requires TiDB 8.5.7+ inner LATERAL joins.",
)
class TiDBInnerDerivedRelationTests(DerivedRelationTestCase):
    def joined(
        self, source: QuerySet[Child], condition: Q | None = None
    ) -> QuerySet[Parent]:
        return Parent.objects.alias(
            rows=DerivedRelation(
                source, lateral=True, join_type="inner", condition=condition
            )
        )

    def test_values_and_unused_join_cardinality(self) -> None:
        joined = self.joined(self.source())
        self.assertEqual(joined.count(), 3)
        self.assertEqual(
            list(
                joined.order_by("name", "rows__amount").values_list(
                    "name", "rows__amount"
                )
            ),
            [("many", 20), ("many", 30), ("one", 10)],
        )
        self.assertEqual(
            joined.filter(Q(name="empty") | Q(rows__amount__gt=0)).count(), 3
        )

    def test_ordered_slice_precedes_on_condition(self) -> None:
        source = self.source().order_by("-sequence", "pk")[:1]
        joined = self.joined(source, Q(rows__amount__lt=25))
        self.assertEqual(
            list(joined.values_list("name", "rows__amount")), [("one", 10)]
        )

    def test_empty_sources(self) -> None:
        for source in (
            self.source().none(),
            self.source().filter(pk__in=[]),
            self.source().filter(amount__gt=1000),
            self.source()[:0],
        ):
            with self.subTest(source_type=type(source.query).__name__):
                self.assertEqual(self.joined(source).count(), 0)

    def test_source_grouping_and_outer_aggregation(self) -> None:
        source = (
            self.source()
            .values("parent_id")
            .annotate(total=Sum("amount"))
            .values("total")
        )
        joined = self.joined(source)
        self.assertEqual(
            list(joined.order_by("name").values_list("name", "rows__total")),
            [("many", 50), ("one", 10)],
        )
        self.assertEqual(joined.aggregate(total=Sum("rows__total")), {"total": 60})
        grouped = (
            self.joined(self.source())
            .values("name")
            .annotate(total=Sum("rows__amount"))
        )
        self.assertEqual(
            list(grouped.order_by("name").values_list("name", "total")),
            [("many", 50), ("one", 10)],
        )

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
                rows=DerivedRelation(
                    source,
                    lateral=True,
                    join_type="inner",
                    condition=Q(rows__amount__gt=15),
                )
            )
            .filter(name="many")
        )
        self.assertEqual(
            joined.query.sql_with_params()[1], ("select", "source", 5, 15, "many")
        )
        self.assertEqual(
            list(
                joined.order_by("rows__amount").values_list(
                    "label", "rows__mark", "rows__amount"
                )
            ),
            [("select", "source", 20), ("select", "source", 30)],
        )

    def test_outer_reference_projection(self) -> None:
        joined = self.joined(self.source().values(parent_name=OuterRef("name")))
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__parent_name", flat=True)),
            ["many", "many", "one"],
        )

    def test_repeated_and_dependent_sources(self) -> None:
        descriptor = DerivedRelation(self.source(), lateral=True, join_type="inner")
        repeated = Parent.objects.alias(a=descriptor, b=descriptor)
        self.assertEqual(repeated.count(), 5)
        dependent = Parent.objects.alias(
            a=descriptor,
            b=DerivedRelation(
                Child.objects.filter(amount=OuterRef("a__amount")).values("amount"),
                lateral=True,
                join_type="inner",
            ),
        )
        self.assertEqual(
            list(dependent.order_by("b__amount").values_list("b__amount", flat=True)),
            [10, 20, 30],
        )

    def test_nested_subquery_and_exists(self) -> None:
        joined = self.joined(self.source()).filter(pk=OuterRef("pk"))
        parents = Parent.objects.annotate(
            amount=Subquery(joined.order_by("rows__amount").values("rows__amount")[:1]),
            found=Exists(joined),
        )
        self.assertEqual(
            list(parents.order_by("name").values_list("amount", "found")),
            [(None, False), (20, True), (10, True)],
        )

    def test_computed_null_projection(self) -> None:
        source = self.source().values(
            total=Subquery(Child.objects.filter(pk=-1).values("amount"))
        )
        self.assertEqual(self.joined(source).exclude(rows__total=1).count(), 3)


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
                patch.dict(
                    connections["default"].__dict__,
                    mysql_version=version,
                    mysql_is_mariadb=mariadb,
                ),
                patch.object(django_derived_relation, "_is_tidb", return_value=False),
            ):
                joined = Parent.objects.alias(
                    rows=DerivedRelation(Child.objects.values("amount"), lateral=True)
                )
                if supported:
                    self.assertIn(" LATERAL ", joined.query.sql_with_params()[0])
                else:
                    with self.assertRaisesMessage(NotSupportedError, "LATERAL"):
                        joined.query.sql_with_params()

    def test_tidb_lateral_version_and_join_type(self) -> None:
        with (
            patch.object(connection, "vendor", "mysql"),
            patch.dict(connections["default"].__dict__, mysql_version=(8, 0, 14)),
            patch.object(
                type(connections["default"]), "tidb_version", (8, 5, 3), create=True
            ),
        ):
            self.assertTrue(django_derived_relation._is_tidb(connections["default"]))
            joined = Parent.objects.alias(
                rows=DerivedRelation(Child.objects.values("amount"))
            )
            self.assertNotIn("NO_MERGE", joined.query.sql_with_params()[0])
            for version in ((8, 5, 6), (8, 5, 7), (8, 5, 8)):
                for kind in ("inner", "left"):
                    with (
                        self.subTest(version=version, kind=kind),
                        patch.dict(
                            connections["default"].__dict__, tidb_version=version
                        ),
                    ):
                        lateral = Parent.objects.alias(
                            rows=DerivedRelation(
                                Child.objects.values("amount"),
                                lateral=True,
                                join_type=kind,
                            )
                        )
                        if version >= (8, 5, 7) and kind == "inner":
                            self.assertIn(
                                " LATERAL ", lateral.query.sql_with_params()[0]
                            )
                        else:
                            with self.assertRaisesMessage(NotSupportedError, "LATERAL"):
                                lateral.query.sql_with_params()

    def test_firebird_rejects_lateral(self) -> None:
        with patch.object(connection, "vendor", "firebird"):
            joined = Parent.objects.alias(
                rows=DerivedRelation(Child.objects.values("amount"), lateral=True)
            )
            with self.assertRaisesMessage(NotSupportedError, "LATERAL"):
                joined.query.sql_with_params()

    def test_db2_rejects_source_locks_before_backend_relocates_them(self) -> None:
        source = Child.objects.select_for_update().values("id")
        joined = Parent.objects.alias(rows=DerivedRelation(source))
        with patch.object(connection, "vendor", "DB2"):
            with self.assertRaisesMessage(NotSupportedError, "source-only row locks"):
                joined.query.sql_with_params()

    def test_backend_can_disable_derived_joins(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(Child.objects.values("id")))
        with patch.object(
            connection.features, "supports_derived_joins", False, create=True
        ):
            with self.assertRaisesMessage(NotSupportedError, "DerivedRelation"):
                joined.query.sql_with_params()

    def test_backend_can_disable_lateral_joins(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(Child.objects.values("id"), lateral=True)
        )
        with patch.object(
            connection.features, "supports_lateral_joins", False, create=True
        ):
            with self.assertRaisesMessage(NotSupportedError, "LATERAL"):
                joined.query.sql_with_params()

    def test_annotation_only_source_does_not_export_unselected_fields(self) -> None:
        source = Child.objects.values(renamed=F("amount"))
        joined = Parent.objects.alias(rows=DerivedRelation(source))
        self.assertEqual(
            joined.query.resolve_ref("rows__renamed").output_field.get_internal_type(),
            "IntegerField",
        )
        for field in ("id", "parent_id", "amount", "sequence"):
            with self.subTest(field=field), self.assertRaises(FieldError):
                joined.values(f"rows__{field}")

    def test_registration_rejects_model_field_collisions(self) -> None:
        relation = DerivedRelation(Child.objects.values("amount"))
        for queryset, names in (
            (Parent.objects.all(), ("name", "pk", "id")),
            (Parent.objects.values("pk"), ("name", "pk", "id")),
            (Child.objects.all(), ("parent", "parent_id")),
        ):
            for method in ("alias", "annotate"):
                for name in names:
                    with self.subTest(model=queryset.model, method=method, name=name):
                        with self.assertRaisesMessage(
                            ValueError, "conflicts with a model field"
                        ):
                            getattr(queryset, method)(**{name: relation})

    def test_registration_rejects_existing_expression_names(self) -> None:
        relation = DerivedRelation(Child.objects.values("amount"))
        for existing_method in ("alias", "annotate"):
            original = getattr(Parent.objects, existing_method)(rows=Value(7))
            for method in ("alias", "annotate"):
                with self.subTest(existing=existing_method, method=method):
                    with self.assertRaisesMessage(ValueError, "already in use"):
                        getattr(original, method)(rows=relation)
                    self.assertEqual(original.query.annotations["rows"].value, 7)
                    self.assertFalse(original.query.derived_relations)

    def test_registration_rejects_earlier_expression_in_same_call(self) -> None:
        relation = DerivedRelation(Child.objects.values("amount"))
        for method in ("alias", "annotate"):
            with self.subTest(method=method):
                with self.assertRaisesMessage(ValueError, "conflict with aliases"):
                    getattr(Parent.objects, method)(
                        **{"rows__amount": Value(7), "rows": relation}
                    )

    def test_import_rejected_when_django_provides_derived_relation(self) -> None:
        with patch.object(models, "DerivedRelation", object(), create=True):
            with self.assertRaisesMessage(
                ImportError, "Use django.db.models.DerivedRelation"
            ):
                runpy.run_path(django_derived_relation.__file__)

    def test_empty_relation_and_projection_names_rejected(self) -> None:
        with self.assertRaisesMessage(ValueError, "nonempty"):
            Parent.objects.alias(
                **{"": DerivedRelation(Child.objects.values("amount"))}
            )
        with self.assertRaisesMessage(ValueError, "nonempty"):
            Parent.objects.alias(
                rows=DerivedRelation(Child.objects.values(**{"": F("amount")}))
            )

    def test_source_and_argument_validation(self) -> None:
        with self.assertRaises(TypeError):
            DerivedRelation(Value(1))
        with self.assertRaises(ValueError):
            DerivedRelation(Child.objects.all(), join_type="right")
        with self.assertRaises(TypeError):
            DerivedRelation(Child.objects.all(), condition=Value(1))
        relation = DerivedRelation(Child.objects.all())
        with self.assertRaises(TypeError):
            Parent.objects.annotate(relation)
        with self.assertRaisesMessage(TypeError, "registered"):
            Parent.objects.annotate(value=F("minimum") + relation)

    def test_unsupported_backend(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(Child.objects.values("amount"))
        )
        with (
            patch.object(connection, "vendor", "unknown"),
            self.assertRaises(NotSupportedError),
        ):
            joined.query.sql_with_params()
