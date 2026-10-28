"""
Migration operations for SurrealDB schema changes.

Each operation represents a single schema modification that can be
applied (forwards) or reverted (backwards).
"""

import re
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..fields.relation import on_delete_to_surql
from ..surreal_function import SurrealFunc
from ..types import FieldType
from ..utils import surql_string

if TYPE_CHECKING:
    from .state import FieldState, TableState


def _normalize_field_type(field_type: FieldType | str) -> str:
    """
    Normalize a field type to its string representation.

    Accepts FieldType enum or string. For strings, validates that it's either
    a known FieldType value or a valid generic type (e.g., "array<string>").

    Args:
        field_type: FieldType enum or string type specification

    Returns:
        String representation of the type for SurrealQL

    Raises:
        ValueError: If the string is not a valid SurrealDB type
    """
    if isinstance(field_type, FieldType):
        return field_type.value

    # Check if it's a known base type
    try:
        return FieldType(field_type).value
    except ValueError:
        pass

    # Check if it's a generic type (e.g., "array<string>", "record<users>")
    if "<" in field_type and field_type.endswith(">"):
        base_type = field_type.split("<")[0]
        try:
            FieldType(base_type)
            return field_type  # Valid generic type
        except ValueError:
            pass

    # Check for union types (e.g., "int | null", "option<string>")
    if "|" in field_type:
        return field_type  # Allow union types

    raise ValueError(
        f"Invalid field type: '{field_type}'. "
        f"Must be a FieldType enum value, a valid SurrealDB type string, "
        f"or a generic type like 'array<string>' or 'record<users>'."
    )


def _apply_nullable(field_type: str, nullable: bool) -> str:
    """
    Wrap a field type in ``option<>`` when the column is optional.

    ``FieldState.nullable`` used to stop at the diff boundary: only
    ``define_table()`` wrapped the type, so generated migrations silently lost
    optionality (#170).  The wrap is skipped when the type already carries its
    own optionality — ``option<T>``, or a union with a ``none``/``null`` member
    such as SurrealDB 3.x's ``none | T`` — so that a hand-written migration is
    never double-wrapped.  Detection mirrors :func:`parse_define_field`, the
    other side of the round-trip; a union *without* such a member (``int |
    string``) is not optional and is wrapped like any other type.

    Args:
        field_type: The normalized SurrealDB type
        nullable: Whether the column accepts NONE

    Returns:
        The type, wrapped in ``option<>`` when that is both needed and absent
    """
    if not nullable:
        return field_type
    lowered = field_type.lower()
    if lowered.startswith("option<") or "| null" in lowered or lowered.startswith("none |"):
        return field_type
    return f"option<{field_type}>"


#: The VALUE expression that makes a column an ``Encrypted`` one.
ARGON2_VALUE = "crypto::argon2::generate($value)"

#: The attributes a field definition carries, in clause order — the clauses the
#: ORM *models*. ``AddField.forwards``, ``AlterField.forwards`` and
#: ``AlterField.backwards`` are all driven off this one tuple, through
#: ``_render_define_field`` and ``_render_alter_field``. They used to be three
#: hand-written renderers, and every clause added to one of them was eventually
#: forgotten in another — that is #170 (REFERENCE), #196 (VALUE) and the DEFAULT
#: divergence below, all the same omission. A clause that is *not* here, field
#: ``PERMISSIONS`` first, is one ``ALTER FIELD`` leaves as the database has it.
FIELD_DEFINITION_FIELDS = (
    "field_type",
    "nullable",
    "flexible",
    "value",
    "default",
    "assertion",
    "readonly",
    "reference",
    "on_delete",
    "comment",
)


def _previous_attr(name: str) -> str:
    """
    Name the ``previous_*`` slot holding *name*'s pre-change value.

    All of them follow the ``previous_<field>`` pattern except the type, which
    shipped as ``previous_type``. Generated migration files on disk spell it
    that way, so it stays.

    Args:
        name: A field-definition attribute name

    Returns:
        The matching previous slot's attribute name
    """
    return "previous_type" if name == "field_type" else f"previous_{name}"


#: A ``str`` default still emitted as an expression, for migration files and
#: models written before ``SurrealFunc`` defaults: a whole no-argument function
#: call (``time::now()``) or a whole bare parameter (``$value``). Deliberately
#: narrow: no arguments, no spaces, no operators, so nothing but that one call
#: or parameter is ever emitted unquoted.
_LEGACY_EXPRESSION_DEFAULT = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+\(\)|\$[A-Za-z_][A-Za-z0-9_]*",
    re.ASCII,
)

#: The warning for a ``str`` default read as an expression, shared by the
#: renderer and the model introspector.
_LEGACY_EXPRESSION_DEFAULT_WARNING = (
    "Passing a server-side expression as a str default is deprecated; wrap it in SurrealFunc(...)"
)


def _render_default(default: Any) -> str:
    """
    Render a DEFAULT value as SurrealQL.

    A bare Python ``repr`` is wrong two ways: ``True`` is not ``true``, and a
    string needs SurrealQL's backslash escapes (``'it\\'s'``, ``'C:\\\\temp'``).
    Doubling the apostrophe, as SQL does, is a parse error in SurrealQL.

    A ``str`` is a text, whatever it contains: ``'fr::CA'`` is a string, not a
    function. A server-side expression such as ``time::now()`` is a
    :class:`~surreal_orm.SurrealFunc`, which is what ``_parse_default_value``
    and the model introspector produce for one, and is emitted as written.

    One deprecated exception keeps older migration files working: a ``str``
    that is *exactly* a no-argument function call (``time::now()``) or a bare
    parameter (``$value``) is still emitted unquoted, with a
    ``DeprecationWarning``. Anything else (arguments, operators, spaces,
    quotes, ``;``) is quoted.

    Args:
        default: The default value, as the model or the database reports it

    Returns:
        The SurrealQL literal or expression
    """
    if isinstance(default, SurrealFunc):
        return default.expression
    if isinstance(default, bool):
        return str(default).lower()
    if isinstance(default, str):
        if _LEGACY_EXPRESSION_DEFAULT.fullmatch(default):
            warnings.warn(_LEGACY_EXPRESSION_DEFAULT_WARNING, DeprecationWarning, stacklevel=3)
            return default
        return surql_string(default)
    return str(default)


def _render_comment(comment: str) -> str:
    """
    Render a COMMENT text as a SurrealQL string literal.

    Shared by both field renderers so a comment is quoted the same way whether
    the field is being defined or altered.

    Args:
        comment: The comment text

    Returns:
        The quoted literal
    """
    return surql_string(comment)


