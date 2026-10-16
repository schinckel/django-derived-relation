"""Compiler adaptation for lazy joins and projected outputs."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, TypeAlias

from django.db.models.sql.compiler import SQLCompiler

from .expressions import DerivedCol
from .fields import CompositeField

if TYPE_CHECKING:
    from django.db.models.expressions import BaseExpression
_SQLParams: TypeAlias = tuple[object, ...] | list[object]
_SQLFragment: TypeAlias = tuple[str, _SQLParams]
_SelectList: TypeAlias = list[tuple["BaseExpression", _SQLFragment, str | None]]
_OrderList: TypeAlias = list[tuple["BaseExpression", tuple[str, _SQLParams, bool]]]


@lru_cache(maxsize=None)
def _compiler_class(base: type[SQLCompiler]) -> type[SQLCompiler]:
    return type("DerivedCompiler", (DerivedCompilerMixin, base), {})


class DerivedCompilerMixin:
    def get_select(
        self, *args: object, **kwargs: object
    ) -> tuple[_SelectList, dict[str, object] | None, dict[str, int]]:
        select, klass_info, annotation_map = super().get_select(*args, **kwargs)
        names = getattr(self.query, "_table_output_names", None)
        if names is not None:
            select = [
                (expression, sql, name)
                for (expression, sql, _), name in zip(select, names, strict=True)
            ]
        return select, klass_info, annotation_map

    def pre_sql_setup(
        self, *args: object, **kwargs: object
    ) -> tuple[_SelectList, _OrderList, list[_SQLFragment]]:
        result = super().pre_sql_setup(*args, **kwargs)
        self._source_ordering = [expression for expression, _ in result[1]]
        return result

    def get_from_clause(self) -> tuple[list[str], list[object]]:
        selected = [expression for expression, *_ in self.select]
        if any(
            isinstance(expression.output_field, CompositeField)
            for expression in selected
        ):
            raise ValueError(
                "Select table-source columns individually, not the whole source."
            )
        ordering = getattr(self, "_source_ordering", [])
        distinct = getattr(self, "_source_distinct", [])
        required = {
            col.alias for col in self.query._gen_cols(selected + ordering + distinct)
        }
        required.update(getattr(self.query, "_retained_source_aliases", ()))
        expressions = selected + ordering + [self.where, self.having, self.qualify]
        if isinstance(self.query.group_by, tuple):
            expressions.extend(self.query.group_by)
        needed = {col.alias for col in self.query._gen_cols(expressions)} | required
        needed = self.query._source_dependencies(needed)
        for alias, candidate in self.query.alias_map.items():
            if not getattr(candidate, "lazy_source", False):
                continue
            self.query.alias_refcount[alias] = int(alias in needed)
            node = self.query.alias_map[alias]
            if alias in required:
                self.query.alias_map[alias] = node.demote()
        return super().get_from_clause()

    def collapse_group_by(
        self, expressions: list[BaseExpression], having: list[BaseExpression]
    ) -> list[BaseExpression]:
        if any(isinstance(expression, DerivedCol) for expression in expressions):
            return expressions
        return super().collapse_group_by(expressions, having)

    def get_distinct(self) -> tuple[list[str], list[tuple[object, ...]]]:
        result, params = [], []
        self._source_distinct = []
        fields = self.query.distinct_fields
        try:
            for name in fields:
                if self.query._source_path(name):
                    expression = self.query.resolve_ref(name)
                    self._source_distinct.append(expression)
                    sql, column_params = self.compile(expression)
                    result.append(sql)
                    params.append(column_params)
                else:
                    self.query.distinct_fields = (name,)
                    sql, column_params = super().get_distinct()
                    result.extend(sql)
                    params.extend(column_params)
        finally:
            self.query.distinct_fields = fields
        return result, params
