"""
Integration test for #635: server-reported text written into ``inspectdb`` output.

A ``VALUE`` clause holding a double quote and a backslash is read back from
``INFO FOR TABLE`` and turned into model source. The generated module is only
parsed with ``ast``; nothing is imported or executed.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncGenerator

import pytest

from src.surreal_orm.introspection import generate_models_from_db
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_sdk import SurrealDB
from src.surreal_sdk.connection.http import HTTPConnection
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_635"

pytestmark = pytest.mark.integration


@pytest.fixture
async def i635_db() -> AsyncGenerator[HTTPConnection, None]:
    async with SurrealDB.http(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE) as db:
        await db.signin(SURREALDB_USER, SURREALDB_PASS)
        await db.query(
            "REMOVE TABLE IF EXISTS i635_items;"
            " DEFINE TABLE i635_items SCHEMALESS;"
            ' DEFINE FIELD label ON i635_items VALUE "a\\"b" + \'c\\\\d\';'
            ' DEFINE EVENT audit ON i635_items WHEN $value.label = "x\\"y" THEN {};'
        )
        try:
            yield db
        finally:
            await db.query("REMOVE TABLE IF EXISTS i635_items;")


async def test_generated_computed_matches_the_server_value(i635_db: HTTPConnection) -> None:
    state = await DatabaseIntrospector(i635_db).introspect()
    value = state.tables["i635_items"].fields["label"].value
    assert value is not None
    assert '"' in value and "\\" in value

    source = await generate_models_from_db(connection=i635_db)
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [c.name for c in classes] == ["I635Item"]
    body = classes[0].body
    assert isinstance(body[0], ast.Assign)
    annotated = [stmt for stmt in body[1:] if isinstance(stmt, ast.AnnAssign)]
    assert len(annotated) == len(body) - 1
    names = [stmt.target.id for stmt in annotated if isinstance(stmt.target, ast.Name)]
    assert names == ["id", "label"]

    label = annotated[1]
    assert isinstance(label.value, ast.Call)
    assert isinstance(label.value.func, ast.Name) and label.value.func.id == "Computed"
    assert ast.literal_eval(label.value.args[0]) == value
