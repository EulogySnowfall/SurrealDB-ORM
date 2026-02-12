"""
Tests for TYPE RELATION — CreateTable relation support, parser, diff, enum.
"""

from __future__ import annotations

from surreal_orm.migrations.define_parser import parse_define_table
from surreal_orm.migrations.operations import CreateTable
from surreal_orm.migrations.state import SchemaState, TableState
from surreal_orm.types import TableType

# ---------------------------------------------------------------------------
# TableType enum
# ---------------------------------------------------------------------------


class TestTableTypeEnum:
    """Tests for RELATION and ANY in TableType."""

    def test_relation_exists(self) -> None:
        assert TableType.RELATION == "relation"
        assert TableType.RELATION.value == "relation"

    def test_any_exists(self) -> None:
        assert TableType.ANY == "any"
        assert TableType.ANY.value == "any"

    def test_all_types(self) -> None:
        expected = {"normal", "user", "stream", "hash", "relation", "any"}
        actual = {t.value for t in TableType}
        assert expected == actual


# ---------------------------------------------------------------------------
# CreateTable — TYPE RELATION
# ---------------------------------------------------------------------------


class TestCreateTableRelation:
    """Tests for CreateTable with TYPE RELATION."""

    def test_relation_basic(self) -> None:
        op = CreateTable(
            name="likes",
            table_type="relation",
            schema_mode="SCHEMAFULL",
        )
        sql = op.forwards()
        assert "TYPE RELATION" in sql
        assert "DEFINE TABLE likes" in sql

    def test_relation_in_out(self) -> None:
        op = CreateTable(
            name="likes",
            table_type="relation",
            relation_in="person",
            relation_out="blog_post",
            schema_mode="SCHEMAFULL",
        )
        sql = op.forwards()
        assert "TYPE RELATION IN person OUT blog_post" in sql

    def test_relation_enforced(self) -> None:
        op = CreateTable(
            name="likes",
            table_type="relation",
            relation_in="person",
            relation_out="blog_post",
            enforced=True,
            schema_mode="SCHEMAFULL",
        )
        sql = op.forwards()
        assert "ENFORCED" in sql
        assert "IN person" in sql
        assert "OUT blog_post" in sql

    def test_relation_multiple_out(self) -> None:
        op = CreateTable(
            name="likes",
            table_type="relation",
            relation_in="person",
            relation_out="blog_post | book",
            schema_mode="SCHEMAFULL",
        )
        sql = op.forwards()
        assert "OUT blog_post | book" in sql

    def test_normal_type_not_emitted(self) -> None:
        op = CreateTable(name="users", table_type="normal", schema_mode="SCHEMAFULL")
        sql = op.forwards()
        assert "TYPE" not in sql

    def test_user_type_emitted(self) -> None:
        op = CreateTable(name="users", table_type="user", schema_mode="SCHEMAFULL")
        sql = op.forwards()
        assert "TYPE USER" in sql

    def test_any_type_emitted(self) -> None:
        op = CreateTable(name="data", table_type="any", schema_mode="SCHEMAFULL")
        sql = op.forwards()
        assert "TYPE ANY" in sql


# ---------------------------------------------------------------------------
# Parser — TYPE RELATION IN/OUT/ENFORCED
# ---------------------------------------------------------------------------


class TestParseDefineTableRelation:
    """Tests for parsing DEFINE TABLE ... TYPE RELATION."""

    def test_parse_relation_basic(self) -> None:
        stmt = "DEFINE TABLE likes TYPE RELATION SCHEMAFULL"
        result = parse_define_table(stmt)
        assert result["table_type"] == "relation"
        assert result["relation_in"] is None
        assert result["relation_out"] is None
        assert result["enforced"] is False

    def test_parse_relation_in_out(self) -> None:
        stmt = "DEFINE TABLE likes TYPE RELATION IN person OUT blog_post SCHEMAFULL"
        result = parse_define_table(stmt)
        assert result["table_type"] == "relation"
        assert result["relation_in"] == "person"
        assert result["relation_out"] == "blog_post"

    def test_parse_relation_enforced(self) -> None:
        stmt = "DEFINE TABLE likes TYPE RELATION IN person OUT blog_post ENFORCED SCHEMAFULL"
        result = parse_define_table(stmt)
        assert result["table_type"] == "relation"
        assert result["enforced"] is True

    def test_parse_relation_multi_in_out(self) -> None:
        stmt = "DEFINE TABLE likes TYPE RELATION IN person | company OUT blog_post | book SCHEMAFULL"
        result = parse_define_table(stmt)
        assert result["relation_in"] is not None
        assert "person" in result["relation_in"]
        assert result["relation_out"] is not None
        assert "blog_post" in result["relation_out"]

    def test_parse_any_type(self) -> None:
        stmt = "DEFINE TABLE data TYPE ANY SCHEMAFULL"
        result = parse_define_table(stmt)
        assert result["table_type"] == "any"

    def test_parse_normal_table(self) -> None:
        stmt = "DEFINE TABLE users SCHEMAFULL"
        result = parse_define_table(stmt)
        assert result["table_type"] == "normal"
        assert result["relation_in"] is None
        assert result["relation_out"] is None
        assert result["enforced"] is False


