"""
Function call API for SurrealDB SDK.

Provides a fluent interface for calling SurrealDB built-in and custom functions.
"""

from typing import TYPE_CHECKING, Any, Generator

if TYPE_CHECKING:
    from .connection.base import BaseSurrealConnection


# Built-in SurrealDB function namespaces
BUILTIN_NAMESPACES = frozenset(
    {
        "array",
        "bytes",
        "count",
        "crypto",
        "duration",
        "encoding",
        "geo",
        "http",
        "math",
        "meta",
        "object",
        "parse",
        "rand",
        "record",
        "search",
        "session",
        "sleep",
        "string",
        "time",
        "type",
        "value",
        "vector",
    }
)


class FunctionCall:
    """
    Represents a SurrealDB function call ready for execution.

    Can be awaited directly to execute the function.

    Usage:
        result = await conn.fn.math.sqrt(16)
        # Equivalent to: SELECT * FROM math::sqrt(16)
    """

    def __init__(
        self,
        connection: "BaseSurrealConnection",
        function_path: str,
        args: tuple[Any, ...],
    ):
        """
        Initialize a function call.

        Args:
            connection: The database connection
            function_path: Full function path (e.g., "math::sqrt" or "fn::my_func")
            args: Function arguments
        """
        self._connection = connection
        self._function_path = function_path
        self._args = args

    @property
    def function_path(self) -> str:
        """Get the function path."""
        return self._function_path

    @property
    def args(self) -> tuple[Any, ...]:
        """Get the function arguments."""
        return self._args

    def to_sql(self) -> tuple[str, dict[str, Any]]:
        """
        Convert to SurrealQL with parameterized variables.

        Returns:
            Tuple of (sql_string, variables_dict)
        """
        params: dict[str, Any] = {}
        placeholders: list[str] = []

        for i, arg in enumerate(self._args):
            param_name = f"fn_arg_{i}"
            params[param_name] = arg
            placeholders.append(f"${param_name}")

        args_str = ", ".join(placeholders)
        sql = f"RETURN {self._function_path}({args_str});"

        return sql, params

    async def execute(self) -> Any:
        """
        Execute the function call and return result.

        Returns:
            The function result
        """
        sql, params = self.to_sql()
        result = await self._connection.query(sql, params)

        # Extract scalar result from QueryResponse
        if result.first_result and result.first_result.result is not None:
            return result.first_result.result
        return None

    def __await__(self) -> Generator[Any, None, Any]:
        """Allow direct await: result = await conn.fn.math.sqrt(16)"""
        return self.execute().__await__()

    def __repr__(self) -> str:
        args_repr = ", ".join(repr(a) for a in self._args)
        return f"FunctionCall({self._function_path}({args_repr}))"


class FunctionNamespace:
    """
    Namespace for building function calls.

    Supports dot notation for nested namespaces:
        conn.fn.math.sqrt(16)      -> math::sqrt($fn_arg_0)
        conn.fn.time.now()         -> time::now()
        conn.fn.cast_vote(...)     -> fn::cast_vote(...)

    Built-in namespaces (math, time, array, etc.) use namespace::function format.
    Unknown namespaces are treated as custom functions with fn:: prefix.
    """

    def __init__(
        self,
        connection: "BaseSurrealConnection",
        path: list[str] | None = None,
    ):
        """
        Initialize a function namespace.

        Args:
            connection: The database connection
            path: Current namespace path (e.g., ["math", "sqrt"])
        """
        self._connection = connection
        self._path = path or []

    def __getattr__(self, name: str) -> "FunctionNamespace":
        """
        Access a sub-namespace or prepare a function call.

        Args:
            name: Namespace or function name

        Returns:
            New FunctionNamespace with extended path

        Examples:
            conn.fn.math     -> FunctionNamespace(path=["math"])
            conn.fn.math.sqrt -> FunctionNamespace(path=["math", "sqrt"])
        """
        # Prevent recursion on special attributes
        if name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
        return FunctionNamespace(self._connection, self._path + [name])

    def __call__(self, *args: Any) -> FunctionCall:
        """
        Call the function with arguments.

        Args:
            *args: Function arguments

        Returns:
            FunctionCall ready to execute

        Raises:
            ValueError: If called on root namespace

        Examples:
            conn.fn.math.sqrt(16)      -> FunctionCall for "math::sqrt"
            conn.fn.cast_vote(a, b, c) -> FunctionCall for "fn::cast_vote"
        """
        if not self._path:
            raise ValueError("Cannot call root function namespace directly")

        # Build function path based on namespace type
        if self._path[0] in BUILTIN_NAMESPACES:
            # Built-in function: math::sqrt, time::now, etc.
            function_path = "::".join(self._path)
        else:
            # Custom function: fn::my_function, fn::nested::func
            function_path = "fn::" + "::".join(self._path)

        return FunctionCall(self._connection, function_path, args)

    def __repr__(self) -> str:
        path_str = "::".join(self._path) if self._path else "<root>"
        return f"FunctionNamespace({path_str})"

    # Typed helper properties for common namespaces

    @property
    def math(self) -> "MathFunctions":
        """Access math functions with type hints."""
        return MathFunctions(self._connection)

    @property
    def time(self) -> "TimeFunctions":
        """Access time functions with type hints."""
        return TimeFunctions(self._connection)

    @property
    def array(self) -> "ArrayFunctions":
        """Access array functions with type hints."""
        return ArrayFunctions(self._connection)

    @property
    def string(self) -> "StringFunctions":
        """Access string functions with type hints."""
        return StringFunctions(self._connection)

    @property
    def crypto(self) -> "CryptoFunctions":
        """Access crypto functions with type hints."""
        return CryptoFunctions(self._connection)