def _render_define_field(
    table: str,
    name: str,
    *,
    overwrite: bool = False,
    if_not_exists: bool = False,
    field_type: "FieldType | str | None" = None,
    nullable: bool = False,
    flexible: bool = False,
    value: str | None = None,
    default: Any = None,
    assertion: str | None = None,
    readonly: bool = False,
    reference: bool = False,
    on_delete: str | None = None,
    comment: str | None = None,
) -> str:
    """
    Render one ``DEFINE FIELD`` statement.

    Used by ``AddField`` and, with ``if_not_exists``, by the create half of
    :func:`_render_create_or_alter_field`. An existing field is changed with
    :func:`_render_alter_field` instead: a redefinition replaces clauses the
    ORM does not model, field ``PERMISSIONS`` among them (#195).

    Args:
        table: Table the field belongs to
        name: Field name
        overwrite: Emit ``DEFINE FIELD OVERWRITE``
        if_not_exists: Emit ``DEFINE FIELD IF NOT EXISTS``
        field_type: Field type; no TYPE clause when absent
        nullable: Whether the column accepts NONE
        flexible: FLEXIBLE TYPE
        value: VALUE expression, already resolved for encrypted columns
        default: DEFAULT value
        assertion: ASSERT expression
        readonly: READONLY
        reference: SurrealDB 3.0 REFERENCE clause
        on_delete: ON DELETE strategy, in Django or SurrealDB vocabulary
        comment: COMMENT text

    Returns:
        The complete statement, semicolon included

    Raises:
        ValueError: If both ``overwrite`` and ``if_not_exists`` are set
    """
    if overwrite and if_not_exists:
        raise ValueError("DEFINE FIELD takes OVERWRITE or IF NOT EXISTS, not both")
    keyword = "DEFINE FIELD OVERWRITE" if overwrite else "DEFINE FIELD IF NOT EXISTS" if if_not_exists else "DEFINE FIELD"
    parts = [f"{keyword} {name} ON {table}"]

    if flexible:
        parts.append("FLEXIBLE")

    if field_type:
        parts.append(f"TYPE {_apply_nullable(_normalize_field_type(field_type), nullable)}")

    if value:
        parts.append(f"VALUE {value}")

    if default is not None:
        parts.append(f"DEFAULT {_render_default(default)}")

    if assertion:
        parts.append(f"ASSERT {assertion}")

    if readonly:
        parts.append("READONLY")

    # SurrealDB 3.0 REFERENCE clause (for record/array<record> fields)
    if reference:
        parts.append("REFERENCE")
        if on_delete:
            parts.append(f"ON DELETE {on_delete_to_surql(on_delete)}")

    if comment:
        parts.append(f"COMMENT {_render_comment(comment)}")

    return " ".join(parts) + ";"


def _split_top_level(text: str, separator: str) -> list[str]:
    """
    Split a SurrealQL type on *separator*, outside brackets and quotes.

    ``array<record<a | b>> | string`` splits on ``|`` into two members, and
    ``array<string, 5>`` on ``,`` into the element and the length.

    Args:
        text: The type text
        separator: A one-character separator

    Returns:
        The stripped parts, in order
    """
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        elif ch == separator and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def _union_members(field_type: str) -> list[str]:
    """
    Flatten a type into its union members, without ``none``/``null``.

    ``option<T>`` is ``none | T``, so it is unwrapped into T's members.

    Args:
        field_type: The type text

    Returns:
        The non-null members
    """
    members: list[str] = []
    for member in _split_top_level(field_type, "|"):
        lowered = member.lower()
        if lowered in ("none", "null"):
            continue
        if lowered.startswith("option<") and member.endswith(">"):
            members.extend(_union_members(member[len("option<") : -1]))
        else:
            members.append(member)
    return members


def _is_union(field_type: str) -> bool:
    """
    Whether a type, as written, is a union.

    Read on the raw text, before :func:`_union_members` drops ``none`` and
    ``null``: several top-level ``|`` members, or ``option<T>`` — ``none | T``
    — even when ``T`` is a single member.

    Args:
        field_type: The type text

    Returns:
        True for ``a | b``, ``none | T`` and ``option<T>``
    """
    return len(_split_top_level(field_type, "|")) > 1 or field_type.lower().startswith("option<")


def _element_type(field_type: str) -> str | None:
    """
    The type of the ``name.*`` level of an array or set column.

    Defining a field whose type has an ``array<T>`` or ``set<T>`` member also
    defines ``name.*`` with type ``T`` — the union of every such member's
    element, ``array<int> | set<string>`` giving ``int | string``.

    An untyped ``array``/``set`` member, or an element with an ``any`` member
    (``array<any>``, ``set<any>``), makes the element ``any`` — for the whole
    union, since ``any`` absorbs the other members. A redefinition of
    ``array<int> | array<any>`` makes the server derive ``int | any``, which
    SurrealDB 3.3 then fails to parse back, leaving ``INFO FOR TABLE`` broken.
    Whether an ``any`` level exists at all is for
    :func:`_render_element_types` to decide.

    Args:
        field_type: The column's type, as rendered

    Returns:
        The element type, or ``None`` for a type with no array or set member
    """
    elements: list[str] = []
    for member in _union_members(field_type):
        lowered = member.lower()
        if lowered in ("array", "set"):
            return "any"
        for kind in ("array<", "set<"):
            if lowered.startswith(kind) and member.endswith(">"):
                element = _split_top_level(member[len(kind) : -1], ",")[0]
                if any(m.lower() == "any" for m in _union_members(element)):
                    return "any"
                if element not in elements:
                    elements.append(element)
                break
    return " | ".join(elements) or None


