import pickle
from unittest import skipUnless

from django.core.exceptions import FieldError
from django.db import DatabaseError, NotSupportedError, connection, models, transaction
from django.db.models import F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.test import TestCase

from django_derived_relation import DerivedRelation

from .models import Child, CompositeChild, Parent


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL derived joins.")
class CombinedSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.empty = Parent.objects.create(name="empty", minimum=0)
        cls.one = Parent.objects.create(name="one", minimum=10)
        cls.many = Parent.objects.create(name="many", minimum=30)
        Child.objects.create(parent=cls.one, amount=10, sequence=1)
        Child.objects.create(parent=cls.many, amount=20, sequence=1)
        Child.objects.create(parent=cls.many, amount=30, sequence=2)

    def test_set_operations_keep_first_projection_and_cardinality(self) -> None:
        children = Child.objects.values(x=F("amount"))
        parents = Parent.objects.values(another_name=F("minimum"))
        for source, expected in (
            (children.union(parents), [0, 10, 20, 30]),
            (children.union(parents, all=True), [0, 10, 10, 20, 30, 30]),
            (children.intersection(parents), [10, 30]),
            (children.difference(parents), [20]),
        ):
            with self.subTest(
                operator=source.query.combinator, all=source.query.combinator_all
            ):
                joined = Parent.objects.alias(rows=DerivedRelation(source))
                self.assertEqual(joined.count(), 3 * len(expected))
                self.assertEqual(
                    list(
                        joined.filter(pk=self.one.pk)
                        .order_by("rows__x")
                        .values_list("rows__x", flat=True)
                    ),
                    expected,
                )

    def test_union_on_condition_preserves_unmatched_outer_rows(self) -> None:
        source = Child.objects.values(x=F("amount")).union(
            Child.objects.filter(amount=30).values(other=F("amount"))
        )
        for join_type, expected in (
            ("left", [("empty", None), ("many", 30), ("one", 10)]),
            ("inner", [("many", 30), ("one", 10)]),
        ):
            with self.subTest(join_type=join_type):
                joined = Parent.objects.alias(
                    rows=DerivedRelation(
                        source,
                        join_type=join_type,
                        condition=Q(rows__x=F("minimum")),
                    )
                )
                self.assertEqual(
                    list(joined.order_by("name").values_list("name", "rows__x")),
                    expected,
                )

    def test_union_preserves_positional_columns_and_ordered_slice(self) -> None:
        first = Child.objects.values(x=F("amount"), y=F("sequence"))
        second = Parent.objects.values(other=F("minimum"), fixed=Value(9))
        source = first.union(second).order_by("-x", "-y")[:2]
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=DerivedRelation(source)
        )
        self.assertEqual(
            list(joined.order_by("-rows__y").values_list("rows__x", "rows__y")),
            [(30, 9), (30, 2)],
        )

    def test_bare_model_union_has_named_columns(self) -> None:
        source = Child.objects.all().union(Child.objects.filter(sequence=1))
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=DerivedRelation(source)
        )
        self.assertEqual(
            list(
                joined.order_by("rows__amount").values_list("rows__amount", flat=True)
            ),
            [10, 20, 30],
        )

    def test_deferred_operands_preserve_native_projection_and_inputs(self) -> None:
        for first, later, names in (
            (
                Child.objects.only("amount"),
                Child.objects.defer("parent", "sequence"),
                ("id", "amount"),
            ),
            (
                Child.objects.only("parent__name"),
                Child.objects.only("parent_id"),
                ("id", "parent_id"),
            ),
            (
                Child.objects.defer("id"),
                Child.objects.all(),
                ("id", "parent_id", "amount", "sequence"),
            ),
        ):
            for nested in (False, True):
                with self.subTest(names=names, nested=nested):
                    operand = later.union(later) if nested else later
                    source = first.union(operand)
                    before = tuple(
                        pickle.dumps(queryset.query)
                        for queryset in (first, later, source)
                    )
                    joined = Parent.objects.filter(pk=self.one.pk).alias(
                        rows=DerivedRelation(source)
                    )
                    self.assertEqual(
                        tuple(joined.query.annotations),
                        tuple(f"rows__{name}" for name in names),
                    )
                    self.assertEqual(
                        list(
                            joined.order_by("rows__id").values_list(
                                *(f"rows__{name}" for name in names)
                            )
                        ),
                        list(Child.objects.order_by("id").values_list(*names)),
                    )
                    self.assertEqual(
                        tuple(
                            pickle.dumps(queryset.query)
                            for queryset in (first, later, source)
                        ),
                        before,
                    )

    def test_later_deferred_width_mismatch_keeps_native_database_error(self) -> None:
        for branch in (Child.objects.only("amount"), Child.objects.defer("sequence")):
            for nested in (False, True):
                with self.subTest(shape=branch.query.deferred_loading, nested=nested):
                    operand = Child.objects.all().union(branch) if nested else branch
                    source = Child.objects.all().union(operand)
                    with self.assertRaises(DatabaseError), transaction.atomic():
                        list(source)
                    joined = Parent.objects.alias(rows=DerivedRelation(source))
                    with self.assertRaises(DatabaseError), transaction.atomic():
                        list(joined.values_list("rows__amount", flat=True))

    @skipUnless(
        hasattr(models, "CompositePrimaryKey"), "Requires composite primary keys."
    )
    def test_composite_primary_key_deferred_operands_keep_components(self) -> None:
        CompositeChild.objects.create(parent=self.one, amount=40, sequence=3)
        first = CompositeChild.objects.only("amount")
        later = CompositeChild.objects.only("amount").filter(parent=self.one)
        source = first.union(later.union(later))
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

    def test_explicit_combined_values_normalize_bare_operands(self) -> None:
        base = Child.objects.all()
        for source in (
            base.values("amount").union(base),
            base.union(base).values("amount"),
            base.values("amount").union(base.defer("amount")),
            base.only("sequence").union(base.defer("amount")).values("amount"),
        ):
            with self.subTest(source=getattr(source.query, "selected", None)):
                joined = Parent.objects.filter(pk=self.one.pk).alias(
                    rows=DerivedRelation(source)
                )
                self.assertEqual(
                    list(
                        joined.order_by("rows__amount").values_list(
                            "rows__amount", flat=True
                        )
                    ),
                    [10, 20, 30],
                )

    def test_later_deferred_columns_preserve_positional_selection(self) -> None:
        first = Child.objects.only("amount")
        later = Child.objects.only("sequence")
        source = first.union(later.union(later))
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=DerivedRelation(source)
        )
        self.assertEqual(
            list(
                joined.order_by("rows__amount").values_list("rows__amount", flat=True)
            ),
            [1, 1, 2, 10, 20, 30],
        )

    def test_native_compound_ordering_adds_private_columns(self) -> None:
        base = Child.objects.all()
        for source, columns, expected in (
            (
                base.only("amount")
                .union(base.only("amount"))
                .values("amount")
                .order_by("-sequence")[:1],
                ("amount",),
                [30],
            ),
            (
                base.union(base)
                .values("amount")
                .order_by((F("amount") + 1).desc())[:1],
                ("amount",),
                [30],
            ),
            (
                base.union(base).values("amount").order_by("-parent__name")[:1],
                ("amount",),
                [10],
            ),
            (
                base.union(base).values("amount").order_by("-sequence")[:1],
                ("amount",),
                [30],
            ),
        ):
            with self.subTest(ordering=source.query.order_by, columns=columns):
                before = pickle.dumps(source.query)
                native = source._chain()
                native.query = pickle.loads(before)
                self.assertEqual(
                    [
                        row["amount"] if isinstance(row, dict) else row.amount
                        for row in native
                    ],
                    expected,
                )
                joined = Parent.objects.filter(pk=self.one.pk).alias(
                    rows=DerivedRelation(source)
                )
                stored = pickle.loads(pickle.dumps(joined.query))
                self.assertEqual(
                    list(joined.values_list("rows__amount", flat=True)), expected
                )
                self.assertEqual(
                    list(joined.values_list("rows__amount", flat=True)), expected
                )
                self.assertEqual(
                    tuple(joined.query.annotations),
                    tuple(f"rows__{name}" for name in columns),
                )
                with self.assertRaises(FieldError):
                    joined.values(
                        "rows__sequence"
                        if "sequence" not in columns
                        else "rows____orderbycol1"
                    )
                original_source = stored.alias_map["rows"].source
                current_source = joined.query.alias_map["rows"].source
                for original, current in zip(
                    (original_source, *original_source.combined_queries),
                    (current_source, *current_source.combined_queries),
                    strict=True,
                ):
                    self.assertEqual(
                        getattr(current, "selected", None),
                        getattr(original, "selected", None),
                    )
                    self.assertEqual(
                        tuple(current.annotations), tuple(original.annotations)
                    )
                    self.assertEqual(current.table_map, original.table_map)
                original_query = pickle.loads(before)
                for original, current in zip(
                    (original_query, *original_query.combined_queries),
                    (source.query, *source.query.combined_queries),
                    strict=True,
                ):
                    self.assertEqual(
                        getattr(current, "selected", None),
                        getattr(original, "selected", None),
                    )
                    self.assertEqual(
                        tuple(current.annotations), tuple(original.annotations)
                    )
                    self.assertEqual(current.table_map, original.table_map)
                    self.assertEqual(
                        current.deferred_loading, original.deferred_loading
                    )
                    self.assertEqual(current.order_by, original.order_by)
                restored = Parent.objects.all()
                restored.query = pickle.loads(pickle.dumps(joined.query))
                self.assertEqual(
                    list(restored.values_list("rows__amount", flat=True)), expected
                )

    def test_explicit_operand_projection_rejects_private_ordering(self) -> None:
        base = Child.objects.values("amount")
        source = base.union(base).order_by("sequence")
        with self.assertRaises(DatabaseError):
            list(source)
        joined = Parent.objects.alias(rows=DerivedRelation(source))
        with self.assertRaises(DatabaseError):
            list(joined.values_list("rows__amount", flat=True))

    def test_known_empty_branches_keep_left_join_rows(self) -> None:
        empty = Child.objects.filter(pk__in=[]).values("amount")
        full = Child.objects.values("amount")
        for source, expected in (
            (empty.union(full), [10, 20, 30]),
            (full.union(empty), [10, 20, 30]),
            (empty.union(empty), [None]),
            (full.intersection(empty), [None]),
            (empty.difference(full), [None]),
        ):
            with self.subTest(
                operator=source.query.combinator, empty=source.query.is_empty()
            ):
                joined = Parent.objects.filter(pk=self.one.pk).alias(
                    rows=DerivedRelation(source)
                )
                self.assertEqual(
                    list(
                        joined.order_by("rows__amount").values_list(
                            "rows__amount", flat=True
                        )
                    ),
                    expected,
                )

    def test_correlated_later_branch_requires_lateral(self) -> None:
        empty = Child._base_manager.filter(pk__in=[]).values("amount")
        correlated = Child._base_manager.filter(parent_id=OuterRef("pk")).values(
            "amount"
        )
        source = empty.union(correlated)
        with self.assertRaisesMessage(ValueError, "require lateral=True"):
            Parent.objects.alias(rows=DerivedRelation(source))
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(
            list(
                joined.order_by("name", "rows__amount").values_list(
                    "name", "rows__amount"
                )
            ),
            [("empty", None), ("many", 20), ("many", 30), ("one", 10)],
        )

    def test_nested_compound_correlation_requires_lateral(self) -> None:
        empty = Child._base_manager.filter(pk__in=[]).values("amount")
        correlated = Child._base_manager.filter(parent_id=OuterRef("pk")).values(
            "amount"
        )
        source = empty.union(empty.union(correlated))
        with self.assertRaisesMessage(ValueError, "require lateral=True"):
            Parent.objects.alias(rows=DerivedRelation(source))
        joined = Parent.objects.alias(rows=DerivedRelation(source, lateral=True))
        self.assertEqual(joined.count(), 4)
        self.assertEqual(
            joined.filter(name="one").values_list("rows__amount", flat=True).get(), 10
        )

    def test_union_branch_with_derived_source_preserves_correlation(self) -> None:
        inner = Child._base_manager.filter(parent_id=OuterRef("pk")).values("amount")
        branch = Parent.objects.alias(
            detail=DerivedRelation(inner, lateral=True)
        ).values(x=F("detail__amount"))
        source = branch.union(Parent.objects.values(x=F("minimum")))
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=DerivedRelation(source)
        )
        self.assertEqual(
            list(joined.order_by("rows__x").values_list("rows__x", flat=True)),
            [0, 10, 20, 30, None],
        )

    def test_union_inside_nested_scalar_subquery(self) -> None:
        empty = Child._base_manager.filter(pk__in=[]).values("amount")
        correlated = Child._base_manager.filter(
            parent_id=OuterRef(OuterRef("pk"))
        ).values("amount")
        middle = (
            Parent.objects.alias(
                rows=DerivedRelation(empty.union(correlated), lateral=True)
            )
            .order_by("rows__amount")
            .values("rows__amount")[:1]
        )
        parents = Parent.objects.annotate(chosen=Subquery(middle)).order_by("name")
        self.assertEqual(list(parents.values_list("chosen", flat=True)), [None, 20, 10])

    def test_union_parameter_order_and_input_immutability(self) -> None:
        first = Child.objects.filter(amount__gte=10).values(x=F("amount") + Value(1))
        second = Parent.objects.filter(minimum__gte=10).values(
            x=F("minimum") + Value(2)
        )
        source = first.union(second)
        before = tuple(
            queryset.query.sql_with_params() for queryset in (first, second, source)
        )
        joined = Parent.objects.filter(name="one").alias(
            rows=DerivedRelation(source, condition=Q(rows__x__gt=15))
        )
        self.assertEqual(joined.query.sql_with_params()[1], (1, 10, 2, 10, 15, "one"))
        self.assertEqual(
            list(joined.order_by("rows__x").values_list("rows__x", flat=True)),
            [21, 31, 32],
        )
        self.assertEqual(
            tuple(
                queryset.query.sql_with_params() for queryset in (first, second, source)
            ),
            before,
        )
        restored = Parent.objects.all()
        restored.query = pickle.loads(pickle.dumps(joined.query))
        self.assertEqual(
            list(restored.order_by("rows__x").values_list("rows__x", flat=True)),
            [21, 31, 32],
        )

    def test_elided_first_union_branch_keeps_output_names(self) -> None:
        first = Child.objects.filter(pk__in=[]).values(x=F("amount"))
        second = Parent.objects.values(other=F("minimum"))
        for all_rows in (False, True):
            for sliced in (False, True):
                with self.subTest(all=all_rows, sliced=sliced):
                    source = first.union(second, all=all_rows).order_by("x")
                    if sliced:
                        source = source[:2]
                    joined = Parent.objects.filter(pk=self.one.pk).alias(
                        rows=DerivedRelation(source)
                    )
                    self.assertEqual(
                        list(
                            joined.order_by("rows__x").values_list("rows__x", flat=True)
                        ),
                        [0, 10] if sliced else [0, 10, 30],
                    )

    def test_later_source_branches_preserve_shape_validation(self) -> None:
        base = Child.objects.all()
        branch = base.select_related("parent")
        for nested in (False, True):
            with self.subTest(nested=nested):
                operand = base.union(branch) if nested else branch
                with self.assertRaises(ValueError):
                    Parent.objects.alias(rows=DerivedRelation(base.union(operand)))

    def test_locked_operands_keep_native_postgresql_restrictions(self) -> None:
        base = Child.objects.values("amount")
        locked = base.select_for_update()
        for source in (
            locked.union(base),
            base.union(locked),
            base.union(base.union(locked)),
        ):
            with self.subTest(source=source.query.combined_queries):
                with self.assertRaises(NotSupportedError), transaction.atomic():
                    list(source)
                joined = Parent.objects.alias(rows=DerivedRelation(source))
                with self.assertRaises(NotSupportedError), transaction.atomic():
                    list(joined.values_list("rows__amount", flat=True))

    def test_explicit_values_projection_normalizes_operand_shapes(self) -> None:
        first = Child.objects.values("amount")
        for branch in (
            Child.objects.only("amount"),
            Child.objects.defer("sequence"),
            Child.objects.select_related("parent"),
        ):
            with self.subTest(shape=branch.query.deferred_loading):
                source = first.union(branch.values("amount"))
                joined = Parent.objects.filter(pk=self.one.pk).alias(
                    rows=DerivedRelation(source)
                )
                self.assertEqual(
                    list(
                        joined.order_by("rows__amount").values_list(
                            "rows__amount", flat=True
                        )
                    ),
                    [10, 20, 30],
                )

    def test_later_union_operand_nulls_survive_negation(self) -> None:
        first = Child.objects.filter(parent=self.one).values(value=F("amount"))
        second = Parent.objects.filter(pk=self.empty.pk).values(
            other=Value(None, output_field=IntegerField())
        )
        joined = Parent.objects.filter(pk=self.one.pk).alias(
            rows=DerivedRelation(first.union(second), join_type="inner")
        )
        self.assertEqual(
            list(joined.exclude(rows__value=10).values_list("rows__value", flat=True)),
            [None],
        )

    def test_empty_aggregate_union_does_not_invent_a_row(self) -> None:
        empty = (
            Child.objects.filter(pk__in=[])
            .order_by()
            .values(dummy=Value(1))
            .annotate(total=Sum("amount"))
            .values("total")
        )
        source = empty.union(empty)
        joined = Parent.objects.alias(rows=DerivedRelation(source))
        self.assertEqual(
            list(joined.order_by("name").values_list("rows__total", flat=True)),
            [None, None, None],
        )
        inner = Parent.objects.alias(rows=DerivedRelation(source, join_type="inner"))
        self.assertEqual(inner.count(), 0)
