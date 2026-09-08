"""
Unit tests for what ``AlterField.backwards()`` restores.

``backwards()`` re-emits ``DEFINE FIELD OVERWRITE``, which replaces the whole
definition — so every clause it has no ``previous_*`` slot for, or renders
differently from ``forwards()``, is silently lost. Losing ``VALUE`` is the
severe one: an Encrypted column stops hashing and stores plaintext from then
on. This was latent while a rollback was a silent no-op; #162's ``OVERWRITE``
fix made it actively destructive.
"""

from dataclasses import fields

import pytest

from src.surreal_orm.migrations.operations import (
    ARGON2_VALUE,
    FIELD_DEFINITION_FIELDS,
    AddField,
    AlterField,
    _previous_attr,
)
from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState


class TestRollbackRestoresTheValueClause:
    """``VALUE`` is what makes an Encrypted or Computed column work."""

    def test_an_encrypted_column_is_restored_as_encrypted(self) -> None:
        """Otherwise the column silently stops hashing."""
        op = AlterField.from_field_states(
            "users",
            FieldState(name="password", field_type="string", nullable=False, encrypted=True),
            FieldState(name="password", field_type="string", nullable=False),
        )

        assert "VALUE crypto::argon2::generate($value)" in op.backwards()

    def test_a_computed_column_keeps_its_expression(self) -> None:
        """A Computed field without its VALUE is just an empty column."""
        op = AlterField(
            table="users",
            name="full_name",
            field_type="string",
            previous_type="string",
            previous_value="string::concat(first_name, ' ', last_name)",
        )

        assert "VALUE string::concat(first_name, ' ', last_name)" in op.backwards()

    def test_encryption_is_carried_as_the_value_expression(self) -> None:
        """``encrypted`` *is* "VALUE is argon2"; there is no second slot to disagree with."""
        op = AlterField.from_field_states(
            "users",
            FieldState(name="password", field_type="string", nullable=False, encrypted=True),
            FieldState(name="password", field_type="string", nullable=False),
        )

        assert op.previous_value == ARGON2_VALUE

    def test_a_column_that_had_no_value_gets_none(self) -> None:
        """No clause invented where there was none."""
        op = AlterField(table="users", name="age", field_type="int", previous_type="string")

        assert "VALUE" not in op.backwards()

    def test_the_value_clause_precedes_default(self) -> None:
        """Clause order the server accepts, same as ``forwards()``."""
        op = AlterField(
            table="users",
            name="f",
            field_type="string",
            previous_type="string",
            previous_value="time::now()",
            previous_default="x",
        )
        rollback = op.backwards()

        assert rollback.index("VALUE") < rollback.index("DEFAULT")


class TestTheDiffCarriesThem:
    """A generated rollback is only as good as what the diff forwarded."""

    def test_from_field_states_wires_both_attributes(self) -> None:
        """The factory is the single place these are wired."""
        current = FieldState(name="password", field_type="string", encrypted=True)
        target = FieldState(name="password", field_type="string")

        op = AlterField.from_field_states("users", current, target)

        assert "VALUE crypto::argon2::generate($value)" in op.backwards()
        assert "VALUE" not in op.forwards()

    def test_a_real_diff_produces_a_restoring_rollback(self) -> None:
        """End to end: dropping encryption must be reversible."""
        current = SchemaState()
        current.tables["users"] = TableState(
            name="users",
            fields={"password": FieldState(name="password", field_type="string", encrypted=True)},
        )
        target = SchemaState()
        target.tables["users"] = TableState(
            name="users",
            fields={"password": FieldState(name="password", field_type="string")},
        )

        alter = next(op for op in current.diff(target) if isinstance(op, AlterField))

        assert "VALUE crypto::argon2::generate($value)" in alter.backwards()


class TestTheRollbackRendersLikeTheStatementItReverses:
    """The invariant, rather than one assertion per clause.

    ``AddField.forwards``, ``AlterField.forwards`` and ``AlterField.backwards``
    were three hand-written renderers of the same statement. Every clause added
    to one was eventually forgotten in another: REFERENCE (#170), VALUE (#195),
    and DEFAULT, which ``backwards()`` single-quoted unconditionally — so
    ``time::now()`` came back as the string ``'time::now()'``, ``True`` as
    ``True`` rather than ``true``, and an apostrophe produced a parse error.
    """

    @pytest.mark.parametrize(
        ("attribute", "value"),
        [
            ("default", "time::now()"),
            ("default", True),
            ("default", False),
            ("default", 7),
            ("default", "it's"),
            ("default", "plain"),
            ("assertion", "$value > 0"),
            ("flexible", True),
            ("readonly", True),
            ("value", "string::uppercase(name)"),
            ("encrypted", True),
            ("comment", "why this column exists"),
            ("comment", "o'brien"),
            ("nullable", True),
        ],
    )
    def test_backwards_matches_the_add_that_would_recreate_it(self, attribute: str, value: object) -> None:
        """Restoring a state must render it exactly as creating it would."""
        base = {"nullable": False} | {attribute: value}
        previous = FieldState(name="f", field_type="string", **base)
        target = FieldState(name="f", field_type="int", nullable=False)

        rollback = AlterField.from_field_states("t", previous, target).backwards()
        recreate = AddField.from_field_state("t", previous).forwards()

        assert rollback == recreate.replace("DEFINE FIELD ", "DEFINE FIELD OVERWRITE ", 1)

    def test_every_field_state_attribute_has_a_previous_slot(self) -> None:
        """A new clause on FieldState must not be able to reach only one direction."""
        # `encrypted` is deliberately not a slot of its own: it *is* "VALUE is
        # the argon2 expression", and `from_field_states` resolves it into
        # `previous_value`. A second slot could only disagree with the first.
        derived = {"name", "encrypted"}
        missing = [
            f.name
            for f in fields(FieldState)
            if f.name not in derived and _previous_attr(f.name) not in AlterField.__dataclass_fields__
        ]

        assert missing == [], f"AlterField cannot roll back: {missing}"

    def test_every_rendered_clause_is_in_the_shared_tuple(self) -> None:
        """The tuple is what keeps the three renderers in step."""
        for name in FIELD_DEFINITION_FIELDS:
            assert name in AlterField.__dataclass_fields__, name
            assert _previous_attr(name) in AlterField.__dataclass_fields__, name