#: A quoted string inside a type — a literal type (``'object'``) — whose text
#: is not a type keyword.
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`")
#: A ``record<…>`` member, whose contents are table names, not types.
_RECORD_TABLES = re.compile(r"\brecord\s*<[^>]*>", re.IGNORECASE)
_OBJECT_KEYWORD = re.compile(r"\bobject\b", re.IGNORECASE)


def _contains_object(field_type: str) -> bool:
    """
    Whether a type contains an object, the condition for ``FLEXIBLE``.

    The SurrealDB parser's own test (``FLEXIBLE can only be used with types
    containing object``): the ``object`` keyword or a literal object type
    ``{ … }`` anywhere in the type — ``array<int> | object``,
    ``array<option<object>>`` and ``{ a: int }`` qualify. A table named in
    ``record<…>`` and a string literal do not, which is why both are removed
    before looking.

    Args:
        field_type: The type text

    Returns:
        True when ``FLEXIBLE`` is valid on the type
    """
    text = _RECORD_TABLES.sub("record", _QUOTED.sub("''", field_type))
    return "{" in text or _OBJECT_KEYWORD.search(text) is not None


def _render_element_types(table: str, name: str, field_type: str, *, flexible: bool = False) -> list[str]:
    """
    Align the element sub-fields SurrealDB derives for an array or set column.

    ``DEFINE FIELD tags ON t TYPE array<string>`` also defines ``tags.*`` as
    ``string`` (and ``n.*.*`` for ``array<array<…>>``); a redefinition
    re-derives them. ``ALTER FIELD tags ON t TYPE array<int>`` does not: it
    leaves ``tags.*`` at ``string`` and the column then refuses every
    non-empty value. Each element level SurrealDB derives itself for the new
    type is therefore aligned the way the redefinition did it, and nothing
    more: its TYPE, plus FLEXIBLE on a level whose type contains an object
    (:func:`_contains_object`) when the column is FLEXIBLE, or ``DROP
    FLEXIBLE`` on a level whose type contains none. Those are the only two
    modelled clauses a redefinition carried down to the elements. The field
    is the server's, and any other clause set on it by hand stays.

    A FLEXIBLE column whose elements were left strict reported success and
    still refused ``[{x: 'hello'}]`` with ``Found field 'items[0].x', but no
    such field exists``.

    FLEXIBLE is never removed from an object element level: a non-FLEXIBLE
    redefinition left ``items.*`` FLEXIBLE too, and a FLEXIBLE set there by
    hand must survive. Known limitation, the same as with the redefinition:
    removing FLEXIBLE from an array column leaves its object element levels
    FLEXIBLE. A ``RawSQL`` operation with
    ``ALTER FIELD items.* ON <table> DROP FLEXIBLE;`` removes it.

    A level whose type contains no object always gets ``DROP FLEXIBLE``.
    FLEXIBLE does nothing there, yet the server derives it (``int FLEXIBLE``
    for the inner level of a FLEXIBLE ``array<array<int> | object>``, ``any
    FLEXIBLE`` under ``array<any> | object``) and keeps it when a FLEXIBLE
    ``array<object>`` is retyped to ``array<string>``. On such a level a
    ``TYPE`` without ``DROP FLEXIBLE`` is refused (``FLEXIBLE can only be
    used with types containing object``): the rollback of ``array<string>`` →
    FLEXIBLE ``array<object>`` failed halfway and left the column refusing
    every non-empty list. ``DROP FLEXIBLE`` is accepted on a level without
    FLEXIBLE as well, and writing FLEXIBLE on such a type is a parse error,
    so no hand-set clause can be lost.

    A typed element is always derived. An ``any`` element — ``array<any>``, an
    untyped ``array`` or ``set`` — only under an optional column or a union, a
    union anywhere on the path above that level (:func:`_is_union`):
    ``option<array<any>>`` derives ``tags.*`` as ``any``, and widening
    ``option<array<int>>`` to it has to widen ``tags.*`` too. Under a
    non-optional ``array``/``array<any>`` the server derives nothing, so a
    ``tags.*`` defined there by hand is left as it is, and the walk stops.
    It also stops after an ``any`` level, ``any`` having no element of its
    own.

    Known limitation, the same as with the redefinition: a non-optional
    column taken from ``array<T>`` to ``array<any>``, to ``array`` or to
    ``any`` keeps ``tags.*`` at ``T``. A ``RawSQL`` operation with
    ``ALTER FIELD tags.* ON <table> TYPE any;`` or
    ``REMOVE FIELD tags.* ON <table>;`` lifts it.

    ``IF EXISTS`` because whether the server derived a level depends on the
    previous definition — none for a column that was not an array before —
    and where it derived none, the parent's TYPE alone checks the elements.

    Args:
        table: Table the column belongs to
        name: Column name
        field_type: The column's new type, as rendered
        flexible: Whether the column is FLEXIBLE

    Returns:
        One statement per derived element level, outermost first; none for a
        column with no array or set member
    """
    statements: list[str] = []
    path = name
    union_seen = _is_union(field_type)
    element = _element_type(field_type)
    while element:
        if element.lower() == "any" and not union_seen:
            break
        path += ".*"
        if not _contains_object(element):
            clauses = f"TYPE {element} DROP FLEXIBLE"
        elif flexible:
            clauses = f"TYPE {element} FLEXIBLE"
        else:
            clauses = f"TYPE {element}"
        statements.append(f"ALTER FIELD IF EXISTS {path} ON {table} {clauses};")
        union_seen = union_seen or _is_union(element)
        element = _element_type(element)
    return statements


def _render_alter_field(
    table: str,
    name: str,
    *,
    field_type: "FieldType | str | None" = None,
    nullable: bool = False,
    flexible: bool = False,
    value: str | None = None,
    default: Any = None,
    assertion: str | None = None,
    readonly: bool = False,
    reference: bool = False,
    on_delete: str | None = None,
    comment: str | None = None,
) -> str:
    """
    Render the ``ALTER FIELD`` statement setting every modelled clause.

    ``DEFINE FIELD OVERWRITE`` replaces the whole definition, so a clause the
    ORM does not model was reset to the server default: a field restricted
    with ``PERMISSIONS FOR select NONE`` came back ``PERMISSIONS FULL`` after
    the next alteration (#195). ``ALTER FIELD`` changes only the clauses it
    names. Each clause of :data:`FIELD_DEFINITION_FIELDS` is therefore named
    every time — set when present, ``DROP``-ped when absent — so the modelled
    semantics stay those of a full redefinition (removing a clause from the
    model removes it from the database) while every other clause is left
    untouched by construction.

    ``ALTER FIELD`` requires the field to exist: on a missing one SurrealDB
    answers ``The field 'x' does not exist``. Shared by both directions of
    ``AlterField``, so a rollback cannot render a clause differently from the
    statement it reverses.

    For an array or set column, the statement is followed by one per element
    sub-field SurrealDB derives itself for the new type (``tags.*``,
    ``n.*.*``): a redefinition re-derived them, ``ALTER FIELD`` does not. Each
    gets its TYPE, plus FLEXIBLE on the levels that hold an object when the
    column is FLEXIBLE, as the redefinition did, and ``DROP FLEXIBLE`` on the
    levels that hold none, where the server refuses a TYPE change while
    FLEXIBLE is set; nothing else is dropped there. A typed element is aligned always, an ``any`` element only under an
    optional column or a union; under a non-optional ``array``/``array<any>``
    the server derives nothing, and a hand-defined ``tags.*`` is left as it
    is (:func:`_render_element_types`).

    Args:
        table: Table the field belongs to
        name: Field name
        field_type: Field type; ``DROP TYPE`` when absent
        nullable: Whether the column accepts NONE
        flexible: FLEXIBLE (object types only)
        value: VALUE expression, already resolved for encrypted columns
        default: DEFAULT value
        assertion: ASSERT expression
        readonly: READONLY
        reference: SurrealDB 3.0 REFERENCE clause
        on_delete: ON DELETE strategy, in Django or SurrealDB vocabulary;
            rendered only with ``reference``
        comment: COMMENT text

    Returns:
        The ALTER FIELD statement, then any element sub-field statements,
        newline-separated, each with its semicolon
    """
    parts = [f"ALTER FIELD {name} ON {table}"]

    rendered_type = _apply_nullable(_normalize_field_type(field_type), nullable) if field_type else None
    parts.append(f"TYPE {rendered_type}" if rendered_type else "DROP TYPE")

    # FLEXIBLE is only valid on a type containing object. The server checks the
    # field once every clause is applied, so a statement that changes the type
    # and FLEXIBLE together is accepted in either direction.
    parts.append("FLEXIBLE" if flexible else "DROP FLEXIBLE")

    parts.append(f"VALUE {value}" if value else "DROP VALUE")

    parts.append(f"DEFAULT {_render_default(default)}" if default is not None else "DROP DEFAULT")

    parts.append(f"ASSERT {assertion}" if assertion else "DROP ASSERT")

    parts.append("READONLY" if readonly else "DROP READONLY")

    if reference:
        # A bare REFERENCE resets ON DELETE to the server default, so the
        # strategy is always restated with it.
        parts.append(f"REFERENCE ON DELETE {on_delete_to_surql(on_delete)}" if on_delete else "REFERENCE")
    else:
        parts.append("DROP REFERENCE")

    parts.append(f"COMMENT {_render_comment(comment)}" if comment else "DROP COMMENT")

    statements = [" ".join(parts) + ";"]
    if rendered_type:
        statements.extend(_render_element_types(table, name, rendered_type, flexible=flexible))
    return "\n".join(statements)


def _field_clauses(op: "AddField | AlterField") -> dict[str, Any]:
    """
    Collect the modelled clauses of a field operation, ready for a renderer.

    ``encrypted`` *is* "VALUE is the argon2 expression", so it is resolved here
    rather than in each renderer.

    Args:
        op: The operation whose target definition to render

    Returns:
        One entry per :data:`FIELD_DEFINITION_FIELDS` attribute
    """
    clauses = {f: getattr(op, f) for f in FIELD_DEFINITION_FIELDS}
    if op.encrypted:
        clauses["value"] = ARGON2_VALUE
    return clauses


def _render_create_or_alter_field(op: "AddField") -> str:
    """
    Render a field as a create-or-update pair.

    ``DEFINE FIELD IF NOT EXISTS`` creates the field with its whole definition
    when it is missing and is a no-op otherwise; the ``ALTER FIELD`` that
    follows then brings an existing field's modelled clauses in line, leaving
    the clauses the ORM does not model — field ``PERMISSIONS`` — as they are,
    and aligns the element sub-fields of an array or set column.
    ``DEFINE FIELD OVERWRITE`` was create-or-redefine in one statement, but it
    reset those clauses (#195).

    Args:
        op: The field to apply

    Returns:
        The statements, newline-separated
    """
    clauses = _field_clauses(op)
    return "\n".join(
        (
            _render_define_field(op.table, op.name, if_not_exists=True, **clauses),
            _render_alter_field(op.table, op.name, **clauses),
        )
    )


#: The attributes a ``DEFINE TABLE`` statement carries. ``CreateTable``,
#: ``AlterTable`` and the renderer are all driven off this one tuple: a clause
#: added here reaches the forward statement, the rollback and the diff at once,
#: instead of being spelled out in four places and forgotten in one of them.
TABLE_DEFINITION_FIELDS = (
    "schema_mode",
    "table_type",
    "changefeed",
    "permissions",
    "comment",
    "view_query",
    "relation_in",
    "relation_out",
    "enforced",
)

#: The only table types SurrealDB itself accepts. NORMAL, USER, STREAM and HASH
#: are ORM-level concepts.
_SURQL_TABLE_TYPES = ("relation", "any")


def _surql_table_type(table_type: str | None) -> str | None:
    """
    Reduce an ORM table type to one SurrealDB will parse.

    ``TableType`` carries ORM-only classifications — USER marks an auth table,
    STREAM and HASH are hints — but ``DEFINE TABLE ... TYPE`` accepts only
    ``NORMAL``, ``RELATION`` and ``ANY``. Passing USER through produced
    ``Parse error: Unexpected token `USER``` and made any change to such a table
    unmigratable.

    Args:
        table_type: The table type as the model or the database reports it

    Returns:
        The type when SurrealDB understands it, otherwise None
    """
    if table_type and table_type.lower() in _SURQL_TABLE_TYPES:
        return table_type
    return None


def _render_permission_rule(action: str, rule: str) -> str:
    """
    Render one ``FOR <action>`` group of a PERMISSIONS clause.

    ``FULL`` and ``NONE`` are keywords, not expressions: SurrealDB accepts
    ``FOR select WHERE FULL`` but evaluates ``FULL`` as a field reference, gets
    ``NONE`` and denies — turning a world-readable table into an unreadable one
    with no error.

    Args:
        action: The action the rule governs (select, create, update, delete)
        rule: A condition expression, or the keyword FULL / NONE

    Returns:
        The rendered group
    """
    keyword = str(rule).strip().upper()
    if keyword in ("FULL", "NONE"):
        return f"FOR {action} {keyword}"
    return f"FOR {action} WHERE {rule}"


def _effective_permissions(permissions: dict[str, str] | None) -> dict[str, str]:
    """
    Drop permission entries that only restate SurrealDB's default.

    A table defined without a ``PERMISSIONS`` clause is reported back by
    ``INFO FOR DB`` as ``{"select": "NONE", "create": "NONE", ...}`` — those are
    the server's defaults, not a configured value. Comparing the raw dicts made a
    database-read state differ from a model state that configures nothing, so the
    diff re-emitted ``CreateTable`` for every table on every run (#171).

    Normalising both sides also makes an explicit ``NONE`` equal to omitting the
    clause, which is what it means, and keeps a generated rollback from carrying
    the server's three default ``NONE`` entries as if they had been configured.

    Args:
        permissions: The permissions mapping, or None

    Returns:
        The mapping without its default-valued entries
    """
    if not permissions:
        return {}
    return {action: rule for action, rule in permissions.items() if str(rule).strip().upper() != "NONE"}


def _relation_tables(value: "str | list[str] | None") -> str | None:
    """
    Render an IN/OUT target list as SurrealDB spells it.

    ``SurrealConfigDict`` accepts a list — ``relation_out=["blog_post", "book"]``
    is the documented form — but the clause takes pipe-separated names, so a
    list reached the DDL as ``OUT ['blog_post', 'book']``.

    Args:
        value: One table name, several, or None

    Returns:
        The pipe-separated form, or None
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return " | ".join(str(v) for v in value)


def _render_define_table(
    name: str,
    *,
    overwrite: bool,
    schema_mode: str | None = None,
    table_type: str | None = None,
    changefeed: str | None = None,
    permissions: dict[str, str] | None = None,
    comment: str | None = None,
    view_query: str | None = None,
    relation_in: str | None = None,
    relation_out: str | None = None,
    enforced: bool = False,
) -> str:
    """
    Render one ``DEFINE TABLE`` statement.

    Shared by ``CreateTable`` and both directions of ``AlterTable`` so a
    rollback cannot drift from the definition it restores.

    Args:
        name: Table name
        overwrite: Emit ``DEFINE TABLE OVERWRITE``, redefining an existing table
        schema_mode: SCHEMAFULL or SCHEMALESS
        table_type: ORM table type; sanitised to what SurrealDB accepts
        changefeed: CHANGEFEED duration
        permissions: Action → rule mapping
        comment: COMMENT text
        view_query: AS SELECT ... body for a materialized view
        relation_in: IN table(s) for TYPE RELATION
        relation_out: OUT table(s) for TYPE RELATION
        enforced: Whether the relation constraint is enforced

    Returns:
        The complete statement, semicolon included
    """
    keyword = "DEFINE TABLE OVERWRITE" if overwrite else "DEFINE TABLE"

    # Materialized view — different syntax
    if view_query:
        return f"{keyword} {name} AS ({view_query});"

    parts = [f"{keyword} {name}"]

    surql_type = _surql_table_type(table_type)
    if surql_type and surql_type.upper() == "RELATION":
        type_clause = "TYPE RELATION"
        if relation_in:
            type_clause += f" IN {_relation_tables(relation_in)}"
        if relation_out:
            type_clause += f" OUT {_relation_tables(relation_out)}"
        if enforced:
            type_clause += " ENFORCED"
        parts.append(type_clause)
    elif surql_type:
        parts.append(f"TYPE {surql_type.upper()}")

    if schema_mode:
        parts.append(schema_mode)

    if changefeed:
        parts.append(f"CHANGEFEED {changefeed}")

    if comment:
        parts.append(f"COMMENT {surql_string(comment)}")

    # PERMISSIONS is a clause of DEFINE TABLE. It used to be emitted as a
    # second `DEFINE TABLE ... PERMISSIONS ...`, which SurrealDB rejects once
    # the table exists ("The table 'x' already exists"), so declared table
    # permissions never reached the database at all.
    if permissions:
        parts.append("PERMISSIONS " + " ".join(_render_permission_rule(a, r) for a, r in permissions.items()))

    return " ".join(parts) + ";"


def split_destructive(operations: "list[Operation]") -> "tuple[list[Operation], list[Operation]]":
    """
    Separate the operations that cannot be rolled back from the rest.

    A table the database holds and no model declares is not necessarily
    obsolete: it may be a RELATE edge table — ``ModelIntrospector`` skips
    ``Relation`` fields, so those can only ever look unmodelled — or belong to a
    module that simply was not imported. The same is true of an analyzer, an
    API, a GraphQL config, and of the ``in``/``out`` fields SurrealDB defines on
    a ``TYPE RELATION`` table itself: nothing a model can ever declare, so the
    diff proposes removing them on every run.

    Partitioning on ``reversible`` rather than on a list of operation classes is
    what keeps the next irreversible operation from having to be remembered
    here.

    Args:
        operations: The operations a diff produced

    Returns:
        ``(keep, destructive)`` — the reversible operations, and the ones a
        caller must opt into
    """
    keep = [op for op in operations if op.reversible]
    destructive = [op for op in operations if not op.reversible]
    return keep, destructive


@dataclass
class Operation(ABC):
    """
    Base class for all migration operations.

    Operations must implement forwards() and backwards() methods
    that return SurrealQL statements.
    """

    reversible: bool = field(default=True, init=False)

    @abstractmethod
    def forwards(self) -> str:
        """Generate forward SurrealQL statement."""
        ...

    @abstractmethod
    def backwards(self) -> str:
        """Generate rollback SurrealQL statement."""
        ...

    def describe(self) -> str:
        """Human-readable description of the operation."""
        return f"{self.__class__.__name__}"


@dataclass
class CreateTable(Operation):
    """
    Create a new table with optional schema mode and changefeed.

    Supports materialized views (``view_query``) and TYPE RELATION
    constraints (``relation_in``, ``relation_out``, ``enforced``).

    Redefining a table that already exists is :class:`AlterTable`, not this
    operation: a plain ``DEFINE TABLE`` is rejected once the table exists, and
    rolling back a creation means dropping the table.

    Example:
        CreateTable(name="users", schema_mode="SCHEMAFULL", changefeed="7d")

    Generates:
        DEFINE TABLE users SCHEMAFULL CHANGEFEED 7d;
    """

    name: str
    schema_mode: str = "SCHEMAFULL"
    table_type: str | None = None
    changefeed: str | None = None
    permissions: dict[str, str] | None = None
    comment: str | None = None
    view_query: str | None = None
    relation_in: str | None = None
    relation_out: str | None = None
    enforced: bool = False

    @classmethod
    def from_table_state(cls, state: "TableState") -> "CreateTable":
        """
        Build the operation that creates *state*.

        The single place that maps a ``TableState`` onto the definition fields,
        so a new clause cannot reach the diff and miss table creation.

        Args:
            state: The table definition the models describe

        Returns:
            A ``CreateTable`` carrying every definition field of *state*
        """
        return cls(
            name=state.name,
            permissions=_effective_permissions(state.permissions) or None,
            **{f: getattr(state, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
        )

    def forwards(self) -> str:
        return _render_define_table(
            self.name,
            overwrite=False,
            **{f: getattr(self, f) for f in TABLE_DEFINITION_FIELDS},
        )

    def backwards(self) -> str:
        return f"REMOVE TABLE {self.name};"

    def describe(self) -> str:
        return f"Create table {self.name}"


@dataclass
class AlterTable(Operation):
    """
    Redefine a table that already exists.

    Emitted by the diff when a table's definition changed. A plain
    ``DEFINE TABLE`` is not idempotent — the server rejects it once the table
    exists ("The table 'x' already exists") — so the statement is
    ``DEFINE TABLE OVERWRITE``, which preserves the table's fields, indexes,
    events and rows.

    Rolling back restores the definition that was replaced rather than dropping
    the table, which is why the previous values travel with the operation. An
    ``AlterTable`` built without them is **not** reversible: a bare
    ``DEFINE TABLE OVERWRITE t;`` resets the table to
    ``TYPE ANY SCHEMALESS PERMISSIONS NONE``, silently discarding the very
    definition a rollback is supposed to restore.

    Example:
        AlterTable(name="users", schema_mode="SCHEMAFULL",
                   previous_schema_mode="SCHEMALESS")

    Generates:
        DEFINE TABLE OVERWRITE users SCHEMAFULL;
    """

    name: str
    schema_mode: str | None = None
    table_type: str | None = None
    changefeed: str | None = None
    permissions: dict[str, str] | None = None
    comment: str | None = None
    view_query: str | None = None
    relation_in: str | None = None
    relation_out: str | None = None
    enforced: bool = False
    # Previous definition, for a non-destructive rollback
    previous_schema_mode: str | None = None
    previous_table_type: str | None = None
    previous_changefeed: str | None = None
    previous_permissions: dict[str, str] | None = None
    previous_comment: str | None = None
    previous_view_query: str | None = None
    previous_relation_in: str | None = None
    previous_relation_out: str | None = None
    previous_enforced: bool = False

    def __post_init__(self) -> None:
        # Mirrors AlterField: without the previous definition there is nothing
        # to restore, and emitting a bare OVERWRITE would reset the table.
        self.reversible = self.previous_schema_mode is not None

    @classmethod
    def from_table_state(cls, state: "TableState") -> "AlterTable":
        """
        Build the operation that redefines a table to match *state*.

        For callers that apply a model's schema directly — ``define_table()`` —
        rather than migrating between two known states. The result carries no
        previous definition and is therefore not reversible.

        Args:
            state: The table definition the model describes

        Returns:
            An ``AlterTable`` carrying only the target definition
        """
        return cls(
            name=state.name,
            permissions=_effective_permissions(state.permissions) or None,
            **{f: getattr(state, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
        )

    @classmethod
    def from_table_states(cls, current: "TableState", target: "TableState") -> "AlterTable":
        """
        Build the operation that redefines *current* as *target*.

        Both permission mappings go through :func:`_effective_permissions`, so
        the server's default ``NONE`` entries do not travel into generated
        migration files as though they had been configured.

        Args:
            current: The table definition the database holds
            target: The definition the models describe

        Returns:
            An ``AlterTable`` carrying both definitions
        """
        return cls(
            name=target.name,
            permissions=_effective_permissions(target.permissions) or None,
            previous_permissions=_effective_permissions(current.permissions) or None,
            **{f: getattr(target, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
            **{f"previous_{f}": getattr(current, f) for f in TABLE_DEFINITION_FIELDS if f != "permissions"},
        )

    def forwards(self) -> str:
        return _render_define_table(
            self.name,
            overwrite=True,
            **{f: getattr(self, f) for f in TABLE_DEFINITION_FIELDS},
        )

    def backwards(self) -> str:
        if not self.reversible:
            raise ValueError(
                f"AlterTable({self.name!r}) carries no previous definition, so it cannot be "
                f"rolled back: a bare DEFINE TABLE OVERWRITE would reset the table to "
                f"TYPE ANY SCHEMALESS PERMISSIONS NONE. Build it with from_table_states()."
            )
        return _render_define_table(
            self.name,
            overwrite=True,
            **{f: getattr(self, f"previous_{f}") for f in TABLE_DEFINITION_FIELDS},
        )

    def describe(self) -> str:
        return f"Alter table {self.name}"


@dataclass
class DropTable(Operation):
    """
    Drop an existing table.

    Example:
        DropTable(name="users")

    Generates:
        REMOVE TABLE users;
    """

    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE TABLE {self.name};"

    def backwards(self) -> str:
        # Cannot reverse without knowing the original schema
        return ""

    def describe(self) -> str:
        return f"Drop table {self.name}"


@dataclass
class AddField(Operation):
    """
    Add a field to a table.

    Example:
        AddField(
            table="users",
            name="email",
            field_type=FieldType.STRING,  # or "string"
            assertion="is::email($value)"
        )

        # With generic types
        AddField(
            table="users",
            name="tags",
            field_type=FieldType.ARRAY.generic("string"),  # "array<string>"
        )

    Generates:
        DEFINE FIELD email ON users TYPE string ASSERT is::email($value);
    """

    table: str
    name: str
    field_type: FieldType | str
    default: Any = None
    assertion: str | None = None
    encrypted: bool = False
    flexible: bool = False
    readonly: bool = False
    value: str | None = None
    comment: str | None = None
    reference: bool = False
    on_delete: str | None = None
    nullable: bool = False
    #: Emit ``DEFINE FIELD OVERWRITE``. A migration adds a field that is not
    #: there yet, so the plain form is right there and a second run *should*
    #: fail. OVERWRITE replaces the whole definition, including clauses the ORM
    #: does not model such as field ``PERMISSIONS``, so ``define_table()`` no
    #: longer uses it (#195); it is kept for code that opts in explicitly.
    overwrite: bool = False

    def __post_init__(self) -> None:
        """Validate field_type on initialization."""
        # Validate the field type (raises ValueError if invalid)
        _normalize_field_type(self.field_type)

    @classmethod
    def from_field_state(cls, table: str, state: "FieldState") -> "AddField":
        """
        Build the operation that defines *state* on *table*.

        Every attribute is wired here and only here.  Enumerating them by hand
        at each call site is what let ``reference`` / ``on_delete`` go missing
        from one diff branch and ``nullable`` from all of them (#170).

        Args:
            table: Name of the table the field belongs to
            state: The desired field state

        Returns:
            An ``AddField`` carrying the whole field state
        """
        return cls(
            table=table,
            name=state.name,
            field_type=state.field_type,
            nullable=state.nullable,
            default=state.default,
            assertion=state.assertion,
            encrypted=state.encrypted,
            flexible=state.flexible,
            readonly=state.readonly,
            value=state.value,
            reference=state.reference,
            on_delete=state.on_delete,
            comment=state.comment,
        )

    def forwards(self) -> str:
        return _render_define_field(self.table, self.name, overwrite=self.overwrite, **_field_clauses(self))

    def backwards(self) -> str:
        return f"REMOVE FIELD {self.name} ON {self.table};"

    def describe(self) -> str:
        return f"Add field {self.name} to {self.table}"


@dataclass
class DropField(Operation):
    """
    Remove a field from a table.

    Example:
        DropField(table="users", name="old_field")

    Generates:
        REMOVE FIELD old_field ON users;
    """

    table: str
    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE FIELD {self.name} ON {self.table};"

    def backwards(self) -> str:
        # Cannot reverse without knowing the original field definition
        return ""

    def describe(self) -> str:
        return f"Drop field {self.name} from {self.table}"


@dataclass
class AlterField(Operation):
    """
    Alter an existing field's definition.

    Every modelled clause is stated: set when the operation carries it,
    ``DROP``-ped when it does not. Clauses the ORM does not model, such as a
    field-level ``PERMISSIONS`` set by hand, are left as the database has them.
    The field must exist — altering a missing one fails the migration with
    ``The field 'x' does not exist``; ``AddField`` is the operation that
    creates a field.

    Example:
        AlterField(
            table="users",
            name="email",
            field_type=FieldType.STRING,  # or "string"
            assertion="is::email($value)"
        )

    Generates:
        ALTER FIELD email ON users TYPE string DROP FLEXIBLE DROP VALUE DROP DEFAULT
        ASSERT is::email($value) DROP READONLY DROP REFERENCE DROP COMMENT;
    """

    table: str
    name: str
    field_type: FieldType | str | None = None
    default: Any = None
    assertion: str | None = None
    encrypted: bool = False
    flexible: bool = False
    readonly: bool = False
    value: str | None = None
    reference: bool = False
    on_delete: str | None = None
    nullable: bool = False
    comment: str | None = None
    # Store previous definition for rollback
    previous_type: FieldType | str | None = None
    previous_default: Any = None
    previous_assertion: str | None = None
    previous_flexible: bool = False
    previous_readonly: bool = False
    previous_nullable: bool = False
    previous_reference: bool = False
    previous_on_delete: str | None = None
    previous_value: str | None = None
    previous_comment: str | None = None

    @classmethod
    def from_field_states(cls, table: str, current: "FieldState", target: "FieldState") -> "AlterField":
        """
        Build the operation that moves *current* to *target* on *table*.

        The counterpart to :meth:`AddField.from_field_state`, wiring the target
        state forwards and the current state into the ``previous_*`` attributes
        ``backwards()`` needs.  Hand-copying these is what left ``REFERENCE``,
        ``FLEXIBLE`` and ``READONLY`` off every generated rollback.

        Args:
            table: Name of the table the field belongs to
            current: The field state the database is in
            target: The field state the models describe

        Returns:
            An ``AlterField`` carrying both states in full
        """
        return cls(
            table=table,
            name=target.name,
            field_type=target.field_type,
            nullable=target.nullable,
            default=target.default,
            assertion=target.assertion,
            encrypted=target.encrypted,
            flexible=target.flexible,
            readonly=target.readonly,
            value=target.value,
            reference=target.reference,
            on_delete=target.on_delete,
            comment=target.comment,
            previous_type=current.field_type,
            previous_nullable=current.nullable,
            previous_default=current.default,
            previous_assertion=current.assertion,
            previous_flexible=current.flexible,
            previous_readonly=current.readonly,
            previous_reference=current.reference,
            previous_on_delete=current.on_delete,
            # `encrypted` *is* "VALUE is the argon2 expression"; resolving it
            # here leaves backwards() with a single VALUE branch instead of a
            # precedence rule over a state no diff can emit.
            previous_value=ARGON2_VALUE if current.encrypted else current.value,
            previous_comment=current.comment,
        )

    def __post_init__(self) -> None:
        """Validate field_type and set reversible based on previous state."""
        # Validate field types if provided
        if self.field_type is not None:
            _normalize_field_type(self.field_type)
        if self.previous_type is not None:
            _normalize_field_type(self.previous_type)
        object.__setattr__(self, "reversible", self.previous_type is not None)

    def forwards(self) -> str:
        # ALTER FIELD, not DEFINE FIELD. A plain `DEFINE FIELD` over an existing
        # field does not update it — SurrealDB answers `The field 'x' already
        # exists` — so every AlterField was a no-op (#162). `DEFINE FIELD
        # OVERWRITE` fixed that but replaces the whole definition, resetting
        # every clause the ORM does not model: a field restricted with
        # `PERMISSIONS FOR select NONE` came back `PERMISSIONS FULL` (#195).
        # ALTER FIELD changes only what it names, and a missing field is an
        # error the executor raises as MigrationStatementError rather than a
        # field silently created under a mistyped name.
        return _render_alter_field(self.table, self.name, **_field_clauses(self))

    def backwards(self) -> str:
        if not self.previous_type:
            return ""

        # The same renderer as forwards(), over the previous_* slots: every
        # modelled clause is restored — VALUE included, or an Encrypted column
        # stops hashing and stores plaintext (#196) — and nothing else is
        # touched.
        return _render_alter_field(
            self.table,
            self.name,
            **{f: getattr(self, _previous_attr(f)) for f in FIELD_DEFINITION_FIELDS},
        )

    def describe(self) -> str:
        return f"Alter field {self.name} on {self.table}"


