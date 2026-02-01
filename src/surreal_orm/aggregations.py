"""
Aggregation classes for QuerySet GROUP BY operations.

These classes are used with QuerySet.annotate() to compute aggregated values.

Example:
    from surreal_orm import QuerySet
    from surreal_orm.aggregations import Count, Sum, Avg

    stats = await Order.objects().values("status").annotate(
        count=Count(),
        total=Sum("amount"),
        avg_amount=Avg("amount"),
    )
"""

from abc import ABC, abstractmethod


class Aggregation(ABC):
    """Base class for aggregation functions."""

    @abstractmethod
    def to_surql(self, alias: str) -> str:
        """
        Convert the aggregation to SurrealQL syntax.

        Args:
            alias: The alias for the result field.

        Returns:
            str: SurrealQL expression for the aggregation.
        """
        ...

    @property
    @abstractmethod
    def function_name(self) -> str:
        """Return the SurrealQL function name."""
        ...


class Count(Aggregation):
    """
    Count aggregation.

    Counts the number of records in each group.

    Example:
        Count()  # Counts all records
    """

    def to_surql(self, alias: str) -> str:
        return f"count() AS {alias}"

    @property
    def function_name(self) -> str:
        return "count"


class Sum(Aggregation):
    """
    Sum aggregation.

    Calculates the sum of a numeric field.

    Args:
        field: The field name to sum.

    Example:
        Sum("amount")  # Sums the "amount" field
    """

    def __init__(self, field: str):
        self.field = field

    def to_surql(self, alias: str) -> str:
        return f"math::sum({self.field}) AS {alias}"

    @property
    def function_name(self) -> str:
        return "math::sum"


class Avg(Aggregation):
    """
    Average aggregation.

    Calculates the average of a numeric field.

    Args:
        field: The field name to average.

    Example:
        Avg("age")  # Averages the "age" field
    """

    def __init__(self, field: str):
        self.field = field

    def to_surql(self, alias: str) -> str:
        return f"math::mean({self.field}) AS {alias}"

    @property
    def function_name(self) -> str:
        return "math::mean"


class Min(Aggregation):
    """
    Minimum aggregation.

    Finds the minimum value of a field.

    Args:
        field: The field name to find minimum of.

    Example:
        Min("price")  # Finds minimum "price"
    """

    def __init__(self, field: str):
        self.field = field

    def to_surql(self, alias: str) -> str:
        return f"math::min({self.field}) AS {alias}"

    @property
    def function_name(self) -> str:
        return "math::min"


class Max(Aggregation):
    """
    Maximum aggregation.

    Finds the maximum value of a field.

    Args:
        field: The field name to find maximum of.

    Example:
        Max("price")  # Finds maximum "price"
    """

    def __init__(self, field: str):
        self.field = field

    def to_surql(self, alias: str) -> str:
        return f"math::max({self.field}) AS {alias}"

    @property
    def function_name(self) -> str:
        return "math::max"


__all__ = [
    "Aggregation",
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
]