class MathFunctions:
    """
    Typed math function namespace with IDE hints.

    All SurrealDB math:: functions with type annotations.
    """

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["math"])

    def abs(self, value: float | int) -> FunctionCall:
        """Return absolute value. math::abs(number)"""
        return self._ns.abs(value)

    def ceil(self, value: float | int) -> FunctionCall:
        """Return ceiling value. math::ceil(number)"""
        return self._ns.ceil(value)

    def floor(self, value: float | int) -> FunctionCall:
        """Return floor value. math::floor(number)"""
        return self._ns.floor(value)

    def round(self, value: float | int) -> FunctionCall:
        """Return rounded value. math::round(number)"""
        return self._ns.round(value)

    def sqrt(self, value: float | int) -> FunctionCall:
        """Return square root. math::sqrt(number)"""
        return self._ns.sqrt(value)

    def pow(self, base: float | int, exponent: float | int) -> FunctionCall:
        """Return power. math::pow(base, exponent)"""
        return self._ns.pow(base, exponent)

    def log(self, value: float | int) -> FunctionCall:
        """Return natural logarithm. math::log(number)"""
        return self._ns.log(value)

    def log2(self, value: float | int) -> FunctionCall:
        """Return base-2 logarithm. math::log2(number)"""
        return self._ns.log2(value)

    def log10(self, value: float | int) -> FunctionCall:
        """Return base-10 logarithm. math::log10(number)"""
        return self._ns.log10(value)

    def sin(self, value: float | int) -> FunctionCall:
        """Return sine. math::sin(number)"""
        return self._ns.sin(value)

    def cos(self, value: float | int) -> FunctionCall:
        """Return cosine. math::cos(number)"""
        return self._ns.cos(value)

    def tan(self, value: float | int) -> FunctionCall:
        """Return tangent. math::tan(number)"""
        return self._ns.tan(value)

    def max(self, *values: float | int) -> FunctionCall:
        """Return maximum value. math::max(numbers...)"""
        return self._ns.max(*values)

    def min(self, *values: float | int) -> FunctionCall:
        """Return minimum value. math::min(numbers...)"""
        return self._ns.min(*values)

    def sum(self, values: list[float | int]) -> FunctionCall:
        """Return sum of values. math::sum(array)"""
        return self._ns.sum(values)

    def mean(self, values: list[float | int]) -> FunctionCall:
        """Return mean of values. math::mean(array)"""
        return self._ns.mean(values)

    def median(self, values: list[float | int]) -> FunctionCall:
        """Return median of values. math::median(array)"""
        return self._ns.median(values)

    def __getattr__(self, name: str) -> "FunctionNamespace":
        """Fall back to dynamic namespace for unlisted functions."""
        result: FunctionNamespace = getattr(self._ns, name)
        return result


