"""Unit tests for #494 (v2): every validation alias name is a model name.

A KNN/search annotation key that is also a model name (``id``, a field or its
alias) is kept in the row, so the field is populated from it (#470). Only
``str`` aliases used to count: a field whose ``validation_alias`` is an
``AliasChoices`` or an ``AliasPath`` lost its value, because the annotation key
was stripped from the row before the model was built. These tests need no
server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from pydantic import AliasChoices, AliasPath, Field

from surreal_orm import BaseSurrealModel, SearchScore, SurrealConfigDict
from surreal_orm.query_set import QuerySet


class I494Choices(BaseSurrealModel):
    """A field read from either of two keys (``extra="allow"``)."""

    model_config = SurrealConfigDict(table_name="i494_choices", extra="allow")

    id: str | None = None
    title: str = ""
    score: float | None = Field(default=None, validation_alias=AliasChoices("score_a", "score_b"))


class I494Path(BaseSurrealModel):
    """A field read from the first item of a key (``extra="allow"``)."""

    model_config = SurrealConfigDict(table_name="i494_path", extra="allow")

    id: str | None = None
    title: str = ""
    score: float | None = Field(default=None, validation_alias=AliasPath("score_b", 0))


class I494Nested(BaseSurrealModel):
    """An ``AliasChoices`` whose second choice is an ``AliasPath``."""

    model_config = SurrealConfigDict(table_name="i494_nested")

    id: str | None = None
    title: str = ""
    score: float | None = Field(
        default=None,
        validation_alias=AliasChoices("score_a", AliasPath("score_b", 0)),
    )


class I494Plain(BaseSurrealModel):
    """A plain ``str`` alias, the case that already worked."""

    model_config = SurrealConfigDict(table_name="i494_plain")

    id: str | None = None
    title: str = ""
    score: float | None = Field(default=None, alias="score_b")


async def _scored(model: type[BaseSurrealModel], value: Any) -> Any:
    """Run a full-text query annotated ``score_b`` over one row carrying ``score_b``."""
    rows = [{"id": "x:1", "title": "quantum", "score_b": value}]
    qs = model.objects().search(title="quantum").annotate(score_b=SearchScore(0))
    with patch.object(QuerySet, "_execute_query", new=AsyncMock(return_value=rows)):
        (instance,) = await qs.exec()
    assert isinstance(instance, model)
    return instance


class TestAnnotationKeyIsAValidationAlias:
    async def test_alias_choices_populates_the_field(self) -> None:
        instance = await _scored(I494Choices, 1.5)
        assert instance.score == 1.5
        assert "score_b" not in (instance.model_extra or {})

    async def test_alias_path_populates_the_field(self) -> None:
        instance = await _scored(I494Path, [1.5])
        assert instance.score == 1.5
        assert "score_b" not in (instance.model_extra or {})

    async def test_alias_path_inside_alias_choices_populates_the_field(self) -> None:
        instance = await _scored(I494Nested, [1.5])
        assert instance.score == 1.5

    async def test_plain_str_alias_still_populates_the_field(self) -> None:
        instance = await _scored(I494Plain, 1.5)
        assert instance.score == 1.5


class TestModelNames:
    def test_alias_choices_names(self) -> None:
        names = I494Choices.objects()._model_names()
        assert {"id", "title", "score", "score_a", "score_b"} <= names

    def test_alias_path_first_element(self) -> None:
        names = I494Path.objects()._model_names()
        assert "score_b" in names
        assert 0 not in names

    def test_nested_alias_path_first_element(self) -> None:
        assert {"score_a", "score_b"} <= I494Nested.objects()._model_names()

    def test_plain_str_alias(self) -> None:
        assert I494Plain.objects()._model_names() == {"id", "title", "score", "score_b"}
