"""Tuple lookups with local compatibility for older Django versions."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from django.core.exceptions import EmptyResultSet
from django.db import NotSupportedError
from django.db.models import expressions
from django.db.models.lookups import Lookup
from django.db.models.sql.query import Query
from django.db.models.sql.where import WhereNode

from .compat import _projected_expressions

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.expressions import BaseExpression
    from django.db.models.sql.compiler import SQLCompiler


try:
    from django.db.models.fields.tuple_lookups import Tuple, tuple_lookups
except ImportError:
    tuple_lookups = {}

    class Tuple(expressions.Func):
        """Typed row expression for Django versions before tuple expressions."""

        function = ""
        allows_composite_expressions = True

        def __len__(self) -> int:
            return len(self.source_expressions)

        def __iter__(self) -> Iterator[BaseExpression]:
            return iter(self.source_expressions)

    class _TupleLookup(Lookup):
        """Local tuple lookups using ordinary Boolean lookup expressions."""

        def get_prep_lookup(self) -> object:
            if self.lookup_name == "isnull":
                if not isinstance(self.rhs, bool):
                    raise ValueError("Tuple isnull requires a boolean.")
                return self.rhs
            if not isinstance(self.rhs, (tuple, list, Tuple)):
                raise ValueError("Tuple lookups require tuple values.")
            rows = self.rhs if self.lookup_name == "in" else [self.rhs]
            if any(
                not isinstance(row, (tuple, list, Tuple)) or len(row) != len(self.lhs)
                for row in rows
            ):
                raise ValueError("Tuple lookup requires one value per output column.")
            return self.rhs

        def _comparison(self, values: Iterable[object], operation: str) -> WhereNode:
            columns = list(self.lhs)
            values = list(values)
            if len(values) != len(columns):
                raise ValueError("Tuple lookup requires one value per output column.")
            equals = [
                column.get_lookup("exact")(column, value)
                for column, value in zip(columns, values)
            ]
            if operation == "exact":
                return WhereNode(equals, connector="AND")
            branches = []
            strict = "gt" if operation.startswith("g") else "lt"
            for index, (column, value) in enumerate(zip(columns, values)):
                operator = operation if index == len(columns) - 1 else strict
                branches.append(
                    WhereNode(
                        [*equals[:index], column.get_lookup(operator)(column, value)],
                        connector="AND",
                    )
                )
            return WhereNode(branches, connector="OR")

        def as_sql(
            self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
        ) -> tuple[str, tuple[object, ...]]:
            if self.lookup_name == "isnull":
                if not isinstance(self.rhs, bool):
                    raise ValueError("Tuple isnull requires a boolean.")
                clause = WhereNode(
                    [
                        column.get_lookup("isnull")(column, self.rhs)
                        for column in self.lhs
                    ],
                    connector="OR" if self.rhs else "AND",
                )
            elif self.lookup_name == "in":
                clause = WhereNode(
                    [
                        self._comparison(values, "exact")
                        for values in self.rhs
                        if all(value is not None for value in values)
                    ],
                    connector="OR",
                )
                if not clause.children:
                    raise EmptyResultSet
            else:
                if not isinstance(self.rhs, (tuple, list, Tuple)):
                    raise NotSupportedError(
                        "This tuple lookup requires tuple values on older Django."
                    )
                clause = self._comparison(self.rhs, self.lookup_name)
            sql, params = compiler.compile(clause)
            return sql, tuple(params)

    class _TupleExact(_TupleLookup):
        lookup_name = "exact"

    class _TupleIn(_TupleLookup):
        lookup_name = "in"

    class _TupleIsNull(_TupleLookup):
        lookup_name = "isnull"

    class _TupleGreaterThan(_TupleLookup):
        lookup_name = "gt"

    class _TupleGreaterThanOrEqual(_TupleLookup):
        lookup_name = "gte"

    class _TupleLessThan(_TupleLookup):
        lookup_name = "lt"

    class _TupleLessThanOrEqual(_TupleLookup):
        lookup_name = "lte"

    tuple_lookups = {
        lookup.lookup_name: lookup
        for lookup in (
            _TupleExact,
            _TupleIn,
            _TupleIsNull,
            _TupleGreaterThan,
            _TupleGreaterThanOrEqual,
            _TupleLessThan,
            _TupleLessThanOrEqual,
        )
    }


class _SourceTupleIn(tuple_lookups["in"]):
    """Accept expression tuples on Django versions that only count ColPairs."""

    def get_prep_lookup(self) -> object:
        if isinstance(self.rhs, Query):
            if len(_projected_expressions(self.rhs)) != len(self.lhs):
                raise ValueError("Tuple subquery must select one field per component.")
            self.rhs.clear_ordering(clear_default=True)
            return self.rhs
        return super().get_prep_lookup()

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        if not isinstance(self.rhs, Query):
            return super().as_sql(compiler, connection)
        if getattr(self.rhs, "_db", None) not in {None, connection.alias}:
            raise ValueError("Tuple subqueries cannot use a different database.")
        lhs, lhs_params = compiler.compile(self.lhs)
        rhs, rhs_params = compiler.compile(self.rhs)
        return f"{lhs} IN {rhs}", (*lhs_params, *rhs_params)
