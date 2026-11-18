"""Service replies as types (SPEC "Services"): TypedDicts of Kavita's, Suwayomi's and the metadata sources' replies.

Types only: the package imports nothing of the program.
"""

from collections.abc import Mapping, Sequence

# Any JSON value (json.loads'), what a validator of a JSON text takes before it knows the shape.
type Json = str | int | float | bool | Sequence[Json] | Mapping[str, Json] | None
