from enum import StrEnum
from typing import Any


class SurrealFunc:
    """
    Marker for embedding a raw SurrealQL expression as a field value.

    When a field value is a ``SurrealFunc``, the ORM will generate a raw
    ``SET field = expression`` query instead of using the RPC data-dict path.
    This allows using server-side functions like ``time::now()`` or
    ``crypto::argon2::generate()`` in save/update operations.

    Warning:
        The expression is inserted **directly** into the query string.
        Only use with developer-controlled values, **never** with user input.

    Example::

        from surreal_orm import SurrealFunc

        player = Player(seat_position=1)
        await player.save(server_values={
            "joined_at": SurrealFunc("time::now()"),
            "last_ping": SurrealFunc("time::now()"),
        })
        # Generates: UPSERT players:... SET seat_position = $_sv_seat_position, joined_at = time::now(), last_ping = time::now()
    """

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __repr__(self) -> str:
        return f"SurrealFunc({self.expression!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SurrealFunc):
            return self.expression == other.expression
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.expression)


class SurealFunction(StrEnum): ...


# XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
class SurrealArrayFunction(SurealFunction):
    APPEND = "array::append"


# https://surrealdb.com/docs/surrealql/functions/database/time
class SurrealTimeFunction(SurealFunction):
    CEIL = "time::ceil"
    DAY = "time::day"
    FLOOR = "time::floor"
    FORMAT = "time::format"
    GROUP = "time::group"
    HOUR = "time::hour"
    MAX = "time::max"
    MICROS = "time::micros"
    MILLIS = "time::millis"
    MIN = "time::min"
    MINUTE = "time::minute"
    MONTH = "time::month"
    NANOS = "time::nanos"
    NOW = "time::now"
    ROUND = "time::round"
    SECOND = "time::second"
    TIMEZONE = "time::timezone"
    UNIX = "time::unix"
    WEEK = "time::week"
    YDAY = "time::yday"
    YEAR = "time::year"
    IS_LEAP_YEAR = "time::is::leap_year"
    FROM_MICROS = "time::from::micros"
    FROM_MILLIS = "time::from::millis"
    FROM_NANOS = "time::from::nanos"
    FROM_SECONDS = "time::from::seconds"
    FROM_UNIX = "time::from::unix"
    FROM_ULID = "time::from::ulid"
    FROM_UUID = "time::from::uuid"


# https://surrealdb.com/docs/surrealql/functions/database/math
class SurrealMathFunction(SurealFunction):
    ABS = "math::abs"
    ACOS = "math::acos"
    ACOT = "math::acot"
    ASIN = "math::asin"
    ATAN = "math::atan"
    BOTTOM = "math::bottom"
    CEIL = "math::ceil"
    CLAAMP = "math::clamp"
    COS = "math::cos"
    COT = "math::cot"
    COUNT = "count"  # https://surrealdb.com/docs/surrealql/functions/database/count
    DEG2RAD = "math::deg2rad"
    E = "math::e"
    FIXED = "math::fixed"
    FLOOR = "math::floor"
    FRAC_1_PI = "math::frac_1_pi"
    FRAC_1_SQRT_2 = "math::frac_1_sqrt_2"
    FRAC_2_PI = "math::frac_2_pi"
    FRAC_2_SQRT_PI = "math::frac_2_sqrt_pi"
    FRAC_PI_2 = "math::frac_pi_2"
    FRAC_PI_3 = "math::frac_pi_3"
    FRAC_PI_4 = "math::frac_pi_4"
    FRAC_PI_6 = "math::frac_pi_6"
    FRAC_PI_8 = "math::frac_pi_8"
    INF = "math::inf"
    INTERQUARTILE = "math::interquartile"
    LERP = "math::lerp"
    LERPANGLE = "math::lerpangle"
    LN = "math::ln"
    LN_10 = "math::ln_10"
    LN_2 = "math::ln_2"
    LOG = "math::log"
    LOG10 = "math::log10"
    LOG10_2 = "math::log10_2"
    LOG10_E = "math::log10_e"
    LOG2 = "math::log2"
    LOG2_10 = "math::log2_20"
    LOG2_E = "math::log2_e"
    ###
    PI = "math::pi"
    POW = "math::pow"
    ROUND = "math::round"
    SIGN = "math::sign"
    SIN = "math::sin"
    SINH = "math::sinh"
    SQRT = "math::sqrt"
    TAN = "math::tan"
    TANH = "math::tanh"
    TRUNC = "math::trunc"