@dataclass
class CreateIndex(Operation):
    """
    Create an index on a table.

    Supports standard, unique, full-text search (BM25), and vector (HNSW) indexes.

    Example:
        # Unique index
        CreateIndex(table="users", name="email_idx", fields=["email"], unique=True)

        # Full-text search index
        CreateIndex(
            table="posts", name="ft_title", fields=["title"],
            search_analyzer="my_analyzer", bm25=True, highlights=True,
        )

        # HNSW vector index
        CreateIndex(
            table="documents", name="vec_idx", fields=["embedding"],
            hnsw=True, dimension=1536, dist="COSINE", vector_type="F32",
        )

    Generates:
        DEFINE INDEX email_idx ON users FIELDS email UNIQUE;
        DEFINE INDEX ft_title ON posts FIELDS title FULLTEXT ANALYZER my_analyzer BM25 HIGHLIGHTS;
        DEFINE INDEX vec_idx ON documents FIELDS embedding HNSW DIMENSION 1536 DIST COSINE TYPE F32;
    """

    table: str
    name: str
    fields: list[str]
    unique: bool = False
    search_analyzer: str | None = None
    bm25: tuple[float, float] | bool | None = None
    highlights: bool = False
    hnsw: bool = False
    dimension: int | None = None
    dist: str | None = None
    vector_type: str | None = None
    efc: int | None = None
    m: int | None = None
    concurrently: bool = False
    comment: str | None = None

    def forwards(self) -> str:
        fields_str = ", ".join(self.fields)
        parts = [f"DEFINE INDEX {self.name} ON {self.table} FIELDS {fields_str}"]

        if self.unique:
            parts.append("UNIQUE")

        if self.search_analyzer:
            parts.append(f"FULLTEXT ANALYZER {self.search_analyzer}")

            if self.bm25 is True:
                parts.append("BM25")
            elif isinstance(self.bm25, tuple):
                parts.append(f"BM25({self.bm25[0]},{self.bm25[1]})")

            if self.highlights:
                parts.append("HIGHLIGHTS")

        if self.hnsw:
            parts.append("HNSW")

            if self.dimension is not None:
                parts.append(f"DIMENSION {self.dimension}")

            if self.dist:
                parts.append(f"DIST {self.dist}")

            if self.vector_type:
                parts.append(f"TYPE {self.vector_type}")

            if self.efc is not None:
                parts.append(f"EFC {self.efc}")

            if self.m is not None:
                parts.append(f"M {self.m}")

            if self.concurrently:
                parts.append("CONCURRENTLY")

        if self.comment:
            parts.append(f"COMMENT {surql_string(self.comment)}")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return f"REMOVE INDEX {self.name} ON {self.table};"

    def describe(self) -> str:
        return f"Create index {self.name} on {self.table}"


