import pickle

from django.core.exceptions import FieldError
from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Abs
from django.test import skipUnlessDBFeature

from .base import DerivedRelationTestCase
from .models import Child, Parent


class TableSourceTests(DerivedRelationTestCase):
    def query(self) -> QuerySet[Parent]:
        return Parent.objects.alias(
            rows=Child.objects.values("amount", "sequence"),
        )

    def test_unused_source_does_not_multiply_rows(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.values("amount", "sequence"),
        )
        self.assertEqual(query.count(), 3)
        self.assertTrue(query.exists())

    def test_selected_empty_source_is_required(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.filter(pk=-1).values("amount", "sequence"),
        ).values_list("pk", "rows__amount")
        self.assertEqual(list(query), [])

    def test_optional_empty_source_preserves_other_or_branch(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.filter(pk=-1).values("amount", "sequence"),
        ).filter(Q(name="empty") | Q(rows__amount=10))
        self.assertEqual(list(query.values_list("pk", flat=True)), [self.empty.pk])

    def test_scalar_expressions_retain_native_behaviour(self) -> None:
        source = QuerySet(model=Child).order_by("pk").values("amount")[:1]
        query = Parent.objects.annotate(
            bare=source,
            wrapped=Subquery(source),
            present=Exists(source),
            absolute=Abs(F("minimum")),
        )
        self.assertEqual(
            list(query.values_list("bare", "wrapped", "present", "absolute")),
            [(10, 10, True, 0)] * 3,
        )

    def test_selected_source_multiplies_rows_and_reuses_join(self) -> None:
        query = self.query().values_list("rows__amount", "rows__sequence")
        self.assertEqual(sorted(query), sorted([(10, 1), (20, 1), (30, 2)] * 3))

    def test_filter_and_transform(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.values("amount", label=Value("HELLO")),
        ).filter(rows__label__iexact="hello", rows__amount=10)
        self.assertEqual(query.count(), 3)

    def test_ordering_component(self) -> None:
        self.assertEqual(
            list(
                self.query()
                .order_by("-rows__amount", "pk")
                .values_list("rows__amount", flat=True)
            ),
            [30] * 3 + [20] * 3 + [10] * 3,
        )

    def test_selection_makes_or_source_required(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.none().values("amount", "sequence"),
        ).filter(Q(name="empty") | Q(rows__amount=10))
        self.assertEqual(list(query.values_list("pk", "rows__amount")), [])

    def test_optional_source_isnull_and_negation(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.none().values("amount", "sequence"),
        )
        self.assertEqual(query.filter(rows__amount__isnull=True).count(), 3)
        self.assertEqual(query.exclude(rows__amount=10).count(), 3)

    def test_nested_boolean_votes(self) -> None:
        query = self.query().filter(
            (Q(name="empty") | Q(rows__amount=10)) & Q(rows__sequence=1),
        )
        self.assertEqual(query.count(), 4)

    def test_component_annotation_and_aggregate(self) -> None:
        query = self.query().annotate(chosen=F("rows__amount"))
        self.assertEqual(query.aggregate(total=Count("chosen")), {"total": 9})

    def test_clone_projection_and_source_replacement(self) -> None:
        original = self.query()
        selected = original.values("rows__amount")
        self.assertEqual(original.count(), 3)
        self.assertEqual(selected.count(), 9)
        replaced = original.alias(
            rows=Child.objects.none().values("amount", "sequence")
        )
        self.assertEqual(list(replaced.values("rows__amount")), [])
        self.assertEqual(original.count(), 3)

    def test_projected_relation_path_name(self) -> None:
        query = Parent.objects.alias(
            rows=Child.objects.values("parent__name", "amount"),
        ).filter(rows__parent__name__iexact="many", rows__amount=20)
        self.assertEqual(query.count(), 3)

    def test_requires_projection_and_individual_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit values"):
            Parent.objects.alias(rows=Child.objects.all())
        with self.assertRaisesRegex(ValueError, "alias.*annotate"):
            Parent.objects.annotate(rows=Child.objects.values("amount", "sequence"))
        with self.assertRaises(FieldError):
            list(self.query().values("rows__missing"))

    def test_field_and_annotation_collisions(self) -> None:
        with self.assertRaises(ValueError):
            Parent.objects.alias(name=Child.objects.values("amount", "sequence"))
        query = self.query().alias(rows=Value(7)).annotate(chosen=F("rows"))
        self.assertEqual(list(query.values_list("chosen", flat=True)), [7] * 3)

    def test_tuple_lookup(self) -> None:
        query = self.query().filter(rows=(10, 1))
        self.assertEqual(query.count(), 3)
        self.assertEqual(self.query().filter(rows__in=[(10, 1), (30, 2)]).count(), 6)

    def test_tuple_ordering(self) -> None:
        query = self.query().order_by("rows", "pk")
        self.assertEqual(
            list(query.values_list("rows__amount", flat=True)),
            [10] * 3 + [20] * 3 + [30] * 3,
        )

    def test_replacing_materialised_source_preserves_frozen_references(self) -> None:
        query = (
            self.query()
            .filter(rows__amount=10)
            .alias(
                rows=Child.objects.filter(amount=30).values("amount", "sequence"),
            )
        )
        self.assertEqual(list(query.values_list("rows__amount", flat=True)), [30] * 3)

    def test_dropping_projection_does_not_retain_join(self) -> None:
        query = self.query().values("rows__amount").values("pk")
        self.assertEqual(query.count(), 3)

    def test_nested_exists_relabels_source(self) -> None:
        inner = self.query().filter(pk=OuterRef("pk"), rows__amount=30)
        self.assertEqual(Parent.objects.filter(Exists(inner)).count(), 3)

    def test_tuple_comparisons_and_empty_membership(self) -> None:
        self.assertEqual(self.query().filter(rows__gt=(10, 1)).count(), 6)
        self.assertEqual(self.query().filter(rows__gte=(10, 1)).count(), 9)
        self.assertEqual(self.query().filter(rows__lt=(20, 1)).count(), 3)
        self.assertEqual(self.query().filter(rows__lte=(20, 1)).count(), 6)
        self.assertEqual(self.query().filter(rows__in=[]).count(), 0)

    def test_tuple_subquery_membership(self) -> None:
        source = Child.objects.filter(amount=10).values("amount", "sequence")
        self.assertEqual(self.query().filter(rows__in=source).count(), 3)

    def test_whole_source_selection_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "individually"):
            list(self.query().values("rows"))

    def test_invalid_tuple_arity_gives_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.query().filter(rows__in=[(10,)])
        with self.assertRaises(ValueError):
            self.query().filter(rows=(10,))

    def test_evaluated_and_unevaluated_queryset_pickle(self) -> None:
        query = (
            self.query()
            .order_by("pk", "rows__amount")
            .values_list("rows__amount", flat=True)
        )
        restored = pickle.loads(pickle.dumps(query))
        self.assertEqual(list(restored), [10, 20, 30] * 3)
        self.assertEqual(list(query), [10, 20, 30] * 3)
        restored = pickle.loads(pickle.dumps(query))
        self.assertEqual(list(restored), [10, 20, 30] * 3)

    def test_compiling_selection_does_not_change_later_projection(self) -> None:
        query = (
            Parent.objects.alias(
                rows=Child.objects.none().values("amount", "sequence"),
            )
            .filter(Q(name="empty") | Q(rows__amount=10))
            .values("pk", "rows__amount")
        )
        self.assertEqual(list(query.values_list("pk", flat=True)), [self.empty.pk])
        self.assertEqual(list(query), [])
        self.assertEqual(list(query.values_list("pk", flat=True)), [self.empty.pk])

    @skipUnlessDBFeature("can_distinct_on_fields")
    def test_distinct_on_without_selection_or_ordering(self) -> None:
        query = self.query().distinct("rows__amount")
        self.assertEqual(len(list(query)), 3)
        self.assertEqual(self.query().distinct("rows__amount").count(), 3)

    def test_combined_scalar_sources_keep_native_behaviour(self) -> None:
        source = Child.objects.values("amount").union(Child.objects.values("amount"))[
            :1
        ]
        for expression in (source, Subquery(source)):
            with self.subTest(wrapped=isinstance(expression, Subquery)):
                expected = list(
                    QuerySet(model=Parent)
                    .annotate(chosen=expression)
                    .values_list("chosen", flat=True)
                )
                actual = list(
                    Parent.objects.annotate(chosen=expression).values_list(
                        "chosen", flat=True
                    )
                )
                self.assertEqual(actual, expected)
