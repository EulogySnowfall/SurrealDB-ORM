"""Unit tests for surreal_orm.q — Q objects for complex query expressions."""

from surreal_orm.q import Q


class TestQInit:
    def test_empty(self) -> None:
        q = Q()
        assert q.children == []
        assert q.connector == Q.AND
        assert q.negated is False

    def test_simple_filter(self) -> None:
        q = Q(age=18)
        assert len(q.children) == 1
        assert q.children[0] == ("age", "exact", 18)

    def test_lookup_filter(self) -> None:
        q = Q(age__gte=18)
        assert q.children[0] == ("age", "gte", 18)

    def test_multiple_filters(self) -> None:
        q = Q(age__gte=18, name__startswith="A")
        assert len(q.children) == 2

    def test_complex_lookup(self) -> None:
        q = Q(name__icontains="alice")
        assert q.children[0] == ("name", "icontains", "alice")


class TestQOr:
    def test_or_creates_or_connector(self) -> None:
        q1 = Q(age=18)
        q2 = Q(role="admin")
        result = q1 | q2
        assert result.connector == Q.OR
        assert q1 in result.children
        assert q2 in result.children

    def test_or_preserves_children(self) -> None:
        q1 = Q(a=1)
        q2 = Q(b=2)
        result = q1 | q2
        assert len(result.children) == 2

    def test_chained_or(self) -> None:
        q = Q(a=1) | Q(b=2) | Q(c=3)
        assert q.connector == Q.OR


class TestQAnd:
    def test_and_creates_and_connector(self) -> None:
        q1 = Q(age=18)
        q2 = Q(role="admin")
        result = q1 & q2
        assert result.connector == Q.AND
        assert q1 in result.children
        assert q2 in result.children

    def test_chained_and(self) -> None:
        q = Q(a=1) & Q(b=2) & Q(c=3)
        assert q.connector == Q.AND


class TestQNot:
    def test_not_creates_negated(self) -> None:
        q = Q(status="banned")
        result = ~q
        assert result.negated is True
        assert q in result.children

    def test_not_preserves_connector(self) -> None:
        q = ~Q(status="banned")
        assert q.connector == Q.AND

    def test_double_negation(self) -> None:
        q = ~~Q(status="banned")
        assert q.negated is True


class TestQRepr:
    def test_repr_simple(self) -> None:
        q = Q(age=18)
        r = repr(q)
        assert "Q(" in r
        assert "age" in r

    def test_repr_negated(self) -> None:
        q = ~Q(status="banned")
        r = repr(q)
        assert "~Q(" in r

    def test_repr_or(self) -> None:
        q = Q(a=1) | Q(b=2)
        r = repr(q)
        assert "Q.OR(" in r

    def test_repr_and(self) -> None:
        q = Q(a=1) & Q(b=2)
        r = repr(q)
        assert "Q(" in r
        assert "Q.OR" not in r
        assert "~Q" not in r


class TestQComplexExpressions:
    def test_and_with_or(self) -> None:
        q = Q(role="admin") & (Q(age__gte=18) | Q(verified=True))
        assert q.connector == Q.AND
        # Second child should be the OR expression
        or_child = q.children[1]
        assert isinstance(or_child, Q)
        assert or_child.connector == Q.OR

    def test_negated_or(self) -> None:
        q = ~(Q(a=1) | Q(b=2))
        assert q.negated is True

    def test_complex_tree(self) -> None:
        q = (Q(a=1) | Q(b=2)) & (~Q(c=3))
        assert q.connector == Q.AND
        assert len(q.children) == 2