@dataclass
class DropIndex(Operation):
    """
    Remove an index from a table.

    Example:
        DropIndex(table="users", name="email_idx")

    Generates:
        REMOVE INDEX email_idx ON users;
    """

    table: str
    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE INDEX {self.name} ON {self.table};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Drop index {self.name} from {self.table}"


@dataclass
class DefineAccess(Operation):
    """
    Define access control for authentication (DEFINE ACCESS ... TYPE RECORD).

    Example:
        DefineAccess(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email", "password": "crypto::argon2::generate($password)"},
            signin_where="email = $email AND crypto::argon2::compare(password, $password)"
        )

    Generates:
        DEFINE ACCESS user_auth ON DATABASE TYPE RECORD
            SIGNUP (CREATE User SET email = $email, password = crypto::argon2::generate($password))
            SIGNIN (SELECT * FROM User WHERE email = $email AND crypto::argon2::compare(password, $password))
            DURATION FOR TOKEN 15m, FOR SESSION 12h;
    """

    name: str
    table: str
    signup_fields: dict[str, str]
    signin_where: str
    duration_token: str = "15m"
    duration_session: str = "12h"
    comment: str | None = None

    def forwards(self) -> str:
        signup_sets = ", ".join(f"{field} = {expr}" for field, expr in self.signup_fields.items())

        sql = f"""DEFINE ACCESS {self.name} ON DATABASE TYPE RECORD
    SIGNUP (CREATE {self.table} SET {signup_sets})
    SIGNIN (SELECT * FROM {self.table} WHERE {self.signin_where})
    DURATION FOR TOKEN {self.duration_token}, FOR SESSION {self.duration_session}"""

        if self.comment:
            sql += f"\n    COMMENT {surql_string(self.comment)}"

        return sql + ";"

    def backwards(self) -> str:
        return f"REMOVE ACCESS {self.name} ON DATABASE;"

    def describe(self) -> str:
        return f"Define access {self.name} for {self.table}"


