"""Tests for function call module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.surreal_sdk.functions import (
    FunctionCall,
    FunctionNamespace,
    MathFunctions,
    TimeFunctions,
    ArrayFunctions,
    StringFunctions,
    CryptoFunctions,
    BUILTIN_NAMESPACES,
)
from src.surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus


class TestFunctionCall:
    """Tests for FunctionCall class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        conn = MagicMock()
        conn.query = AsyncMock(
            return_value=QueryResponse(
                results=[QueryResult(status=ResponseStatus.OK, time="1ms", result=4.0)],
                raw=[{"result": 4.0}],
            )
        )
        return conn

    def test_function_call_properties(self, mock_connection: MagicMock) -> None:
        """Test function call properties."""
        fn = FunctionCall(mock_connection, "math::sqrt", (16,))

        assert fn.function_path == "math::sqrt"
        assert fn.args == (16,)

    def test_to_sql_no_args(self, mock_connection: MagicMock) -> None:
        """Test SQL generation with no arguments."""
        fn = FunctionCall(mock_connection, "time::now", ())

        sql, params = fn.to_sql()

        assert sql == "RETURN time::now();"
        assert params == {}

    def test_to_sql_single_arg(self, mock_connection: MagicMock) -> None:
        """Test SQL generation with single argument."""
        fn = FunctionCall(mock_connection, "math::sqrt", (16,))

        sql, params = fn.to_sql()

        assert sql == "RETURN math::sqrt($fn_arg_0);"
        assert params == {"fn_arg_0": 16}

    def test_to_sql_multiple_args(self, mock_connection: MagicMock) -> None:
        """Test SQL generation with multiple arguments."""
        fn = FunctionCall(mock_connection, "math::pow", (2, 8))

        sql, params = fn.to_sql()

        assert sql == "RETURN math::pow($fn_arg_0, $fn_arg_1);"
        assert params == {"fn_arg_0": 2, "fn_arg_1": 8}

    @pytest.mark.asyncio
    async def test_execute(self, mock_connection: MagicMock) -> None:
        """Test function execution."""
        fn = FunctionCall(mock_connection, "math::sqrt", (16,))

        result = await fn.execute()

        assert result == 4.0
        mock_connection.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_await_directly(self, mock_connection: MagicMock) -> None:
        """Test awaiting function call directly."""
        fn = FunctionCall(mock_connection, "math::sqrt", (16,))

        result = await fn

        assert result == 4.0

    def test_repr(self, mock_connection: MagicMock) -> None:
        """Test string representation."""
        fn = FunctionCall(mock_connection, "math::sqrt", (16,))

        assert repr(fn) == "FunctionCall(math::sqrt(16))"


