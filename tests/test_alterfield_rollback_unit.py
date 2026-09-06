"""
Unit tests for what ``AlterField.backwards()`` restores.

Found reviewing what shipped unreviewed. ``backwards()`` re-emits
``DEFINE FIELD OVERWRITE``, which replaces the whole definition — so every
clause it has no ``previous_*`` slot for is silently dropped. Two were missing,
and the consequence of one is severe:

    AlterField.from_field_states("users", encrypted_password, plain_password)
    forwards()  -> DEFINE FIELD OVERWRITE password ON users TYPE string;
    backwards() -> DEFINE FIELD OVERWRITE password ON users TYPE string;

The rollback drops ``VALUE crypto::argon2::generate($value)``, so the column
stops hashing and subsequent writes store the password in plaintext. Computed
fields lose their expression the same way.

This was latent while a rollback was a silent no-op; #162's ``OVERWRITE`` fix
made it actively destructive.
"""

from src.surreal_orm.migrations.operations import AlterField
from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState


class TestRollbackRestoresTheValueClause:
    """``VALUE`` is what makes an Encrypted or Computed column work."""

    def test_an_encrypted_column_is_restored_as_encrypted(self) -> None:
        """Otherwise the column silently stops hashing."""
        op = AlterField(
            table="users",
            name="password",
            field_type="string",
            previous_type="string",
            previous_encrypted=True,
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

    def test_encryption_wins_over_a_plain_value(self) -> None:
        """Mirrors ``forwards()``, where ``encrypted`` takes precedence."""
        op = AlterField(
            table="users",
            name="password",
            field_type="string",
            previous_type="string",
            previous_encrypted=True,
            previous_value="something_else",
        )

        assert "crypto::argon2::generate" in op.backwards()
        assert "something_else" not in op.backwards()

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
