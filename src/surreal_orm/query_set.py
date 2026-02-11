from __future__ import annotations

from .constants import LOOKUP_OPERATORS, like_to_regex
from .enum import OrderBy
from .q import Q
from .utils import remove_quotes_for_variables, format_thing, parse_record_id
from . import BaseSurrealModel, SurrealDBConnectionManager
from .aggregations import Aggregation
from .subquery import Subquery
from .prefetch import Prefetch
from .search import SearchScore, SearchHighlight
from .geo import GeoDistance
from typing import TYPE_CHECKING, Self, Any, Sequence, cast
from datetime import datetime
from pydantic_core import ValidationError

if TYPE_CHECKING:
    from .live import LiveModelStream, ChangeModelStream
    from surreal_sdk.streaming.live_select import ReconnectCallback

import logging
import re as _re

_SAFE_IDENTIFIER_RE = _re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class SurrealDbError(Exception):
    """Error from SurrealDB operations."""

    pass


logger = logging.getLogger(__name__)


class QuerySet:
    """
    A class used to build, execute, and manage queries on a SurrealDB table associated with a specific model.

    The `QuerySet` class provides a fluent interface to construct complex queries using method chaining.
    It supports selecting specific fields, filtering results, ordering, limiting, and offsetting the results.
    Additionally, it allows executing custom queries and managing table-level operations such as deletion.

    Example:
        ```python
        queryset = QuerySet(UserModel)
        users = await queryset.filter(age__gt=21).order_by('name').limit(10).all()
        ```
    """

    def __init__(self, model: type[BaseSurrealModel]) -> None:
        """
        Initialize the QuerySet with a specific model.

        This constructor sets up the initial state of the QuerySet, including the model it operates on,
        default filters, selected fields, and other query parameters.

        Args:
            model (type[BaseSurrealModel]): The model class associated with the table. This model should
                inherit from `BaseSurrealModel` and define the table name either via a `_table_name` attribute
                or by defaulting to the class name.

        Attributes:
            model (type[BaseSurrealModel]): The model class associated with the table.
            _filters (list[tuple[str, str, Any]]): A list of filter conditions as tuples of (field, lookup, value).
            select_item (list[str]): A list of field names to be selected in the query.
            _limit (int | None): The maximum number of records to retrieve.
            _offset (int | None): The number of records to skip before starting to return records.
            _order_by (str | None): The field and direction to order the results by.
            _model_table (str): The name of the table in SurrealDB.
            _variables (dict): A dictionary of variables to be used in the query.
        """
        self.model = model
        self._filters: list[tuple[str, str, Any]] = []
        self._q_filters: list[Q] = []
        self.select_item: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._order_by: str | None = None
        self._model_table: str = model.get_table_name()
        self._variables: dict = {}
        self._group_by_fields: list[str] = []
        self._annotations: dict[str, Aggregation | Subquery | SearchScore | SearchHighlight | GeoDistance] = {}
        # Vector similarity search (KNN)
        self._knn_field: str | None = None
        self._knn_vector: list[float] | None = None
        self._knn_limit: int | None = None
        self._knn_ef: int | None = None
        # Full-text search
        self._search_fields: list[tuple[str, str, int]] = []  # (field, query, ref_index)
        # Geo proximity filter
        self._geo_field: str | None = None
        self._geo_point: tuple[float, float] | None = None
        self._geo_max_distance: float | None = None
        # Relation query options
        self._select_related: list[str] = []
        self._prefetch_related: list[str | Prefetch] = []
        self._fetch_fields: list[str] = []
        self._traversal_path: str | None = None
        # Cache
        self._cache_ttl: int | None = None

    def select(self, *fields: str) -> Self:
        """
        Specify the fields to retrieve in the query.

        By default, all fields are selected (`SELECT *`). This method allows you to specify
        a subset of fields to be retrieved, which can improve performance by fetching only necessary data.

        Args:
            *fields (str): Variable length argument list of field names to select.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.select('id', 'name', 'email')
            ```
        """
        # Store the list of fields to retrieve
        self.select_item = list(fields)
        return self

    def variables(self, **kwargs: Any) -> Self:
        """
        Set variables for the query.

        Variables can be used in parameterized queries to safely inject values without risking SQL injection.

        Args:
            **kwargs (Any): Arbitrary keyword arguments representing variable names and their corresponding values.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.variables(status='active', role='admin')
            ```
        """
        self._variables = {key: value for key, value in kwargs.items()}
        return self

    def filter(self, *args: Q, **kwargs: Any) -> Self:
        """
        Add filter conditions to the query.

        This method allows adding one or multiple filter conditions to narrow down the query results.
        Accepts both Q objects (for complex OR/NOT logic) and keyword arguments (for simple AND conditions).

        Supported lookup types include:
            - exact, gt, gte, lt, lte, in, not_in
            - like, ilike, contains, icontains, not_contains
            - containsall, containsany
            - startswith, istartswith, endswith, iendswith
            - match, regex, iregex, isnull

        Args:
            *args (Q): Q objects for complex query expressions (OR, NOT).
            **kwargs (Any): Keyword arguments representing filter conditions. The key should be in the format
                ``field__lookup`` (e.g., ``age__gt=30``). If no lookup is provided, ``exact`` is assumed.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            # Simple AND filters
            queryset.filter(age__gt=21, status='active')

            # OR with Q objects
            from surreal_orm import Q
            queryset.filter(Q(name__contains="alice") | Q(email__contains="alice"))

            # Mixed: Q objects AND keyword filters
            queryset.filter(
                Q(name__contains=search) | Q(email__contains=search),
                role="admin",
            )
            ```
        """
        for arg in args:
            if isinstance(arg, Q):
                self._q_filters.append(arg)
            else:
                raise TypeError(f"filter() positional arguments must be Q objects, got {type(arg).__name__!r}.")
        for key, value in kwargs.items():
            field_name, lookup = self._parse_lookup(key)
            self._filters.append((field_name, lookup, value))
        return self

    def _parse_lookup(self, key: str) -> tuple[str, str]:
        """
        Parse the lookup type from the filter key.

        This helper method splits the filter key into the field name and the lookup type.
        If no lookup type is specified, it defaults to `exact`.

        Args:
            key (str): The filter key in the format `field__lookup` or just `field`.

        Returns:
            tuple[str, str]: A tuple containing the field name and the lookup type.

        Example:
            ```python
            _parse_lookup('age__gt')  # Returns ('age', 'gt')
            _parse_lookup('status')    # Returns ('status', 'exact')
            ```
        """
        if "__" in key:
            field_name, lookup_name = key.split("__", 1)
        else:
            field_name, lookup_name = key, "exact"
        return field_name, lookup_name

    def limit(self, value: int) -> Self:
        """
        Set a limit on the number of results to retrieve.

        This method restricts the number of records returned by the query, which is useful for pagination
        or when only a subset of results is needed.

        Args:
            value (int): The maximum number of records to retrieve.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.limit(10)
            ```
        """
        self._limit = value
        return self

    def offset(self, value: int) -> Self:
        """
        Set an offset for the results.

        This method skips a specified number of records before starting to return records.
        It is commonly used in conjunction with `limit` for pagination purposes.

        Args:
            value (int): The number of records to skip.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.offset(20)
            ```
        """
        self._offset = value
        return self

    def order_by(self, field_name: str, order_type: OrderBy = OrderBy.ASC) -> Self:
        """
        Set the field and direction to order the results by.

        This method allows sorting the query results based on a specified field and direction
        (ascending or descending). Supports Django-style ``-field`` prefix for descending order.

        Args:
            field_name (str): The name of the field to sort by. Prefix with ``-`` for descending
                order (e.g., ``"-created_at"``).
            order_type (OrderBy, optional): The direction to sort by. Defaults to `OrderBy.ASC`.
                Ignored when ``field_name`` starts with ``-``.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            queryset.order_by('name', OrderBy.DESC)
            queryset.order_by('-created_at')  # Django-style descending
            ```
        """
        if field_name.startswith("-"):
            field_name = field_name[1:]
            order_type = OrderBy.DESC
        self._order_by = f"{field_name} {order_type}"
        return self

    def values(self, *fields: str) -> Self:
        """
        Specify the fields to group by for aggregation queries.

        This method is used in conjunction with `annotate()` to perform GROUP BY operations.
        The specified fields become the grouping keys, and aggregation functions are applied
        to each group.

        Args:
            *fields (str): Variable length argument list of field names to group by.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            # Group orders by status and calculate statistics
            stats = await Order.objects().values("status").annotate(
                count=Count(),
                total=Sum("amount"),
            )
            # Result: [{"status": "paid", "count": 42, "total": 5000}, ...]
            ```
        """
        self._group_by_fields = list(fields)
        return self

    def annotate(
        self,
        **aggregations: (Aggregation | Subquery | SearchScore | SearchHighlight | GeoDistance),
    ) -> Self:
        """
        Add aggregation functions, subqueries, search annotations, or geo distances.

        This method is used in conjunction with `values()` to perform GROUP BY operations,
        with `search()` / `similar_to()` to add relevance scores and highlights,
        or with `nearby()` to add distance annotations.

        Each keyword argument should be an Aggregation instance (Count, Sum, Avg, Min, Max),
        a Subquery instance, a SearchScore, a SearchHighlight, or a GeoDistance.

        Args:
            **aggregations: Keyword arguments where keys are alias names and values are
                Aggregation, Subquery, SearchScore, SearchHighlight, or GeoDistance instances.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            from surreal_orm.aggregations import Count, Sum, Avg
            from surreal_orm import Subquery, SearchScore, SearchHighlight

            # Calculate statistics per status
            stats = await Order.objects().values("status").annotate(
                count=Count(),
                total=Sum("amount"),
                avg_amount=Avg("amount"),
            )

            # Search with BM25 scoring and highlighting
            results = await Post.objects().search(title="quantum").annotate(
                relevance=SearchScore(0),
                snippet=SearchHighlight("<b>", "</b>", 0),
            ).exec()
            ```
        """
        self._annotations = aggregations
        return self

    # ==================== Relation Query Methods ====================

    def select_related(self, *relations: str) -> Self:
        """
        Eagerly load related objects using SurrealDB's FETCH clause.

        The specified relation names are appended to the ``FETCH`` clause
        of the compiled query, causing SurrealDB to resolve record links
        inline and return the full referenced records instead of bare IDs.

        .. note::

            Like :meth:`fetch`, this only takes effect when the query is
            executed via :meth:`exec` or :meth:`first`.  The :meth:`all`
            and :meth:`get` shortcuts use the SDK ``select()`` directly
            and will not apply FETCH.

        Args:
            *relations: Names of relations to load eagerly.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example::

            # Load posts with their authors resolved inline
            posts = await Post.objects().select_related("author").exec()
            for post in posts:
                print(post.author.name)  # Full record, not just an ID

        Raises:
            ValueError: If any relation name is not a valid SurrealQL identifier.
        """
        for r in relations:
            if not _SAFE_IDENTIFIER_RE.match(r):
                raise ValueError(
                    f"Invalid FETCH target: {r!r}. "
                    "Only valid SurrealQL identifiers are allowed "
                    "(letters, digits, underscores; must start with a letter or underscore)."
                )
        self._select_related = list(relations)
        return self

    def prefetch_related(self, *relations: str | Prefetch) -> Self:
        """
        Prefetch related objects using separate optimized queries.

        This method reduces N+1 query problems by fetching related objects
        in batches after the main query completes.  Each argument can be a
        plain relation name (string) or a ``Prefetch`` object for fine-grained
        control over the queryset and target attribute.

        After ``exec()``, each parent instance will have the prefetched list
        attached as an attribute (the relation name or ``Prefetch.to_attr``).

        Args:
            *relations: Relation names or ``Prefetch`` objects.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example::

            # Simple string prefetch
            users = await User.objects().prefetch_related("posts").exec()

            # Prefetch with custom queryset
            from surreal_orm import Prefetch

            users = await User.objects().prefetch_related(
                Prefetch("posts", queryset=Post.objects().filter(published=True)),
            ).exec()
        """
        self._prefetch_related = list(relations)
        return self

    def fetch(self, *fields: str) -> Self:
        """
        Add a FETCH clause to resolve record links inline.

        SurrealDB's ``FETCH`` clause replaces record link values with the
        actual referenced records in a single query, avoiding N+1 problems.

        .. note::

            FETCH is only applied when the query is executed via
            :meth:`exec` or :meth:`first` (which use ``_compile_query()``).
            The :meth:`all` and :meth:`get` shortcuts bypass the compiled
            query and call the SDK ``select()`` directly, so FETCH will
            have no effect there.

        Args:
            *fields: Field names or relation edge names to fetch.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example::

            # Resolve the 'author' record link inline
            posts = await Post.objects().fetch("author").exec()
            # Each post['author'] is the full record, not just a record ID

            # Multiple fields
            orders = await Order.objects().fetch("customer", "items").exec()

        Raises:
            ValueError: If any field name is not a valid SurrealQL identifier.
        """
        for f in fields:
            if not _SAFE_IDENTIFIER_RE.match(f):
                raise ValueError(
                    f"Invalid FETCH target: {f!r}. "
                    "Only valid SurrealQL identifiers are allowed "
                    "(letters, digits, underscores; must start with a letter or underscore)."
                )
        self._fetch_fields = list(fields)
        return self

    def cache(self, ttl: int | None = None) -> Self:
        """
        Enable caching for this query.

        When the query is executed via :meth:`exec`, the compiled query and
        variables are hashed to produce a cache key.  If a cached result
        exists and has not expired, it is returned without hitting the
        database.  Otherwise the result is stored for future calls.

        Args:
            ttl: Time-to-live in seconds.  If ``None``, the global default
                from ``QueryCache.configure()`` is used.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example::

            # Cache for 30 seconds
            users = await User.objects().filter(role="admin").cache(ttl=30).exec()

            # Cache with default TTL
            users = await User.objects().cache().exec()
        """
        from .cache import QueryCache

        if not QueryCache._enabled:
            logger.warning("QueryCache is disabled — .cache() has no effect")
        self._cache_ttl = ttl if ttl is not None else QueryCache._default_ttl
        return self

    def similar_to(
        self,
        field: str,
        vector: list[float],
        limit: int = 10,
        *,
        ef: int | None = None,
    ) -> Self:
        """
        Find records by vector similarity using SurrealDB's KNN operator.

        Requires a vector index (HNSW or MTREE) on the target field.  The
        query uses the ``<|N|>`` (or ``<|N, EF|>``) operator to perform
        nearest-neighbour search.

        Results are automatically ordered by distance (ascending) and
        include a ``_knn_distance`` attribute on each returned model
        instance.

        Args:
            field: Name of the vector field (e.g., ``"embedding"``).
            vector: Query vector as a list of floats.
            limit: Maximum number of nearest neighbours (K). Defaults to 10.
            ef: Optional HNSW search-time ``ef`` parameter for recall tuning.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example::

            docs = await Document.objects().similar_to(
                "embedding", query_vector, limit=5,
            ).exec()
            for doc in docs:
                print(doc.title, doc._knn_distance)
        """
        if not _SAFE_IDENTIFIER_RE.match(field):
            raise ValueError(f"Invalid field name for similar_to(): {field!r}")
        self._knn_field = field
        self._knn_vector = vector
        self._knn_limit = limit
        self._knn_ef = ef
        return self

    def search(self, **field_queries: str) -> Self:
        """
        Perform full-text search on indexed fields.

        Each keyword argument maps a field name to a search query string.
        The field must have a ``SEARCH ANALYZER`` index with BM25 scoring
        in SurrealDB.

        Each field gets a unique match-reference index (``@0@``, ``@1@``,
        etc.) that can be referenced in ``SearchScore`` and
        ``SearchHighlight`` annotations.

        Args:
            **field_queries: Keyword arguments mapping field names to
                search query strings.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example::

            # Single field
            posts = await Post.objects().search(title="quantum physics").exec()

            # Multi-field with scoring
            from surreal_orm import SearchScore

            posts = await Post.objects().search(
                title="quantum", body="physics",
            ).annotate(
                title_score=SearchScore(0),
                body_score=SearchScore(1),
            ).exec()
        """
        ref = len(self._search_fields)
        for field_name, query_text in field_queries.items():
            if not _SAFE_IDENTIFIER_RE.match(field_name):
                raise ValueError(f"Invalid field name for search(): {field_name!r}")
            self._search_fields.append((field_name, query_text, ref))
            ref += 1
        return self

    def nearby(
        self,
        field: str,
        point: tuple[float, float],
        max_distance: float,
    ) -> Self:
        """
        Filter by geographic proximity.

        Generates ``WHERE geo::distance(field, (lon, lat)) <= max_distance``.

        Args:
            field: Name of the geometry field.
            point: Reference point as ``(longitude, latitude)`` tuple (GeoJSON order).
            max_distance: Maximum distance in metres.

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example::

            restaurants = await Restaurant.objects().nearby(
                "location", (-73.98, 40.74), max_distance=5000,
            ).exec()
        """
        if not _SAFE_IDENTIFIER_RE.match(field):
            raise ValueError(f"Invalid field name for nearby(): {field!r}")
        self._geo_field = field
        self._geo_point = point
        self._geo_max_distance = max_distance
        return self

    async def hybrid_search(
        self,
        *,
        vector_field: str,
        vector: list[float],
        vector_limit: int = 20,
        text_field: str,
        text_query: str,
        text_limit: int = 20,
        rrf_k: int = 60,
    ) -> list[dict[str, Any]]:
        """
        Combine vector similarity and full-text search using Reciprocal Rank Fusion.

        Executes two sub-queries (KNN + FTS) and merges the results using
        ``search::rrf()`` for balanced ranking.

        .. note::

            This is a terminal method — it executes the query and returns
            results as dictionaries (not model instances), since the result
            set is a custom projection.

        Args:
            vector_field: Name of the vector field.
            vector: Query vector.
            vector_limit: KNN limit (K neighbours).
            text_field: Name of the FTS field.
            text_query: Search query string.
            text_limit: Number of FTS results to consider.
            rrf_k: Reciprocal Rank Fusion constant (default 60).

        Returns:
            list[dict]: Results ordered by combined RRF score.

        Example::

            results = await Document.objects().hybrid_search(
                vector_field="embedding",
                vector=query_vec,
                vector_limit=10,
                text_field="content",
                text_query="quantum computing",
                text_limit=10,
            )
        """
        if not _SAFE_IDENTIFIER_RE.match(vector_field):
            raise ValueError(f"Invalid field name for hybrid_search(): {vector_field!r}")
        if not _SAFE_IDENTIFIER_RE.match(text_field):
            raise ValueError(f"Invalid field name for hybrid_search(): {text_field!r}")

        # Build a raw query using LET bindings for each sub-query
        table = self._model_table
        variables: dict[str, Any] = {
            "_hybrid_vec": vector,
            "_hybrid_text": text_query,
        }

        query = (
            f"LET $vec_results = (SELECT id, vector::distance::knn() AS _d "
            f"FROM {table} WHERE {vector_field} <|{vector_limit}|> $_hybrid_vec "
            f"ORDER BY _d);\n"
            f"LET $fts_results = (SELECT id, search::score(0) AS _s "
            f"FROM {table} WHERE {text_field} @0@ $_hybrid_text "
            f"ORDER BY _s DESC LIMIT {text_limit});\n"
            f"SELECT *, "
            f"search::rrf({rrf_k}, $vec_results, $fts_results) AS _rrf_score "
            f"FROM array::union($vec_results.id, $fts_results.id) "
            f"ORDER BY _rrf_score DESC;"
        )

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), variables)
        return cast(list[dict[str, Any]], result.all_records)

    def traverse(self, path: str) -> Self:
        """
        Add a graph traversal path to the query.

        This method allows querying across graph relations using
        SurrealDB's traversal syntax.

        Args:
            path: Graph traversal path (e.g., "->follows->users->likes->posts")

        Returns:
            Self: The current instance of QuerySet to allow method chaining.

        Example:
            ```python
            # Get all posts liked by users that alice follows
            posts = await User.objects().filter(id="alice").traverse(
                "->follows->users->likes->posts"
            ).all()
            ```
        """
        self._traversal_path = path
        return self

    async def graph_query(self, traversal: str, **variables: Any) -> list[dict[str, Any]]:
        """
        Execute a raw graph traversal query.

        This method provides direct access to SurrealDB's graph capabilities
        for complex traversal patterns that can't be expressed through
        the standard QuerySet API.

        Args:
            traversal: Graph traversal expression (e.g., "->follows->User")
            **variables: Variables to bind in the query

        Returns:
            list[dict[str, Any]]: Raw query results as dictionaries

        Example:
            ```python
            # Find users that alice follows
            result = await User.objects().filter(id="alice").graph_query("->follows->User")

            # Multi-hop traversal
            result = await User.objects().filter(id="alice").graph_query(
                "->follows->User->follows->User"
            )
            ```
        """
        # Parse the traversal to determine edge and direction
        # Expected format: "->edge->Table" or "<-edge<-Table"
        # For now, support simple single-hop traversals

        # Check if we have an id filter to use as starting point
        source_id = None
        for field_name, lookup_name, value in self._filters:
            if field_name == "id" and lookup_name == "exact":
                source_id = value
                break

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())

        if source_id:
            # Use specific record as starting point with proper escaping
            source_thing = format_thing(self._model_table, str(source_id))

            # Parse the traversal to get edge and direction
            # Simple pattern: ->edge->Table or <-edge<-Table
            if traversal.startswith("->"):
                # Outgoing: get targets where source is 'in'
                parts = traversal.split("->")
                if len(parts) >= 3:
                    edge = parts[1]
                    # Use SELECT VALUE out.* for more reliable record extraction
                    query = f"SELECT VALUE out.* FROM {edge} WHERE in = {source_thing};"
                    result = await client.query(query, {**self._variables, **variables})
                    records: list[dict[str, Any]] = []
                    for record in result.all_records or []:
                        if isinstance(record, dict):
                            records.append(record)
                    return records
            elif traversal.startswith("<-"):
                # Incoming: get sources where target is 'out'
                parts = traversal.split("<-")
                if len(parts) >= 3:
                    edge = parts[1]
                    # Use SELECT VALUE in.* for more reliable record extraction
                    query = f"SELECT VALUE in.* FROM {edge} WHERE out = {source_thing};"
                    result = await client.query(query, {**self._variables, **variables})
                    records = []
                    for record in result.all_records or []:
                        if isinstance(record, dict):
                            records.append(record)
                    return records

        # Fallback: return empty for unsupported patterns
        return []

    async def _execute_annotate(self) -> list[dict[str, Any]]:
        """
        Execute the GROUP BY query with annotations.

        Annotations may be ``Aggregation`` instances (Count, Sum, …) or
        ``Subquery`` instances (compiled to inline sub-SELECTs).

        Returns:
            list[dict[str, Any]]: A list of dictionaries containing grouped results.
        """
        # Build WHERE clause first so the shared counter is advanced before
        # subquery annotations — prevents $_fN variable collisions.
        where_parts, filter_vars = self._build_where_parts()
        if filter_vars:
            self._variables.update(filter_vars)
        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # Build SELECT clause with group fields and aggregations/subqueries.
        # Re-use the counter from _build_where_parts() so annotation subqueries
        # continue numbering where the filters left off.
        select_parts: list[str] = list(self._group_by_fields)

        # Determine the next counter value after WHERE clause variables
        next_counter = max((int(k[2:]) for k in filter_vars if k.startswith("_f")), default=-1) + 1
        counter: list[int] = [next_counter]
        sub_vars: dict[str, Any] = {}

        for alias, annotation in self._annotations.items():
            if isinstance(annotation, Subquery):
                select_parts.append(f"{annotation.to_surql(sub_vars, counter)} AS {alias}")
            else:
                select_parts.append(annotation.to_surql(alias))

        if sub_vars:
            self._variables.update(sub_vars)

        select_clause = ", ".join(select_parts)

        # Build GROUP BY clause
        if self._group_by_fields:
            group_clause = f" GROUP BY {', '.join(self._group_by_fields)}"
        else:
            group_clause = " GROUP ALL"

        query = f"SELECT {select_clause} FROM {self._model_table}{where_clause}{group_clause};"

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)

        return cast(list[dict[str, Any]], result.all_records)

    async def _execute_prefetch(
        self,
        instances: list[Any],
    ) -> None:
        """
        Batch-fetch related objects for a list of parent instances.

        For each entry in ``_prefetch_related`` (string or ``Prefetch``), this
        queries the relation edge table in a single batch call and attaches the
        results to each parent instance.

        Args:
            instances: The parent model instances returned by the main query.
        """
        if not instances:
            return

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())

        for item in self._prefetch_related:
            if isinstance(item, Prefetch):
                relation_name = item.relation_name
                to_attr = item.to_attr
                custom_qs = item.queryset
            else:
                relation_name = item
                to_attr = item
                custom_qs = None

            if not _SAFE_IDENTIFIER_RE.match(relation_name):
                raise ValueError(f"Invalid prefetch relation name: {relation_name!r}")

            # Collect source IDs
            source_ids: list[str] = []
            for inst in instances:
                if isinstance(inst, BaseSurrealModel):
                    sid = inst.get_id()
                    if sid:
                        source_ids.append(format_thing(self._model_table, sid))

            if not source_ids:
                for inst in instances:
                    object.__setattr__(inst, to_attr, [])
                continue

            # Build batch query for this relation (treated as edge table).
            # Use ``out.*`` to dereference the target node so callers get
            # the related records (not raw edge records).  The ``in`` field
            # is kept for grouping results by source instance.
            id_list = ", ".join(source_ids)
            query = f"SELECT in, out.* FROM {relation_name} WHERE in IN [{id_list}];"

            # If custom queryset has filters, append them as AND conditions.
            # Note: filters apply to the *edge* table fields.
            extra_where = ""
            extra_vars: dict[str, Any] = {}
            if custom_qs is not None:
                parts, fvars = custom_qs._build_where_parts()
                if parts:
                    extra_where = " AND " + " AND ".join(parts)
                    extra_vars = fvars
                query = f"SELECT in, out.* FROM {relation_name} WHERE in IN [{id_list}]{extra_where};"

            result = await client.query(remove_quotes_for_variables(query), extra_vars)

            # Group results by source (the 'in' field)
            grouped: dict[str, list[dict[str, Any]]] = {}
            for record in result.all_records or []:
                if isinstance(record, dict):
                    in_ref = record.get("in", "")
                    if hasattr(in_ref, "__str__"):
                        in_ref = str(in_ref)
                    # Remove the 'in' key so the attached dict only has
                    # target-node fields.
                    node = {k: v for k, v in record.items() if k != "in"}
                    grouped.setdefault(in_ref, []).append(node)

            # Attach to instances
            for inst in instances:
                if isinstance(inst, BaseSurrealModel):
                    sid = inst.get_id()
                    thing = format_thing(self._model_table, sid) if sid else ""
                    related = grouped.get(thing, [])
                    object.__setattr__(inst, to_attr, related)

    @staticmethod
    def _render_condition(
        field_name: str,
        lookup_name: str,
        value: Any,
        variables: dict[str, Any],
        counter: list[int],
    ) -> str:
        """
        Render a single filter condition to a parameterized SurrealQL expression.

        Values are bound as ``$_fN`` variables to prevent injection, unless the value
        is a string starting with ``$`` (treated as a user-provided variable reference
        for backwards compatibility with ``.variables()``).

        Args:
            field_name: The database field name.
            lookup_name: The lookup type (e.g., "exact", "gt", "isnull").
            value: The filter value.
            variables: Mutable dict to collect parameterized variables.
            counter: Mutable single-element list ``[int]`` used as auto-increment counter.

        Returns:
            str: The rendered SurrealQL condition.
        """

        # ── Helper: bind a value as $_fN and return the var name ──────
        def _bind(val: Any) -> str:
            vn = f"_f{counter[0]}"
            counter[0] += 1
            variables[vn] = val
            return vn

        op = LOOKUP_OPERATORS.get(lookup_name, "=")

        # ── Subquery values ──────────────────────────────────────────
        if isinstance(value, Subquery):
            sub_sql = value.to_surql(variables, counter)
            _COLLECTION_LOOKUPS = {"in", "not_in", "containsall", "containsany"}
            if lookup_name in _COLLECTION_LOOKUPS:
                return f"{field_name} {op} {sub_sql}"
            return f"{field_name} {op} array::first({sub_sql})"

        # ── IS NULL / IS NOT NULL ────────────────────────────────────
        if lookup_name == "isnull":
            if not isinstance(value, bool):
                raise TypeError(
                    f"Value for 'isnull' lookup on field '{field_name}' must be a bool, got {type(value).__name__!r}."
                )
            return f"{field_name} IS {'NULL' if value else 'NOT NULL'}"

        # ── Function-based lookups (no SurrealQL operator equivalent) ─
        # startswith / istartswith → string::starts_with()
        if lookup_name in ("startswith", "istartswith"):
            return f"string::starts_with({field_name}, ${_bind(value)})"

        # endswith / iendswith → string::ends_with()
        if lookup_name in ("endswith", "iendswith"):
            return f"string::ends_with({field_name}, ${_bind(value)})"

        # like → string::matches(field, regex)  (LIKE pattern converted to regex)
        if lookup_name == "like":
            return f"string::matches({field_name}, ${_bind(like_to_regex(value))})"

        # ilike → string::matches(field, (?i)regex)  (case-insensitive)
        if lookup_name == "ilike":
            return f"string::matches({field_name}, ${_bind('(?i)' + like_to_regex(value))})"

        # icontains → string::contains(string::lowercase(field), lowercase(value))
        if lookup_name == "icontains":
            return f"string::contains(string::lowercase({field_name}), ${_bind(value.lower() if isinstance(value, str) else value)})"

        # regex → string::matches(field, pattern)
        if lookup_name == "regex":
            return f"string::matches({field_name}, ${_bind(value)})"

        # iregex → string::matches(field, (?i)pattern)
        if lookup_name == "iregex":
            return f"string::matches({field_name}, ${_bind('(?i)' + value if isinstance(value, str) else value)})"

        # match → @@ (full-text search operator)
        if lookup_name == "match":
            return f"{field_name} @@ ${_bind(value)}"

        # ── Backwards compat: $variable references ───────────────────
        if isinstance(value, str) and value.startswith("$"):
            return f"{field_name} {op} {value}"

        # ── Collection lookups (IN, NOT IN, CONTAINSALL, CONTAINSANY) ─
        if lookup_name in ("in", "not_in", "containsall", "containsany"):
            if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
                raise TypeError(
                    f"Value for lookup '{lookup_name}' on field '{field_name}' "
                    f"must be a list, tuple, or set, got {type(value).__name__!r}."
                )
            return f"{field_name} {op} ${_bind(list(value))}"

        # ── Generic operator-based lookup (exact, gt, gte, lt, lte, contains, etc.)
        return f"{field_name} {op} ${_bind(value)}"

    def _render_q(
        self,
        q: Q,
        variables: dict[str, Any],
        counter: list[int],
    ) -> str:
        """
        Recursively render a Q object tree to a parameterized SurrealQL expression.

        Args:
            q: The Q object to render.
            variables: Mutable dict to collect parameterized variables.
            counter: Mutable single-element list ``[int]`` used as auto-increment counter.

        Returns:
            str: The rendered SurrealQL expression with proper parenthesization.
        """
        if not q.children:
            return ""

        parts: list[str] = []
        for child in q.children:
            if isinstance(child, Q):
                rendered = self._render_q(child, variables, counter)
                if rendered:
                    parts.append(rendered)
            else:
                field_name, lookup_name, value = child
                parts.append(self._render_condition(field_name, lookup_name, value, variables, counter))

        if not parts:
            return ""

        connector = f" {q.connector} "
        result = connector.join(parts)

        if len(parts) > 1:
            result = f"({result})"

        if q.negated:
            result = f"NOT ({result})"

        return result

    def _build_where_parts(self) -> tuple[list[str], dict[str, Any]]:
        """
        Build all WHERE clause parts from both keyword filters and Q objects.

        Returns:
            A tuple of (parts, filter_variables) where parts is a list of SurrealQL
            condition strings to be joined with AND, and filter_variables is a dict
            of parameterized variable bindings.
        """
        variables: dict[str, Any] = {}
        counter: list[int] = [0]
        parts: list[str] = []

        # Keyword-based filters (always AND-joined)
        for field_name, lookup_name, value in self._filters:
            parts.append(self._render_condition(field_name, lookup_name, value, variables, counter))

        # Q object filters
        for q in self._q_filters:
            rendered = self._render_q(q, variables, counter)
            if rendered:
                parts.append(rendered)

        return parts, variables

    def _compile_query(self) -> str:
        """
        Compile the QuerySet parameters into a parameterized SQL query string.

        Filter values are bound as ``$_fN`` variables (merged into ``self._variables``)
        to prevent injection. This method constructs the final SQL query by combining
        the selected fields, filters, ordering, limit, and offset parameters.

        Supports:
        - Standard SELECT queries
        - KNN vector similarity (``<|N|>`` operator)
        - Full-text search (``@N@`` operator)

        Returns:
            str: The compiled SQL query string.
        """
        # ── SELECT clause ───────────────────────────────────────────────
        extra_select: list[str] = []

        # KNN: add distance function to SELECT
        if self._knn_field:
            extra_select.append("vector::distance::knn() AS _knn_distance")

        # Search / Geo annotate: add SearchScore / SearchHighlight / GeoDistance to SELECT
        for alias, annotation in self._annotations.items():
            if isinstance(annotation, (SearchScore, SearchHighlight, GeoDistance)):
                extra_select.append(annotation.to_surql(alias))

        if self.select_item:
            fields = ", ".join(self.select_item)
            if extra_select:
                fields += ", " + ", ".join(extra_select)
            query = f"SELECT {fields} FROM {self._model_table}"
        else:
            if extra_select:
                query = f"SELECT *, {', '.join(extra_select)} FROM {self._model_table}"
            else:
                query = f"SELECT * FROM {self._model_table}"

        # ── WHERE clause ────────────────────────────────────────────────
        where_parts, filter_vars = self._build_where_parts()
        if filter_vars:
            self._variables.update(filter_vars)

        # KNN: append <|K|> or <|K, EF|> condition
        if self._knn_field and self._knn_vector is not None and self._knn_limit is not None:
            self._variables["_knn_vec"] = self._knn_vector
            if self._knn_ef is not None:
                knn_op = f"{self._knn_field} <|{self._knn_limit},{self._knn_ef}|> $_knn_vec"
            else:
                knn_op = f"{self._knn_field} <|{self._knn_limit}|> $_knn_vec"
            where_parts.append(knn_op)

        # FTS: append @N@ conditions
        for field_name, query_text, ref_idx in self._search_fields:
            var_name = f"_s{ref_idx}"
            self._variables[var_name] = query_text
            where_parts.append(f"{field_name} @{ref_idx}@ ${var_name}")

        # Geo: append distance filter
        # Note: SurrealDB cannot parse variables inside tuple constructors,
        # so coordinates are inlined. max_distance is still parameterized.
        if self._geo_field and self._geo_point is not None and self._geo_max_distance is not None:
            self._variables["_geo_max"] = self._geo_max_distance
            lon, lat = self._geo_point
            geo_expr = f"geo::distance({self._geo_field}, ({lon}, {lat})) <= $_geo_max"
            where_parts.append(geo_expr)

        if where_parts:
            query += " WHERE " + " AND ".join(where_parts)

        # ── ORDER BY ────────────────────────────────────────────────────
        if self._order_by:
            query += f" ORDER BY {self._order_by}"
        elif self._knn_field:
            # Auto-order by KNN distance when no explicit order set
            query += " ORDER BY _knn_distance"

        # Append LIMIT if set
        if self._limit is not None:
            query += f" LIMIT {self._limit}"

        # Append OFFSET (START) if set
        if self._offset is not None:
            query += f" START {self._offset}"

        # Append FETCH clause.
        # Both explicit fetch() calls and select_related() paths are emitted
        # as SurrealQL FETCH targets, causing SurrealDB to eagerly resolve
        # record links inline.  Dedup while preserving order.
        fetch_targets: list[str] = []
        seen: set[str] = set()
        for t in list(self._fetch_fields) + list(self._select_related):
            if t not in seen:
                fetch_targets.append(t)
                seen.add(t)
        if fetch_targets:
            query += f" FETCH {', '.join(fetch_targets)}"

        query += ";"
        return query

    async def exec(self) -> Any:
        """
        Execute the compiled query and return the results.

        This method runs the constructed SQL query against the SurrealDB database and processes
        the results. If the data conforms to the model schema, it returns a list of model instances;
        otherwise, it returns a list of dictionaries.

        When `annotate()` has been called with ``Aggregation`` or ``Subquery`` annotations,
        this returns the aggregated results as dictionaries (GROUP BY path).
        ``SearchScore`` and ``SearchHighlight`` annotations are handled inline in the
        SELECT clause and still return model instances.

        Returns:
            list[BaseSurrealModel] | list[dict]: A list of model instances if validation is successful,
            otherwise a list of dictionaries representing the raw data. For aggregation/subquery
            annotated queries, returns a list of dictionaries.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            # Regular query
            results = await queryset.exec()

            # Aggregation query
            stats = await Order.objects().values("status").annotate(
                count=Count(),
                total=Sum("amount"),
            ).exec()
            ```
        """
        # If annotations are set with Aggregation or Subquery, execute as GROUP BY query.
        # SearchScore / SearchHighlight are handled inline by _compile_query().
        has_group_annotations = any(isinstance(a, (Aggregation, Subquery)) for a in self._annotations.values())
        if self._annotations and has_group_annotations:
            # _execute_annotate() only applies filter/Q-object WHERE parts.
            # search() and similar_to() constraints are not supported in this path.
            if self._search_fields or self._knn_field:
                raise ValueError(
                    "Combining .search() or .similar_to() with aggregation/subquery "
                    "annotations is not supported. Execute them as separate queries."
                )
            return await self._execute_annotate()

        query = self._compile_query()

        # ── Cache: check for hit ────────────────────────────────────────
        cache_key: str | None = None
        if self._cache_ttl is not None:
            from .cache import QueryCache

            # Include prefetch config in key so different prefetch combos
            # get separate cache entries.
            prefetch_fp = ""
            if self._prefetch_related:
                parts = []
                for p in self._prefetch_related:
                    if isinstance(p, Prefetch):
                        parts.append(f"{p.relation_name}:{p.to_attr}")
                    else:
                        parts.append(str(p))
                prefetch_fp = "|".join(parts)

            key_vars = {**self._variables, "_pfp": prefetch_fp} if prefetch_fp else self._variables
            cache_key = QueryCache.make_key(query, key_vars, self._model_table)
            cached = QueryCache.get(cache_key)
            if cached is not None:
                return cached

        results = await self._execute_query(query)

        # ── KNN / Search annotations: extract extra fields before model parsing
        extra_fields_per_record: list[dict[str, Any]] = []
        extra_keys: set[str] = set()

        if self._knn_field:
            extra_keys.add("_knn_distance")
        for alias, annotation in self._annotations.items():
            if isinstance(annotation, (SearchScore, SearchHighlight)):
                extra_keys.add(alias)

        if extra_keys and isinstance(results, list):
            for record in results:
                if isinstance(record, dict):
                    extras = {k: record.get(k) for k in extra_keys if k in record}
                    extra_fields_per_record.append(extras)
                else:
                    extra_fields_per_record.append({})

        try:
            # surrealdb SDK 1.0.8 returns records directly, not wrapped in {"result": ...}
            parsed = self.model.from_db(cast(dict | list | None, results))
        except ValidationError as e:
            logger.info(f"Pydantic invalid format for the class, returning dict value: {e}")
            parsed = results

        # Attach extra fields (KNN distance, search scores) to model instances
        if extra_keys and isinstance(parsed, list) and extra_fields_per_record:
            for i, inst in enumerate(parsed):
                if i < len(extra_fields_per_record) and isinstance(inst, BaseSurrealModel):
                    for k, v in extra_fields_per_record[i].items():
                        object.__setattr__(inst, k, v)

        # ── Prefetch related ─────────────────────────────────────────────
        if self._prefetch_related and isinstance(parsed, list):
            await self._execute_prefetch(parsed)

        # ── Cache: store result (after prefetch, so hits include prefetched data)
        if cache_key is not None:
            from .cache import QueryCache

            QueryCache.set(cache_key, parsed, self._model_table, self._cache_ttl)  # type: ignore[arg-type]

        return parsed

    async def first(self) -> Any:
        """
        Execute the query and return the first result.

        This method modifies the QuerySet to limit the results to one and retrieves the first record.
        If no records are found, it returns `None`.

        Returns:
            BaseSurrealModel | dict | None: The first model instance if available, a dictionary if
            model validation fails, or `None` if no results are found.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            first_user = await queryset.filter(name='Alice').first()
            ```
        """
        self._limit = 1
        results = await self.exec()
        if results:
            return results[0]

        raise self.model.DoesNotExist("Query returned no results.")

    async def get(self, id_item: Any = None, *, id: Any = None) -> Any:
        """
        Retrieve a single record by its unique identifier or based on the current QuerySet filters.

        This method fetches a specific record by its ID if provided. If no ID is provided, it attempts
        to retrieve a single record based on the existing filters. It raises an error if multiple or
        no records are found when no ID is specified.

        The method automatically handles both formats:
        - Just the ID: "abc123"
        - Full SurrealDB format: "table:abc123"

        IDs that start with a digit or contain special characters are automatically escaped.

        Args:
            id_item (str | None, optional): The unique identifier of the item to retrieve. Defaults to `None`.
            id (str | None, optional): Keyword-only alias for `id_item`. Defaults to `None`.

        Returns:
            BaseSurrealModel | dict[str, Any]: The retrieved model instance or a dictionary representing the raw data.

        Raises:
            SurrealDbError: If multiple records are found when `id_item` is not provided or if no records are found.

        Example:
            ```python
            user = await queryset.get('user_123')
            user = await queryset.get(id='user_123')
            user = await queryset.get(id_item='user_123')
            # Also accepts full SurrealDB format
            user = await queryset.get('users:user_123')
            # IDs starting with digits are properly handled
            user = await queryset.get('7abc123')
            ```
        """
        # Allow 'id' keyword to be used as alias for 'id_item'
        record_id = id if id is not None else id_item
        if record_id:
            record_id_str = str(record_id)
            # Handle full SurrealDB format (table:id) - extract just the ID part
            _, id_part = parse_record_id(record_id_str)
            # Format the thing reference with proper escaping for special IDs
            thing = format_thing(self._model_table, id_part)
            client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
            result = await client.select(thing)
            # SDK returns RecordsResponse
            if result.is_empty:
                raise self.model.DoesNotExist("Record not found.")
            return self.model.from_db(cast(dict | list | None, result.first))
        else:
            result = await self.exec()
            if len(result) > 1:
                raise SurrealDbError("More than one result found.")

            if len(result) == 0:
                raise self.model.DoesNotExist("Record not found.")
            return result[0]

    async def all(self) -> Any:
        """
        Fetch all records from the associated table.

        This method retrieves every record from the table without applying any filters, limits, or ordering.

        Returns:
            list[BaseSurrealModel]: A list of model instances representing all records in the table.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            all_users = await queryset.all()
            ```
        """
        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.select(self._model_table)
        return self.model.from_db(cast(dict | list | None, result.records))

    # ==================== Aggregation Methods ====================

    def _compile_where_clause(self) -> str:
        """
        Compile the WHERE clause from filters and Q objects (parameterized).

        Filter variables are merged into ``self._variables``.

        Returns:
            str: The WHERE clause string (including WHERE keyword) or empty string.
        """
        where_parts, filter_vars = self._build_where_parts()
        if filter_vars:
            self._variables.update(filter_vars)
        if not where_parts:
            return ""

        return " WHERE " + " AND ".join(where_parts)

    async def count(self) -> int:
        """
        Count the number of records matching the current filters.

        Returns:
            int: The number of matching records.

        Example:
            ```python
            total = await User.objects().count()
            active = await User.objects().filter(active=True).count()
            ```
        """
        where_clause = self._compile_where_clause()
        query = f"SELECT count() FROM {self._model_table}{where_clause} GROUP ALL;"

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)

        if result.all_records:
            record = result.all_records[0]
            if isinstance(record, dict) and "count" in record:
                return int(record["count"])
        return 0

    async def sum(self, field: str) -> float | int:
        """
        Calculate the sum of a numeric field.

        Args:
            field: The field name to sum.

        Returns:
            float | int: The sum of the field values, or 0 if no records match.

        Example:
            ```python
            total = await Order.objects().filter(status="paid").sum("amount")
            ```
        """
        where_clause = self._compile_where_clause()
        query = f"SELECT math::sum({field}) AS total FROM {self._model_table}{where_clause} GROUP ALL;"

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)

        if result.all_records:
            record = result.all_records[0]
            if isinstance(record, dict) and "total" in record:
                value = record["total"]
                return value if value is not None else 0
        return 0

    async def avg(self, field: str) -> float | None:
        """
        Calculate the average of a numeric field.

        Args:
            field: The field name to average.

        Returns:
            float | None: The average value, or None if no records match.

        Example:
            ```python
            avg_age = await User.objects().filter(active=True).avg("age")
            ```
        """
        where_clause = self._compile_where_clause()
        query = f"SELECT math::mean({field}) AS average FROM {self._model_table}{where_clause} GROUP ALL;"

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)

        if result.all_records:
            record = result.all_records[0]
            if isinstance(record, dict) and "average" in record:
                value = record["average"]
                return float(value) if value is not None else None
        return None

    async def min(self, field: str) -> Any:
        """
        Get the minimum value of a field.

        Args:
            field: The field name to find the minimum of.

        Returns:
            Any: The minimum value, or None if no records match.

        Example:
            ```python
            min_price = await Product.objects().min("price")
            ```
        """
        where_clause = self._compile_where_clause()
        query = f"SELECT math::min({field}) AS minimum FROM {self._model_table}{where_clause} GROUP ALL;"

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)

        if result.all_records:
            record = result.all_records[0]
            if isinstance(record, dict) and "minimum" in record:
                return record["minimum"]
        return None

    async def max(self, field: str) -> Any:
        """
        Get the maximum value of a field.

        Args:
            field: The field name to find the maximum of.

        Returns:
            Any: The maximum value, or None if no records match.

        Example:
            ```python
            max_price = await Product.objects().max("price")
            ```
        """
        where_clause = self._compile_where_clause()
        query = f"SELECT math::max({field}) AS maximum FROM {self._model_table}{where_clause} GROUP ALL;"

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)

        if result.all_records:
            record = result.all_records[0]
            if isinstance(record, dict) and "maximum" in record:
                return record["maximum"]
        return None

    async def _execute_query(self, query: str) -> list[Any]:
        """
        Execute the given SQL query using the SurrealDB client.

        This internal method handles the execution of the compiled SQL query and returns the raw results
        from the database.

        Args:
            query (str): The SQL query string to execute.

        Returns:
            list[Any]: A list of query response objects containing the query results.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            results = await self._execute_query("SELECT * FROM users;")
            ```
        """
        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        return await self._run_query_on_client(client, query)

    async def _run_query_on_client(self, client: Any, query: str) -> list[Any]:
        """
        Run the SQL query on the provided SurrealDB client.

        This internal method sends the query to the SurrealDB client along with any predefined variables
        and returns the raw query responses.

        Args:
            client: The active SurrealDB client instance.
            query (str): The SQL query string to execute.

        Returns:
            list[Any]: A list of query response objects containing the query results.

        Raises:
            SurrealDbError: If there is an issue executing the query.

        Example:
            ```python
            results = await self._run_query_on_client(client, "SELECT * FROM users;")
            ```
        """
        from .debug import _log_query, _start_timer, _elapsed_ms

        final_query = remove_quotes_for_variables(query)
        start = _start_timer()
        result = await client.query(final_query, self._variables)
        _log_query(final_query, self._variables, _elapsed_ms(start))
        # SDK returns QueryResponse, extract all records
        return cast(list[Any], result.all_records)

    async def delete_table(self) -> bool:
        """
        Delete the associated table from the SurrealDB database.

        This method performs a destructive operation by removing the entire table from the database.
        Use with caution, especially in production environments.

        Returns:
            bool: `True` if the table was successfully deleted.

        Raises:
            SurrealDbError: If there is an issue deleting the table.

        Example:
            ```python
            success = await queryset.delete_table()
            ```
        """
        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        await client.delete(self._model_table)
        return True

    async def query(self, query: str, variables: dict[str, Any] = {}) -> Any:
        """
        Execute a custom SQL query on the SurrealDB database.

        This method allows running arbitrary SQL queries, provided they operate on the correct table
        associated with the current model. It ensures that the query includes the `FROM` clause referencing
        the correct table to maintain consistency and security.

        Args:
            query (str): The custom SQL query string to execute.
            variables (dict[str, Any], optional): A dictionary of variables to substitute into the query.
                Defaults to an empty dictionary.

        Returns:
            Any: The result of the query, typically a model instance or a list of model instances.

        Raises:
            SurrealDbError: If the query does not include the correct `FROM` clause or if there is an issue executing the query.

        Example:
            ```python
            custom_query = "SELECT name, email FROM UserModel WHERE status = $status;"
            results = await queryset.query(custom_query, variables={'status': 'active'})
            ```
        """
        if f"FROM {self._model_table}" not in query:
            raise SurrealDbError(f"The query must include 'FROM {self._model_table}' to reference the correct table.")
        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), variables)
        # SDK returns QueryResponse, extract all records
        return self.model.from_db(cast(dict | list | None, result.all_records))

    # ==================== Bulk Operations ====================

    async def bulk_create(
        self,
        instances: Sequence[BaseSurrealModel],
        atomic: bool = False,
        batch_size: int | None = None,
    ) -> list[BaseSurrealModel]:
        """
        Create multiple model instances in the database efficiently.

        Args:
            instances: A sequence of model instances to create.
            atomic: If True, all creates are wrapped in a transaction.
                    If any fails, all are rolled back.
            batch_size: If specified, instances are created in batches of this size.
                        Useful for very large datasets to avoid memory issues.

        Returns:
            list[BaseSurrealModel]: The created instances.

        Example:
            ```python
            users = [User(name=f"User{i}") for i in range(1000)]

            # Simple bulk create
            created = await User.objects().bulk_create(users)

            # Atomic bulk create
            created = await User.objects().bulk_create(users, atomic=True)

            # With batch size
            created = await User.objects().bulk_create(users, batch_size=100)
            ```
        """
        if not instances:
            return []

        created: list[BaseSurrealModel] = []

        if atomic:
            # Use transaction for atomicity
            async with await SurrealDBConnectionManager.transaction() as tx:
                for instance in instances:
                    await instance.save(tx=tx)
                    created.append(instance)
        elif batch_size:
            # Process in batches
            for i in range(0, len(instances), batch_size):
                batch = instances[i : i + batch_size]
                for instance in batch:
                    await instance.save()
                    created.append(instance)
        else:
            # Simple sequential create
            for instance in instances:
                await instance.save()
                created.append(instance)

        return created

    async def bulk_update(
        self,
        data: dict[str, Any],
        atomic: bool = False,
    ) -> int:
        """
        Update all records matching the current filters.

        Args:
            data: A dictionary of field names and values to update.
            atomic: If True, all updates are wrapped in a transaction.

        Returns:
            int: The number of records updated.

        Example:
            ```python
            # Update all matching records
            updated = await User.objects().filter(
                last_login__lt="2025-01-01"
            ).bulk_update({"status": "inactive"})

            # Atomic update
            updated = await User.objects().filter(role="guest").bulk_update(
                {"verified": True},
                atomic=True
            )
            ```
        """
        where_clause = self._compile_where_clause()

        # Build SET clause
        set_parts = []
        for field, value in data.items():
            set_parts.append(f"{field} = {repr(value)}")
        set_clause = ", ".join(set_parts)

        query = f"UPDATE {self._model_table} SET {set_clause}{where_clause};"

        if atomic:
            # For atomic operations, count first then update in transaction
            current_count = await self.count()
            async with await SurrealDBConnectionManager.transaction() as tx:
                await tx.query(remove_quotes_for_variables(query), self._variables)
            return current_count

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)
        return len(result.all_records)

    async def bulk_delete(self, atomic: bool = False) -> int:
        """
        Delete all records matching the current filters.

        Args:
            atomic: If True, all deletes are wrapped in a transaction.

        Returns:
            int: The number of records deleted.

        Example:
            ```python
            # Delete all matching records
            deleted = await User.objects().filter(status="deleted").bulk_delete()

            # Atomic delete
            deleted = await Order.objects().filter(
                created_at__lt="2024-01-01"
            ).bulk_delete(atomic=True)
            ```
        """
        where_clause = self._compile_where_clause()
        # Use RETURN BEFORE to get deleted records count
        query = f"DELETE FROM {self._model_table}{where_clause} RETURN BEFORE;"

        if atomic:
            # For atomic operations, count first then delete in transaction
            current_count = await self.count()
            delete_query = f"DELETE FROM {self._model_table}{where_clause};"
            async with await SurrealDBConnectionManager.transaction() as tx:
                await tx.query(remove_quotes_for_variables(delete_query), self._variables)
            return current_count

        client = await SurrealDBConnectionManager.get_client(self.model.get_connection_name())
        result = await client.query(remove_quotes_for_variables(query), self._variables)
        return len(result.all_records)

    # ==================== Real-time Methods ====================

    def live(
        self,
        *,
        auto_resubscribe: bool = True,
        diff: bool = False,
        on_reconnect: ReconnectCallback | None = None,
    ) -> LiveModelStream[BaseSurrealModel]:
        """
        Subscribe to real-time changes for this query via WebSocket Live Query.

        Returns an async context manager and iterator that yields
        ``ModelChangeEvent`` instances with typed model objects whenever
        matching records are created, updated, or deleted.

        Requires a WebSocket connection (created lazily by the connection
        manager on first call).

        Args:
            auto_resubscribe: Automatically resubscribe after WebSocket
                reconnect. Defaults to True.
            diff: If True, receive only changed fields (DIFF mode).
            on_reconnect: Optional async callback ``(old_id, new_id)``
                invoked when the subscription is re-established after
                a reconnect.

        Returns:
            ``LiveModelStream`` — async context manager and iterator.

        Example::

            async with User.objects().filter(role="admin").live() as stream:
                async for event in stream:
                    match event.action:
                        case LiveAction.CREATE:
                            print(f"New admin: {event.instance.name}")
                        case LiveAction.UPDATE:
                            print(f"Admin updated: {event.instance}")
                        case LiveAction.DELETE:
                            print(f"Admin removed: {event.record_id}")
        """
        from .live import LiveModelStream

        # Build WHERE clause and params from current filters
        where_parts, filter_vars = self._build_where_parts()
        where_clause = " AND ".join(where_parts) if where_parts else None
        params = {**self._variables, **filter_vars}

        return LiveModelStream(
            model=self.model,
            connection=None,  # type: ignore[arg-type]  # resolved in __aenter__
            table=self._model_table,
            where=where_clause,
            params=params or None,
            diff=diff,
            auto_resubscribe=auto_resubscribe,
            on_reconnect=on_reconnect,
        )

    def changes(
        self,
        *,
        since: str | datetime | None = None,
        poll_interval: float = 0.1,
        batch_size: int = 100,
    ) -> ChangeModelStream[BaseSurrealModel]:
        """
        Stream change feed events for this model's table via HTTP.

        Returns an async iterator that yields ``ModelChangeEvent`` instances
        for each change captured by SurrealDB's Change Feed. This is stateless
        and ideal for microservices event streaming, data replication, and
        audit trails.

        .. note::

            Change Feeds must be enabled on the table (``DEFINE TABLE ... CHANGEFEED ...``)
            before this method will yield results.

        Args:
            since: Starting point as an ISO 8601 timestamp string or
                ``datetime`` object. If None, streams from "now".
            poll_interval: Seconds between polls when no changes are
                available. Defaults to 0.1.
            batch_size: Maximum changes per poll. Defaults to 100.

        Returns:
            ``ChangeModelStream`` — async iterator yielding ``ModelChangeEvent``.

        Example::

            async for event in User.objects().changes(since="2026-01-01"):
                await publish_to_queue({
                    "type": f"user.{event.action.value.lower()}",
                    "data": event.raw,
                })
        """
        from .live import ChangeModelStream

        return ChangeModelStream(
            model=self.model,
            connection=None,  # type: ignore[arg-type]  # resolved at iteration time
            table=self._model_table,
            since=since,
            poll_interval=poll_interval,
            batch_size=batch_size,
        )