@dataclass
class RemoveAccess(Operation):
    """
    Remove an access definition.

    Example:
        RemoveAccess(name="user_auth")

    Generates:
        REMOVE ACCESS user_auth ON DATABASE;
    """

    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE ACCESS {self.name} ON DATABASE;"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Remove access {self.name}"


@dataclass
class DefineBearerAccess(Operation):
    """
    Define a bearer access method for machine-to-machine authentication (SurrealDB 3.0+).

    Bearer access issues opaque API keys via GRANT/REVOKE instead of
    SIGNUP/SIGNIN used by RECORD access.

    Example:
        DefineBearerAccess(
            name="api_key",
            duration_grant="30d",
            duration_session="1h",
        )

    Generates:
        DEFINE ACCESS api_key ON DATABASE TYPE BEARER
            DURATION FOR GRANT 30d, FOR SESSION 1h;
    """

    name: str
    bearer_for: str = "USER"
    duration_grant: str = "30d"
    duration_session: str = "1h"
    comment: str | None = None

    def forwards(self) -> str:
        sql = f"DEFINE ACCESS {self.name} ON DATABASE TYPE BEARER FOR {self.bearer_for.upper()}"
        if self.duration_grant or self.duration_session:
            sql += f"\n    DURATION FOR GRANT {self.duration_grant}, FOR SESSION {self.duration_session}"

        if self.comment:
            sql += f"\n    COMMENT {surql_string(self.comment)}"

        return sql + ";"

    def backwards(self) -> str:
        return f"REMOVE ACCESS {self.name} ON DATABASE;"

    def describe(self) -> str:
        return f"Define bearer access {self.name}"


