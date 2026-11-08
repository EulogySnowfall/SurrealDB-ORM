"""
Integration test for #637 (v2): server-reported text written into ``inspectdb`` output.

A ``VALUE`` clause holding a double quote and a backslash is read back from
``INFO FOR TABLE`` on SurrealDB 2.x and turned into model source. The
generated module is only parsed with ``ast``; nothing is imported or executed.
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

SURREALDB_DATABASE = "test_issue_637_v2"

pytestmark = pytest.mark.integration


@pytest.fixture
async def i637_db() -> AsyncGenerator[HTTPConnection, None]:
    async with SurrealDB.http(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE) as db:
        await db.signin(SURREALDB_USER, SURREALDB_PASS)
        await db.query(
            "REMOVE TABLE IF EXISTS i637_items;"
            " DEFINE TABLE i637_items SCHEMALESS;"
            ' DEFINE FIELD label ON i637_items VALUE "a\\"b" + \'c\\\\d\';'
            " DEFINE FIELD spot ON i637_items TYPE geometry<point> | null;"
            ' DEFINE EVENT audit ON i637_items WHEN $value.label = "x\\"y" THEN {};'
        )
        try:
            yield db
        finally:
            await db.query("REMOVE TABLE IF EXISTS i637_items;")


async def test_generated_module_round_trips_the_server_text(i637_db: HTTPConnection) -> None:
    state = await DatabaseIntrospector(i637_db).introspect()
    table = state.tables["i637_items"]
    value = table.fields["label"].value
    assert value is not None
    assert '"' in value and "\\" in value
    when = table.events["audit"].when
    assert '"' in when

    source = await generate_models_from_db(connection=i637_db)
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [c.name for c in classes] == ["I637Item"]
    body = classes[0].body
    assert isinstance(body[0], ast.Assign)
    annotated = [stmt for stmt in body[1:] if isinstance(stmt, ast.AnnAssign)]
    assert len(annotated) == len(body) - 1
    by_name = {stmt.target.id: stmt for stmt in annotated if isinstance(stmt.target, ast.Name)}
    assert list(by_name) == ["id", "label", "spot"]

    label = by_name["label"]
    assert isinstance(label.value, ast.Call)
    assert isinstance(label.value.func, ast.Name) and label.value.func.id == "Computed"
    assert ast.literal_eval(label.value.args[0]) == value

    # A nullable geometry column, reported by 2.x as ``geometry<point> | null``.
    assert ast.unparse(by_name["spot"].annotation) == "GeoField['point'] | None"

    assert f"    # - audit: WHEN {when}" in source.split("\n")