class TestFunctionNamespace:
    """Tests for FunctionNamespace class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        return MagicMock()

    def test_root_namespace(self, mock_connection: MagicMock) -> None:
        """Test root namespace."""
        ns = FunctionNamespace(mock_connection)

        assert repr(ns) == "FunctionNamespace(<root>)"

    def test_namespace_access(self, mock_connection: MagicMock) -> None:
        """Test accessing a namespace (typed helpers override)."""
        ns = FunctionNamespace(mock_connection)

        # math property returns MathFunctions (typed helper)
        math_ns = ns.math
        assert isinstance(math_ns, MathFunctions)

        # Non-typed namespaces return FunctionNamespace
        custom_ns = ns.custom
        assert isinstance(custom_ns, FunctionNamespace)
        assert repr(custom_ns) == "FunctionNamespace(custom)"

    def test_nested_namespace(self, mock_connection: MagicMock) -> None:
        """Test nested namespace access."""
        ns = FunctionNamespace(mock_connection)

        # Access via dynamic namespace (not typed helper)
        custom_func = ns.custom.nested.func
        assert repr(custom_func) == "FunctionNamespace(custom::nested::func)"

    def test_call_builtin_function(self, mock_connection: MagicMock) -> None:
        """Test calling a built-in function."""
        ns = FunctionNamespace(mock_connection)

        fn_call = ns.math.sqrt(16)

        assert isinstance(fn_call, FunctionCall)
        assert fn_call.function_path == "math::sqrt"
        assert fn_call.args == (16,)

    def test_call_custom_function(self, mock_connection: MagicMock) -> None:
        """Test calling a custom function."""
        ns = FunctionNamespace(mock_connection)

        fn_call = ns.cast_vote("user:1", "table:1", "yes")

        assert isinstance(fn_call, FunctionCall)
        assert fn_call.function_path == "fn::cast_vote"
        assert fn_call.args == ("user:1", "table:1", "yes")

    def test_call_root_namespace_raises(self, mock_connection: MagicMock) -> None:
        """Test that calling root namespace raises error."""
        ns = FunctionNamespace(mock_connection)

        with pytest.raises(ValueError, match="Cannot call root"):
            ns()

    def test_private_attribute_raises(self, mock_connection: MagicMock) -> None:
        """Test that private attributes raise AttributeError."""
        ns = FunctionNamespace(mock_connection)

        with pytest.raises(AttributeError):
            ns._private

    def test_builtin_namespaces_constant(self) -> None:
        """Test that BUILTIN_NAMESPACES contains expected namespaces."""
        assert "math" in BUILTIN_NAMESPACES
        assert "time" in BUILTIN_NAMESPACES
        assert "array" in BUILTIN_NAMESPACES
        assert "string" in BUILTIN_NAMESPACES
        assert "crypto" in BUILTIN_NAMESPACES

    def test_typed_helpers_accessible(self, mock_connection: MagicMock) -> None:
        """Test that typed helper properties are accessible."""
        ns = FunctionNamespace(mock_connection)

        assert isinstance(ns.math, MathFunctions)
        assert isinstance(ns.time, TimeFunctions)
        assert isinstance(ns.array, ArrayFunctions)
        assert isinstance(ns.string, StringFunctions)
        assert isinstance(ns.crypto, CryptoFunctions)


class TestMathFunctions:
    """Tests for MathFunctions class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        return MagicMock()

    def test_sqrt(self, mock_connection: MagicMock) -> None:
        """Test math::sqrt."""
        math = MathFunctions(mock_connection)

        fn_call = math.sqrt(16)

        assert fn_call.function_path == "math::sqrt"
        assert fn_call.args == (16,)

    def test_pow(self, mock_connection: MagicMock) -> None:
        """Test math::pow."""
        math = MathFunctions(mock_connection)

        fn_call = math.pow(2, 8)

        assert fn_call.function_path == "math::pow"
        assert fn_call.args == (2, 8)

    def test_abs(self, mock_connection: MagicMock) -> None:
        """Test math::abs."""
        math = MathFunctions(mock_connection)

        fn_call = math.abs(-5)

        assert fn_call.function_path == "math::abs"
        assert fn_call.args == (-5,)

    def test_fallback_to_dynamic(self, mock_connection: MagicMock) -> None:
        """Test fallback to dynamic namespace."""
        math = MathFunctions(mock_connection)

        # Access a function not explicitly defined
        fn_ns = math.some_other_function

        assert isinstance(fn_ns, FunctionNamespace)


class TestTimeFunctions:
    """Tests for TimeFunctions class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        return MagicMock()

    def test_now(self, mock_connection: MagicMock) -> None:
        """Test time::now."""
        time = TimeFunctions(mock_connection)

        fn_call = time.now()

        assert fn_call.function_path == "time::now"
        assert fn_call.args == ()

    def test_year(self, mock_connection: MagicMock) -> None:
        """Test time::year."""
        time = TimeFunctions(mock_connection)

        fn_call = time.year("2024-01-15T12:00:00Z")

        assert fn_call.function_path == "time::year"
        assert fn_call.args == ("2024-01-15T12:00:00Z",)

    def test_format(self, mock_connection: MagicMock) -> None:
        """Test time::format."""
        time = TimeFunctions(mock_connection)

        fn_call = time.format("2024-01-15T12:00:00Z", "%Y-%m-%d")

        assert fn_call.function_path == "time::format"
        assert fn_call.args == ("2024-01-15T12:00:00Z", "%Y-%m-%d")


class TestArrayFunctions:
    """Tests for ArrayFunctions class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        return MagicMock()

    def test_len(self, mock_connection: MagicMock) -> None:
        """Test array::len."""
        arr = ArrayFunctions(mock_connection)

        fn_call = arr.len([1, 2, 3])

        assert fn_call.function_path == "array::len"
        assert fn_call.args == ([1, 2, 3],)

    def test_append(self, mock_connection: MagicMock) -> None:
        """Test array::append."""
        arr = ArrayFunctions(mock_connection)

        fn_call = arr.append([1, 2], 3)

        assert fn_call.function_path == "array::append"
        assert fn_call.args == ([1, 2], 3)

    def test_slice_with_end(self, mock_connection: MagicMock) -> None:
        """Test array::slice with end parameter."""
        arr = ArrayFunctions(mock_connection)

        fn_call = arr.slice([1, 2, 3, 4, 5], 1, 3)

        assert fn_call.function_path == "array::slice"
        assert fn_call.args == ([1, 2, 3, 4, 5], 1, 3)

    def test_slice_without_end(self, mock_connection: MagicMock) -> None:
        """Test array::slice without end parameter."""
        arr = ArrayFunctions(mock_connection)

        fn_call = arr.slice([1, 2, 3, 4, 5], 2)

        assert fn_call.function_path == "array::slice"
        assert fn_call.args == ([1, 2, 3, 4, 5], 2)


