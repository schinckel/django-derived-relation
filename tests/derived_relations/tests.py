import pickle
import runpy
from datetime import date
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import FieldError
from django.db import NotSupportedError, connection, models
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
from django.db.models.lookups import GreaterThan
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.query import Query
from django.test import SimpleTestCase, TestCase

import derived_relation_backport
from derived_relation_backport import DerivedRelation

from .models import Child, Parent


class PostgreSQLSourceQuery(Query):
    def as_postgresql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        query = self.clone()
        query.add_q(Q(amount__gte=30))
        sql, params = query.as_sql(compiler, connection)
        return sql, tuple(params)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL derived joins.")
class DerivedRelationTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.empty = Parent.objects.create(name="empty")
        cls.one = Parent.objects.create(name="one")
        cls.many = Parent.objects.create(name="many")
        Child.objects.create(parent=cls.one, amount=10, sequence=1)
        Child.objects.create(parent=cls.many, amount=20, sequence=1)
        Child.objects.create(parent=cls.many, amount=30, sequence=2)

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

    def source(self) -> QuerySet[Child]:
        return Child.objects.filter(parent_id=OuterRef("pk")).values(
            "amount", "sequence"
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

    def test_ordinary_grouped_source(self) -> None:
        source = Child.objects.values("parent_id").annotate(total=Sum("amount"))
        joined = Parent.objects.alias(
            totals=DerivedRelation(source, condition=Q(totals__parent_id=F("pk")))
        )
        self.assertEqual(
            list(joined.order_by("name").values_list("totals__total", flat=True)),
            [None, 50, 10],
        )

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

    def test_on_relationship_dependency(self) -> None:
        source = Parent.objects.values("name", "minimum")
        joined = Child.objects.alias(
            info=DerivedRelation(source, condition=Q(info__name=F("parent__name")))
        )
        self.assertEqual(joined.count(), 3)

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

    def test_null_rhs_from_ordinary_outer_join(self) -> None:
        joined = Parent.objects.alias(
            rows=DerivedRelation(Child.objects.values("amount"), join_type="inner")
        )
        self.assertEqual(joined.exclude(rows__amount=F("children__amount")).count(), 9)

    def test_negated_in_including_none(self) -> None:
        joined = Parent.objects.alias(rows=DerivedRelation(self.source(), lateral=True))
        self.assertEqual(joined.exclude(rows__amount__in=[20, None]).count(), 2)

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
            list(joined.order_by("selected").values_list("selected", flat=True)),
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

    def test_source_vendor_compiler_dispatch(self) -> None:
        source = self.source()
        source.query.__class__ = PostgreSQLSourceQuery
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(joined.count(), 3)
        self.assertEqual(joined.filter(rows__amount__isnull=False).count(), 1)

    def test_column_types_and_transforms(self) -> None:
        source = (
            self.source()
            .annotate(day=Value(date(2026, 10, 13)), price=Value(Decimal("1.25")))
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
            list(joined.order_by("name").values_list("rows__parent_name", flat=True)),
            [None, "many", "many", "one"],
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

    def test_unsupported_source_shapes(self) -> None:
        sources = (
            Child.objects.select_related("parent"),
            Child.objects.only("amount"),
            Child.objects.defer("amount"),
        )
        for source in sources:
            with self.subTest(source=source.query.deferred_loading):
                with self.assertRaises(ValueError):
                    Parent.objects.alias(rows=DerivedRelation(source))
        with self.assertRaises(NotSupportedError):
            Parent.objects.alias(
                rows=DerivedRelation(Child.objects.all().union(Child.objects.all()))
            )
        with self.assertRaises(NotSupportedError):
            Parent.objects.alias(
                rows=DerivedRelation(Child.objects.select_for_update())
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


class DerivedRelationValidationTests(SimpleTestCase):
    def test_import_rejected_when_django_provides_derived_relation(self) -> None:
        with patch.object(models, "DerivedRelation", object(), create=True):
            with self.assertRaisesMessage(
                ImportError, "Use django.db.models.DerivedRelation"
            ):
                runpy.run_path(derived_relation_backport.__file__)

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
        with self.assertRaisesMessage(TypeError, "alias()"):
            Parent.objects.annotate(rows=DerivedRelation(Child.objects.all()))

    def test_unsupported_backend(self) -> None:
        if getattr(
            connection.features,
            "supports_derived_joins",
            connection.vendor == "postgresql",
        ):
            self.skipTest("Requires a backend without derived-join support.")
        joined = Parent.objects.alias(
            rows=DerivedRelation(Child.objects.values("amount"))
        )
        with self.assertRaises(NotSupportedError):
            joined.query.sql_with_params()