class TimeFunctions:
    """
    Typed time function namespace with IDE hints.

    All SurrealDB time:: functions with type annotations.
    """

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["time"])

    def now(self) -> FunctionCall:
        """Return current datetime. time::now()"""
        return self._ns.now()

    def day(self, datetime: Any) -> FunctionCall:
        """Return day of month. time::day(datetime)"""
        return self._ns.day(datetime)

    def month(self, datetime: Any) -> FunctionCall:
        """Return month. time::month(datetime)"""
        return self._ns.month(datetime)

    def year(self, datetime: Any) -> FunctionCall:
        """Return year. time::year(datetime)"""
        return self._ns.year(datetime)

    def hour(self, datetime: Any) -> FunctionCall:
        """Return hour. time::hour(datetime)"""
        return self._ns.hour(datetime)

    def minute(self, datetime: Any) -> FunctionCall:
        """Return minute. time::minute(datetime)"""
        return self._ns.minute(datetime)

    def second(self, datetime: Any) -> FunctionCall:
        """Return second. time::second(datetime)"""
        return self._ns.second(datetime)

    def floor(self, datetime: Any, duration: str) -> FunctionCall:
        """Floor datetime to duration. time::floor(datetime, duration)"""
        return self._ns.floor(datetime, duration)

    def ceil(self, datetime: Any, duration: str) -> FunctionCall:
        """Ceil datetime to duration. time::ceil(datetime, duration)"""
        return self._ns.ceil(datetime, duration)

    def round(self, datetime: Any, duration: str) -> FunctionCall:
        """Round datetime to duration. time::round(datetime, duration)"""
        return self._ns.round(datetime, duration)

    def unix(self, datetime: Any) -> FunctionCall:
        """Return Unix timestamp. time::unix(datetime)"""
        return self._ns.unix(datetime)

    def format(self, datetime: Any, format_str: str) -> FunctionCall:
        """Format datetime. time::format(datetime, format)"""
        return self._ns.format(datetime, format_str)

    def __getattr__(self, name: str) -> "FunctionNamespace":
        """Fall back to dynamic namespace for unlisted functions."""
        result: FunctionNamespace = getattr(self._ns, name)
        return result


class ArrayFunctions:
    """
    Typed array function namespace with IDE hints.

    All SurrealDB array:: functions with type annotations.
    """

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["array"])

    def len(self, array: list[Any]) -> FunctionCall:
        """Return array length. array::len(array)"""
        return self._ns.len(array)

    def append(self, array: list[Any], value: Any) -> FunctionCall:
        """Append value to array. array::append(array, value)"""
        return self._ns.append(array, value)

    def prepend(self, array: list[Any], value: Any) -> FunctionCall:
        """Prepend value to array. array::prepend(array, value)"""
        return self._ns.prepend(array, value)

    def concat(self, *arrays: list[Any]) -> FunctionCall:
        """Concatenate arrays. array::concat(arrays...)"""
        return self._ns.concat(*arrays)

    def distinct(self, array: list[Any]) -> FunctionCall:
        """Return distinct values. array::distinct(array)"""
        return self._ns.distinct(array)

    def flatten(self, array: list[Any]) -> FunctionCall:
        """Flatten nested arrays. array::flatten(array)"""
        return self._ns.flatten(array)

    def reverse(self, array: list[Any]) -> FunctionCall:
        """Reverse array. array::reverse(array)"""
        return self._ns.reverse(array)

    def sort(self, array: list[Any]) -> FunctionCall:
        """Sort array. array::sort(array)"""
        return self._ns.sort(array)

    def slice(self, array: list[Any], start: int, end: int | None = None) -> FunctionCall:
        """Slice array. array::slice(array, start, end)"""
        if end is not None:
            return self._ns.slice(array, start, end)
        return self._ns.slice(array, start)

    def first(self, array: list[Any]) -> FunctionCall:
        """Return first element. array::first(array)"""
        return self._ns.first(array)

    def last(self, array: list[Any]) -> FunctionCall:
        """Return last element. array::last(array)"""
        return self._ns.last(array)

    def __getattr__(self, name: str) -> "FunctionNamespace":
        """Fall back to dynamic namespace for unlisted functions."""
        result: FunctionNamespace = getattr(self._ns, name)
        return result


