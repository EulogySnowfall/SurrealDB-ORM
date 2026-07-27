"""
Integration test pinning the CREATE vs UPDATE permission-denial bugfix.

Against a real SurrealDB instance with row-level ``PERMISSIONS``, a write made
by a non-owner record user is denied by the server (which returns an *empty*
result, not an error). Before the fix, the merge / persisted-save paths
swallowed that empty result and returned ``self`` silently — a denied update
was indistinguishable from a successful one.

This test signs in as a non-owner record user and asserts that:

- A denied ``merge()`` raises ``SurrealDbError`` (was a silent no-op).
- A denied persisted ``save()`` raises ``SurrealDbError`` (was a silent no-op).
- The owner can still update their own row (the fix doesn't break the happy path).
- The DB row is genuinely unchanged after a denied write.

Run with: pytest -m integration tests/test_permission_denial_integration.py
"""

from __future__ import annotations

import pytest

from src import surreal_orm
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
)
from src.surreal_orm.surreal_function import SurrealFunc
from tests.conftest import (
    SURREALDB_NAMESPACE,
    SURREALDB_PASS,
    SURREALDB_URL,
    SURREALDB_USER,
)

SURREALDB_DATABASE = "test_perm_denial"

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "owner_secret"
INTRUDER_EMAIL = "intruder@example.com"
INTRUDER_PASSWORD = "intruder_secret"


class Note(BaseSurrealModel):
    """A note guarded by row-level PERMISSIONS (only the owner may write)."""

    model_config = SurrealConfigDict(table_name="note")
    id: str | None = None
    title: str
    owner: str | None = None


@pytest.fixture(scope="module", autouse=True)
def setup_connection() -> None:
    """Point the ORM at the dedicated permission-denial test database."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


async def _root_client():
    """Return a freshly root-authenticated client (drops any record-auth context)."""
    client = await surreal_orm.SurrealDBConnectionManager.reconnect()
    assert client is not None
    return client


NOTE_ID = "testnote"


@pytest.fixture
async def seeded_note():
    """Create the schema, two record users, and one note owned by ``owner``.

    Yields the bare record id (``str``) of the seeded note. Leaves the
    connection authenticated as root. Cleans up before and after.
    """

    async def cleanup() -> None:
        client = await _root_client()
        await client.query("REMOVE ACCESS IF EXISTS note_user ON DATABASE;")
        await client.query("REMOVE TABLE IF EXISTS note;")
        await client.query("REMOVE TABLE IF EXISTS note_user;")

    await cleanup()

    client = await _root_client()
    # Schema: a record-auth user table + a note table that only its owner may
    # create/select/update/delete.
    await client.query(
        """
        DEFINE TABLE note_user SCHEMAFULL;
        DEFINE FIELD email ON note_user TYPE string;
        DEFINE FIELD password ON note_user TYPE string;
        DEFINE INDEX note_user_email ON note_user FIELDS email UNIQUE;

        DEFINE ACCESS note_user ON DATABASE TYPE RECORD
            SIGNUP (CREATE note_user SET
                email = $email,
                password = crypto::argon2::generate($password)
            )
            SIGNIN (SELECT * FROM note_user WHERE
                email = $email AND
                crypto::argon2::compare(password, $password)
            )
            DURATION FOR TOKEN 1h, FOR SESSION 24h;

        DEFINE TABLE note SCHEMAFULL
            PERMISSIONS
                FOR create, select, update, delete
                WHERE owner = $auth.id;
        DEFINE FIELD title ON note TYPE string;
        DEFINE FIELD owner ON note TYPE record<note_user>;
        """
    )

    # Create both record users via record-auth signup.
    await client.signup(
        namespace=SURREALDB_NAMESPACE,
        database=SURREALDB_DATABASE,
        access="note_user",
        email=OWNER_EMAIL,
        password=OWNER_PASSWORD,
    )
    client = await _root_client()
    await client.signup(
        namespace=SURREALDB_NAMESPACE,
        database=SURREALDB_DATABASE,
        access="note_user",
        email=INTRUDER_EMAIL,
        password=INTRUDER_PASSWORD,
    )

    # As root (permissions bypassed), seed a note with a known id owned by OWNER.
    client = await _root_client()
    await client.query(
        """
        LET $owner = (SELECT VALUE id FROM note_user WHERE email = $email)[0];
        CREATE type::record('note', $note_id) SET title = 'original', owner = $owner;
        """,
        {"email": OWNER_EMAIL, "note_id": NOTE_ID},
    )

    yield NOTE_ID

    await cleanup()


async def _signin(email: str, password: str) -> None:
    """Sign the singleton ORM client in as a record user (changes auth context)."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.signin(
        namespace=SURREALDB_NAMESPACE,
        database=SURREALDB_DATABASE,
        access="note_user",
        email=email,
        password=password,
    )


