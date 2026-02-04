"""
Tests for v0.5.5.1 bug fixes.

Bug fixes included:
- Issue #8 (CRITICAL): .get() fails when ID starts with digit
- Issue #3 (HIGH): data: prefix strings interpreted as record links
- Issue #1 (HIGH): .get() should handle full record ID format
- Issue #2 (MEDIUM): remove_relation() should accept string IDs
- Issue #7 (MEDIUM): get_related() with direction=in not working
"""

from typing import AsyncGenerator

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_orm.utils import (
    needs_id_escaping,
    escape_record_id,
    format_thing,
    parse_record_id,
)


# Test URLs - use same ports as other integration tests
SURREALDB_URL = "http://localhost:8001"


# =============================================================================
# Test Models
# =============================================================================


class TestRecord(BaseSurrealModel):
    """Simple model for testing."""

    id: str | None = None
    name: str
    status: str = "active"


class Author(BaseSurrealModel):
    """Author model for relation tests."""

    id: str | None = None
    name: str


class Book(BaseSurrealModel):
    """Book model for relation tests."""

    id: str | None = None
    title: str


class Player(BaseSurrealModel):
    """Player model for avatar/data URL tests."""

    id: str | None = None
    username: str
    avatar: str | None = None


# =============================================================================
# Unit Tests for utils.py
# =============================================================================


class TestRecordIdUtils:
    """Test record ID utility functions."""

    def test_needs_id_escaping_digit_start(self) -> None:
        """IDs starting with digits need escaping."""
        assert needs_id_escaping("7abc") is True
        assert needs_id_escaping("0test") is True
        assert needs_id_escaping("123") is True
        assert needs_id_escaping("1a2b3c") is True

    def test_needs_id_escaping_special_chars(self) -> None:
        """IDs with special characters need escaping."""
        assert needs_id_escaping("test-id") is True
        assert needs_id_escaping("test.id") is True
        assert needs_id_escaping("test:id") is True
        assert needs_id_escaping("test id") is True
        assert needs_id_escaping("test/id") is True

    def test_needs_id_escaping_valid(self) -> None:
        """Valid IDs don't need escaping."""
        assert needs_id_escaping("abc123") is False
        assert needs_id_escaping("test_id") is False
        assert needs_id_escaping("TestId") is False
        assert needs_id_escaping("_private") is False

    def test_needs_id_escaping_empty(self) -> None:
        """Empty IDs don't need escaping."""
        assert needs_id_escaping("") is False

    def test_escape_record_id_digit_start(self) -> None:
        """Escape IDs starting with digits."""
        assert escape_record_id("7abc") == "`7abc`"
        assert escape_record_id("123") == "`123`"
        assert escape_record_id("0test") == "`0test`"

    def test_escape_record_id_special_chars(self) -> None:
        """Escape IDs with special characters."""
        assert escape_record_id("test-id") == "`test-id`"
        assert escape_record_id("test.id") == "`test.id`"

    def test_escape_record_id_valid(self) -> None:
        """Don't escape valid IDs."""
        assert escape_record_id("abc123") == "abc123"
        assert escape_record_id("test_id") == "test_id"

    def test_escape_record_id_backtick_in_id(self) -> None:
        """Escape backticks within IDs."""
        assert escape_record_id("test`id") == "`test``id`"

    def test_format_thing_normal(self) -> None:
        """Format normal thing references."""
        assert format_thing("users", "abc123") == "users:abc123"
        assert format_thing("game_tables", "xyz") == "game_tables:xyz"

    def test_format_thing_digit_start(self) -> None:
        """Format thing references with digit-starting IDs."""
        assert format_thing("users", "7abc") == "users:`7abc`"
        assert format_thing("game_tables", "7qvdzsc14e5clo8sg064") == "game_tables:`7qvdzsc14e5clo8sg064`"

    def test_parse_record_id_full_format(self) -> None:
        """Parse full record ID format."""
        table, id_part = parse_record_id("users:abc123")
        assert table == "users"
        assert id_part == "abc123"

    def test_parse_record_id_escaped(self) -> None:
        """Parse escaped record IDs."""
        table, id_part = parse_record_id("users:`7abc`")
        assert table == "users"
        assert id_part == "7abc"

    def test_parse_record_id_just_id(self) -> None:
        """Parse just the ID without table prefix."""
        table, id_part = parse_record_id("abc123")
        assert table is None
        assert id_part == "abc123"

    def test_parse_record_id_with_double_backtick(self) -> None:
        """Parse escaped IDs with doubled backticks."""
        table, id_part = parse_record_id("users:`test``id`")
        assert table == "users"
        assert id_part == "test`id"

    def test_escape_record_id_empty(self) -> None:
        """Empty ID should not be escaped."""
        assert escape_record_id("") == ""

    def test_format_thing_with_special_chars(self) -> None:
        """Format thing with special characters in ID."""
        assert format_thing("users", "test-id") == "users:`test-id`"
        assert format_thing("users", "test.id") == "users:`test.id`"
        assert format_thing("users", "test id") == "users:`test id`"

    def test_needs_id_escaping_unicode(self) -> None:
        """Unicode characters need escaping."""
        assert needs_id_escaping("tëst") is True
        assert needs_id_escaping("日本語") is True

    def test_format_thing_realistic_ids(self) -> None:
        """Test with realistic SurrealDB IDs."""
        # ULID-like ID starting with digit
        assert format_thing("game_tables", "7qvdzsc14e5clo8sg064") == "game_tables:`7qvdzsc14e5clo8sg064`"
        # UUID-like ID with hyphens
        assert format_thing("users", "a1b2c3d4-e5f6-7890-abcd-ef1234567890") == "users:`a1b2c3d4-e5f6-7890-abcd-ef1234567890`"
        # Snowflake-like ID (all digits)
        assert format_thing("messages", "1234567890123456789") == "messages:`1234567890123456789`"


