from unittest import skipUnless

from django.db import connection, models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import F, Func, Q, QuerySet, Value
from django.db.models.sql.compiler import SQLCompiler

from django_derived_relation import CompositeField, Join

from .base import DerivedRelationTestCase
from .models import Parent


class GenerateSeries(Func):
    function = "generate_series"
    output_field = models.IntegerField()
    table_source = True


class JsonEach(Func):
    function = "json_each"
    output_field = CompositeField(
        label=models.TextField(db_column="key"),
        payload=models.JSONField(db_column="value"),
    )
    table_source = True

    def as_postgresql(
        self,
        compiler: SQLCompiler,
        connection: BaseDatabaseWrapper,
    ) -> tuple[str, tuple[object, ...]]:
        sql, params = self.as_sql(compiler, connection, function="jsonb_each")
        return sql, tuple(params)


class SQLiteJsonEach(JsonEach):
    # SQLite exposes numeric JSON values as SQL numbers, unlike jsonb_each.
    output_field = CompositeField(
        label=models.TextField(db_column="key"),
        payload=models.IntegerField(db_column="value"),
    )


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL generate_series")
class ScalarTableFunctionTests(DerivedRelationTestCase):
    def series_source(self) -> GenerateSeries:
        return GenerateSeries(Value(1), Value(2))

    def test_unused_source_is_lazy(self) -> None:
        self.assertEqual(Parent.objects.alias(series=self.series_source()).count(), 3)

    def test_single_column_annotation_and_filter(self) -> None:
        query = Parent.objects.annotate(series=self.series_source()).filter(
            series__gt=1
        )
        self.assertEqual(list(query.values_list("series", flat=True)), [2] * 3)

    def test_correlated_argument_uses_outer_column(self) -> None:
        Parent.objects.filter(pk=self.many.pk).update(minimum=2)
        query = Parent.objects.alias(series=GenerateSeries(Value(1), F("minimum")))
        self.assertEqual(
            sorted(query.values_list("name", "series")), [("many", 1), ("many", 2)]
        )

    def test_explicit_left_empty_function(self) -> None:
        query = Parent.objects.alias(
            series=Join(
                GenerateSeries(Value(1), Value(0)),
                join_type="left",
            )
        )
        self.assertEqual(list(query.values_list("series", flat=True)), [None] * 3)
        self.assertEqual(query.filter(series__isnull=True).count(), 3)

    def test_on_condition_is_not_where(self) -> None:
        query = Parent.objects.alias(
            series=Join(
                self.series_source(),
                join_type="left",
                condition=Q(series__gt=5),
            )
        )
        self.assertEqual(list(query.values_list("series", flat=True)), [None] * 3)


@skipUnless(
    connection.vendor in {"postgresql", "sqlite"},
    "Requires a verified JSON table function",
)
class CompositeTableFunctionTests(DerivedRelationTestCase):
    def query(self) -> QuerySet[Parent]:
        function = SQLiteJsonEach if connection.vendor == "sqlite" else JsonEach
        return Parent.objects.alias(
            item=function(Value({"alpha": 12}, output_field=models.JSONField()))
        )

    @skipUnless(connection.vendor == "sqlite", "SQLite implicit function correlation")
    def test_sqlite_correlated_function(self) -> None:
        payload = Func(
            Value("minimum"),
            F("minimum"),
            function="json_object",
            output_field=models.JSONField(),
        )
        query = Parent.objects.alias(item=SQLiteJsonEach(payload))
        self.assertEqual(
            list(query.values_list("item__label", "item__payload")),
            [("minimum", 0)] * 3,
        )

    def test_named_outputs_and_db_column_mapping(self) -> None:
        self.assertEqual(
            list(self.query().values_list("item__label", "item__payload")),
            [("alpha", 12)] * 3,
        )

    def test_composite_output_filter(self) -> None:
        self.assertEqual(self.query().filter(item__label="alpha").count(), 3)

    def test_explicit_composite_function(self) -> None:
        query = Parent.objects.alias(
            item=Join(
                JsonEach(Value({}, output_field=models.JSONField())),
                join_type="left",
            )
        )
        self.assertEqual(
            list(query.values_list("item__label", "item__payload")), [(None, None)] * 3
        )

    def test_multi_column_annotate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Parent.objects.annotate(
                item=JsonEach(Value({}, output_field=models.JSONField()))
            )
