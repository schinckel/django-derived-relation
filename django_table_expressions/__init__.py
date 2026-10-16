"""Named derived-query sources and their projected columns."""

from . import expressions as _expressions
from . import joins as _joins
from . import lookups as _lookups
from . import query as _query
from .fields import CompositeField
from .joins import Join
from .queryset import DerivedQuerySet, DerivedQuerySetMixin

# Keep the previous module globals used by serialized queries importable.
DerivedCol = _expressions.DerivedCol
TableSource = _expressions.TableSource
_restore_derived_col = _expressions._restore_derived_col
DerivedJoin = _joins.DerivedJoin
FunctionJoin = _joins.FunctionJoin
_SourceTupleIn = _lookups._SourceTupleIn
_restore_query = _query._restore_query
Tuple = _lookups.Tuple
if hasattr(_lookups, "_TupleLookup"):
    _TupleLookup = _lookups._TupleLookup
    _TupleExact = _lookups._TupleExact
    _TupleIn = _lookups._TupleIn
    _TupleIsNull = _lookups._TupleIsNull
    _TupleGreaterThan = _lookups._TupleGreaterThan
    _TupleGreaterThanOrEqual = _lookups._TupleGreaterThanOrEqual
    _TupleLessThan = _lookups._TupleLessThan
    _TupleLessThanOrEqual = _lookups._TupleLessThanOrEqual

__all__ = ["CompositeField", "DerivedQuerySet", "DerivedQuerySetMixin", "Join"]