# ---------------------------------------------------------------------------
# SchemaState diff — relation fields
# ---------------------------------------------------------------------------


class TestRelationStateDiff:
    """Tests for SchemaState diff with relation type changes."""

    def _make_state(self, tables: dict[str, TableState]) -> SchemaState:
        state = SchemaState()
        state.tables = tables
        return state

    def test_create_relation_table(self) -> None:
        """Creating a new relation table should set relation fields."""
        target = self._make_state(
            {
                "likes": TableState(
                    name="likes",
                    table_type="relation",
                    relation_in="person",
                    relation_out="blog_post",
                    enforced=True,
                )
            }
        )
        current = self._make_state({})
        ops = current.diff(target)

        create_ops = [op for op in ops if isinstance(op, CreateTable)]
        assert len(create_ops) == 1
        assert create_ops[0].relation_in == "person"
        assert create_ops[0].relation_out == "blog_post"
        assert create_ops[0].enforced is True

    def test_relation_change_detected(self) -> None:
        """Changing relation_in/out should trigger table recreation."""
        current = self._make_state(
            {
                "likes": TableState(
                    name="likes",
                    table_type="relation",
                    relation_in="person",
                    relation_out="blog_post",
                )
            }
        )
        target = self._make_state(
            {
                "likes": TableState(
                    name="likes",
                    table_type="relation",
                    relation_in="person",
                    relation_out="blog_post | book",
                )
            }
        )
        ops = current.diff(target)

        create_ops = [op for op in ops if isinstance(op, CreateTable)]
        assert len(create_ops) == 1
        assert "book" in create_ops[0].relation_out

    def test_enforced_change_detected(self) -> None:
        """Changing enforced flag should trigger table recreation."""
        current = self._make_state(
            {
                "likes": TableState(
                    name="likes",
                    table_type="relation",
                    enforced=False,
                )
            }
        )
        target = self._make_state(
            {
                "likes": TableState(
                    name="likes",
                    table_type="relation",
                    enforced=True,
                )
            }
        )
        ops = current.diff(target)

        create_ops = [op for op in ops if isinstance(op, CreateTable)]
        assert len(create_ops) == 1
        assert create_ops[0].enforced is True

    def test_no_change_no_ops(self) -> None:
        """Identical relation tables produce no operations."""
        current = self._make_state(
            {
                "likes": TableState(
                    name="likes",
                    table_type="relation",
                    relation_in="person",
                    relation_out="blog_post",
                    enforced=True,
                )
            }
        )
        target = self._make_state(
            {
                "likes": TableState(
                    name="likes",
                    table_type="relation",
                    relation_in="person",
                    relation_out="blog_post",
                    enforced=True,
                )
            }
        )
        ops = current.diff(target)

        create_ops = [op for op in ops if isinstance(op, CreateTable)]
        assert len(create_ops) == 0


# ---------------------------------------------------------------------------
# Model generator — relation config
# ---------------------------------------------------------------------------


class TestModelGeneratorRelation:
    """Tests for ModelCodeGenerator with relation tables."""

    def test_generate_relation_model(self) -> None:
        from surreal_orm.migrations.model_generator import ModelCodeGenerator

        gen = ModelCodeGenerator()
        table = TableState(
            name="likes",
            table_type="relation",
            relation_in="person",
            relation_out="blog_post",
            enforced=True,
        )
        block, extra_imports = gen._generate_model_class(table)

        assert "table_type=TableType.RELATION" in block
        assert "relation_in='person'" in block
        assert "relation_out='blog_post'" in block
        assert "enforced=True" in block
        assert "from surreal_orm.types import TableType" in extra_imports