@dataclass
class DataMigration(Operation):
    """
    Execute data transformations (UPDATE, DELETE operations on records).

    Used for the 'upgrade' command to transform existing data.

    Example:
        DataMigration(
            forwards_sql="UPDATE User SET status = 'active' WHERE status IS NULL;",
            backwards_sql="UPDATE User SET status = NULL WHERE status = 'active';"
        )

    Or with async functions:
        DataMigration(
            forwards_func=async_migrate_passwords,
            backwards_func=None  # Irreversible
        )
    """

    forwards_sql: str | None = None
    backwards_sql: str | None = None
    forwards_func: Callable[[], Coroutine[Any, Any, None]] | None = None
    backwards_func: Callable[[], Coroutine[Any, Any, None]] | None = None
    description: str = "Data migration"

    def __post_init__(self) -> None:
        if not self.forwards_sql and not self.forwards_func:
            raise ValueError("DataMigration requires either forwards_sql or forwards_func")
        self.reversible = bool(self.backwards_sql or self.backwards_func)

    def forwards(self) -> str:
        return self.forwards_sql or ""

    def backwards(self) -> str:
        return self.backwards_sql or ""

    @property
    def has_func(self) -> bool:
        """Check if this migration uses async functions."""
        return self.forwards_func is not None

    def describe(self) -> str:
        return self.description


@dataclass
class RawSQL(Operation):
    """
    Execute raw SurrealQL statements.

    Use with caution - prefer structured operations when possible.

    Example:
        RawSQL(
            sql="DEFINE EVENT user_created ON TABLE User WHEN $event = 'CREATE' THEN (CREATE log SET action = 'user_created');",
            reverse_sql="REMOVE EVENT user_created ON TABLE User;"
        )
    """

    sql: str
    reverse_sql: str = ""
    description: str = "Raw SQL"

    def __post_init__(self) -> None:
        self.reversible = bool(self.reverse_sql)

    def forwards(self) -> str:
        return self.sql

    def backwards(self) -> str:
        return self.reverse_sql

    def describe(self) -> str:
        return self.description


