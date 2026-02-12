"""Unit tests for surreal_sdk.functions — Function call API."""

from unittest.mock import MagicMock

import pytest

from surreal_sdk.functions import (
    Argon2Functions,
    ArrayFunctions,
    BcryptFunctions,
    CryptoFunctions,
    FunctionCall,
    FunctionNamespace,
    MathFunctions,
    ScryptFunctions,
    StringFunctions,
    TimeFunctions,
)


class TestFunctionCall:
    def test_init(self) -> None:
        conn = MagicMock()
        fc = FunctionCall(conn, "math::sqrt", (16,))
        assert fc.function_path == "math::sqrt"
        assert fc.args == (16,)

    def test_to_sql_no_args(self) -> None:
        conn = MagicMock()
        fc = FunctionCall(conn, "time::now", ())
        sql, params = fc.to_sql()
        assert sql == "RETURN time::now();"
        assert params == {}

    def test_to_sql_single_arg(self) -> None:
        conn = MagicMock()
        fc = FunctionCall(conn, "math::sqrt", (16,))
        sql, params = fc.to_sql()
        assert sql == "RETURN math::sqrt($fn_arg_0);"
        assert params == {"fn_arg_0": 16}

    def test_to_sql_multiple_args(self) -> None:
        conn = MagicMock()
        fc = FunctionCall(conn, "math::pow", (2, 10))
        sql, params = fc.to_sql()
        assert sql == "RETURN math::pow($fn_arg_0, $fn_arg_1);"
        assert params == {"fn_arg_0": 2, "fn_arg_1": 10}

    def test_repr(self) -> None:
        conn = MagicMock()
        fc = FunctionCall(conn, "math::sqrt", (16,))
        r = repr(fc)
        assert "math::sqrt" in r
        assert "16" in r

    def test_repr_no_args(self) -> None:
        conn = MagicMock()
        fc = FunctionCall(conn, "time::now", ())
        r = repr(fc)
        assert "time::now" in r