class StringFunctions:
    """
    Typed string function namespace with IDE hints.

    All SurrealDB string:: functions with type annotations.
    """

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["string"])

    def len(self, string: str) -> FunctionCall:
        """Return string length. string::len(string)"""
        return self._ns.len(string)

    def lowercase(self, string: str) -> FunctionCall:
        """Convert to lowercase. string::lowercase(string)"""
        return self._ns.lowercase(string)

    def uppercase(self, string: str) -> FunctionCall:
        """Convert to uppercase. string::uppercase(string)"""
        return self._ns.uppercase(string)

    def trim(self, string: str) -> FunctionCall:
        """Trim whitespace. string::trim(string)"""
        return self._ns.trim(string)

    def concat(self, *strings: str) -> FunctionCall:
        """Concatenate strings. string::concat(strings...)"""
        return self._ns.concat(*strings)

    def split(self, string: str, delimiter: str) -> FunctionCall:
        """Split string. string::split(string, delimiter)"""
        return self._ns.split(string, delimiter)

    def join(self, array: list[str], delimiter: str) -> FunctionCall:
        """Join array. string::join(array, delimiter)"""
        return self._ns.join(array, delimiter)

    def replace(self, string: str, search: str, replace: str) -> FunctionCall:
        """Replace in string. string::replace(string, search, replace)"""
        return self._ns.replace(string, search, replace)

    def slice(self, string: str, start: int, end: int | None = None) -> FunctionCall:
        """Slice string. string::slice(string, start, end)"""
        if end is not None:
            return self._ns.slice(string, start, end)
        return self._ns.slice(string, start)

    def contains(self, string: str, search: str) -> FunctionCall:
        """Check if string contains substring. string::contains(string, search)"""
        return self._ns.contains(string, search)

    def starts_with(self, string: str, prefix: str) -> FunctionCall:
        """Check if string starts with prefix. string::startsWith(string, prefix)"""
        return self._ns.startsWith(string, prefix)

    def ends_with(self, string: str, suffix: str) -> FunctionCall:
        """Check if string ends with suffix. string::endsWith(string, suffix)"""
        return self._ns.endsWith(string, suffix)

    def __getattr__(self, name: str) -> "FunctionNamespace":
        """Fall back to dynamic namespace for unlisted functions."""
        result: FunctionNamespace = getattr(self._ns, name)
        return result


class CryptoFunctions:
    """
    Typed crypto function namespace with IDE hints.

    All SurrealDB crypto:: functions with type annotations.
    """

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["crypto"])

    def md5(self, value: str) -> FunctionCall:
        """Return MD5 hash. crypto::md5(string)"""
        return self._ns.md5(value)

    def sha1(self, value: str) -> FunctionCall:
        """Return SHA-1 hash. crypto::sha1(string)"""
        return self._ns.sha1(value)

    def sha256(self, value: str) -> FunctionCall:
        """Return SHA-256 hash. crypto::sha256(string)"""
        return self._ns.sha256(value)

    def sha512(self, value: str) -> FunctionCall:
        """Return SHA-512 hash. crypto::sha512(string)"""
        return self._ns.sha512(value)

    def __getattr__(self, name: str) -> "FunctionNamespace":
        """Fall back to dynamic namespace for unlisted functions."""
        result: FunctionNamespace = getattr(self._ns, name)
        return result

    @property
    def argon2(self) -> "Argon2Functions":
        """Access crypto::argon2 functions."""
        return Argon2Functions(self._connection)

    @property
    def bcrypt(self) -> "BcryptFunctions":
        """Access crypto::bcrypt functions."""
        return BcryptFunctions(self._connection)

    @property
    def scrypt(self) -> "ScryptFunctions":
        """Access crypto::scrypt functions."""
        return ScryptFunctions(self._connection)


class Argon2Functions:
    """Crypto argon2 sub-namespace."""

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["crypto", "argon2"])

    def compare(self, hash: str, password: str) -> FunctionCall:
        """Compare password with argon2 hash. crypto::argon2::compare(hash, password)"""
        return self._ns.compare(hash, password)

    def generate(self, password: str) -> FunctionCall:
        """Generate argon2 hash. crypto::argon2::generate(password)"""
        return self._ns.generate(password)


class BcryptFunctions:
    """Crypto bcrypt sub-namespace."""

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["crypto", "bcrypt"])

    def compare(self, hash: str, password: str) -> FunctionCall:
        """Compare password with bcrypt hash. crypto::bcrypt::compare(hash, password)"""
        return self._ns.compare(hash, password)

    def generate(self, password: str) -> FunctionCall:
        """Generate bcrypt hash. crypto::bcrypt::generate(password)"""
        return self._ns.generate(password)


class ScryptFunctions:
    """Crypto scrypt sub-namespace."""

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._ns = FunctionNamespace(connection, ["crypto", "scrypt"])

    def compare(self, hash: str, password: str) -> FunctionCall:
        """Compare password with scrypt hash. crypto::scrypt::compare(hash, password)"""
        return self._ns.compare(hash, password)

    def generate(self, password: str) -> FunctionCall:
        """Generate scrypt hash. crypto::scrypt::generate(password)"""
        return self._ns.generate(password)