@dataclass
class DefineAnalyzer(Operation):
    """
    Define a full-text search analyzer.

    Example:
        DefineAnalyzer(
            name="my_analyzer",
            tokenizers=["blank", "class"],
            filters=["lowercase", "snowball(english)"],
        )

    Generates:
        DEFINE ANALYZER my_analyzer TOKENIZERS blank, class FILTERS lowercase, snowball(english);
    """

    name: str
    tokenizers: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)

    def forwards(self) -> str:
        parts = [f"DEFINE ANALYZER {self.name}"]

        if self.tokenizers:
            parts.append(f"TOKENIZERS {', '.join(self.tokenizers)}")

        if self.filters:
            parts.append(f"FILTERS {', '.join(self.filters)}")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return f"REMOVE ANALYZER {self.name};"

    def describe(self) -> str:
        return f"Define analyzer {self.name}"


@dataclass
class RemoveAnalyzer(Operation):
    """
    Remove a full-text search analyzer.

    Example:
        RemoveAnalyzer(name="my_analyzer")

    Generates:
        REMOVE ANALYZER my_analyzer;
    """

    name: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE ANALYZER {self.name};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Remove analyzer {self.name}"


@dataclass
class DefineEvent(Operation):
    """
    Define a server-side event (trigger) on a table.

    Example:
        DefineEvent(
            name="audit_create",
            table="users",
            when="$event = 'CREATE'",
            then="CREATE audit_log SET table = 'users', action = 'create', at = time::now()",
        )

    Generates:
        DEFINE EVENT audit_create ON users WHEN $event = 'CREATE'
            THEN (CREATE audit_log SET table = 'users', action = 'create', at = time::now());
    """

    name: str
    table: str
    when: str
    then: str
    comment: str | None = None

    def forwards(self) -> str:
        parts = [f"DEFINE EVENT {self.name} ON {self.table}"]
        parts.append(f"WHEN {self.when}")
        parts.append(f"THEN ({self.then})")

        if self.comment:
            parts.append(f"COMMENT {surql_string(self.comment)}")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return f"REMOVE EVENT {self.name} ON {self.table};"

    def describe(self) -> str:
        return f"Define event {self.name} on {self.table}"


@dataclass
class RemoveEvent(Operation):
    """
    Remove a server-side event from a table.

    Example:
        RemoveEvent(name="audit_create", table="users")

    Generates:
        REMOVE EVENT audit_create ON users;
    """

    name: str
    table: str

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return f"REMOVE EVENT {self.name} ON {self.table};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Remove event {self.name} from {self.table}"


@dataclass
class DefineApi(Operation):
    """
    Define a REST API endpoint (SurrealDB 3.0+).

    Example:
        DefineApi(
            name="/users/list",
            method="get",
            handler="SELECT * FROM users",
        )

    Generates:
        DEFINE API "/users/list" FOR get THEN { SELECT * FROM users; };
    """

    name: str
    method: str | None = None
    handler: str = ""
    middleware: list[str] | None = None
    permissions: str | None = None
    comment: str | None = None

    def forwards(self) -> str:
        # Ensure the endpoint path is quoted
        quoted_name = f'"{self.name}"' if not self.name.startswith('"') else self.name
        parts = [f"DEFINE API {quoted_name}"]

        if self.method:
            parts.append(f"FOR {self.method.lower()}")

        if self.middleware:
            parts.append(f"MIDDLEWARE {', '.join(self.middleware)}")

        if self.permissions:
            parts.append(f"PERMISSIONS {self.permissions}")

        if self.handler:
            parts.append(f"THEN {{ {self.handler}; }}")

        if self.comment:
            parts.append(f"COMMENT {surql_string(self.comment)}")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        quoted_name = f'"{self.name}"' if not self.name.startswith('"') else self.name
        return f"REMOVE API {quoted_name};"

    def describe(self) -> str:
        method = f" {self.method.upper()}" if self.method else ""
        return f"Define API{method} {self.name}"


@dataclass
class RemoveApi(Operation):
    """
    Remove a REST API endpoint (SurrealDB 3.0+).

    Example:
        RemoveApi(name="/users/list")

    Generates:
        REMOVE API "/users/list";
    """

    name: str
    method: str | None = None

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        quoted_name = f'"{self.name}"' if not self.name.startswith('"') else self.name
        return f"REMOVE API {quoted_name};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        method = f" {self.method.upper()}" if self.method else ""
        return f"Remove API{method} {self.name}"


@dataclass
class RebuildIndex(Operation):
    """
    Rebuild an index on a table (SurrealDB 3.0+).

    Useful after bulk data imports or index definition changes
    to force a full reindex.

    Example:
        RebuildIndex(table="documents", name="idx_embedding")
        RebuildIndex(table="articles", name="idx_fts", if_exists=True)

    Generates:
        REBUILD INDEX idx_embedding ON documents;
        REBUILD INDEX IF EXISTS idx_fts ON articles;
    """

    table: str
    name: str
    if_exists: bool = False

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        ie = "IF EXISTS " if self.if_exists else ""
        return f"REBUILD INDEX {ie}{self.name} ON {self.table};"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return f"Rebuild index {self.name} on {self.table}"


@dataclass
class DefineGraphQLConfig(Operation):
    """
    Define or update GraphQL configuration (SurrealDB 3.0+).

    Example:
        DefineGraphQLConfig(tables_mode="AUTO", functions_mode="AUTO")
        DefineGraphQLConfig(tables_mode="INCLUDE", tables_list=["users", "orders"])
        DefineGraphQLConfig(tables_mode="EXCLUDE", tables_list=["audit_log"])

    Generates:
        DEFINE CONFIG GRAPHQL TABLES AUTO FUNCTIONS AUTO;
        DEFINE CONFIG GRAPHQL TABLES INCLUDE users, orders FUNCTIONS AUTO;
        DEFINE CONFIG GRAPHQL TABLES EXCLUDE audit_log FUNCTIONS AUTO;
    """

    tables_mode: str = "AUTO"
    tables_list: list[str] = field(default_factory=list)
    functions_mode: str = "AUTO"
    functions_list: list[str] = field(default_factory=list)

    def forwards(self) -> str:
        parts = ["DEFINE CONFIG GRAPHQL"]

        # Tables clause
        if self.tables_list:
            parts.append(f"TABLES {self.tables_mode} {', '.join(self.tables_list)}")
        else:
            parts.append(f"TABLES {self.tables_mode}")

        # Functions clause
        if self.functions_list:
            parts.append(f"FUNCTIONS {self.functions_mode} {', '.join(self.functions_list)}")
        else:
            parts.append(f"FUNCTIONS {self.functions_mode}")

        return " ".join(parts) + ";"

    def backwards(self) -> str:
        return "DEFINE CONFIG GRAPHQL TABLES NONE FUNCTIONS NONE;"

    def describe(self) -> str:
        return f"Define GraphQL config (tables={self.tables_mode}, functions={self.functions_mode})"


@dataclass
class RemoveGraphQLConfig(Operation):
    """
    Disable GraphQL configuration by setting TABLES NONE FUNCTIONS NONE (SurrealDB 3.0+).

    SurrealDB 3.0 does not support ``REMOVE CONFIG GRAPHQL``.
    Instead, we overwrite the configuration with NONE modes to effectively disable it.

    Generates:
        DEFINE CONFIG GRAPHQL TABLES NONE FUNCTIONS NONE;
    """

    def __post_init__(self) -> None:
        self.reversible = False

    def forwards(self) -> str:
        return "DEFINE CONFIG GRAPHQL TABLES NONE FUNCTIONS NONE;"

    def backwards(self) -> str:
        return ""

    def describe(self) -> str:
        return "Remove GraphQL config"