class TestFunctionNamespace:
    def test_init_empty_path(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        assert ns._path == []

    def test_init_with_path(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn, ["math"])
        assert ns._path == ["math"]

    def test_getattr_extends_path(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        sub = ns.math
        assert sub._path == ["math"]

    def test_getattr_nested(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn, ["my_ns"])
        sub = ns.my_func
        assert sub._path == ["my_ns", "my_func"]

    def test_getattr_private_raises(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        with pytest.raises(AttributeError):
            _ = ns._private

    def test_call_builtin(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn, ["math", "sqrt"])
        fc = ns(16)
        assert isinstance(fc, FunctionCall)
        assert fc.function_path == "math::sqrt"
        assert fc.args == (16,)

    def test_call_custom_function(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn, ["my_custom_func"])
        fc = ns(1, 2, 3)
        assert fc.function_path == "fn::my_custom_func"
        assert fc.args == (1, 2, 3)

    def test_call_root_raises(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        with pytest.raises(ValueError, match="Cannot call root"):
            ns()

    def test_repr_with_path(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn, ["math", "sqrt"])
        assert "math::sqrt" in repr(ns)

    def test_repr_root(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        assert "<root>" in repr(ns)

    def test_call_nested_custom(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn, ["my_ns", "my_func"])
        fc = ns("arg")
        assert fc.function_path == "fn::my_ns::my_func"


class TestMathFunctions:
    def setup_method(self) -> None:
        self.conn = MagicMock()
        self.math = MathFunctions(self.conn)

    def test_sqrt(self) -> None:
        fc = self.math.sqrt(16)
        assert fc.function_path == "math::sqrt"
        assert fc.args == (16,)

    def test_abs(self) -> None:
        fc = self.math.abs(-5)
        assert fc.function_path == "math::abs"

    def test_ceil(self) -> None:
        fc = self.math.ceil(3.2)
        assert fc.function_path == "math::ceil"

    def test_floor(self) -> None:
        fc = self.math.floor(3.8)
        assert fc.function_path == "math::floor"

    def test_round(self) -> None:
        fc = self.math.round(3.5)
        assert fc.function_path == "math::round"

    def test_pow(self) -> None:
        fc = self.math.pow(2, 10)
        assert fc.function_path == "math::pow"
        assert fc.args == (2, 10)

    def test_log(self) -> None:
        fc = self.math.log(100)
        assert fc.function_path == "math::log"

    def test_log2(self) -> None:
        fc = self.math.log2(8)
        assert fc.function_path == "math::log2"

    def test_log10(self) -> None:
        fc = self.math.log10(1000)
        assert fc.function_path == "math::log10"

    def test_max(self) -> None:
        fc = self.math.max(1, 2, 3)
        assert fc.function_path == "math::max"
        assert fc.args == (1, 2, 3)

    def test_min(self) -> None:
        fc = self.math.min(1, 2, 3)
        assert fc.function_path == "math::min"

    def test_sum(self) -> None:
        fc = self.math.sum([1, 2, 3])
        assert fc.function_path == "math::sum"

    def test_mean(self) -> None:
        fc = self.math.mean([1, 2, 3])
        assert fc.function_path == "math::mean"

    def test_median(self) -> None:
        fc = self.math.median([1, 2, 3])
        assert fc.function_path == "math::median"

    def test_sin(self) -> None:
        fc = self.math.sin(1.0)
        assert fc.function_path == "math::sin"

    def test_cos(self) -> None:
        fc = self.math.cos(1.0)
        assert fc.function_path == "math::cos"

    def test_tan(self) -> None:
        fc = self.math.tan(1.0)
        assert fc.function_path == "math::tan"

    def test_fallback_getattr(self) -> None:
        result = self.math.custom_func
        assert isinstance(result, FunctionNamespace)


class TestTimeFunctions:
    def setup_method(self) -> None:
        self.conn = MagicMock()
        self.time = TimeFunctions(self.conn)

    def test_now(self) -> None:
        fc = self.time.now()
        assert fc.function_path == "time::now"

    def test_day(self) -> None:
        fc = self.time.day("2026-01-01")
        assert fc.function_path == "time::day"

    def test_month(self) -> None:
        fc = self.time.month("2026-01-01")
        assert fc.function_path == "time::month"

    def test_year(self) -> None:
        fc = self.time.year("2026-01-01")
        assert fc.function_path == "time::year"

    def test_hour(self) -> None:
        fc = self.time.hour("2026-01-01T12:00:00")
        assert fc.function_path == "time::hour"

    def test_minute(self) -> None:
        fc = self.time.minute("2026-01-01T12:30:00")
        assert fc.function_path == "time::minute"

    def test_second(self) -> None:
        fc = self.time.second("2026-01-01T12:30:45")
        assert fc.function_path == "time::second"

    def test_floor(self) -> None:
        fc = self.time.floor("2026-01-01T12:30:00", "1h")
        assert fc.function_path == "time::floor"

    def test_ceil(self) -> None:
        fc = self.time.ceil("2026-01-01T12:30:00", "1h")
        assert fc.function_path == "time::ceil"

    def test_round(self) -> None:
        fc = self.time.round("2026-01-01T12:30:00", "1h")
        assert fc.function_path == "time::round"

    def test_unix(self) -> None:
        fc = self.time.unix("2026-01-01T00:00:00")
        assert fc.function_path == "time::unix"

    def test_format(self) -> None:
        fc = self.time.format("2026-01-01", "%Y-%m-%d")
        assert fc.function_path == "time::format"

    def test_fallback_getattr(self) -> None:
        result = self.time.nano
        assert isinstance(result, FunctionNamespace)


class TestArrayFunctions:
    def setup_method(self) -> None:
        self.conn = MagicMock()
        self.arr = ArrayFunctions(self.conn)

    def test_len(self) -> None:
        fc = self.arr.len([1, 2, 3])
        assert fc.function_path == "array::len"

    def test_append(self) -> None:
        fc = self.arr.append([1, 2], 3)
        assert fc.function_path == "array::append"

    def test_prepend(self) -> None:
        fc = self.arr.prepend([1, 2], 0)
        assert fc.function_path == "array::prepend"

    def test_concat(self) -> None:
        fc = self.arr.concat([1], [2])
        assert fc.function_path == "array::concat"

    def test_distinct(self) -> None:
        fc = self.arr.distinct([1, 1, 2])
        assert fc.function_path == "array::distinct"

    def test_flatten(self) -> None:
        fc = self.arr.flatten([[1], [2]])
        assert fc.function_path == "array::flatten"

    def test_reverse(self) -> None:
        fc = self.arr.reverse([1, 2, 3])
        assert fc.function_path == "array::reverse"

    def test_sort(self) -> None:
        fc = self.arr.sort([3, 1, 2])
        assert fc.function_path == "array::sort"

    def test_slice_with_end(self) -> None:
        fc = self.arr.slice([1, 2, 3, 4], 1, 3)
        assert fc.function_path == "array::slice"
        assert fc.args == ([1, 2, 3, 4], 1, 3)

    def test_slice_without_end(self) -> None:
        fc = self.arr.slice([1, 2, 3], 1)
        assert fc.args == ([1, 2, 3], 1)

    def test_first(self) -> None:
        fc = self.arr.first([1, 2])
        assert fc.function_path == "array::first"

    def test_last(self) -> None:
        fc = self.arr.last([1, 2])
        assert fc.function_path == "array::last"

    def test_fallback_getattr(self) -> None:
        result = self.arr.intersect
        assert isinstance(result, FunctionNamespace)


class TestStringFunctions:
    def setup_method(self) -> None:
        self.conn = MagicMock()
        self.str_fn = StringFunctions(self.conn)

    def test_len(self) -> None:
        fc = self.str_fn.len("hello")
        assert fc.function_path == "string::len"

    def test_lowercase(self) -> None:
        fc = self.str_fn.lowercase("HELLO")
        assert fc.function_path == "string::lowercase"

    def test_uppercase(self) -> None:
        fc = self.str_fn.uppercase("hello")
        assert fc.function_path == "string::uppercase"

    def test_trim(self) -> None:
        fc = self.str_fn.trim(" hello ")
        assert fc.function_path == "string::trim"

    def test_concat(self) -> None:
        fc = self.str_fn.concat("hello", " ", "world")
        assert fc.function_path == "string::concat"

    def test_split(self) -> None:
        fc = self.str_fn.split("a,b,c", ",")
        assert fc.function_path == "string::split"

    def test_join(self) -> None:
        fc = self.str_fn.join(["a", "b"], ",")
        assert fc.function_path == "string::join"

    def test_replace(self) -> None:
        fc = self.str_fn.replace("hello world", "world", "earth")
        assert fc.function_path == "string::replace"

    def test_slice_with_end(self) -> None:
        fc = self.str_fn.slice("hello", 1, 3)
        assert fc.function_path == "string::slice"

    def test_slice_without_end(self) -> None:
        fc = self.str_fn.slice("hello", 1)
        assert fc.function_path == "string::slice"

    def test_contains(self) -> None:
        fc = self.str_fn.contains("hello world", "world")
        assert fc.function_path == "string::contains"

    def test_starts_with(self) -> None:
        fc = self.str_fn.starts_with("hello", "he")
        assert fc.function_path == "string::startsWith"

    def test_ends_with(self) -> None:
        fc = self.str_fn.ends_with("hello", "lo")
        assert fc.function_path == "string::endsWith"

    def test_fallback_getattr(self) -> None:
        result = self.str_fn.reverse
        assert isinstance(result, FunctionNamespace)


class TestCryptoFunctions:
    def setup_method(self) -> None:
        self.conn = MagicMock()
        self.crypto = CryptoFunctions(self.conn)

    def test_md5(self) -> None:
        fc = self.crypto.md5("data")
        assert fc.function_path == "crypto::md5"

    def test_sha1(self) -> None:
        fc = self.crypto.sha1("data")
        assert fc.function_path == "crypto::sha1"

    def test_sha256(self) -> None:
        fc = self.crypto.sha256("data")
        assert fc.function_path == "crypto::sha256"

    def test_sha512(self) -> None:
        fc = self.crypto.sha512("data")
        assert fc.function_path == "crypto::sha512"

    def test_argon2_property(self) -> None:
        argon2 = self.crypto.argon2
        assert isinstance(argon2, Argon2Functions)

    def test_bcrypt_property(self) -> None:
        bcrypt = self.crypto.bcrypt
        assert isinstance(bcrypt, BcryptFunctions)

    def test_scrypt_property(self) -> None:
        scrypt = self.crypto.scrypt
        assert isinstance(scrypt, ScryptFunctions)

    def test_fallback_getattr(self) -> None:
        result = self.crypto.blake2b
        assert isinstance(result, FunctionNamespace)


class TestArgon2Functions:
    def test_generate(self) -> None:
        conn = MagicMock()
        a2 = Argon2Functions(conn)
        fc = a2.generate("password")
        assert fc.function_path == "crypto::argon2::generate"

    def test_compare(self) -> None:
        conn = MagicMock()
        a2 = Argon2Functions(conn)
        fc = a2.compare("hash", "password")
        assert fc.function_path == "crypto::argon2::compare"


class TestBcryptFunctions:
    def test_generate(self) -> None:
        conn = MagicMock()
        bc = BcryptFunctions(conn)
        fc = bc.generate("password")
        assert fc.function_path == "crypto::bcrypt::generate"

    def test_compare(self) -> None:
        conn = MagicMock()
        bc = BcryptFunctions(conn)
        fc = bc.compare("hash", "password")
        assert fc.function_path == "crypto::bcrypt::compare"


class TestScryptFunctions:
    def test_generate(self) -> None:
        conn = MagicMock()
        sc = ScryptFunctions(conn)
        fc = sc.generate("password")
        assert fc.function_path == "crypto::scrypt::generate"

    def test_compare(self) -> None:
        conn = MagicMock()
        sc = ScryptFunctions(conn)
        fc = sc.compare("hash", "password")
        assert fc.function_path == "crypto::scrypt::compare"


class TestFunctionNamespaceTypedProperties:
    def test_math_property(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        math = ns.math
        assert isinstance(math, MathFunctions)

    def test_time_property(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        time = ns.time
        assert isinstance(time, TimeFunctions)

    def test_array_property(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        arr = ns.array
        assert isinstance(arr, ArrayFunctions)

    def test_string_property(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        string = ns.string
        assert isinstance(string, StringFunctions)

    def test_crypto_property(self) -> None:
        conn = MagicMock()
        ns = FunctionNamespace(conn)
        crypto = ns.crypto
        assert isinstance(crypto, CryptoFunctions)
