from unittest import skipUnless

from django.db import connection
from django.db.models import F, OuterRef, Q, Sum

from django_derived_relation import Join

from .base import DerivedRelationTestCase
from .models import Child, Parent


class ExplicitJoinTests(DerivedRelationTestCase):
    def test_grouped_left_join_preserves_unmatched_parent(self) -> None:
        totals = (
            Child.objects.order_by().values("parent_id").annotate(total=Sum("amount"))
        )
        query = Parent.objects.alias(
            rows=Join(
                totals,
                join_type="left",
                condition=Q(rows__parent_id=F("pk")),
            )
        )
        self.assertEqual(
            list(query.order_by("name").values_list("name", "rows__total")),
            [("empty", None), ("many", 50), ("one", 10)],
        )

    def test_single_column_source_and_unused_join(self) -> None:
        query = Parent.objects.alias(
            rows=Join(
                Child.objects.values("amount"),
                join_type="inner",
            )
        )
        self.assertEqual(query.count(), 3)
        self.assertEqual(
            sorted(query.values_list("rows__amount", flat=True)),
            [10] * 3 + [20] * 3 + [30] * 3,
        )

    def test_false_on_retains_parent_but_where_removes_it(self) -> None:
        query = Parent.objects.alias(
            rows=Join(
                Child.objects.values("amount"),
                join_type="left",
                condition=Q(pk__in=[]),
            )
        )
        self.assertEqual(list(query.values_list("rows__amount", flat=True)), [None] * 3)
        self.assertEqual(query.filter(rows__amount=10).count(), 0)

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL LATERAL")
    def test_correlation_is_inferred_and_on_is_after_limit(self) -> None:
        source = (
            Child.objects.filter(parent_id=OuterRef("pk"))
            .order_by(
                "-sequence",
            )
            .values("amount", "sequence")[:1]
        )
        bare = Parent.objects.alias(rows=source)
        self.assertEqual(
            sorted(bare.values_list("name", "rows__amount")),
            [("many", 30), ("one", 10)],
        )
        explicit = Parent.objects.alias(
            rows=Join(
                source,
                join_type="left",
                condition=Q(rows__amount__lte=20),
            )
        )
        self.assertEqual(
            list(explicit.order_by("name").values_list("name", "rows__amount")),
            [("empty", None), ("many", None), ("one", 10)],
        )