@pytest.mark.integration
class TestPermissionDenialRaises:
    """The denied update/merge paths must raise, mirroring the create guard."""

    async def test_denied_merge_raises(self, seeded_note: str) -> None:
        """A non-owner merge is denied by the DB and must raise, not no-op."""
        note_id = seeded_note

        # Authenticate as the intruder (not the owner).
        await _signin(INTRUDER_EMAIL, INTRUDER_PASSWORD)

        note = Note(id=note_id, title="original", owner=None)
        note._db_persisted = True

        with pytest.raises(SurrealDbError):
            await note.merge(title="hacked", refresh=False)

        # Verify the row is genuinely unchanged (root bypasses permissions).
        client = await _root_client()
        rows = await client.query("SELECT title FROM note;")
        assert rows.first["title"] == "original"

    async def test_denied_persisted_save_raises(self, seeded_note: str) -> None:
        """A non-owner save() on a persisted instance must also raise."""
        note_id = seeded_note

        await _signin(INTRUDER_EMAIL, INTRUDER_PASSWORD)

        note = Note(id=note_id, title="original", owner=None)
        note._db_persisted = True
        note.title = "hacked-via-save"

        with pytest.raises(SurrealDbError):
            await note.save()

        client = await _root_client()
        rows = await client.query("SELECT title FROM note;")
        assert rows.first["title"] == "original"

    async def test_owner_merge_succeeds(self, seeded_note: str) -> None:
        """The happy path is unaffected: the owner can update their own note."""
        note_id = seeded_note

        # Authenticate as the owner.
        await _signin(OWNER_EMAIL, OWNER_PASSWORD)

        note = Note(id=note_id, title="original", owner=None)
        note._db_persisted = True

        result = await note.merge(title="legitimately-updated", refresh=False)
        assert result is note

        # Verify the update actually landed.
        client = await _root_client()
        rows = await client.query("SELECT title FROM note;")
        assert rows.first["title"] == "legitimately-updated"


@pytest.mark.integration
class TestPermissionDenialSurrealFunc:
    """The SurrealFunc (raw UPDATE) merge path must also surface denial.

    A SurrealFunc value routes through ``client.query("UPDATE ... SET ...")``,
    a different code path from the plain ``client.merge()`` above. SurrealDB
    returns an empty result for a denied update there too, so it must raise.
    """

    async def test_denied_surrealfunc_merge_raises(self, seeded_note: str) -> None:
        note_id = seeded_note

        await _signin(INTRUDER_EMAIL, INTRUDER_PASSWORD)

        note = Note(id=note_id, title="original", owner=None)
        note._db_persisted = True

        with pytest.raises(SurrealDbError):
            await note.merge(title=SurrealFunc("string::uppercase('hacked')"), refresh=False)

        # Row unchanged.
        client = await _root_client()
        rows = await client.query("SELECT title FROM note;")
        assert rows.first["title"] == "original"

    async def test_owner_surrealfunc_merge_succeeds(self, seeded_note: str) -> None:
        note_id = seeded_note

        await _signin(OWNER_EMAIL, OWNER_PASSWORD)

        note = Note(id=note_id, title="original", owner=None)
        note._db_persisted = True

        result = await note.merge(title=SurrealFunc("string::uppercase('legit')"), refresh=False)
        assert result is note

        client = await _root_client()
        rows = await client.query("SELECT title FROM note;")
        assert rows.first["title"] == "LEGIT"
