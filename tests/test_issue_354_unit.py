"""
Unit tests for issue #354: the migration state of a ``TableType.USER`` model
carries a UNIQUE index on the identifier field, and the diff neither repeats
nor duplicates it.
"""

import pytest

from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.operations import CreateIndex, DropIndex, split_destructive
from src.surreal_orm.migrations.state import IndexState, SchemaState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.types import SchemaMode, TableType


class I354User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name="i354_user",
        schema_mode=SchemaMode.SCHEMAFULL,
    )
    id: str | None = None
    email: str
    password: Encrypted


class I354Member(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name="i354_member",
        identifier_field="username",
    )
    id: str | None = None
    username: str
    email: str = ""
    password: Encrypted


class I354Post(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i354_post")
    id: str | None = None
    email: str


def _model_state(*models: type[BaseSurrealModel]) -> SchemaState:
    return ModelIntrospector(list(models)).introspect()


def _index_ops(operations: list) -> list:
    return [op for op in operations if isinstance(op, CreateIndex | DropIndex)]


class TestIntrospectedIndex:
    def test_default_identifier_gets_a_unique_index(self) -> None:
        table = _model_state(I354User).tables["i354_user"]

        assert table.indexes == {
            "i354_user_email_unique": IndexState(name="i354_user_email_unique", fields=["email"], unique=True),
        }

    def test_custom_identifier_gets_the_unique_index(self) -> None:
        table = _model_state(I354Member).tables["i354_member"]

        assert table.indexes == {
            "i354_member_username_unique": IndexState(
                name="i354_member_username_unique",
                fields=["username"],
                unique=True,
            ),
        }

    def test_non_user_table_gets_no_index(self) -> None:
        assert _model_state(I354Post).tables["i354_post"].indexes == {}

    def test_index_name_is_stable_across_runs(self) -> None:
        assert _model_state(I354User) == _model_state(I354User)


class TestDiff:
    def test_new_user_table_emits_one_unique_index(self) -> None:
        operations = SchemaState().diff(_model_state(I354User))

        indexes = _index_ops(operations)
        assert len(indexes) == 1
        index = indexes[0]
        assert isinstance(index, CreateIndex)
        assert (index.table, index.name, index.fields, index.unique) == (
            "i354_user",
            "i354_user_email_unique",
            ["email"],
            True,
        )
        assert index.forwards() == "DEFINE INDEX i354_user_email_unique ON i354_user FIELDS email UNIQUE;"

    def test_existing_user_table_without_the_index_gets_it(self) -> None:
        current = _model_state(I354User)
        current.tables["i354_user"].indexes.clear()

        indexes = _index_ops(current.diff(_model_state(I354User)))

        assert [(type(op), op.name) for op in indexes] == [(CreateIndex, "i354_user_email_unique")]

    def test_nothing_once_the_database_has_the_index(self) -> None:
        assert _model_state(I354User).diff(_model_state(I354User)) == []

    def test_unique_index_under_another_name_is_kept(self) -> None:
        current = _model_state(I354User)
        current.tables["i354_user"].indexes = {
            "users_email_idx": IndexState(name="users_email_idx", fields=["email"], unique=True),
        }

        operations = current.diff(_model_state(I354User))

        assert _index_ops(operations) == []
        keep, destructive = split_destructive(operations)
        assert keep == [] and destructive == []

    def test_non_unique_index_on_the_identifier_does_not_count(self) -> None:
        current = _model_state(I354User)
        current.tables["i354_user"].indexes = {
            "users_email_idx": IndexState(name="users_email_idx", fields=["email"]),
        }

        indexes = _index_ops(current.diff(_model_state(I354User)))

        assert [(type(op), op.name) for op in indexes] == [
            (CreateIndex, "i354_user_email_unique"),
            (DropIndex, "users_email_idx"),
        ]

    def test_composite_unique_index_does_not_count(self) -> None:
        current = _model_state(I354User)
        current.tables["i354_user"].indexes = {
            "users_email_tenant": IndexState(name="users_email_tenant", fields=["email", "tenant"], unique=True),
        }

        indexes = _index_ops(current.diff(_model_state(I354User)))

        assert [(type(op), op.name) for op in indexes] == [
            (CreateIndex, "i354_user_email_unique"),
            (DropIndex, "users_email_tenant"),
        ]

    def test_one_existing_index_satisfies_only_one_target(self) -> None:
        """Two target indexes with the same definition cannot both reuse one database index."""
        target = _model_state(I354User)
        target.tables["i354_user"].indexes["second_email_unique"] = IndexState(
            name="second_email_unique",
            fields=["email"],
            unique=True,
        )
        current = _model_state(I354User)
        current.tables["i354_user"].indexes = {
            "users_email_idx": IndexState(name="users_email_idx", fields=["email"], unique=True),
        }

        indexes = _index_ops(current.diff(target))

        assert len([op for op in indexes if isinstance(op, CreateIndex)]) == 1
        assert not [op for op in indexes if isinstance(op, DropIndex)]


@pytest.mark.parametrize("model", [I354User, I354Member])
def test_identifier_index_matches_the_access_identifier(model: type[BaseSurrealModel]) -> None:
    from src.surreal_orm.auth.access import AccessGenerator

    definition = AccessGenerator.from_model(model)
    assert definition is not None
    table = _model_state(model).tables[model.get_table_name()]

    assert [index.fields for index in table.indexes.values()] == [[definition.identifier_field]]