class TestStringFunctions:
    """Tests for StringFunctions class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        return MagicMock()

    def test_len(self, mock_connection: MagicMock) -> None:
        """Test string::len."""
        string = StringFunctions(mock_connection)

        fn_call = string.len("hello")

        assert fn_call.function_path == "string::len"
        assert fn_call.args == ("hello",)

    def test_lowercase(self, mock_connection: MagicMock) -> None:
        """Test string::lowercase."""
        string = StringFunctions(mock_connection)

        fn_call = string.lowercase("HELLO")

        assert fn_call.function_path == "string::lowercase"
        assert fn_call.args == ("HELLO",)

    def test_concat(self, mock_connection: MagicMock) -> None:
        """Test string::concat."""
        string = StringFunctions(mock_connection)

        fn_call = string.concat("Hello", " ", "World")

        assert fn_call.function_path == "string::concat"
        assert fn_call.args == ("Hello", " ", "World")


class TestCryptoFunctions:
    """Tests for CryptoFunctions class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection."""
        return MagicMock()

    def test_sha256(self, mock_connection: MagicMock) -> None:
        """Test crypto::sha256."""
        crypto = CryptoFunctions(mock_connection)

        fn_call = crypto.sha256("password")

        assert fn_call.function_path == "crypto::sha256"
        assert fn_call.args == ("password",)

    def test_argon2_generate(self, mock_connection: MagicMock) -> None:
        """Test crypto::argon2::generate."""
        crypto = CryptoFunctions(mock_connection)

        fn_call = crypto.argon2.generate("password")

        assert fn_call.function_path == "crypto::argon2::generate"
        assert fn_call.args == ("password",)

    def test_argon2_compare(self, mock_connection: MagicMock) -> None:
        """Test crypto::argon2::compare."""
        crypto = CryptoFunctions(mock_connection)

        fn_call = crypto.argon2.compare("hash", "password")

        assert fn_call.function_path == "crypto::argon2::compare"
        assert fn_call.args == ("hash", "password")

    def test_bcrypt_generate(self, mock_connection: MagicMock) -> None:
        """Test crypto::bcrypt::generate."""
        crypto = CryptoFunctions(mock_connection)

        fn_call = crypto.bcrypt.generate("password")

        assert fn_call.function_path == "crypto::bcrypt::generate"
        assert fn_call.args == ("password",)


class TestFunctionIntegration:
    """Integration-style tests for function API via connection."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock connection with fn property."""
        conn = MagicMock()
        conn.query = AsyncMock(
            return_value=QueryResponse(
                results=[QueryResult(status=ResponseStatus.OK, time="1ms", result=4.0)],
                raw=[{"result": 4.0}],
            )
        )
        return conn

    @pytest.mark.asyncio
    async def test_full_function_call_flow(self, mock_connection: MagicMock) -> None:
        """Test complete flow from namespace to execution."""
        ns = FunctionNamespace(mock_connection)

        result = await ns.math.sqrt(16)

        assert result == 4.0
        mock_connection.query.assert_called_once()
        call_args = mock_connection.query.call_args
        assert "math::sqrt" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_custom_function_call(self, mock_connection: MagicMock) -> None:
        """Test custom function call."""
        mock_connection.query.return_value = QueryResponse(
            results=[QueryResult(status=ResponseStatus.OK, time="1ms", result={"success": True})],
            raw=[{"result": {"success": True}}],
        )
        ns = FunctionNamespace(mock_connection)

        result = await ns.cast_vote("user:1", "table:1", "yes")

        assert result == {"success": True}
        call_args = mock_connection.query.call_args
        assert "fn::cast_vote" in call_args[0][0]