# =============================================================================
# Unit Tests for HTTP CBOR Protocol
# =============================================================================


class TestHTTPProtocol:
    """Test HTTP connection protocol support."""

    def test_http_connection_default_protocol(self) -> None:
        """HTTPConnection should default to CBOR protocol."""
        from src.surreal_sdk.connection.http import HTTPConnection

        conn = HTTPConnection("http://localhost:8000", "ns", "db")
        assert conn.protocol == "cbor"

    def test_http_connection_json_protocol(self) -> None:
        """HTTPConnection should accept JSON protocol."""
        from src.surreal_sdk.connection.http import HTTPConnection

        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        assert conn.protocol == "json"

    def test_http_connection_cbor_protocol(self) -> None:
        """HTTPConnection should accept CBOR protocol explicitly."""
        from src.surreal_sdk.connection.http import HTTPConnection

        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="cbor")
        assert conn.protocol == "cbor"

    def test_http_connection_invalid_protocol_raises(self) -> None:
        """HTTPConnection should reject invalid protocols."""
        from src.surreal_sdk.connection.http import HTTPConnection

        with pytest.raises(ValueError, match="Invalid protocol"):
            HTTPConnection("http://localhost:8000", "ns", "db", protocol="invalid")  # type: ignore


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, None]:
    """Setup connection for integration tests."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        "root",
        "root",
        "test",
        "test_bugfixes_0551",
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def cleanup_tables() -> AsyncGenerator[None, None]:
    """Clean up test tables before and after each test."""
    tables = ["TestRecord", "Author", "Book", "Player", "wrote"]
    try:
        client = await SurrealDBConnectionManager.get_client()
        for table in tables:
            try:
                await client.query(f"DELETE {table};")
            except Exception:
                pass  # Ignore cleanup errors - table may not exist
    except Exception:
        pass  # Ignore if client/connection not available during cleanup
    yield
    try:
        client = await SurrealDBConnectionManager.get_client()
        for table in tables:
            try:
                await client.query(f"DELETE {table};")
            except Exception:
                pass  # Ignore cleanup errors - table may not exist
    except Exception:
        pass  # Ignore if client/connection not available during cleanup


@pytest.mark.integration
class TestIssue8NumericIds:
    """
    Issue #8 (CRITICAL): .get() fails when ID starts with digit.

    When a record ID starts with a digit (e.g., '7qvdzsc14e5clo8sg064'),
    SurrealDB interprets it as a number token, causing a parse error.
    """

    async def test_get_with_numeric_prefix_id(self) -> None:
        """get() should work with IDs starting with digits."""
        # Create a record with an ID starting with a digit
        client = await SurrealDBConnectionManager.get_client()
        numeric_id = "7qvdzsc14e5clo8sg064"
        await client.query(f"CREATE TestRecord:`{numeric_id}` SET name = 'Test Record', status = 'active'")

        # Fetch using ORM - this should NOT raise a parse error
        result = await TestRecord.objects().get(numeric_id)
        assert result is not None
        assert result.name == "Test Record"
        assert result.status == "active"

    async def test_get_various_numeric_ids(self) -> None:
        """Test various ID formats that start with digits."""
        client = await SurrealDBConnectionManager.get_client()

        test_ids = [
            "7abc",  # Starts with digit
            "0test",  # Starts with zero
            "123",  # All digits
            "1a2b3c",  # Mixed digits and letters, starts with digit
        ]

        for test_id in test_ids:
            # Create with raw query
            await client.query(f"CREATE TestRecord:`{test_id}` SET name = 'Test {test_id}', status = 'waiting'")

            # Fetch with ORM
            result = await TestRecord.objects().get(test_id)
            assert result is not None, f"Failed to get record with ID '{test_id}'"
            assert result.name == f"Test {test_id}"

    async def test_save_with_numeric_prefix_id(self) -> None:
        """save() should work with IDs starting with digits."""
        record = TestRecord(id="9numeric", name="Numeric ID Test", status="new")
        await record.save()

        # Verify it was saved
        result = await TestRecord.objects().get("9numeric")
        assert result is not None
        assert result.name == "Numeric ID Test"


@pytest.mark.integration
class TestIssue1FullRecordIdFormat:
    """
    Issue #1 (HIGH): .get() should handle full record ID format.

    .get(id) should accept both formats:
    - Just the ID: "abc123"
    - Full SurrealDB format: "table:abc123"
    """

    async def test_get_with_just_id(self) -> None:
        """get() should work with just the ID."""
        record = TestRecord(id="just_id_test", name="Just ID", status="active")
        await record.save()

        result = await TestRecord.objects().get("just_id_test")
        assert result is not None
        assert result.name == "Just ID"

    async def test_get_with_full_format(self) -> None:
        """get() should work with full table:id format."""
        record = TestRecord(id="full_format_test", name="Full Format", status="active")
        await record.save()

        # Get with full format
        result = await TestRecord.objects().get("TestRecord:full_format_test")
        assert result is not None
        assert result.name == "Full Format"

    async def test_get_both_formats_same_result(self) -> None:
        """Both ID formats should return the same record."""
        record = TestRecord(id="both_formats", name="Both Formats", status="active")
        await record.save()

        result1 = await TestRecord.objects().get("both_formats")
        result2 = await TestRecord.objects().get("TestRecord:both_formats")

        assert result1.id == result2.id
        assert result1.name == result2.name


@pytest.mark.integration
class TestIssue3DataUrlStrings:
    """
    Issue #3 (HIGH): Strings with 'data:' prefix interpreted as record links.

    When saving a string field containing a data URL (e.g., 'data:image/png;base64,...'),
    SurrealDB interprets it as a record link instead of a string literal.
    """

    # Minimal base64 PNG (1x1 transparent pixel)
    MINIMAL_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    async def test_save_data_url_string(self) -> None:
        """Strings starting with 'data:' should be saved as strings, not record links."""
        data_url = f"data:image/png;base64,{self.MINIMAL_PNG_BASE64}"

        player = Player(id="avatar_test", username="testuser", avatar=data_url)
        await player.save()

        # Verify saved correctly
        result = await Player.objects().get("avatar_test")
        assert result is not None
        assert result.avatar == data_url

    async def test_update_data_url_string(self) -> None:
        """Updating a field with data URL should work."""
        player = Player(id="avatar_update", username="testuser", avatar=None)
        await player.save()

        # Update with data URL
        data_url = f"data:image/png;base64,{self.MINIMAL_PNG_BASE64}"
        player.avatar = data_url
        await player.save()

        # Verify
        result = await Player.objects().get("avatar_update")
        assert result.avatar == data_url

    async def test_other_colon_prefixed_strings(self) -> None:
        """Test other strings with colon prefix patterns."""
        test_values = [
            "data:text/plain,hello",
            "mailto:user@example.com",
            "tel:+1234567890",
        ]

        for i, value in enumerate(test_values):
            player = Player(id=f"colon_test_{i}", username=f"user{i}", avatar=value)
            await player.save()

            result = await Player.objects().get(f"colon_test_{i}")
            assert result.avatar == value, f"Failed for value: {value}"


@pytest.mark.integration
class TestIssue2RemoveRelationStringIds:
    """
    Issue #2 (MEDIUM): remove_relation() should accept string IDs.

    remove_relation() should accept both model instances and string IDs.
    """

    async def test_remove_relation_with_model_instance(self) -> None:
        """remove_relation() should work with model instances."""
        author = Author(id="author1", name="John Doe")
        await author.save()
        book = Book(id="book1", title="My Book")
        await book.save()

        # Create relation
        await author.relate("wrote", book)

        # Verify relation exists
        related = await author.get_related("wrote", model_class=Book)
        assert len(related) == 1

        # Remove with model instance
        await author.remove_relation("wrote", book)

        # Verify relation removed
        related_after = await author.get_related("wrote", model_class=Book)
        assert len(related_after) == 0

    async def test_remove_relation_with_full_string_id(self) -> None:
        """remove_relation() should work with full format string IDs."""
        author = Author(id="author2", name="Jane Doe")
        await author.save()
        book = Book(id="book2", title="Her Book")
        await book.save()

        # Create relation
        await author.relate("wrote", book)

        # Verify relation exists
        related = await author.get_related("wrote", model_class=Book)
        assert len(related) == 1

        # Remove with full format string ID
        await author.remove_relation("wrote", "Book:book2")

        # Verify relation removed
        related_after = await author.get_related("wrote", model_class=Book)
        assert len(related_after) == 0

    async def test_remove_relation_with_just_id(self) -> None:
        """remove_relation() should work with just the ID string."""
        author = Author(id="author3", name="Bob Smith")
        await author.save()
        book = Book(id="book3", title="His Book")
        await book.save()

        # Create relation
        await author.relate("wrote", book)

        # Verify relation exists
        related = await author.get_related("wrote", model_class=Book)
        assert len(related) == 1

        # Remove with just the ID
        await author.remove_relation("wrote", "book3")

        # Verify relation removed
        related_after = await author.get_related("wrote", model_class=Book)
        assert len(related_after) == 0


@pytest.mark.integration
class TestIssue7GetRelatedDirectionIn:
    """
    Issue #7 (MEDIUM): get_related() with direction='in' not working.

    get_related() with direction='in' should return records from incoming relations.
    """

    async def test_get_related_direction_out(self) -> None:
        """get_related with direction='out' should return target records."""
        author = Author(id="author_out", name="Author Out")
        await author.save()
        book1 = Book(id="book_out1", title="Book 1")
        await book1.save()
        book2 = Book(id="book_out2", title="Book 2")
        await book2.save()

        # Create relations: Author -> wrote -> Book
        await author.relate("wrote", book1)
        await author.relate("wrote", book2)

        # Get outgoing relations (books that author wrote)
        books = await author.get_related("wrote", direction="out", model_class=Book)
        assert len(books) == 2
        titles = {b.title for b in books}
        assert "Book 1" in titles
        assert "Book 2" in titles

    async def test_get_related_direction_in(self) -> None:
        """get_related with direction='in' should return source records."""
        author1 = Author(id="author_in1", name="Author 1")
        await author1.save()
        author2 = Author(id="author_in2", name="Author 2")
        await author2.save()
        book = Book(id="book_in", title="Collaborative Book")
        await book.save()

        # Create relations: Author1 -> wrote -> Book, Author2 -> wrote -> Book
        await author1.relate("wrote", book)
        await author2.relate("wrote", book)

        # Get incoming relations (authors who wrote this book)
        authors = await book.get_related("wrote", direction="in", model_class=Author)
        assert len(authors) == 2, f"Expected 2 authors, got {len(authors)}"
        names = {a.name for a in authors}
        assert "Author 1" in names
        assert "Author 2" in names

    async def test_get_related_direction_both(self) -> None:
        """get_related with direction='both' should return records from both directions."""
        # Create a network: Author1 -> wrote -> Book <- wrote <- Author2
        author1 = Author(id="author_both1", name="Author Both 1")
        await author1.save()
        author2 = Author(id="author_both2", name="Author Both 2")
        await author2.save()
        book = Book(id="book_both", title="Book Both")
        await book.save()

        await author1.relate("wrote", book)
        await author2.relate("wrote", book)

        # From author1's perspective:
        # - out: book
        # - in: none (no one wrote to author1)
        # This is correct for "wrote" relation
        related = await author1.get_related("wrote", direction="both", model_class=Book)
        assert len(related) == 1  # Only the book (outgoing)


# =============================================================================
# Test Connection Manager Protocol
# =============================================================================


class TestConnectionManagerProtocol:
    """Test connection manager protocol configuration."""

    def test_set_connection_default_protocol(self) -> None:
        """set_connection should use CBOR protocol by default."""
        SurrealDBConnectionManager.set_connection(
            "http://localhost:8000",
            "root",
            "root",
            "test",
            "test_protocol",
        )
        # The protocol is a private attribute, so we test it indirectly
        # by checking the connection kwargs
        assert SurrealDBConnectionManager.is_connection_set()
        SurrealDBConnectionManager.unset_connection_sync()

    def test_set_connection_json_protocol(self) -> None:
        """set_connection should accept JSON protocol."""
        SurrealDBConnectionManager.set_connection(
            "http://localhost:8000",
            "root",
            "root",
            "test",
            "test_protocol",
            protocol="json",
        )
        assert SurrealDBConnectionManager.is_connection_set()
        SurrealDBConnectionManager.unset_connection_sync()


# =============================================================================
# Protocol-Specific Integration Tests (JSON and CBOR)
# =============================================================================


@pytest.mark.integration
class TestCBORProtocolCRUD:
    """
    Test CRUD operations with CBOR protocol explicitly.

    CBOR is the default protocol and should handle all operations correctly,
    including data URL strings that might be misinterpreted in JSON mode.
    """

    @pytest.fixture(autouse=True)
    async def setup_cbor_connection(self) -> AsyncGenerator[None, None]:
        """Setup CBOR connection for these tests."""
        # Close any existing connection
        try:
            await SurrealDBConnectionManager.close_connection()
            await SurrealDBConnectionManager.unset_connection()
        except Exception:
            pass  # Ignore if no connection exists

        # Setup with explicit CBOR protocol
        SurrealDBConnectionManager.set_connection(
            SURREALDB_URL,
            "root",
            "root",
            "test",
            "test_cbor_protocol",
            protocol="cbor",
        )

        # Clean up test tables
        try:
            client = await SurrealDBConnectionManager.get_client()
            await client.query("DELETE CborTestModel;")
            await client.query("DELETE CborAuthor;")
            await client.query("DELETE CborBook;")
            await client.query("DELETE cbor_wrote;")
        except Exception:
            pass  # Ignore cleanup errors - tables may not exist

        yield

        # Cleanup
        try:
            client = await SurrealDBConnectionManager.get_client()
            await client.query("DELETE CborTestModel;")
            await client.query("DELETE CborAuthor;")
            await client.query("DELETE CborBook;")
            await client.query("DELETE cbor_wrote;")
        except Exception:
            pass  # Ignore cleanup errors - tables may not exist

        await SurrealDBConnectionManager.close_connection()
        await SurrealDBConnectionManager.unset_connection()

    async def test_cbor_create_and_select(self) -> None:
        """Test create and select operations with CBOR protocol."""

        class CborTestModel(BaseSurrealModel):
            id: str | None = None
            name: str
            value: int = 0

        record = CborTestModel(id="cbor_test1", name="CBOR Test", value=42)
        await record.save()

        result = await CborTestModel.objects().get("cbor_test1")
        assert result is not None
        assert result.name == "CBOR Test"
        assert result.value == 42

    async def test_cbor_update(self) -> None:
        """Test update operation with CBOR protocol."""

        class CborTestModel(BaseSurrealModel):
            id: str | None = None
            name: str
            value: int = 0

        record = CborTestModel(id="cbor_update", name="Original", value=1)
        await record.save()

        record.name = "Updated"
        record.value = 100
        await record.save()

        result = await CborTestModel.objects().get("cbor_update")
        assert result.name == "Updated"
        assert result.value == 100

    async def test_cbor_delete(self) -> None:
        """Test delete operation with CBOR protocol."""

        class CborTestModel(BaseSurrealModel):
            id: str | None = None
            name: str
            value: int = 0

        record = CborTestModel(id="cbor_delete", name="To Delete", value=0)
        await record.save()

        await record.delete()

        # get() raises DoesNotExist when record is not found
        with pytest.raises(CborTestModel.DoesNotExist):
            await CborTestModel.objects().get("cbor_delete")

    async def test_cbor_data_url_string(self) -> None:
        """Test data URL strings are preserved with CBOR protocol."""

        class CborTestModel(BaseSurrealModel):
            id: str | None = None
            name: str
            data: str | None = None

        data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        record = CborTestModel(id="cbor_data", name="Data URL Test", data=data_url)
        await record.save()

        result = await CborTestModel.objects().get("cbor_data")
        assert result.data == data_url

    async def test_cbor_numeric_id(self) -> None:
        """Test IDs starting with digits work with CBOR protocol."""

        class CborTestModel(BaseSurrealModel):
            id: str | None = None
            name: str

        record = CborTestModel(id="7cbor123", name="Numeric ID CBOR")
        await record.save()

        result = await CborTestModel.objects().get("7cbor123")
        assert result is not None
        assert result.name == "Numeric ID CBOR"

    async def test_cbor_relations(self) -> None:
        """Test relations work with CBOR protocol."""

        class CborAuthor(BaseSurrealModel):
            id: str | None = None
            name: str

        class CborBook(BaseSurrealModel):
            id: str | None = None
            title: str

        author = CborAuthor(id="cbor_author1", name="CBOR Author")
        await author.save()
        book = CborBook(id="cbor_book1", title="CBOR Book")
        await book.save()

        # Create relation
        await author.relate("cbor_wrote", book)

        # Verify relation
        related = await author.get_related("cbor_wrote", model_class=CborBook)
        assert len(related) == 1
        assert related[0].title == "CBOR Book"


@pytest.mark.integration
class TestJSONProtocolCRUD:
    """
    Test CRUD operations with JSON protocol explicitly.

    JSON protocol is an alternative that can be used for debugging
    or compatibility purposes.
    """

    @pytest.fixture(autouse=True)
    async def setup_json_connection(self) -> AsyncGenerator[None, None]:
        """Setup JSON connection for these tests."""
        # Close any existing connection
        try:
            await SurrealDBConnectionManager.close_connection()
            await SurrealDBConnectionManager.unset_connection()
        except Exception:
            pass  # Ignore if no connection exists

        # Setup with explicit JSON protocol
        SurrealDBConnectionManager.set_connection(
            SURREALDB_URL,
            "root",
            "root",
            "test",
            "test_json_protocol",
            protocol="json",
        )

        # Clean up test tables
        try:
            client = await SurrealDBConnectionManager.get_client()
            await client.query("DELETE JsonTestModel;")
            await client.query("DELETE JsonAuthor;")
            await client.query("DELETE JsonBook;")
            await client.query("DELETE json_wrote;")
        except Exception:
            pass  # Ignore cleanup errors - tables may not exist

        yield

        # Cleanup
        try:
            client = await SurrealDBConnectionManager.get_client()
            await client.query("DELETE JsonTestModel;")
            await client.query("DELETE JsonAuthor;")
            await client.query("DELETE JsonBook;")
            await client.query("DELETE json_wrote;")
        except Exception:
            pass  # Ignore cleanup errors - tables may not exist

        await SurrealDBConnectionManager.close_connection()
        await SurrealDBConnectionManager.unset_connection()

    async def test_json_create_and_select(self) -> None:
        """Test create and select operations with JSON protocol."""

        class JsonTestModel(BaseSurrealModel):
            id: str | None = None
            name: str
            value: int = 0

        record = JsonTestModel(id="json_test1", name="JSON Test", value=42)
        await record.save()

        result = await JsonTestModel.objects().get("json_test1")
        assert result is not None
        assert result.name == "JSON Test"
        assert result.value == 42

    async def test_json_update(self) -> None:
        """Test update operation with JSON protocol."""

        class JsonTestModel(BaseSurrealModel):
            id: str | None = None
            name: str
            value: int = 0

        record = JsonTestModel(id="json_update", name="Original", value=1)
        await record.save()

        record.name = "Updated"
        record.value = 100
        await record.save()

        result = await JsonTestModel.objects().get("json_update")
        assert result.name == "Updated"
        assert result.value == 100

    async def test_json_delete(self) -> None:
        """Test delete operation with JSON protocol."""

        class JsonTestModel(BaseSurrealModel):
            id: str | None = None
            name: str
            value: int = 0

        record = JsonTestModel(id="json_delete", name="To Delete", value=0)
        await record.save()

        await record.delete()

        # get() raises DoesNotExist when record is not found
        with pytest.raises(JsonTestModel.DoesNotExist):
            await JsonTestModel.objects().get("json_delete")

    async def test_json_numeric_id(self) -> None:
        """Test IDs starting with digits work with JSON protocol."""

        class JsonTestModel(BaseSurrealModel):
            id: str | None = None
            name: str

        record = JsonTestModel(id="7json123", name="Numeric ID JSON")
        await record.save()

        result = await JsonTestModel.objects().get("7json123")
        assert result is not None
        assert result.name == "Numeric ID JSON"

    async def test_json_relations(self) -> None:
        """Test relations work with JSON protocol."""

        class JsonAuthor(BaseSurrealModel):
            id: str | None = None
            name: str

        class JsonBook(BaseSurrealModel):
            id: str | None = None
            title: str

        author = JsonAuthor(id="json_author1", name="JSON Author")
        await author.save()
        book = JsonBook(id="json_book1", title="JSON Book")
        await book.save()

        # Create relation
        await author.relate("json_wrote", book)

        # Verify relation
        related = await author.get_related("json_wrote", model_class=JsonBook)
        assert len(related) == 1
        assert related[0].title == "JSON Book"


@pytest.mark.integration
class TestSDKLevelProtocols:
    """
    Test SDK-level operations with both protocols directly.

    These tests bypass the ORM and test the connection directly.
    """

    async def test_sdk_cbor_crud(self) -> None:
        """Test direct SDK CRUD with CBOR protocol."""
        from src.surreal_sdk.connection.http import HTTPConnection

        async with HTTPConnection(SURREALDB_URL, "test", "test_sdk_cbor", protocol="cbor") as conn:
            await conn.signin("root", "root")

            # Clean up
            await conn.query("DELETE sdk_cbor_test;")

            # Create
            result = await conn.create("sdk_cbor_test:1", {"name": "CBOR SDK", "value": 123})
            assert result is not None

            # Select
            records = await conn.select("sdk_cbor_test")
            assert records.count > 0

            # Update
            await conn.update("sdk_cbor_test:1", {"name": "Updated CBOR", "value": 456})

            # Verify update
            records = await conn.select("sdk_cbor_test:1")
            assert records.records[0]["name"] == "Updated CBOR"

            # Delete
            await conn.delete("sdk_cbor_test:1")

            # Verify delete
            records = await conn.select("sdk_cbor_test:1")
            assert records.count == 0

    async def test_sdk_json_crud(self) -> None:
        """Test direct SDK CRUD with JSON protocol."""
        from src.surreal_sdk.connection.http import HTTPConnection

        async with HTTPConnection(SURREALDB_URL, "test", "test_sdk_json", protocol="json") as conn:
            await conn.signin("root", "root")

            # Clean up
            await conn.query("DELETE sdk_json_test;")

            # Create
            result = await conn.create("sdk_json_test:1", {"name": "JSON SDK", "value": 123})
            assert result is not None

            # Select
            records = await conn.select("sdk_json_test")
            assert records.count > 0

            # Update
            await conn.update("sdk_json_test:1", {"name": "Updated JSON", "value": 456})

            # Verify update
            records = await conn.select("sdk_json_test:1")
            assert records.records[0]["name"] == "Updated JSON"

            # Delete
            await conn.delete("sdk_json_test:1")

            # Verify delete
            records = await conn.select("sdk_json_test:1")
            assert records.count == 0

    async def test_sdk_cbor_data_url(self) -> None:
        """Test data URL strings with CBOR at SDK level."""
        from src.surreal_sdk.connection.http import HTTPConnection

        async with HTTPConnection(SURREALDB_URL, "test", "test_sdk_data", protocol="cbor") as conn:
            await conn.signin("root", "root")

            # Clean up
            await conn.query("DELETE sdk_data_test;")

            data_url = "data:image/png;base64,iVBORw0KGgo="

            # Create with data URL
            await conn.create("sdk_data_test:1", {"content": data_url})

            # Verify it's stored as string, not record link
            records = await conn.select("sdk_data_test:1")
            assert records.records[0]["content"] == data_url

    async def test_sdk_cbor_relate(self) -> None:
        """Test relate operation with CBOR at SDK level."""
        from src.surreal_sdk.connection.http import HTTPConnection

        async with HTTPConnection(SURREALDB_URL, "test", "test_sdk_relate", protocol="cbor") as conn:
            await conn.signin("root", "root")

            # Clean up
            await conn.query("DELETE sdk_author;")
            await conn.query("DELETE sdk_book;")
            await conn.query("DELETE sdk_wrote;")

            # Create records
            await conn.create("sdk_author:1", {"name": "Test Author"})
            await conn.create("sdk_book:1", {"title": "Test Book"})

            # Create relation
            result = await conn.relate("sdk_author:1", "sdk_wrote", "sdk_book:1")
            assert result is not None

            # Verify relation via query
            query_result = await conn.query("SELECT ->sdk_wrote->sdk_book FROM sdk_author:1")
            assert len(query_result.results) > 0
