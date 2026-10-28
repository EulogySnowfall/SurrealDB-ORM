# Changelog

All notable changes to this project will be documented in this file.
This file follows the [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format
and adheres to [SemVer](https://semver.org/) versioning.

---

## [0.35.3] - 2026-10-28

**Fix release.** Key checks for `HTTPTransaction` data (#334 via #374).
Read *Behaviour change* below if you pass dotted keys to a transaction
method. The SurrealDB pin is unchanged (tested against 3.3.4, supported
range 3.2.x – 3.3.x).

### Fixed

- **`HTTPTransaction.create()`, `insert()`, `update()`, `upsert()`,
  `merge()` and `relate()` (SDK) check every key of `data` when the
  statement is queued.** A key that is not a plain identifier
  (`[A-Za-z_][A-Za-z0-9_]*`), for example one with a space, a `-` or a
  `;`, raises `ValueError` naming the key at the call. Before, the
  statement was queued as is: most such keys failed at `commit()` with
  `TransactionError`, and a dotted key such as `a.b` committed and wrote
  nothing for that field. `insert()` checks every record before queueing
  any, so a bad key in a later record leaves the transaction unchanged.
  Values are still bound as parameters (#334).

### Behaviour change

- Only keys that are not plain identifiers are affected. They now raise
  `ValueError` at the call instead of failing at `commit()` or, for a
  dotted key, committing without writing that field. The SurrealQL
  generated for valid keys is unchanged.
- ORM `save(tx=)`, `merge(tx=)` and `relate(tx=)` over HTTP raise the same
  `ValueError` when a field alias (or a `relate()` edge-data key) is not an
  identifier.

### Upgrading

- No schema, DDL or public signature change.
- To set a nested field inside a transaction, pass a nested dict
  (`{"a": {"b": 5}}`) instead of a dotted key, or write the statement with
  `query()`. A dotted key was silently dropped before.

## [0.35.2] - 2026-10-28

**Fix release.** Field-name checks for filter keys and `values()` (#328 via
#367). Read *Behaviour change* below if you pass unknown lookups,
backticked names or SurrealQL expressions as filter keys or `values()`
names. The SurrealDB pin is unchanged (tested against 3.3.4, supported
range 3.2.x – 3.3.x).

### Fixed

- **`QuerySet.filter()`, `Q` and relation filter
  (`instance.<relation>.filter()`) keys whose field part is not a field
  name now raise `ValueError` naming the key**, before any query is sent
  (for example `"name x"` or `"a-b"`). Before, they reached the server,
  which either reported a parse error or read them as a different
  condition. Dotted paths into nested objects (`"address.city"`,
  `"address.zip__startswith"`) keep working; each segment must be an
  identifier (#328).
- **`QuerySet.values()` validates each grouping name the same way** (a
  field name or a dotted path), so it is no longer emitted unchecked into
  the SELECT list and `GROUP BY` clause (#328).
- **Trailing whitespace was accepted in identifiers.** A name ending in a
  newline passed `validate_identifier()` and the other identifier checks
  (`bulk_update()` keys, `atomic_append()` / `atomic_remove()` /
  `atomic_set_add()` fields, relation names, SDK stream table names). These
  checks now require a full match. Record-ID rendering follows the same
  rule: `needs_id_escaping()` now returns `True` for an ID ending in a
  newline, so `format_thing()` backtick-escapes it, and `to_record_id()`
  returns a `"table:id"` string whose table part ends in a newline
  unchanged instead of building a `RecordId` (#328).

### Docs

- The `QuerySet.select()` docstring says its arguments are SurrealQL
  expressions rendered as written and must not be built from end-user
  input.

### Behaviour change

- An unknown filter lookup (for example `name__iexact`) now raises
  `ValueError`; it used to be treated silently as an exact match (`=`).
- In relation filters, only `exact`, `gt`, `gte`, `lt`, `lte`, `in`,
  `contains`, `startswith`, `endswith` and `isnull` are rendered; any other
  lookup now raises `ValueError` instead of becoming an equality test.

### Upgrading

- No schema, DDL or public signature change.
- Code that passed backticked names, bracket or `*` paths, SurrealQL
  expressions or unknown lookups as filter keys or `values()` names now
  gets `ValueError`. Use plain field names or dotted paths, `raw_query()`
  for expressions, and a supported lookup. In a relation filter, replace an
  unsupported lookup (`icontains`, `regex`, `not_in`, …) with a supported
  one, or query through `Model.objects().filter()`.

## [0.35.1] - 2026-10-28

**Fix release.** `validate_token()` cache and auth connection routing (#331
via #370). Read *Behaviour change* below if you validate a token through a
model other than the one whose table issued it, or route an
`AuthenticatedUserMixin` model to a connection other than `default`. The
SurrealDB pin is unchanged (tested against 3.3.4, supported range
3.2.x – 3.3.x).

### Fixed

- **`validate_token()` no longer returns a cached result after the token's
  own `exp` (#331).** A cached entry now lasts until `min(ttl, exp)`, so a
  token with a shorter lifetime than the cache TTL is checked against the
  server again once its `exp` has passed. Before, a cache hit only checked
  the cache TTL.
- **`validate_token()` and `authenticate_token()` return `None` for a token
  whose `$auth` record is not in the model's table**, with or without a
  cache hit, and the validation cache is keyed per model table, so an entry
  cached through one model is never returned by another (#331).
- **`validate_token()` cache entries are no longer shared between
  connections.** The cache is keyed by connection (name, URL, namespace,
  database), model table and token (#331).
- **`invalidate_token_cache(token)` removes the token's entries for every
  model table and every connection** (#331).
- **Auth methods on `AuthenticatedUserMixin` models honour the model's
  `connection=` setting and `SurrealDBConnectionManager.using()`** —
  `signup`, `signin`, `authenticate_token`, `validate_token`,
  `define_access` and the bearer-key methods. Before, with the documented
  `class User(AuthenticatedUserMixin, BaseSurrealModel)` order, the mixin's
  runtime `get_connection_name()` stub shadowed the model's and they always
  used the `default` connection (#331).

### Behaviour change

- A token whose `exp` claim cannot be read (opaque token, no `exp` claim,
  or a non-numeric one) is validated against the server on every call and
  never cached.
- Validating a token through a model other than the one whose table issued
  it returns `None`: validate it through the model it belongs to.
- A mixin-first model declared with `connection="x"`, or used inside
  `using("x")`, sends its auth calls to connection `x` instead of
  `default`.

### Upgrading

- No schema, DDL or public signature change.
- Code that wrote directly into the private `_token_cache` must use the new
  `(connection identity, table, token)` key.
- If your access is defined only on the `default` database while the model
  is routed elsewhere, define the access on the routed database or remove
  the `connection=` setting.

### Known limitation

- `validate_token_local()` (local decode, no network call) does not apply
  the new model-table check: it still returns the record id whatever its
  table. Use `validate_token()` when the table matters.

## [0.35.0] - 2026-10-28

**Minor release.** Two fixes (#333 via #362, #329 via #365); the sign-up fix
adds one setting, `SurrealConfigDict(signup_fields=[...])`, hence the minor
bump. **Both change behaviour** — read *Behaviour change* and *Upgrading*
below if your `TableType.USER` models take extra fields at sign-up, or if you
pass anything other than an `int` to `limit()`, `offset()`, `similar_to()` or
`hybrid_search()`. The SurrealDB pin is unchanged (tested against 3.3.4,
supported range 3.2.x – 3.3.x).

### Fixed

- **The generated `DEFINE ACCESS ... SIGNUP` clause assigns only an explicit
  sign-up set (#333).** For a `TableType.USER` model, `define_access()`,
  `makemigrations` and `schema_diff` used to assign every model field from
  the sign-up request (`field = $field`), so model defaults were lost at
  sign-up and fields excluded from every other write path (computed,
  `server_fields`, read-only) were still taken from the request. The clause
  now assigns the identifier, the password (`crypto::<alg>::generate`), the
  fields listed in `signup_fields`, and `created_at = time::now()` when the
  model has that field. Every other field keeps its schema `DEFAULT` or model
  default.
- **`makemigrations` / `schema_diff` build the access definition from the
  same code as `define_access()`**, so they now honour
  `encryption_algorithm` and `access_name` (previously always `argon2` and
  `<table>_auth`).
- **`QuerySet.limit()`, `offset()`, `similar_to(limit, ef)` and
  `hybrid_search(vector_limit, ef, text_limit, rrf_k)` accept only integers
  and check them at the call, before any query is built (#329).** Their
  `int` annotations were never enforced, and these values are written into
  the query text, so a numeric string, a float or a `bool` produced a
  malformed query. Valid integers produce exactly the same SurrealQL as
  before.

### Added

- **`SurrealConfigDict(signup_fields=[...])`** — the fields a caller may set
  at sign-up, besides the identifier and the password. An entry that is
  `id`, the identifier, the password, `created_at`, a computed field, a
  `server_fields` entry, a read-only field or not a model field raises
  `ValueError` naming the field and the reason.

### Behaviour change

- `signup()` accepts only the identifier, the password and the
  `signup_fields` entries; any other keyword raises `SurrealDbError` before
  any request. A model with no `signup_fields` signs up with the identifier
  and password only.
- The emitted access definition changes, so on an existing database
  `makemigrations` / `schema_diff` emit an access update
  (`REMOVE ACCESS` + `DEFINE ACCESS`).
- A non-`int` argument to `limit()`, `offset()`, `similar_to()` or
  `hybrid_search()` — including a numeric string such as `"10"`, a float or
  a `bool` — raises `TypeError`. A value below the minimum raises
  `ValueError`: the minimum is 0 for `limit`, `offset` and `ef`, and 1 for
  the KNN `limit`, `vector_limit`, `text_limit` and `rrf_k`.

### Upgrading

- List in `signup_fields` the fields your sign-up form sets, then re-apply
  the access definition (`makemigrations` + `migrate`, or `define_access()`)
  so an existing database picks up the new `SIGNUP` clause.
- Convert request or query-string values with `int(...)` before passing them
  to `limit()` / `offset()` / `similar_to()` / `hybrid_search()`. Code that
  already passes `int` needs no change.

## [0.34.19] - 2026-10-28

**Fix release.** SDK change feeds only (#346, #361). Read *Behaviour change*
below if you call `ChangeFeedStream.get_changes()` while other tables are
being written. The SurrealDB pin is unchanged (tested against 3.3.4,
supported range 3.2.x – 3.3.x).

### Fixed

- **Change-feed reads no longer fail to return while other tables are being
  written (#346).** A read stopped only on an empty read of the database
  feed, so a single write on any table between two reads kept it going:
  under a steady writer on another table, `ChangeFeedStream.get_changes()`
  never returned, the default start of `stream()` / `stream_batch()` /
  `MultiTableChangeFeed.stream()` never resolved, and a caught-up stream
  polled without sleeping. `QuerySet.changes()` goes through the SDK stream
  and is fixed too. A caught-up poll now ends the read and sleeps
  `poll_interval` even when other tables are busy. The new early stop is
  safe on SurrealDB 3.3, where `SHOW CHANGES … LIMIT` counts (versionstamp,
  table) units while the database view merges them by versionstamp: a
  window cut inside a multi-table transaction is never taken for the end of
  the feed, because a window ends the read only when an upper bound on its
  units is below the scan limit. An over-count costs one extra read, never
  an entry.

### Behaviour change

- `get_changes()` is a snapshot read: it stops at the end of the feed as it
  stood when read, and does not chase writes made meanwhile on any table.
  Each entry is still delivered exactly once.

### Known limitation

- With the JSON protocol (CBOR is the default), a record id whose escaped
  table name contains `:` (for example `` `a:b`:1 ``) is split at the first
  `:`, so two such tables can be counted as one unit. In a window exactly at
  the scan limit this could take a cut window for the end of the feed.

## [0.34.18] - 2026-10-28

**Fix release.** `QueryCache` automatic invalidation only (#338, #356). Read
*Behaviour change* below if your application uses several named connections
with `.cache()`. The SurrealDB pin is unchanged (tested against 3.3.4,
supported range 3.2.x – 3.3.x).

### Fixed

- **A write could leave stale `.cache()` results in place (#338).** Since
  0.34.13 (#309), `save()` / `merge()` / `update()` / `delete()` only dropped
  the cached entries of the writing connection's `(url, namespace, database)`.
  That location does not reliably identify the database: one database reached
  through two URL spellings (e.g. `http://host:8000` and `http://host:8000/`,
  `http` and `ws`, `localhost` and `127.0.0.1`) is two cache buckets, and a
  `save(tx=...)` runs on the transaction's client whatever `using()` or the
  model's `connection` config says. Stale rows were served until their TTL
  expired. A write now drops the written table's cached entries for every
  connection.
- This also removes the 0.34.13 *Known limitation*: calling
  `QueryCache.invalidate(Model)` after a `save(tx=tx)` on a model configured
  with another connection is no longer needed.

### Behaviour change

- `.cache()` reads are still keyed per connection (two databases never share a
  cached result), but a write now invalidates the table's cached entries for
  **all** connections. Applications with several connections will see more
  cache misses after a write; over-invalidating costs a re-query, while the
  previous rule could serve stale rows.
- The `v2` line has the same defect; the backport is tracked in #358.

## [0.34.17] - 2026-10-28

**Fix release.** `AuthenticatedUserMixin` only (#332, #351). Read *Behaviour
change* below if you call `signin()`, `signup()` or `change_password()` on a
table whose identifier values are not unique, or if you rely on their error
handling. The SurrealDB pin is unchanged (tested against 3.3.4, supported
range 3.2.x – 3.3.x).

### Fixed

- **`signin()` and `signup()` return the record the issued session token
  belongs to (#332).** They read `$auth` on the sign-in connection and fetch
  the user by that record ID, instead of looking the user up again by
  `identifier_field` and taking the first row. When several records shared an
  identifier value (no `UNIQUE` index on it), `AuthResult.user` could be a
  different record from the one the token was issued for.
- **`change_password()` updates only the record that signed in (#332)**,
  instead of every record sharing the identifier value.
- **`authenticate_token()` binds the decoded `$auth` record ID** when fetching
  the user, so a record key that looks like a number (e.g. `⟨000000⟩`)
  resolves to the right record (#332). The `record_id` string it returns is
  unchanged.

### Behaviour change

- `signin()` / `signup()` raise `SurrealDbError` if the session record cannot
  be read after a successful access call, and `DoesNotExist` if that record no
  longer exists. They no longer require `identifier_field` among the
  credentials; the `DEFINE ACCESS` definition validates them.
- `change_password()` raises `SurrealDbError` when the update does not affect
  exactly the signed-in record, instead of returning `True`.
- Nothing changes for applications whose identifier values are unique.

## [0.34.16] - 2026-10-28

**Fix release.** SDK `HTTPConnection` sign-in state only (#330, #347). Read
*Behaviour change* below if you call `authenticate()` or `signup()` on an HTTP
connection that was signed in with `signin()` first. The SurrealDB pin is
unchanged (tested against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **`HTTPConnection` no longer signs in again with the credentials of an
  earlier `signin()` when a token installed by `authenticate()` or `signup()`
  is rejected (#330).** The transient-`401` recovery added in 0.31.4 (#101)
  replays the arguments of the last successful `signin()`. Nothing cleared
  them, so once a later token was rejected (for example after it expired),
  the request was answered by signing in again with those earlier
  credentials and went through under that earlier identity instead of
  failing. The retained sign-in arguments now describe only the current
  identity: `authenticate()` and `signup()` drop them, and so do a failed
  `signin()` and `close()`.

### Added

- **`HTTPConnection.invalidate()`** sends the `invalidate` RPC, then clears
  the token, the authenticated flag and the retained sign-in arguments, even
  when the RPC fails (that error is raised). Later requests carry no
  credentials until the next `signin()` or `authenticate()`.

### Behaviour change

- On an HTTP connection whose token came from `authenticate()` or
  `signup()`, a `401` is now raised as `QueryError` (code 401) instead of
  being answered by signing in again. Recovery from a transient `401` (#101)
  is unchanged for tokens obtained by `signin()`. If the re-mint itself
  fails, the retry with the existing token is now the last attempt.
- **Migration:** code that called `authenticate()` or `signup()` on a
  connection after `signin()`, and relied on later requests succeeding once
  that token had run out, must call `signin()` again or supply a fresh token.

## [0.34.15] - 2026-10-28

**Fix release.** `QuerySet` filters only (#327, #341). Read *Behaviour
change* below if you reference query variables from `filter()`. The SurrealDB
pin is unchanged (tested against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **`$`-prefixed filter values are bound as parameters (#327).** A `QuerySet`
  filter value starting with `$` was always written into the WHERE clause as
  a variable reference, because only its first character was checked. A value
  that merely starts with `$` (an email, a search term such as `"$x OR true"`)
  therefore changed the condition instead of being matched as text. A value is
  now a variable reference only when it is exactly `$name` and `name` was
  declared with `.variables()` on the same QuerySet (or on the inner QuerySet
  of a `Subquery`); any other value is bound and compared as data. This
  applies to keyword filters, `Q` objects and `~Q`, subqueries, `annotate()`
  subqueries and `live()`, and gives the same result when a QuerySet is
  compiled more than once (for example `count()` then `exec()`).
- **`Subquery` sends the variables declared on its inner QuerySet (#327).**
  They were dropped, so a reference to one reached the server unbound. An
  outer variable of the same name wins.

### Behaviour change

- An undeclared `$name` filter value, or any `$`-prefixed value that is not
  exactly a declared name (trailing text, a trailing newline, `$auth.id`, a
  name matching one of the ORM's internal parameters such as `$_f0`), is now
  compared as literal text instead of being sent as a reference. On a
  `ForeignKey` field it is treated as a bare ID: `filter(author="$who")`
  without `.variables(who=...)` compares against `<target_table>:$who`.
- **Migration:** to keep using a variable in a filter, declare it on the same
  QuerySet (or on the inner QuerySet of a `Subquery`), e.g.
  `filter(age__gte="$min_age").variables(min_age=18)`. Built-in server
  parameters such as `$auth` cannot be referenced from `filter()`; use
  `raw_query()` for those.

### Added

- `surreal_orm.utils.is_declared_variable_reference(value, declared)`: true
  only for a string that fully matches `$` + identifier whose name is in
  `declared`.

## [0.34.14] - 2026-10-28

**Fix release.** SDK transactions only (#306, #318). ORM users have nothing
to change; direct SDK callers that passed array or object IDs to transaction
methods should read *Changed* below. The SurrealDB pin is unchanged (tested
against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **`HTTPTransaction` handles record IDs that are not plain identifiers
  (#306).** `create` / `update` / `upsert` / `merge` / `delete` / `relate`
  wrote each `table:id` reference into the batched SQL as given, so an ID with
  a space, `-`, `;`, a backtick, a backslash or a leading digit produced a
  statement that failed to parse, and `users:a-b` was read as an expression
  and wrote nothing. Such IDs are now bound as parameters
  (`type::record('table', $_ref)`). The backtick form written by
  `format_thing()` keeps working and is read back once.
- **`HTTPTransaction.merge()` did nothing (#306).** It rendered
  `MERGE { k = $k }`, which the server rejects, so a merge queued in an HTTP
  transaction (including ORM `save()` / `merge()` with `tx=` on an HTTP
  connection) silently had no effect. It now renders `MERGE { k: $k }`.

### Changed

- **Malformed references raise `ValueError`** in `HTTPTransaction` and
  `WebSocketTransaction`, before anything is queued or sent: an empty or
  non-identifier table (`:x`, `a b:c`, a backtick-quoted table), an empty ID,
  an unclosed or malformed delimited ID, an array or object ID (`t:[1,2]`,
  `t:{a:1}`). `relate()`'s `relation` and `insert()`'s `table` must be
  identifiers, and a trailing newline in any of them is rejected.
- Plain IDs, `rand()` / `ulid()` / `uuid()`, table-only references and
  integer IDs are unchanged: any signed 64-bit integer (`t:1`, `t:-5`) stays
  an integer ID, and a larger one stays a string ID, as the server reads it.

### Known limitation

- Integer IDs through `WebSocketTransaction` still reach the server as string
  IDs over CBOR, the default protocol (`t:1` targets `` t:`1` ``):
  `WebSocketConnection` converts every `table:id` to a string `RecordId`. This
  is unchanged and connection-level.

### Upgrading

- Direct SDK callers that passed array or object IDs to transaction methods
  should use `tx.query()` with a bound `RecordId`. The `v2` line has the same
  defects; the backport is tracked in #322.

---

## [0.34.13] - 2026-10-28

**Fix release.** `QuerySet.cache()` no longer shares results between
connections (#297, #309). `QueryCache.make_key()` / `set()` gain one
optional keyword; nothing to change on upgrade for single-connection apps.
The SurrealDB pin is unchanged (tested against 3.3.4, supported range
3.2.x – 3.3.x).

### Fixed

- **`QuerySet.cache()` returned another database's rows (#297).** The cache
  key was built from the query, its variables and the table only, so the same
  query run under `using("a")` and `using("b")` against two databases returned
  the first database's cached rows. The key now includes the effective
  connection: the name, URL, namespace and database of its
  `ConnectionConfig`, resolved like every read path (`using()` override, then
  the model's `connection` config, then `"default"`). Credentials are never
  part of the key.
- **Automatic invalidation is scoped to the database written to.**
  `save()` / `merge()` / `update()` / `delete()` drop the cached entries of
  that database for every connection name pointing at it, plus any entry for
  the table stored without a connection. Entries cached from another database
  for the same table stay cached.

### Changed

- `QueryCache.make_key()` and `QueryCache.set()` accept an optional
  keyword-only `connection` argument. Omitted, the key depends on the query
  alone, as before.
- `QueryCache.invalidate(Model)` still clears the table on every connection;
  `clear()` and `stats()` are unchanged.

### Known limitation

- `Model.transaction()` / `SurrealDBConnectionManager.transaction()` bind the
  transaction to the `using()` connection or `"default"` and ignore the
  model's `connection` config (unchanged). For a model configured with
  `connection="b"`, a `save(tx=tx)` outside any `using()` block writes to
  `default`'s database but invalidates `b`'s cached entries, so a `.cache()`
  result stored for that table under `default` can stay stale until its TTL
  expires. Call `QueryCache.invalidate(Model)` after such a transaction.

### Upgrading

- Apps with several named connections now get one cache entry per database
  for the same query. The `v2` line has the same defect; the backport is
  tracked in #315.

---

## [0.34.12] - 2026-10-27

**Fix release.** Change feeds deliver changes again (#281, #302), and record
IDs containing a backtick or a backslash round-trip (#296, #301). One small
SDK helper is added; change-feed errors now raise (see *Behaviour change*).
The SurrealDB pin is unchanged (tested against 3.3.4, supported range 3.2.x –
3.3.x).

### Fixed

- **Change feeds never delivered a change (#281).** `ChangeFeedStream` sent
  `SINCE '<text>'`, which SurrealDB rejects at parse time, and its polling
  loops swallowed the error, so `QuerySet.changes()`, `stream()` and
  `stream_batch()` waited forever. `since` is now parsed and rendered as
  `d'<UTC>'` or an unquoted versionstamp.
- **Each change-feed entry is delivered once.** The cursor is now the last
  versionstamp + 1; before, the last change came back on every poll.
- **A stream no longer stalls** when at least `batch_size` entries of another
  table follow the cursor (SurrealDB applies `LIMIT` before the table filter).
  Each poll reads the table and the whole database over the same window.
- `ChangeModelStream` yields the updates of a `CHANGEFEED ... INCLUDE ORIGINAL`
  table (`{current, update: [patch]}` shape); they used to be dropped.
- `MultiTableChangeFeed.stream()` keeps one advancing cursor per table,
  honours `since` and raises its errors. Its docs import it from
  `surreal_sdk.streaming.change_feed`.
- `QuerySet.changes()` / `ChangeModelStream.cursor` no longer skips the
  remaining events of a transaction (atomic `bulk_create`, multi-statement
  transaction, multi-record `UPDATE`) when a consumer checkpoints after each
  event and resumes later (#281).
- **Record IDs containing a backtick or a backslash now round-trip (#296).**
  Every ORM path that writes the ID into the query text — `merge()` with a
  `SurrealFunc`, `atomic_append()` / `atomic_remove()` / `atomic_set_add()`,
  `get_related()`, `remove_relation()`, `remove_all_relations()` and graph
  traversals — used to fail with a parse error, or name a different record.
  `escape_record_id()` now writes SurrealDB's `` \` `` and `\\` escape
  sequences instead of doubling the backtick.

### Added

- `since` (change feeds) accepts an `int` versionstamp, a decimal
  versionstamp string (the `cursor`), a `datetime` (naive = UTC, with a
  warning), or an ISO 8601 date or date-time string. Anything else raises
  `ValueError` before a query is sent.
- `surreal_sdk.utils.unescape_record_id()`, the inverse of
  `escape_record_id()`. `parse_record_id()` and the SDK connections' CBOR
  thing conversion both use it.

### Behaviour change

- A failed change-feed request or a rejected statement now raises
  `ChangeFeedError` from `get_changes()`, `stream()`, `stream_batch()`,
  `MultiTableChangeFeed.stream()` and `QuerySet.changes()` iteration, instead
  of being retried silently (`get_changes()` used to return `[]`).
  `batch_size < 1` raises `ValueError`.
- With `since=None`, a stream starts after the last entry already in the
  feed, found by reading the retained feed once. The change feed reports a
  create as an `UPDATE` event, never `CREATE` (server behaviour).
- `ChangeModelStream.cursor` stays on a change-feed entry's versionstamp until
  its last event has been yielded; resuming from a cursor taken
  mid-transaction delivers that transaction again in full (at-least-once).
  Make handlers idempotent per `record_id` + versionstamp.
- Only record IDs containing `` ` `` or `\` produce different SurrealQL.
  Every other ID is written exactly as before.
- `parse_record_id()` and the SDK connections' handling of `table:id` strings
  (`select()`, `create()`, `update()`, `upsert()`, `merge()`, `patch()`,
  `delete()` and `relate()` on HTTP and WebSocket) now decode `\\` and
  `` \` `` inside a backtick-delimited ID: ``t:`a\\b` `` now addresses the ID
  `a\b`, where it used to address `a\\b`. A doubled backtick is still read as
  one backtick, and any other backslash is kept as written.

### Upgrading

- Callers that relied on change-feed `stream()` never raising should now
  handle `ChangeFeedError`. The cursor format (decimal versionstamp string) is
  unchanged, and `ChangeFeedStream.cursor` at the SDK level still advances per
  entry.
- On SurrealDB 3.3 with in-memory storage, a datetime `since` selects nothing
  (server behaviour); resume from a saved `cursor` or a versionstamp
  (`since=0` reads the whole retained feed).
- The `v2` line has both defects; the backports are tracked in #308 (change
  feeds) and #310 (record IDs).

---

## [0.34.11] - 2026-10-27

**Fix release.** The `surreal-orm` CLI works on Python 3.14 and 3.15 (#280,
#298). No new public API, no behaviour change for library code. The SurrealDB
pin is unchanged (tested against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **Every CLI command that reaches the database failed on Python 3.14+ (#280,
  #298).** `makemigrations` (reading the schema), `migrate`, `upgrade`,
  `rollback`, `status`, `sqlmigrate`, `inspectdb`, `schemadiff` and `shell`
  raised `RuntimeError: There is no current event loop` before doing
  anything. The CLI ran each command's coroutine on the thread's current
  event loop, and a command starts with none. Commands now run their
  coroutine with `asyncio.run()` on a fresh event loop. `surreal-orm shell`
  keeps one event loop for the session, with its prompt outside it, so Ctrl+C
  at the prompt exits as before.
- On Python 3.12 and 3.13 the same commands no longer emit an event-loop
  `DeprecationWarning`.

### Changed

- **Tests.** The CLI tests used to replace the helper that runs a command's
  coroutine, so the defect above never ran in CI. New tests run the real
  helper from a thread with no event loop, with `DeprecationWarning` raised as
  an error, and drive `status`, `makemigrations` and `shell` with only the
  database layer mocked. Three unit tests whose outcome on Python 3.14+
  depended on an earlier test leaving an event loop set no longer depend on
  test order. 2,527 unit
  tests pass on each of Python 3.12, 3.13, 3.14 and 3.15. On SurrealDB 3.3.4
  (pin), 572 integration tests passed, 1 skipped, 1 expected failure, 0
  failed.

### Upgrading

- Nothing to do. The `v2` line has the same defect; the backport is tracked
  in #303.

---

## [0.34.10] - 2026-10-27

**Fix release.** An optional `ForeignKey` is now written and filtered as a
record link (#268, #286). No new public API, but rows written through an
optional `ForeignKey` into a column that accepted a string need a one-time
conversion: read *Behaviour change* and *Upgrading*. The SurrealDB pin is
unchanged (tested against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **An optional `ForeignKey` was never converted to a record link (#268,
  #286).** A field declared `ForeignKey("X") | None` or
  `Optional[ForeignKey("X")]` got none of the record-link coercion added in
  0.32.7. `save()`, `update()`, `merge()`, `bulk_update()`, `upsert()` and
  `bulk_upsert()` sent it as a string, so an `option<record<...>>` column
  rejected the write (`Expected none | record<...> but found '...'`) and a
  SCHEMALESS table stored the string instead of a link. `filter()` (`exact` /
  `in` / `not_in`, including inside `Q`) bound a string and silently returned
  no rows for records that exist. `get_foreign_key_targets()` read only
  `FieldInfo.metadata`, and Pydantic leaves the marker of a union member on
  `FieldInfo.annotation`. `get_foreign_key_targets()` /
  `get_foreign_key_columns()` now find the marker inside the union through the
  lookup the migrations introspector already used (0.33.0, #170); both share
  it now, so they cannot drift apart. The `DEFINE FIELD` the migrations emit
  is unchanged.

### Behaviour change

- **Optional `ForeignKey` fields are written as record links and bound as
  `RecordId` in `exact` / `in` / `not_in` filters (#268, #286)**, the same as
  required `ForeignKey` fields since 0.32.7. The model instance, a full
  `"table:id"` and a bare id are all accepted. `None`, `isnull`, `$var`
  references and string lookups are unchanged.
- In a column that accepted a string (a SCHEMALESS table, or a field typed
  `any`), rows written through an optional `ForeignKey` before this release
  hold a string — `'table:id'`, or the bare id when one was passed — and a
  filter on that field no longer matches them. A field declared
  `option<string>` now rejects the write. See *Upgrading*.

### Known limitations

- The exported helpers `is_foreign_key()`, `is_relation_field()`,
  `is_many_to_many()`, `is_graph_relation()` and `get_relation_info()` still
  look only at a top-level `Annotated`, so they report `ForeignKey("X") | None`
  as not a relation (`False` / `None`) while the ORM now treats it as a
  foreign key. This is unchanged from earlier releases.

### Changed

- **Tests.** 2,521 unit tests pass on each of Python 3.12, 3.13, 3.14 and
  3.15. On SurrealDB 3.3.4 (pin), 572 integration tests passed, 1 skipped,
  1 expected failure, 0 failed; the new optional-`ForeignKey` integration
  tests also pass on 3.2.6 (3.2 floor line). The skip is the refresh token the
  test server does not return; the expected failure is the upstream #147
  check, still failing, so `Subquery` keeps its `LET` prelude.

### Upgrading

- Convert the strings stored by earlier releases once, for each table and
  optional `ForeignKey` field:
  `UPDATE <table> SET <field> = IF !type::is_string(<field>) THEN <field> ELSE IF string::contains(<field>, ':') THEN type::record(<field>) ELSE type::record('<target_table>', <field>) END WHERE type::is_string(<field>);`
  Keep the first `IF`: on SurrealDB 3.2.6 the statement without it fails as
  soon as one row has no value or a record link in that field, even though
  the `WHERE` clause excludes those rows, and then converts nothing.
  `type::record(<field>)` alone fails on a bare id, with the same result. The
  statement was checked on SurrealDB 3.3.4 and 3.2.6: full-id and bare-id
  strings become record links, other rows are left as they are.
- A field declared `option<string>` for an optional `ForeignKey`: retype it
  `option<record<target_table>>`, then run the conversion above.
- No DDL change: the `DEFINE FIELD` the migrations emit is identical.
- The `v2` line has the same defect; the backport is tracked in #294.

---

## [0.34.9] - 2026-10-27

**Maintenance release.** Python 3.15 is supported (#235, #288). No library code
changes vs 0.34.8 and no new public API. `requires-python` (`>=3.12`) and every
dependency floor are unchanged. The SurrealDB pin is unchanged (tested against
3.3.4, supported range 3.2.x – 3.3.x).

### Added

- **Python 3.15 support (#235, #288).** CI runs the unit suite on Python 3.12,
  3.13, 3.14 and 3.15; lint and integration tests stay on 3.13. Both the
  package and the SDK carry the `Programming Language :: Python :: 3.15`
  classifier. A new unit test, `tests/test_python_support.py`, keeps the
  tested range in step: the classifiers of both `pyproject.toml` files, the
  `requires-python` floor, the ruff and mypy targets and the
  `make test-all-python` helper must match the CI matrix, and every locked
  package must install from a wheel on every tested Python (PyYAML, a dev
  dependency with a pure-Python fallback, excepted).

### Changed

- **Lock file only, for development and CI (#288).** A targeted refresh so
  that every dependency installs from a wheel on Python 3.15: pydantic
  2.13.4 → 2.14.1 (pydantic-core 2.46.4 → 2.50.1), cbor2 6.1.2 → 6.1.5,
  mypy 2.1.0 → 2.4.0 (librt 0.11.0 → 0.16.0, ast-serialize 0.5.0 → 0.13.1),
  typing-extensions 4.15.0 → 4.16.0 and typing-inspection 0.4.2 → 0.4.4. No
  other package changes. The lock does not ship in the package, so installs
  of `surrealdb-orm` resolve exactly as before.
- **Tests.** The suites of this release pass on SurrealDB 3.3.4 (pin) and
  3.2.6 (3.2 floor line): 2,454 unit tests on each of Python 3.12, 3.13, 3.14
  and 3.15, and 553 integration tests passed, 1 skipped, 1 expected failure,
  0 failed, on each server. The skip is the refresh token the test server
  does not return; the expected failure is the upstream #147 check, still
  failing, so `Subquery` keeps its `LET` prelude.

### Upgrading

- On Python 3.15, use pydantic >= 2.14.0 (pydantic-core 2.50.0) and
  cbor2 >= 6.1.5, the first stable releases with cp315 wheels. Older versions
  that the floors still allow are built from source there, and both
  pydantic-core and cbor2 need a Rust toolchain for that.
- The `v2` line is not concerned: it is an LTS line and takes no new platform
  support.

---

## [0.34.8] - 2026-10-27

**Fix release.** `QuerySet.live()` keeps receiving events after a WebSocket
reconnect, and a naive `datetime` can be sent over CBOR (#255, #276). No new
public API, but naive datetimes change behaviour: read *Behaviour change*. The
0.34.5 entry below is corrected (see *Documentation*). The SurrealDB pin is
unchanged (tested against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **`QuerySet.live()` stopped receiving events after its first WebSocket
  reconnect (#255, #276).** This covered every `LiveSelectStream` with
  `auto_resubscribe=True`, the default. The live query was re-created on the
  server with the right filter, but the stream's handler was not registered
  again and the stream kept the old id. The stream now moves to the re-created
  live query during the resubscribe itself, `live_id` is the new id, and a
  user `on_reconnect` still fires with `(old_id, new_id)`. Subscriptions
  created with their own `LiveSubscriptionParams.callback` were not affected.
- **`stop()` after a reconnect left the re-created live query running (#255,
  #276).** It killed the old, already dead live query id, so the re-created
  query kept running and was re-created again on every later reconnect, even
  after the stream had been stopped. `stop()` now ends the current query and
  removes it from the connection's bookkeeping. This also holds when `stop()`
  begins while the re-created `LIVE SELECT` is still in flight, for example an
  `async with ...live()` block that ends during a server restart: the new
  query is killed instead of being left behind.
- **`datetime` subclasses were sent in a form SurrealDB rejects (#255, #276)**
  (`pandas.Timestamp`, `pendulum.DateTime`, …), aware or not: an HTTP 400 parse
  error, and over WebSocket the request timed out. They are now sent like a
  `datetime`, without digits below the microsecond.

### Behaviour change

- **A naive `datetime` sent over CBOR, the default protocol, is now read as
  UTC (#255, #276).** It used to be rejected before the request left, with
  `CBOREncodeError` over HTTP and `ConnectionError` over WebSocket. This
  covered query variables, model fields on `save()` / `merge()` / `update()`,
  and `filter()` and `live()` values. It is now stored as that wall-clock time
  in UTC, as `raw_query(inline_dicts=True)` already did, and a `RuntimeWarning`
  says so ("A naive datetime was sent to SurrealDB and read as UTC. …"). The
  message names no value, so the default warning filter shows it once per
  process for each path; `raw_query(inline_dicts=True)` now emits the same
  warning. Aware datetimes encode byte for byte as before. On a host whose
  clock is not on UTC, `datetime.now()` is therefore stored as local wall-clock
  time read as UTC: use `datetime.now(UTC)`.
- After a reconnect, `stream.live_id` / `LiveModelStream.live_id` report the
  re-created live query's id instead of the stale one. `LiveSubscriptionParams`
  keeps its constructor, repr and equality.

### Known limitations

- `LiveSelectManager` still keys each stream by the id `watch()` returned.
  After a reconnect, `active_streams` lists that original id and
  `manager.stop()` must be given it: `stop()` with the new id (the one
  `on_reconnect` receives, and the stream's `live_id`) does nothing, and the
  stream and its re-created live query keep running. `stop_all()` is not
  affected. Before this release the stream was silent after a reconnect, so
  the mismatch had no visible effect.
- Pre-existing, unchanged in this release: the callback API
  (`WebSocketConnection.live(table, callback)` and `LiveQuery.subscribe()`)
  only registers a handler, so after a reconnect its live query is not
  re-created and the callback receives nothing more. A subscription whose
  `LIVE SELECT` fails during the resubscribe (the socket drops again, a
  timeout, a server error) is dropped for good: the next reconnect does not
  retry it, and its stream neither ends nor raises.

### Documentation

- **The 0.34.5 entry is corrected (#255).** The #204 fix concerned more keys
  than the entry said: any key that is a prefix of `tx_<i>_<an earlier key>`,
  not only `t`, `tx` or `tx_<i>`, and `relate(tx=)` is reached through its
  edge-data keywords, not through model fields. The `certifi` note now says
  which connections use that bundle: the `https://` connections of the HTTP
  transport, the ORM's default; the WebSocket transport does not use `certifi`.
  The same corrections are made in the README and `CLAUDE.md`.

### Changed

- **Tests.** The suites of this release pass on SurrealDB 3.3.4 (pin) and
  3.2.6 (3.2 floor line): 2,436 unit tests on Python 3.12, 3.13 and 3.14, and
  553 integration tests passed, 1 skipped, 1 expected failure, 0 failed, on
  each server. The skip is the refresh token the test server does not return;
  the expected failure is the upstream #147 check, still failing, so
  `Subquery` keeps its `LET` prelude.

### Upgrading

- Attach a `tzinfo` to every `datetime` you send (`datetime.now(UTC)`). Values
  meant as local time must be converted to aware datetimes first, or they are
  stored as that wall-clock time in UTC.
- To keep failing on naive values, turn the warning into an error:
  `warnings.filterwarnings("error", message="A naive datetime was sent to SurrealDB", category=RuntimeWarning)`.
  The request then fails before it is sent.
- The `v2` line has the live-stream defects; the backport is tracked in #277.
  The naive-`datetime` change is tracked for `v2` in #258.

---

## [0.34.7] - 2026-10-27

**Fix release.** `SurrealDBConnectionManager.reconnect()` no longer breaks
requests and transactions that are still running, and a failed close no longer
leaves a dead client registered (#232, #260). No new public API, but
`reconnect()` changes behaviour: read *Behaviour change*. The SurrealDB pin is
unchanged (tested against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **`reconnect()` broke requests already running (#232, #260).** It closed the
  shared HTTP client before the new one existed, so any request that
  overlapped the call failed with `ConnectionError: Request failed`. It now
  connects and signs in the new client first and swaps it in under the
  connection lock, so concurrent `get_client()` callers always get a connected
  client. The previous client is closed once its in-flight RPC requests (every
  request the ORM sends) and its open HTTP transactions have finished, or
  after 30 s at most, for a call that is not cancelled (see *Behaviour
  change*).
- **An HTTP transaction opened before `reconnect()` lost its writes (#232,
  #260).** `HTTPTransaction` buffers its statements until commit, and the
  commit ran on the closed client: it failed with `Not connected`, and none of
  the writes reached the database. A transaction opened by another task, or
  inside its own body, now commits on the client it was opened on.
- **`close_connection()` / `remove_connection()` kept a dead client
  registered when closing it failed (#232, #260)**, for example with
  `RuntimeError: Event loop is closed`. The error escaped before the registry
  was cleared, so `get_client()` kept handing back the dead client. The failure
  is now logged as a warning with its traceback, and the entry is always
  removed.
- **SDK: `HTTPConnection.close()` left a client whose close raised reporting
  itself as connected (#232, #260).** It now marks the connection as
  disconnected before closing the underlying pool; the exception still
  propagates.

### Behaviour change

- For a call that runs to completion, `reconnect()` returns only after the
  previous client is closed. That can take up to 30 s while other tasks finish
  RPC requests or transactions on it; whatever still uses it after that is cut
  off by the close. Transactions opened by the task calling `reconnect()` are
  not waited for: the previous client closes when the last of them commits or
  rolls back. The default WebSocket client is still closed at once, as before.
- If the new client cannot connect or authenticate, `reconnect()` raises
  `SurrealDbConnectionError` and leaves the current clients in place. The
  previous client used to be closed and removed already.
  `validate_connection()` and `set_url()` / `set_user()` / `set_password()` /
  `set_namespace()` / `set_database()` with `reconnect=True` go through
  `reconnect()`.
- Cancelling `reconnect()` (for example `asyncio.wait_for(reconnect(), 5)`
  timing out, or a task running `validate_connection()` being cancelled) does
  not cut off what is still running on the previous client. Cancelled before
  the new client is swapped in, the current clients stay in place. Cancelled
  after the swap, the new client stays installed and the previous one is
  closed by the last RPC request or HTTP transaction to finish on it (at once
  if none is left), with no time limit. The cancellation then propagates as
  usual.

### Known limitations

- `sql()`, `health()`, `status()` and `rest_*()` called directly on a client
  from `get_client()` do not go through the RPC path, so the drain does not
  wait for them: such a call that spans `reconnect()` can still be cut off.
  The ORM never calls them on the shared client.
- After a cancelled `reconnect()`, the previous client has no time limit: an
  HTTP transaction on it that is never committed or rolled back keeps it, and
  its connection pool, open.
- `reconnect()` awaited from a child task of a task holding an open HTTP
  transaction (`asyncio.gather()`, a `TaskGroup`) waits for that transaction
  until the 30 s bound, then closes the client, and the commit fails with
  `Not connected`. It used to fail at once.
- Pre-existing: a client still being built when `reconnect()` is cancelled
  (for example during sign-in) is not closed; and `QuerySet.changes()` keeps
  its client across polls, so after any `reconnect()` the stream silently
  stops delivering changes. Both were already the case in 0.34.6.

### Changed

- **Tests.** The suites of this release pass on SurrealDB 3.3.4 (pin) and
  3.2.6 (3.2 floor line): 2,401 unit tests on Python 3.12, 3.13 and 3.14, and
  540 integration tests passed, 1 skipped, 1 expected failure, 0 failed, on
  each server. The skip is the refresh token the test server does not
  return; the expected failure is the upstream #147 check, still failing, so
  `Subquery` keeps its `LET` prelude.

### Upgrading

- No change required. Calling `reconnect()` periodically only to renew an
  expired token is unnecessary: SurrealDB answers an expired token with HTTP
  401, and the HTTP client already signs in again with its stored credentials
  and retries (checked on SurrealDB 3.3.4).
- Do not keep a client from `get_client()` across an `await`: one kept past a
  `reconnect()` is closed (`Not connected`), so call `get_client()` again.
- Workarounds that swapped the manager's private client registry or
  connection lock by hand, or delayed closing the old client, can be replaced
  by `reconnect()`.
- The `v2` line has the same defects; the backport is tracked in #266.

---

## [0.34.6] - 2026-10-27

**Fix release.** Migrations no longer reset field permissions set outside the
ORM (#195, #248), and the atomic array helpers no longer lose writes silently
(#233, #257). No new public API, but both fixes change behaviour: read
*Behaviour change* and *Upgrading*. The SurrealDB pin is unchanged (tested
against 3.3.4, supported range 3.2.x – 3.3.x).

### Fixed

- **`AlterField` and `Model.define_table()` reset field permissions set
  outside the ORM (#195, #248).** Both redefined the field with
  `DEFINE FIELD OVERWRITE` (`AlterField` in forwards and in rollback), which
  replaces the whole definition while the ORM re-emits only the clauses it
  models. A field-level `PERMISSIONS` clause set by hand, by another tool or by
  a `RawSQL` migration fell back to the server default, `PERMISSIONS FULL`, on
  the next alteration, its rollback, or the next `define_table()` call. Fields
  are now altered in place with `ALTER FIELD`, which leaves every clause the
  ORM does not model as the database has it. Hand-written migrations are
  covered as well as generated ones.
- **`parse_define_field()` read a field's `PERMISSIONS` clause as other
  clauses (#195, #248).** SurrealDB reports a permission's `WHERE` condition
  with field names unescaped, so `FOR select WHERE type = 'public'` was read as
  a `TYPE` clause, and `comment`, `default`, `readonly`, `reference` or
  `flexible` in a condition set those clauses the same way. An `ASSERT` with a
  comparison (`$value > 0`) on a field with a permission also swallowed the
  permissions clause. `schema_diff()` and `makemigrations` then emitted a
  spurious `AlterField` for that field, and its rollback did not parse.
  Parsing now stops at the `PERMISSIONS` clause, which SurrealDB always reports
  last, and the diff converges.
- **`atomic_set_add()` appended duplicates on an array field (#233, #257).**
  It ran `SET field += $value`, and `+=` on an array appends, so two calls with
  the same value left two copies. It now uses `array::add()`, which adds only
  the values the field does not already hold. A `set<T>` field (what
  migrations generate for a Python `set[T]`) keeps its set type, and a record
  without the field gets `[value]`.
- **`atomic_append()`, `atomic_remove()` and `atomic_set_add()` returned `[]`
  when SurrealDB rejected the statement (#233, #257)**: a write conflict, a
  failed `ASSERT`, an argument of the wrong type. The write was lost silently,
  indistinguishable from an update that matched no record, and
  `@retry_on_conflict` never saw the conflict it exists to retry. They now
  raise (see *Behaviour change*).
- **`atomic_append()` on a record without the field (#233, #257)** was
  rejected by the server (`array::append()` rejects `NONE`), and the rejection
  was swallowed. It now creates `[value]`.

### Behaviour change

Migrations (#248):

- `AlterField` emits `ALTER FIELD <name> ON <table> …;` instead of
  `DEFINE FIELD OVERWRITE`, and states every modelled clause (`TYPE`,
  `FLEXIBLE`, `VALUE`, `DEFAULT`, `ASSERT`, `READONLY`, `REFERENCE`/`ON DELETE`,
  `COMMENT`): set when present, dropped (`DROP …`) when absent. Removing a
  clause from a model still removes it, and a rollback still restores the
  previous clauses. Operations render at run time, so migration files already
  on disk emit the new statements when applied or rolled back.
- For a typed array or set column, `AlterField` then emits
  `ALTER FIELD IF EXISTS <name>.* ON <table> TYPE <element> …;` for each
  element level the server derives, outermost first, so the element sub-fields
  SurrealDB keeps (`tags.*`, `n.*.*`) still follow the column's type and its
  `FLEXIBLE`, as the redefinition did. A level whose type holds an object gets
  `FLEXIBLE` when the column is `FLEXIBLE`. A level whose type holds no object
  always gets `DROP FLEXIBLE`: the server can leave `FLEXIBLE` on such a level
  and then refuses any `TYPE` that keeps it. No other clause reaches an element
  level. Two differences from 0.34.5, both harmless: a level the server never
  derived is not created (`IF EXISTS`; the column's own `TYPE` already checks
  its elements), and a level whose type holds no object loses the `FLEXIBLE`
  the redefinition left on it, where it had no effect.
- `AlterField` on a field that does not exist now fails the migration with
  `MigrationStatementError` (`The field 'x' does not exist`) instead of
  silently creating it. Use `AddField` to create a field.
- `Model.define_table()` emits, for each field, `DEFINE FIELD IF NOT EXISTS …;`
  followed by `ALTER FIELD …;` (plus the element-level statements), instead of
  `DEFINE FIELD OVERWRITE`. It still works on an empty database and when called
  twice. `AddField(overwrite=True)` remains an explicit opt-in.

Atomic array helpers (#257):

- `atomic_append()`, `atomic_remove()` and `atomic_set_add()` raise when
  SurrealDB rejects the statement: `TransactionConflictError` for a write
  conflict, which `@retry_on_conflict` retries, and `QueryError` otherwise.
  Both are `SurrealDBError` subclasses from `surreal_sdk` and carry the
  server's message. An `UPDATE` that matches no record still returns `[]`.
- `atomic_set_add()` with a list value adds only the elements the field does
  not already hold. On an array field it used to append all of them: `['a']`
  plus `['a', 'b']` gave `['a', 'a', 'b']`, and now gives `['a', 'b']`.
- `atomic_set_add()` on a field holding a string or a number now raises
  `QueryError`; `+=` used to concatenate or add (`'str'` plus `'p1'` gave
  `'strp1'`, `10` plus `5` gave `15`). Using it on a non-array field was
  outside the documented contract.

### Known limitations

- Unchanged from earlier releases: widening a non-optional column from
  `array<T>` to `array<any>`, `array` or `any` leaves the derived `tags.*` at
  `T`, and removing `FLEXIBLE` from an array column (rolling back an
  `AlterField` that turned it on included) leaves its object element levels
  `FLEXIBLE`, as `DEFINE FIELD OVERWRITE` did. `docs/migrations.md` documents
  a `RawSQL` workaround for each (`ALTER FIELD tags.* ON <table> TYPE any;`,
  `ALTER FIELD items.* ON <table> DROP FLEXIBLE;`).
- The choice between `TransactionConflictError` and `QueryError` matches the
  server's message against substrings that include `conflict`. A rejection
  whose message contains that word, such as a failed `ASSERT` echoing a value
  like `'conflict-resolved'` or naming a field like `conflicts`, raises
  `TransactionConflictError`, and `@retry_on_conflict` retries it before
  raising. The matcher is the existing one in `surreal_sdk`, not new in this
  release.

### Changed

- **Tests.** The suites of this release pass on SurrealDB 3.3.4 (pin) and
  3.2.6 (3.2 floor line): 2,377 unit tests on Python 3.12, 3.13 and 3.14, and
  537 integration tests passed, 1 skipped, 1 expected failure, 0 failed, on
  each server. The skip is the refresh token the test server does not return;
  the expected failure is the upstream #147 check, still failing, so
  `Subquery` keeps its `LET` prelude.

### Upgrading

- **Migrations.** Any pending `AlterField` for a field that does not exist yet
  now fails, generated ones included. `makemigrations` diffs against the live
  database, so a database whose fields came from `define_table()` or
  hand-written DDL yields `AlterField` operations for fields no earlier
  migration creates. Replaying that chain onto a fresh database (CI, a new
  deployment) stops at the first of them, where `DEFINE FIELD OVERWRITE` used
  to create the field silently. Give the chain an `AddField` that creates the
  field before it is altered, preceded by a `CreateTable` if the table should
  be defined explicitly (for example `SCHEMAFULL`); a `CreateTable` alone
  defines no field. Migrations are not transactional, so a failure partway
  leaves the earlier statements applied. Field permissions already reset by an
  earlier `AlterField` or `define_table()` are not restored: re-apply them by
  hand.
- **Atomic array helpers.** Code that treated `[]` from these helpers as "the
  write did not happen" should catch `QueryError` / `TransactionConflictError`
  (or `SurrealDBError`) instead, or wrap the call in `@retry_on_conflict`.
  Duplicates already written by `atomic_set_add()` are not removed. On
  SurrealDB 3.2, `atomic_set_add()` on a `set<T>` field that is still unset
  raises `QueryError` (it already failed with `+=`, silently): 3.2 does not
  coerce an array into a set, so give such a field an initial value.
- The `v2` line has both defects; the backports are tracked in #259 and #261.

---

## [0.34.5] - 2026-10-27

**Fix and SurrealDB 3.3.4 release.** Three SDK fixes for values that never
reached the server intact (#205), one for parameters an HTTP transaction
renamed out of reach (#204), a `certifi` update in the lock file (#240), and
the tested server moves from 3.3.0 to 3.3.4 (#234); the supported range stays
3.2.x – 3.3.x. No public API change. 0.34.3 and 0.34.4, which shipped without
entries, are backfilled below.

### Fixed

- **Some `QuerySet.live()` / `LiveSelectStream` filters never matched (#205,
  #239)**, for example on a dict (`filter(prefs={...})`) or a `Decimal`. The
  values were written into the `LIVE SELECT` text, a dict as a Python repr and
  a `Decimal` as a quoted string, so the comparison was always false. Every
  filter value is now bound as a query parameter, as `exec()` already does,
  both when the subscription starts and when auto-resubscribe re-creates it
  after a reconnect: SurrealDB 3.x evaluates bound parameters in a
  `LIVE SELECT`. The private helpers `LiveSelectStream._format_value` and
  `_inline_params_static` are removed.
- **Binding a `Decimal` made SurrealDB 3.x reject the whole request (#239)**,
  in any query (`query()`, `exec()` filters). cbor2 encodes a `Decimal` as CBOR
  tag 4, which SurrealDB does not read: over HTTP the request failed with a
  400, and over WebSocket the call waited for its full timeout. A finite
  `Decimal` is now sent as SurrealDB's decimal (tag 10) with every digit kept;
  NaN and infinities keep cbor2's float encoding.
- **Binding a `datetime.date` made the whole request fail (#239)** (cbor2 tag
  1004). `save()`, `exec()` and `query()` now work with a `date` value. It is
  sent as the datetime at midnight UTC, which is what `d'YYYY-MM-DD'` means in
  SurrealDB, and a field typed `date` reads it back as the same date.
- **`HTTPTransaction.commit()` renamed parameters a statement did not bind
  (#204, #243).** Over an HTTP connection, `commit()` sends every queued
  statement as one query and namespaces each statement's keys (`$k` →
  `$tx_<i>_k`). It did so with a plain text replace, one key at a time, so a
  key also rewrote every longer `$name` it was a prefix of. The renamed name is
  unbound and evaluates to `NONE`: a `WHERE` on it silently matched nothing and
  a `SET` from it silently wrote nothing. Two ways in:
  - a key that prefixes a built-in (`$auth`, `$session`, `$this`, `$before`,
    `$after`, `$value`, `$input`) or another unbound parameter such as a
    `DEFINE PARAM` value: with key `a`, `$auth.id` became `$tx_0_auth.id`.
    Only names the caller chooses can do this: `tx.query()` variables,
    `extra_vars` used with a `SurrealFunc`/`server_values` expression on
    `save(tx=)`/`merge(tx=)`, and `QuerySet.variables()` in the atomic bulk
    operations;
  - a key that is a prefix of `tx_<i>_<an earlier key>` (`i` being the
    statement's position in the transaction): `t`, `tx`, `tx_`, `tx_<i>`,
    `tx_<i>_`, …, and `tx_1` also hits positions 10–19, 100–199, …. It
    rewrote the references already renamed for the keys bound before it
    (`$tx_0_amount` → `$tx_0_tx_0_amount`). The SDK builders `tx.create`,
    `tx.upsert` and `tx.relate` bind each field under its own name, so this
    was reached through a model field with such a name on `save(tx=)` of a
    new record and `bulk_create(atomic=True)`, and through an edge-data
    keyword such as `t` or `tx_<i>` on `relate(tx=)`, which binds its
    `**edge_data` keywords, not model fields (`tx` cannot be one of them).
    Each lost the fields bound before it.

  Only whole `$key` references to a key the statement binds are renamed now,
  in a single pass through `surreal_sdk.utils.substitute_params()` (0.33.3).
  The names the ORM generates for its own bindings (`_sv_*`, `_f*`, `_bu*`,
  `_oc*`) could not trigger either case, `delete(tx=)` binds nothing, and
  WebSocket transactions do not rename. A bound `$k` written inside a quoted
  string literal is still renamed, as before. The `v2` line has the same
  defect; its backport is tracked in #247.

### Security

- **`certifi` 2026.5.20 → 2026.10.23 in `uv.lock` (#240, #242)** —
  GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320. The 2026.5.20 bundle still trusts
  `emSign ECC Root CA - G3`, under which eight intermediate CA keys were
  compromised (and revoked), so a TLS connection verified against it accepts
  certificates issued with those keys. 2026.10.23 no longer carries that root.
  `certifi` is a transitive dependency (through `httpx`), so the lock file
  only moves the repository's development and CI environments: see
  *Upgrading*. The `v2` line received the same update in 0.21.12.

### Changed

- **SurrealDB pin 3.3.0 → 3.3.4** (`.surrealdb-version`, `devops/docker-compose.yml`,
  #238). The integration suite of this release passes against 3.3.4 (489
  passed, 1 skipped, 1 expected failure, 0 failed), and against 3.2.6 on the
  3.2 floor line (same counts). The skip is the refresh token the test server
  does not return; the expected failure is the upstream #147 check, still
  failing on 3.3.4, so `Subquery` keeps its `LET` prelude.
- **Docs.** README, `SECURITY.md` and `CLAUDE.md` state "tested against 3.3.4".

### Behaviour change

- The SurrealQL a live query sends now contains only `$name` references, for
  example `LIVE SELECT * FROM t WHERE author = $_f0`, with the values in the
  query variables. Code that asserts on the emitted `LIVE SELECT` text sees
  `$_fN` references instead of literals.
- A `Decimal` is encoded as CBOR tag 10 instead of tag 4. SurrealDB rejected
  tag 4, so no request that used to succeed changes.
- A `date` is stored as the datetime at midnight UTC.

### Known limitation

`tx.merge()` over HTTP is still broken, for a reason this release does not
touch (#251): it emits `UPDATE <record> MERGE { k = $k, ... }`, which
SurrealDB 3.x rejects per statement. `commit()` returns without an error while
the server cancels the whole transaction, its other statements included. This
covers `update(tx=)`, `merge(tx=)` without a `SurrealFunc`, and `save(tx=)` of
an already-saved record whose data holds no `SurrealFunc` and no nested
object/array, on an HTTP connection (the ORM's default). WebSocket
transactions are not affected.

### Fixed — CI

- **The SurrealDB 3.x monitor now picks the newest stable 3.x release by
  version (#234, #238).** It took the first matching release in API order, and the
  releases API lists by creation date, not by version. The v3.2.6 backport was
  cut from a newer commit than v3.3.4 (2026-10-18 vs 2026-10-13), so the API
  listed it first, the monitor picked 3.2.6, the no-downgrade guard (#155)
  refused it, and every daily run ended green while the pin stayed at 3.3.0.
  Selection now reads every page of the API, keeps plain `X.Y.Z` tags of the
  monitor's own major, and takes the highest with `sort -V`. The 2.x monitor
  and the pre-release canary had the same selection and are fixed too.
- **Security detection reads the chosen release by tag**
  (`releases/tags/vX.Y.Z`) for its notes and title, instead of scanning the
  first page of the list. A security release outside that page used to read as
  having no notes. A version with no release entry (an image-only tag, #155)
  still reads as having no notes and does not abort the run.

### Behaviour change (CI only)

- A GitHub API failure now fails the check job of both monitors and of the
  pre-release canary, instead of ending green as "nothing to do".
- The 3.x monitor no longer falls back to `releases/latest`, which is the newest
  release of any line and could have proposed a new major such as 4.0.0.
- A manually dispatched version must be a whole `X.Y.Z` of the monitor's own
  line (`3.3` or `3.3.x` is refused). Dispatch inputs reach the monitor scripts
  through `env:` and are validated there.

### Upgrading

- No migration. Upgrading `surrealdb-orm` does not upgrade `certifi`: this
  package does not declare it, so `pip install -U surrealdb-orm` and existing
  lockfiles keep the installed bundle. Run `pip install -U certifi` (or bump it
  in your lockfile) to `>= 2026.10.19`. The bundle is used by `https://`
  connections made through the HTTP transport, the ORM's default: httpx 0.28
  trusts `certifi.where()` unless `SSL_CERT_FILE` or `SSL_CERT_DIR` is set.
  The WebSocket transport (aiohttp) does not use `certifi`.

---

## [0.34.4] - 2026-10-20

**Dependency maintenance release.** No library code changes vs 0.34.3.
Auto-released by the Dependabot flow; entry backfilled in 0.34.5.

### Security

- **`multidict` 6.7.1 → 6.9.1 in `uv.lock` (#227)** — GHSA-54p9-h82j-f925
  (CVE-2026-104874), a reference leak in the items-view union and subtraction.
  `multidict` is a transitive dependency of `aiohttp`; it is not declared by
  this package, so an existing environment keeps its installed version until
  it is upgraded.

---

## [0.34.3] - 2026-10-20

**Dependency maintenance release.** No library code changes vs 0.34.2.
Auto-released by the Dependabot flow; entry backfilled in 0.34.5.

### Security

- **`aiohttp` 3.14.3 → 3.14.4 in `uv.lock` (#223)**, with `yarl` 1.24.2 →
  1.25.1 — GHSA-2g87-pp6c-29x5 (excessive or unbounded memory and CPU use),
  GHSA-494j-54xm-rwp3 (CVE-2026-111063, `Secure` cookie attribute not enforced
  for a shared cookie) and GHSA-5wr8-gcqf-gqf3 (CVE-2026-111064, no initial
  request/header timeout in aiohttp's server). `aiohttp` is a runtime
  dependency (the SDK's WebSocket client; the SDK does not use aiohttp's
  server). The declared floor stays `aiohttp>=3.12`, so upgrade it in your
  environment: `pip install -U aiohttp`.

---

## [0.34.2] - 2026-10-01

**CI maintenance release.** No library code changes vs 0.34.1.

### Fixed — CI

- **Dependabot security updates now reach the `v2` LTS branch, grouped ones
  included (#220).** A grouped Dependabot PR reports its dependencies as one
  comma-separated string; *Sync to V2* looked `tornado, urllib3` (#217) up as a
  single package, found nothing and finished green. `v2` stayed on six
  advisories until a manual audit (fixed in 0.21.9). The sync now:
  - runs for `uv` updates only. On `main` those are Dependabot *security*
    updates (no `uv` version-update config exists; a test keeps it so), while
    routine updates reach `v2` through its own `target-branch: v2` entry.
    GitHub Actions bumps are skipped with a notice instead of failing;
  - splits the group, syncs the packages `v2` uses in one PR, and fails on
    input it cannot read as package names;
  - warns when `v2`'s constraints hold a package below `main` (e.g.
    `cbor2 <6`) instead of reporting "already up to date";
  - deduplicates on an open PR rather than on the branch, bounds the PR title
    and branch name, and reads every lock entry of a package;
  - passes the package list in a structured marker in the PR body; the title
    is no longer parsed. The old greedy title parse had mislabelled every
    single-package sync (#210: `anyio from 4.13.0 to 4.14.2 (security sync`).

---

## [0.34.1] - 2026-10-01

**Dependency maintenance release.** No library code changes vs 0.34.0.

### Security

- **`tornado` 6.5.8 → 6.5.9 and `urllib3` 2.7.0 → 2.8.0 in `uv.lock` (#217)** —
  GHSA-chx6-46f5-w4vp, GHSA-c2m8-h5v5-343r, GHSA-3hv7-mjh2-fv65;
  PYSEC-2026-4175, PYSEC-2026-4176, PYSEC-2026-4177. Both are
  development-only (Jupyter tooling; the Docker client used by the test
  fixtures) and do not ship in the wheel, so installed users are not exposed.

---

## [0.34.0] - 2026-10-01

**SurrealDB 3.3 compatibility release.** SurrealDB **3.3.0** is the tested
target of `main`. No library code changes vs 0.33.5; the minimum stays 3.2.

### Changed

- **Supported range is SurrealDB 3.2.x – 3.3.x, tested against 3.3.0.** The
  full suite passes unchanged on both ends of the range — 2,128 unit tests, and
  469 integration tests each against a live 3.3.0 and a live 3.2.4 (0 failures;
  the same 2 expected skips on both). The floor is not raised: nothing in the
  ORM needs 3.3, so taking 0.34.0 does not force a server upgrade.
- **Upgrading the server to 3.3 is still recommended.** 3.3.0 carries several
  upstream security fixes (computed-field and field-permission bypasses, tenant
  boundary enforcement on `USE`, record-auth scope checks) that 3.2.4 does not.
- **`Subquery` keeps its `LET` prelude.** The upstream behaviour behind #147 —
  an inline uncorrelated sub-SELECT with `ORDER BY` + `LIMIT` sharing its
  `LIMIT` budget across outer rows — still reproduces on 3.3.0
  (`test_inline_subquery_order_by_and_limit_is_row_stable_upstream` remains an
  expected failure), so the 0.32.0 workaround stays.
- **Docs.** README, `SECURITY.md`, `CLAUDE.md` and `docs/roadmap.md` state the
  3.2.x – 3.3.x range; the roadmap's planned versions move up one slot (Graph
  Power is now 0.35.0). 0.33.4 and 0.33.5, which shipped without entries, are
  backfilled below.
- **Version bump to 0.34.0** — `pyproject.toml`, `surreal_orm/__init__.py`,
  `surreal_sdk/__init__.py`, `surreal_sdk/pyproject.toml`.

---

## [0.33.5] - 2026-09-28

**SurrealDB pin release.** No library code changes vs 0.33.4.

### Changed

- **SurrealDB pin 3.2.4 → 3.3.0 (#211).** `.surrealdb-version` and every image
  in `devops/docker-compose.yml`. Opened and auto-merged by the 3.X version
  monitor after its own integration run passed against 3.3.0, which in turn
  triggered this release with no documentation. The documented support
  statement for 3.3 lands in 0.34.0.

---

## [0.33.4] - 2026-09-18

**Dependency maintenance release.** No library code changes vs 0.33.3.

### Changed

- **`anyio` 4.13.0 → 4.14.2 in `uv.lock` (#207).** `anyio` is a transitive
  dependency of `httpx`; the lock file does not ship in the wheel, so installed
  environments resolve it independently.

---

## [0.33.3] - 2026-09-08

**Bug fix release.** One defect, at the boundary where a value is written into a
query string instead of bound — and it had two call sites, one of which crashed
on any accented text.

### Fixed — Query-string substitution

- **Inlined JSON was parsed as a regex replacement pattern (#193).**
  `inline_dict_variables()` handed the serialised JSON to `re.sub` as a
  replacement *string*, which `re.sub` reads as a mini-pattern: `\1` is a
  backreference, `\g<0>` a group reference, and an unknown escape an error.
  `json.dumps` emits `\uXXXX` for every non-ASCII character, so
  `raw_query(inline_dicts=True)` raised on any accented value:

  ```python
  inline_dict_variables("UPDATE t SET a = $v;", {"v": {"meta": {"k": 1}, "p": "café"}})
  # re.error: bad escape \u at position 28
  ```

  The backslash half failed silently instead, which is worse: every literal
  backslash was doubled by `json.dumps` and then collapsed back by the
  replacement parsing, so a Windows path reached the database altered. Values
  are now inserted verbatim through a callable replacement.

- **Live-query filters went through the same defective path (#193).**
  `LiveSelectStream._inline_params_static` also passed a rendered value to
  `re.sub` as a replacement. `_format_value` doubles backslashes on purpose and
  the parsing undid it, so SurrealQL read `\t` as a tab and a
  `QuerySet.live()` filter containing a backslash silently matched nothing.

- **A later variable could rewrite text inside an earlier one (#193).**
  Substituting key by key re-scanned the JSON just inserted, so a `$b` occurring
  inside a *string value* of `$a` was replaced too — and which one won depended
  on dict insertion order. Substitution is a single pass over the original query
  now.

- **Astral characters were rejected by the server (#193).** `json.dumps` defaults
  to `ensure_ascii=True`, emitting a UTF-16 surrogate pair, which SurrealDB 3.x
  refuses at parse time (`String contains invalid escape sequence`). BMP text
  such as `café` did decode, so this was narrower than the crash above but
  invisible to a `json.loads` round-trip assertion — surrogate pairs are valid
  JSON. `ensure_ascii=False` sends the character raw.

- **An unreferenced complex variable was silently dropped (#193).** A value whose
  `$name` never appeared in the query was serialised, matched nothing, and then
  vanished from both the query and the returned bindings. It stays a binding now,
  and is not serialised at all. This also covers a key that is not an ASCII
  identifier, such as `$my-var`.

- **A lone surrogate raised the wrong error (#193).** `ensure_ascii=False` lets
  it past `json.dumps`, after which it surfaced as a `UnicodeEncodeError` from
  inside the CBOR encoder rather than the `ValueError` the function documents.

### Performance

- **Datetime markers expand in one pass (#193).** Each `d"..."` literal was a
  full `str.replace` over the entire JSON payload. 5,000 datetimes in an 0.8 MB
  payload took 2.80 s; the same work now takes 0.023 s.

### Added — SDK

- **`surreal_sdk.substitute_params()`** and **`surreal_sdk.find_param_references()`**
  — the shared primitive both call sites use. It lives in the SDK because the
  dependency only runs one way: `surreal_orm` imports from `surreal_sdk`, never
  the reverse.

### Known follow-ups

- **#204** — `HTTPTransaction.commit()` namespaces parameters with a boundary-less
  `str.replace`, so `$auth.id` becomes `$tx_0_auth.id` and the `WHERE` compares
  against `NONE`. Affects every `save(tx=)` / `merge(tx=)` over HTTP.
- **#205** — on `main`, `LIVE SELECT` binds its parameters correctly on 3.2.4, so
  the inline renderers are a 2.x workaround the branch no longer needs.

---

## [0.33.2] - 2026-09-07

**Bug fix release.** Three migration defects, all found reviewing what 0.33.1
shipped. Two of them are the dangerous kind: they produce a *wrong answer*
rather than an error.

### Fixed — Migrations

- **Table permissions never reached the database (#194).** The `PERMISSIONS`
  clause was emitted as a *second* `DEFINE TABLE`, which SurrealDB rejects once
  the table exists (`The table 'x' already exists`). Since 0.33.1 started
  surfacing statement errors, the first migration of any model declaring
  `permissions={...}` aborted; before that it failed silently and the server
  stored `PERMISSIONS NONE`.

- **`FULL` was written as a silent deny (#194).** Reading a table defined with
  `FOR select FULL` back and re-emitting it produced
  `FOR select WHERE FULL`. SurrealDB accepts that, evaluates `FULL` as a field
  reference, gets `NONE`, and denies — a world-readable table became unreadable
  with no error anywhere:

  ```
  DEFINE TABLE pw PERMISSIONS FOR select WHERE FULL;   -- status OK
  RETURN !!FULL;                                       -- false
  ```

  `FULL` and `NONE` are rendered as keywords now.

- **Redefining a table is `AlterTable`, not `CreateTable(overwrite=True)`.** The
  diff reused `CreateTable`, whose `backwards()` is `REMOVE TABLE`: a migration
  that changed one permission had a rollback that dropped the table and every
  row in it, and reported `reversible = True`. An `AlterTable` carries the
  definition it replaced and restores it; built without one it is *not*
  reversible, because a bare `DEFINE TABLE OVERWRITE t;` resets the table to
  `TYPE ANY SCHEMALESS PERMISSIONS NONE` rather than restoring anything.

- **`TYPE USER` reached the DDL (#194).** `USER`, `STREAM` and `HASH` classify a
  model; SurrealDB accepts only `NORMAL`, `RELATION` and `ANY`, and answered
  ``Parse error: Unexpected token `USER` ``. Any change to a USER table was
  unmigratable through `makemigrations`.

- **A materialized view was flattened into a plain table (#194).**
  `ModelIntrospector` never populated `view_query`, so a view model diffed on
  every run and the redefinition it emitted carried no `AS` clause.

- **A rollback rendered `DEFAULT` differently from the statement it reverses
  (#196).** `AlterField.backwards()` single-quoted every string with no function
  detection, no quote escaping and Python `str()` for booleans:

  ```
  DEFAULT 'time::now()'   the function became a string literal
  DEFAULT True            not a SurrealDB boolean
  DEFAULT 'it's'          a parse error — the rollback fails
  ```

  `AddField.forwards`, `AlterField.forwards` and `AlterField.backwards` now
  render through one shared function, so a clause cannot be added to one
  direction and forgotten in another.

- **`AlterField.backwards()` dropped the `VALUE` clause (#195, #196).**
  `DEFINE FIELD OVERWRITE` replaces the whole definition, so rolling back an
  alter on an `Encrypted` column stopped it hashing and stored plaintext from
  then on. Computed fields lost their expression the same way.

- **The permissions parser mis-read what the server reports (#194).** A
  condition containing a quoted `FOR` (`label = 'FOR sale'`) truncated at the
  quote; a keyword inside an expression ended the clause, so
  `FOR select WHERE meta.type != NONE` left the condition as `meta.` and the
  table type as `!= none`. Parsing an 80 KB clause also took 26 s; it takes
  0.02 s.

- **`COMMENT` and relation lists (#194, #196).** A table or field comment was
  dropped in both directions of an alter, and `relation_out=["a", "b"]` — the
  documented config form — rendered as `OUT ['a', 'b']` instead of
  `OUT a | b`.

- **`define_table()` was never idempotent (#194).** A second call failed with
  `already exists` and the error was swallowed, so a changed `PERMISSIONS`
  clause never reached an existing table. It emits the `OVERWRITE` form and
  checks per-statement status now.

### Changed — CLI

- **`makemigrations` no longer writes irreversible removals by default
  (#197).** Diffing against the live database, which became the default in
  0.33.1, made `diff()`'s removal branches reachable — and they fire for every
  database object no model can declare: `RELATE` edge tables (`Relation` fields
  are deliberately skipped by the introspector), the migration history,
  analyzers and APIs the model introspector never produces, and the `in`/`out`
  columns SurrealDB defines on a `TYPE RELATION` table. Those operations are
  reported and held back; `--drop-missing` opts in.

  **This is a behaviour change**: since 0.33.1 removing a model produced a
  `REMOVE TABLE`; it now needs the flag.

- **`schemadiff` and `makemigrations` agree about the same database.**
  `schemadiff` reported `Drop table has_player` while `makemigrations` said
  `No changes detected`, so a CI gate on `schemadiff` failed permanently on
  every RELATE edge table.

### Known limitation

Migration files generated between 0.32.6 and 0.33.1 carry no `previous_value`,
so rolling one of them back still emits an `OVERWRITE` with no `VALUE` clause
and nothing warns. An `Encrypted` column rolled back through such a file stops
hashing. Regenerate those migrations, or add the clause by hand, before relying
on their rollback.

### Internal

- `click` is a development dependency, so the CLI tests stop skipping in CI.
- `MIGRATIONS_TABLE` moved to `constants.py`; `state.py` is pure data and was
  importing the executor, and through it the connection manager, for one string.

---

## [0.33.1] - 2026-09-05

**Bug fix release.** `makemigrations` regenerated the whole schema on every run,
by two independent routes.

### Fixed — Migrations

- **`makemigrations` diffed against an empty schema (#171).** The current state
  was a bare `SchemaState()` — the code said so outright — so every table was
  re-emitted on every run:

  ```python
  # For now, assume current state is empty (first migration)
  current_state = SchemaState()
  ```

  It now reads the current schema through `DatabaseIntrospector`, the same path
  `schemadiff` already used. This only became viable once 0.33.0 fixed the
  producer/parser symmetry — while foreign keys introspected as `any` and virtual
  fields produced columns, diffing against the database would have generated
  spurious operations on every run.

- **A table's default permissions read as a configured value (#171).** SurrealDB
  reports `{"select": "NONE", "create": "NONE", "update": "NONE", "delete": "NONE"}`
  for a table defined with no `PERMISSIONS` clause. Those are its defaults, so a
  state read from the database compared unequal to a model state configuring
  nothing, and the diff emitted a redundant `CreateTable` for every table — the
  same symptom by a second route. Both sides are normalised before comparison,
  which also makes an explicit `NONE` equal to omitting the clause.

### Changed

- **`makemigrations` contacts the database by default.** It fails with a clear
  error when the database cannot be read, rather than falling back to an empty
  schema — that fallback is the bug above. Pass `--no-from-db` to generate against
  an empty schema instead, which is what you want for a genuine first migration or
  when working offline:

  ```bash
  surreal-orm makemigrations --name initial --no-from-db
  ```

### Notes

- Generating migrations offline from the replayed migration history — what Django
  does, and what would remove the database requirement entirely — is tracked in
  #186. No operation currently folds back into a `SchemaState`, so that route is a
  subsystem rather than a fix.

## [0.33.0] - 2026-09-05

**Migration introspection release.** Foreign keys, record references, virtual graph
fields and nullability now survive the trip from a model to a `DEFINE FIELD`
statement — and `on_delete` becomes something SurrealDB enforces rather than a
decorative annotation.

### Fixed — Migrations

- **Relation markers never reached the schema state (#170).** `_introspect_field`
  did not unwrap `Annotated`, so every `ForeignKey` and `ReferencesField` fell
  through `_map_type` to `any`. `FieldState`, the operations and the DB-side
  `define_parser` all already carried `reference` / `on_delete`; this one producer
  was the only thing that never filled them:

  ```python
  class Post(BaseSurrealModel):
      author: ForeignKey("User")
      citations: ReferencesField["posts"]

  # Before: DEFINE FIELD author ON posts TYPE any;
  # After:  DEFINE FIELD author ON posts TYPE option<record<users>> REFERENCE ON DELETE CASCADE;
  #         DEFINE FIELD citations ON posts TYPE option<array<record<posts>>> REFERENCE;
  ```

  The target model name is resolved to the model's configured `table_name`; an
  unresolvable target stays an untyped `record` rather than an invented table.

- **`ManyToMany` and `Relation` were emitted as `any` columns (#170).** They are
  virtual — their own core schema says `surreal_type: "virtual"` — and graph edges
  live in their own tables, so they now define no column at all.

- **Nullability was dropped at the diff boundary (#170).** `AddField` / `AlterField`
  had no `nullable` parameter and never wrapped `option<>`, so only `define_table()`
  preserved optionality and generated migrations silently lost it. Both operations
  take `nullable` now; `AlterField` also takes `previous_nullable` so a rollback
  restores the optionality the column actually had.

- **Rollbacks silently dropped clauses.** `AlterField.backwards()` emitted no
  `REFERENCE`, and the diff never forwarded `previous_flexible` / `previous_readonly`,
  so rolling back a foreign-key alter disabled referential integrity without an error.

- **`schema_diff()` proposed the same operation forever.** The model side stored
  Django's `SET_NULL` while the database reported SurrealDB's `UNSET`, and
  `FieldState.__eq__` compares raw strings — so every non-`CASCADE` strategy diffed
  on every run and applying it never converged. `FieldState` now stores the SurrealDB
  keyword, the spelling the database reports back.

- **`inspectdb` turned a scalar foreign key into an array.** `ModelCodeGenerator`
  mapped every `reference=True` field to `ReferencesField`, whose type is
  `array<record<T>>`, so a generated model re-introspected into a destructive
  scalar→array `AlterField` on a schema nobody had edited. Arity now decides:
  `array<record<T>>` keeps `ReferencesField`, a scalar `record<T>` generates
  `ForeignKey`.

- **`option<>` wrapping misread unions.** Any type containing `|` was treated as
  already optional, so `int | string` with `nullable=True` produced a non-nullable
  column. Detection now mirrors `parse_define_field`: `option<`, `| null`, `none |`.

- **A union-wrapped marker was invisible.** `author: ForeignKey("User") | None`
  regressed to `any`, because the annotation's origin is a union rather than
  `Annotated` and Pydantic does not lift metadata out of a union member.

### Added

- **`OnDelete`** — public type alias for the accepted strategies, exported from
  `surreal_orm` and `surreal_orm.fields`. `ForeignKey` now accepts SurrealDB's own
  vocabulary (`UNSET`, `REJECT`, `IGNORE`) alongside Django's (`CASCADE`,
  `SET_NULL`, `PROTECT`).
- **`AddField.from_field_state()` / `AlterField.from_field_states()`** — the single
  place a `FieldState` becomes operation keyword arguments, replacing four
  hand-copied argument lists. That duplication is what produced the fourth defect of
  #170 in the first place.

### Changed

- **`on_delete` is now enforced by SurrealDB.** It used to be decorative: the column
  landed as `any` with no `REFERENCE` clause, so nothing was applied. A `ForeignKey`
  left at the default `on_delete="CASCADE"` now means deleting the referenced record
  deletes the referencing one.

  ```python
  author: ForeignKey("User")                       # ON DELETE CASCADE — now enforced
  author: ForeignKey("User", on_delete="IGNORE")   # opt out: nothing cascades
  ```

- `ON DELETE THEN` is no longer accepted. SurrealDB requires
  `ON DELETE THEN <expression>` and no part of a field definition carries one, so the
  bare keyword only produced DDL that fails mid-migration.

### Notes

- **Expect one-time `REMOVE FIELD` statements on the first diff after upgrading.**
  `ManyToMany` / `Relation` previously introspected as real `any` columns, so existing
  databases physically carry those `DEFINE FIELD` entries. The first
  `schema_diff` / `makemigrations` after upgrading emits `REMOVE FIELD groups ON users;`
  and friends, alongside `any -> option<record<...>>` `AlterField` operations. No data
  is lost — the ORM never read those columns — but the statements are auto-generated
  and worth reviewing rather than applying blind.
- Foreign keys previously introspected as `any` while the database reported
  `record<...>`, so `schema_diff()` proposed a phantom `AlterField` on every run. That
  is gone; an integration test asserts the round trip is stable.

## [0.32.7] - 2026-09-01

**Bug fix release.** Two independent ORM fixes, both reported in #169: foreign
key values never reached the database as record links, and Django's `on_delete`
vocabulary generated invalid DDL.

### Fixed — ORM

- **`ForeignKey` values were never coerced to `RecordId` (#169, #174).** A
  `ForeignKey` field holds a `"table:id"` string in Python, but nothing converted
  it at the wire boundary, so a `record<>` column rejected the write outright and
  a filter compared a string against a record value:

  ```python
  # Before: rejected by SurrealDB — a record<fk_authors> column will not take a string
  await Article(title="Hello", author="fk_authors:alice").save()

  # Before: the row exists, but the filter compares a string to a record value
  await Article.objects().filter(author="fk_authors:alice").exec()  # []
  ```

  Every write path — `save()`, `merge()`, `update()`, `bulk_update()`,
  `upsert()`, `bulk_upsert()` — and every whole-record filter lookup — `exact`,
  `in`, `not_in`, including inside `Q` objects — now converts the value to a
  `RecordId`. Four interchangeable forms are accepted and may be mixed freely
  within a collection: the related model instance, a full `"table:id"` string, a
  bare ID qualified through the field's target table, and a `RecordId`.

  Deliberately left untouched: the string lookups (`contains`, `startswith`,
  `regex`, ...), `isnull`, and explicit `$var` references bound via
  `.variables()`, which pass through uncoerced by design.

- **Aliased foreign keys were filtered as strings (#169).** The defect the issue
  singled out as the worst of the three, because it returned a wrong answer
  rather than an error. The write path resolved aliases while the read path keyed
  off the Python field name only — so an aliased `ForeignKey` was *written* as a
  record link and *filtered* as a plain string, yielding an empty result with no
  error. The new `get_foreign_key_columns()` maps each foreign key under both its
  field name and its declared alias, and both `_coerce_filter_value()` and
  `bulk_update()` key off it.

  Still outstanding, pre-existing and tracked separately: filtering by the
  *Python* field name on an aliased model emits `author = ...` rather than the
  real column `author_id`.

- **Referencing an unsaved instance raised a confusing error.** Assigning an
  unsaved model reported `Input should be a valid string`, and the write and
  filter paths bound the model object straight into the query. The guard now
  lives in the shared `record_link_to_str()` helper, so all three paths raise
  `cannot reference an unsaved Author; save it first`. Note that the helper is
  therefore no longer total — it can raise.

- **`update()` leaked wire types into signal payloads.** `update()` passed its
  coerced dict as `update_fields`, so handlers received `RecordId` objects while
  `merge()` deliberately sent the uncoerced values. `update()` now mirrors
  `merge()`: signals get the uncoerced dict, and only the wire call gets the
  coerced one.

### Fixed — Migrations

- **Django's `on_delete` vocabulary produced invalid DDL (#169).** The value was
  passed through verbatim, but SurrealDB accepts only
  `CASCADE | UNSET | REJECT | IGNORE | THEN`. `AddField` and `AlterField` now map
  through the new `on_delete_to_surql()`:

  | Django | SurrealDB |
  | --- | --- |
  | `SET_NULL` | `UNSET` |
  | `PROTECT` | `REJECT` |
  | `CASCADE` / native keywords | unchanged |

  The Django literals remain the public API; both vocabularies are accepted.

### Added

- **`instance.record_id`** — the reserved record-identity property, the analog of
  Django's `Model.pk`. Returns the identity as a `RecordId`, ready to bind
  against a `record<>` column in `raw_query()` / `.variables()`, where explicit
  bindings still pass through uncoerced. `str(instance.record_id)` gives the
  `"table:id"` form.
- **`to_record_id()`, `record_link_to_str()`, `get_foreign_key_columns()`** —
  public helpers in `model_base`.
- The JSON protocol encoder renders a `RecordId` as `"table:id"`, so the fallback
  protocol keeps working.

---

## [0.32.6] - 2026-08-23

**Bug fix release.** Three independent fixes: a CBOR decoding bug in the SDK, a
migration operation that never did anything, and the CI monitor that pinned
pre-release SurrealDB versions.

`0.32.3`, `0.32.4` and `0.32.5` are automation artifacts: each was published by
the release pipeline off an auto-merged monitor PR that moved the SurrealDB pin
to a pre-release (`3.3.0-beta.1` → `beta.2` → `beta.3`). The wheels behave
identically to 0.32.2 — `.surrealdb-version` and `devops/docker-compose.yml` do
not ship in the package — but the library was declaring support for, and running
CI against, a beta database.

### Fixed — SDK

- **Compact CBOR datetimes decoded as a tuple of ints (#165).** SurrealDB 3.x sends
  tag 12 as a compact `[seconds, nanoseconds]` pair, not an ISO 8601 string, and
  `_cbor_tag_decoder()` only handled the string form. Every datetime the ORM does
  not coerce from a model annotation — `raw_query()` results, datetimes nested
  inside an object field — was returned as `(1739422800, 0)`:

  ```python
  rows = await Event.raw_query("SELECT * FROM events;")
  rows[0]["occurred_at"]     # before: (1739422800, 0)
  rows[0]["payload"]["at"]   # before: (1739422800, 0)
  ```

  Fields annotated `datetime` on a model were unaffected — the ORM rescued those
  after the fact. Both forms now decode, wherever they appear in a payload.

  **Upgrade note:** this changes the type of those values. Code that worked around
  the bug by unpacking the tuple must drop the workaround.

  Precision is kept to the microsecond (a Python `datetime` holds no more); the
  remaining nanoseconds are truncated. Both branches fall back to the raw value
  rather than raising: SurrealDB's datetime range is far wider than Python's year
  1-9999, and an exception inside the tag hook comes back out of `cbor2` as a
  `CBORDecodeError` that takes down every other value in the same response.

  This makes `_parse_datetime()`'s array branch redundant for CBOR; it is left in
  place for the JSON protocol. Covered by `tests/sdk/test_cbor_protocol.py`.

### Fixed — Migrations

Addresses group 1 of #162 (reported by @leosilberg).

- **`AlterField` never altered anything.** It emitted a plain `DEFINE FIELD`,
  which on SurrealDB 3.x does not update an existing field — the server answers
  `The field 'x' already exists` and leaves the definition as it was:

  ```text
  initial           : DEFINE FIELD f ON alt162 TYPE string
  after plain DEFINE: DEFINE FIELD f ON alt162 TYPE string   <- unchanged
  after OVERWRITE   : DEFINE FIELD f ON alt162 TYPE int
  ```

  `forwards()` and `backwards()` now emit `DEFINE FIELD OVERWRITE`, so rollbacks
  stop being no-ops too. `AddField` deliberately keeps failing loudly on an
  existing field — that is a real conflict the migration author needs to see.

- **The executor hid statement-level failures.** `client.query()` raises only on
  an RPC-level failure; a rejected statement rides back inside a perfectly
  successful RPC as `status: ERR` per statement, and the executor never looked.
  That is what let `AlterField` report success, and it hid failures inside
  `DataMigration` and `RawSQL` bodies just as effectively — which is where the
  reporter originally hit it. `migrate()`, `rollback()` and `upgrade()` now inspect every
  statement, including the queries that record and un-record a migration, and
  raise the new `MigrationStatementError`.

  ```python
  from surreal_orm.migrations import MigrationExecutor, MigrationStatementError
  ```

  **Upgrade note:** migrations that previously reported success while silently
  failing now raise. Since migrations are not wrapped in a transaction, a failure
  mid-migration leaves the earlier operations applied and the migration
  unrecorded, so the next `migrate()` replays it from the start.

Two integration tests pin this against a live database — one asserts the field
type actually changes, the other asserts its own precondition (that SurrealDB
really does return a statement-level `ERR`) so it cannot silently stop testing
the thing it is named after. Both were confirmed to fail without the fix.

### Fixed — CI

- **The v3 monitor pinned pre-releases (#163).** Its release query deliberately
  included betas and RCs, left over from the 3.0 alpha migration:

  ```bash
  # Get latest v3.x release (includes pre-releases like beta/RC)
  --jq '[.[] | select(.tag_name | startswith("v3.")) | .tag_name][0]'
  ```

  Once 3.3.0 entered beta, this pinned `3.3.0-beta.x` as the version the library
  declares support for and runs CI against. Because a pin bump auto-merges into
  a version bump, it burned three PyPI releases. The query now filters
  `prerelease` and `draft`, matching what the v2 monitor always did.

  The `sort -V` guard added in 0.32.2 for #155 does not catch this — it makes it
  worse. `sort -V` ranks `3.3.0-beta.3` *above* `3.3.0`, so once pinned to a
  beta the monitor would refuse every subsequent stable release as a
  "downgrade" and stay stuck indefinitely.

  Both monitors now reject a pre-release candidate outright — which also covers
  the manual `workflow_dispatch` input, that bypasses the API query — and treat
  any stable release as superseding a pre-release pin.

- **The pin is restored to SurrealDB 3.2.4** (`.surrealdb-version`,
  `devops/docker-compose.yml`), the newest stable 3.x release.

### Notes

This is the third occurrence of the same cascade (#155/#156, then #163): an
auto-merged monitor PR reaches PyPI with no human in the loop. The constraint
that matters is what the monitors are *allowed to propose*, not the release
gate. `tests/test_surrealdb_monitor.py` extracts the workflows' own shell block
and executes it, and now covers both directions — a pre-release candidate is
never adopted, and a stable release always supersedes a pre-release pin.

---

## [0.32.2] - 2026-08-12

**CI fix + documentation release.** No library code changes vs 0.32.0 or 0.32.1.

### Fixed

- **The SurrealDB monitors downgraded the version pin (#155).** Both monitors
  decided "a new version is available" with a plain string inequality:

  ```bash
  if [[ "$CURRENT_VERSION" != "$LATEST_VERSION" ]]; then HAS_UPDATE="true"; fi
  ```

  "different" is not "newer". SurrealDB published 3.2.1–3.2.3 as image tags
  **without matching GitHub Release entries**, so the newest v3.x *release* read
  `3.2.0` while `.surrealdb-version` already read `3.2.3`. The monitor opened —
  and auto-merged — a PR moving the pin *backwards* to 3.2.0, reverting the
  version the 0.32.0 release had validated against.

  A lexical compare was wrong across digit widths too: `2.10.0` sorts *below*
  `2.6.5` as a string, so the v2 monitor would have skipped a real upgrade.

  Both monitors now compare with `sort -V` and act only when the candidate is
  strictly newer. `.surrealdb-version` and `devops/docker-compose.yml` are
  restored to **3.2.3**.

  This surfaced immediately after #146 was fixed: the monitors had failed every
  day since ~2026-07-15 on the `Subquery` regression (#147), so `Update
  SurrealDB Version` had never been reached. With #148 merged, the monitor's
  integration tests passed for the first time in weeks — and the downgrade path
  finally ran.

### Added

- **`tests/test_surrealdb_monitor.py`** — extracts the comparison block from each
  monitor workflow and *executes it* against a table of version pairs, so the
  guard is tested rather than merely asserted. No database required.

### Changed

- **CHANGELOG backfill for 0.31.14** — released 2026-08-05 (aiohttp 3.14.1 →
  3.14.3) but never documented. Its entry is restored below, and the README
  version-history table gains the matching row.
- **`0.32.0` release date corrected** to 2026-08-12, the date it was actually
  tagged and published. The previous `2026-07-31` was the date the work was
  written, not released — the tag was blocked in the meantime by the
  version-bump auto-merge failure described in #153.
- **Roadmap corrected** — `docs/roadmap.md` still listed 0.32.0 as the "Graph
  Power" milestone (recursive traversal, shortest path, path collection). That
  work did not ship in 0.32.0; the slot was taken by the SurrealDB 3.2 migration
  and the `Subquery` fix. Graph Power moves to 0.33.0.
- **Stale SurrealDB version references refreshed** across `README.md` and
  `CLAUDE.md` — the compatibility notes still said "tested through SurrealDB
  3.1.3" after the pin had moved to 3.2.3.
- **`v2` branch protection restored** — the `V2 LTS Protection` ruleset targeted
  `refs/heads/V2` while the branch is `v2`, so it applied to nothing and `v2` ran
  unprotected (see #153 in 0.32.0). Corrected in repo settings; `v2` now requires
  the `CI Success` check like `main`.
- **Version bump to 0.32.2** — `pyproject.toml`, `surreal_orm/__init__.py`,
  `surreal_sdk/__init__.py`, `surreal_sdk/pyproject.toml`. 0.32.1 was consumed
  by the automation described below, so the fix ships as 0.32.2.

---

## [0.32.1] - 2026-08-12

**Automation artifact — do not use as a reference point.** Published by the
release pipeline as a side effect of the #155 downgrade, not by intent.

The monitor's downgrade PR (#155) was auto-merged, which made the Dependabot
auto-merge workflow open and merge its own version-bump PR (#156). That tagged
`v0.32.1` and published it to PyPI before the problem was noticed.

**The distributed package is byte-identical in behaviour to 0.32.0** — the only
changes were `.surrealdb-version` and `devops/docker-compose.yml`, neither of
which ships in the wheel. Nothing to do if you already installed it; 0.32.2 is
the version carrying the actual fix.

---

## [0.32.0] - 2026-08-12

> ### ⚠️ BREAKING — SurrealDB 3.2+ is now required
>
> **Upgrade your database to 3.2.3+ before upgrading the ORM.** SurrealDB 3.1.x
> and earlier are no longer supported or tested on `main`.
>
> | Your SurrealDB | Install |
> | -------------- | ------- |
> | **3.2+** | `surrealdb-orm` 0.32.x |
> | 3.0 – 3.1 | `surrealdb-orm<0.32` |
> | 2.x | `surrealdb-orm<0.30`, or the `v2` branch (`0.20.x`) |
>
> **`Subquery` also emits different SQL** (a `LET` prelude). The Python API and
> the results are unchanged — only code asserting on the *generated SQL string*
> needs updating.

### Breaking

- **Minimum SurrealDB is now 3.2** — `.surrealdb-version` and every image tag in
  `devops/docker-compose.yml` move from `3.1.5` to `3.2.3`. The 3.1.x line is no
  longer tested or supported on `main`. (SurrealDB 2.x remains served by the
  `v2` branch.)
- **`Subquery` now compiles to a `LET` prelude instead of an inline sub-SELECT.**
  A query with a subquery used to be a single statement:

  ```sql
  SELECT * FROM orders WHERE user_id IN (SELECT VALUE id FROM users WHERE is_active = $_f0);
  ```

  It is now two, with the subquery bound once up front:

  ```sql
  LET $_sq0 = (SELECT VALUE id FROM users WHERE is_active = $_f0);
  SELECT * FROM orders WHERE user_id IN $_sq0;
  ```

  Scalar subqueries wrap the variable — `total = array::first($_sq0)`. Nested
  subqueries emit one `LET` each, innermost first, since a `LET` may only
  reference variables bound before it. Only code asserting on the *generated
  SQL string* is affected; the Python API is unchanged and results are
  identical. A `LET` statement returns no records, so `QueryResponse.all_records`
  is unaffected and no SDK change was needed.

  `LIVE SELECT` cannot carry a prelude, so `QuerySet.live()` keeps the inline
  form — its WHERE clause must stay self-contained.

### Fixed

- **`Subquery` returned wrong results on SurrealDB 3.2.x (#147).** 3.2.x
  evaluates an inline uncorrelated sub-SELECT once per outer row while sharing
  its `LIMIT` budget across those evaluations, so a subquery combining
  `ORDER BY` **and** `LIMIT` yields its value for only some outer rows and `[]`
  for the rest — `QuerySet.filter(field=Subquery(...))` then matched nothing.
  The corruption is non-deterministic (~75% of executions, affected rows varying
  run to run), which is why the regression tests repeat each probe.

  Verified trigger boundary on 3.2.3 — only `ORDER BY` **and** `LIMIT` together
  break; `START`/offset is irrelevant:

  | inner clauses | corrupted |
  | ------------- | --------- |
  | none / `LIMIT` / `ORDER BY` / `START` | 0/12 |
  | `ORDER BY` + `START` | 0/12 |
  | `LIMIT` + `START` | 0/12 |
  | **`ORDER BY` + `LIMIT`** | **12/12** |
  | **`ORDER BY` + `LIMIT` + `START`** | **12/12** |

  Rather than special-casing that combination, `Subquery` now hoists *every*
  uncorrelated subquery into a `LET` binding, which is evaluated exactly once
  and is correct regardless of how the upstream trigger evolves.

  This remains an upstream SurrealDB bug — still unfixed in 3.2.3, the latest
  release, and reproducible in plain SurrealQL. The standalone repro is kept as
  an `xfail`-marked upstream tracker
  (`test_inline_subquery_order_by_and_limit_is_row_stable_upstream`) so it
  documents the defect without gating CI, and will `xpass` once upstream fixes it.

- **The SurrealDB version monitors never filed their failure issue (#146).**
  `create-failure-issue` exists only to run when `test-new-version` fails, but
  its `if:` contained no status-check function. GitHub
  [implicitly ANDs `success()`](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions)
  into such a condition, so a failed dependency skipped the job outright — the
  condition it guarded was unreachable. Both the 3.x and 2.x monitors had
  therefore failed daily since ~2026-07-15 (12+ consecutive scheduled runs)
  without ever opening an issue. Adding `always()` restores the intended
  behaviour; it does not weaken the guard, since a failing `check-release`
  leaves `has-update` empty and a cancelled run yields `result == 'cancelled'`.

- **Version-bump PRs stalled forever on an unprotected base branch (#153).**
  `gh pr merge --auto` is only accepted when the PR's *base* branch carries a
  ruleset / branch protection with required status checks. The `V2 LTS
  Protection` ruleset targeted `refs/heads/V2` while the branch is `v2`, and
  ruleset ref matching is case-sensitive — so `v2` had **no rules at all** and
  GitHub rejected the call with *"Protected branch rules not configured for this
  branch (enablePullRequestAutoMerge)"*. The step failed after the bump branch
  was already pushed and its PR opened, so PR #152 (`0.21.3`) sat open from
  2026-08-05: no tag, no PyPI publish for the v2 LTS line, while the aiohttp
  3.14.3 security fix was already merged into `v2`. Every future v2 security
  sync would have stalled the same way.

  The ruleset casing was corrected in repo settings, and the workflow no longer
  depends on the base branch being protected: a `merge-when-green.sh` helper
  prefers native auto-merge and, when GitHub rejects it, polls the named
  `CI Success` check on that PR and merges explicitly once it is green. The
  helper is written to `$RUNNER_TEMP` rather than committed under
  `.github/scripts/` — `pull_request_target` takes the workflow from the default
  branch but checks out the PR's *base* branch, so a committed helper would be
  missing whenever the base is `v2`.

  This does not reintroduce the #118 self-watch deadlock: that was
  `gh pr checks --watch` waiting on *all* checks, including this job's own
  `Auto-merge & Tag`. The helper polls one *named* check produced by a different
  workflow.

### Added

- **`QuerySet._compile_annotate_query()`** — the GROUP BY / aggregation query is
  now compiled by a dedicated method (extracted from `_execute_annotate()`) so
  it can hoist subquery annotations and be unit-tested without a database.
- **`tests/test_workflow_conditions.py`** — workflow lint tests that fail if any
  job or step condition in `.github/workflows/` tests a dependency's failure
  without a status-check function, so the #146 bug class cannot silently return.
  No database required. Adds `pyyaml` to the dev dependency group.
- **`tests/test_dependabot_automerge.py`** — workflow lint tests guarding the
  #153 and #118 regressions: no step may call `gh pr merge --auto` outside the
  fallback helper, the helper must be defined before it is used and must end the
  version-bump step, and `gh pr checks --watch` may never reappear.
- **Subquery clause-isolation and hoisting tests** — `tests/test_subquery.py`
  gains a `TestSubqueryLetHoisting` unit class (prelude shape, nesting order,
  distinct variables, `annotate()` path, and the inline form `live()` still
  needs) plus integration tests splitting the failure surface by clause.

### Changed

- **Dependency refresh** — lockfile upgraded across the board, notably
  `aiohttp` 3.14.1 → 3.14.3, `cbor2` 6.1.2 → 6.1.3, `certifi` 2026.5.20 →
  2026.7.22, `mypy` 2.1.0 → 2.3.0, `pytest` 9.0.3 → 9.1.1, `ruff` 0.15.16 →
  0.16.0, `coverage` 7.14.1 → 7.15.2, `docker` 7.1.0 → 7.2.0.
- **Ruff no longer formats Markdown** (`exclude = ["examples", "*.md"]`) — ruff
  0.16 started formatting Python code blocks inside `.md`, which would collapse
  the docs' deliberately line-per-argument examples onto single lines. Excluding
  Markdown keeps `ruff format --check` green without rewriting the docs.
- **Version bump to 0.32.0** — `pyproject.toml`, `surreal_orm/__init__.py`,
  `surreal_sdk/__init__.py`, `surreal_sdk/pyproject.toml`.

---

## [0.31.14] - 2026-08-05

**Automated security patch.** No library code changes — dependency bump only.
(Documented retroactively in 0.32.1; the release itself shipped on time.)

### Changed

- **`aiohttp` 3.14.1 → 3.14.3** in the `uv` group (#149, Dependabot auto-merge),
  followed by the automated version bump PR #150. `aiohttp` is the WebSocket
  transport for `surreal_sdk`, so the floor matters for live queries.
- The same bump was synced to the `v2` LTS branch (#151). Its version-bump PR
  (#152, `0.21.3`) then failed to auto-merge and stalled for a week — see #153
  in the 0.32.0 entry above.

---

## [0.31.13] - 2026-07-27

**Bug-fix release.** Two correctness fixes.

### Fixed

- **Denied / missing `UPDATE` and `merge()` now raise instead of silently
  no-opping (#135).** On a table protected by row-level `PERMISSIONS`, a write
  the current user isn't allowed to make — or one targeting a row that no longer
  exists — makes SurrealDB return an *empty* result (zero affected rows), not an
  error. Previously `save()` on a persisted instance and `merge()` discarded
  that result and returned `self`, so a denied update was indistinguishable from
  a successful one and callers could silently lose data while reporting success.
  A denied/missing update now raises `SurrealDbError`, mirroring the create-path
  guard, across every non-deferred write path: plain `client.merge()`, the
  `SurrealFunc` raw-`UPDATE` path, the complex-nested-data SET-clause path, and
  WebSocket transactions. A new `BaseTransaction.defers_results` property keeps
  HTTP-transaction behaviour unchanged (their per-statement results only exist
  at `commit()`, so the guard is skipped there). Known limitation: denial
  *inside* an HTTP transaction remains undetectable at statement time.
- **`istartswith` / `iendswith` are now actually case-insensitive (#134).** They
  previously compiled to the same SurrealQL as the case-sensitive `startswith` /
  `endswith`, so e.g. `name__istartswith="ali"` did not match `"Alice"`. Both
  the field and the value are now lowercased (mirroring `icontains`), and both
  raise `TypeError` for non-string values.

Thanks to [@rmortes](https://github.com/rmortes) for both fixes.

### Changed

- **Version bump to 0.31.13** — `pyproject.toml`, `surreal_orm/__init__.py`,
  `surreal_sdk/__init__.py`, `surreal_sdk/pyproject.toml`.

---

## [0.31.12] - 2026-07-20

**CI maintenance release.** No library code changes — runtime behaviour is
identical to 0.31.11.

### Changed

- **CI: `actions/setup-python` 6 → 7 (#136).**

---

## [0.31.11] - 2026-06-22

**CI maintenance release.** No library code changes — runtime behaviour is
identical to 0.31.10.

### Changed

- **CI: `actions/checkout` 6 → 7 (#132).**

---

## [0.31.10] - 2026-06-20

**Maintenance release.** No library code changes — runtime behaviour is
identical to 0.31.9.

### Changed

- **Dependency: `tornado` 6.5.6 → 6.5.7 (#127)** — transitive dev-group
  dependency; not part of the runtime install.

---

## [0.31.9] - 2026-06-20

**Maintenance release.** No library code changes — the SDK and ORM are
compatible as-is.

### Changed

- **Validated against SurrealDB 3.1.5 (#128)** — bumped the test/CI target and
  Docker Compose image `surrealdb/surrealdb:v3.1.4` → `v3.1.5`.

---

## [0.31.8] - 2026-06-17

**Maintenance release.** No library code changes — runtime behaviour is
identical to 0.31.7.

### Changed

- **Dependency: `aiohttp` 3.14.0 → 3.14.1 (#123)** — the WebSocket transport's
  runtime dependency (Dependabot).

---

## [0.31.7] - 2026-06-11

**Maintenance release.** No library code changes — the SDK and ORM are
compatible as-is.

### Changed

- **Validated against SurrealDB 3.1.4 (#121)** — bumped the test/CI target and
  Docker Compose image `surrealdb/surrealdb:v3.1.3` → `v3.1.4`.

---

## [0.31.6] - 2026-06-09

**CI / maintenance release.** No library code changes — runtime behaviour is
identical to 0.31.5. Integrates the open Dependabot PRs and fixes the
release-automation deadlock that was preventing them from auto-merging.

### Fixed

- **Dependabot auto-merge self-watch deadlock (#118)** — The `Auto-merge & Tag` job in `dependabot-automerge.yml` began with `gh pr checks --watch`, which waits for *all* of the PR's checks to finish — including this job's own check run. The job therefore waited on itself until the 6-hour max-job timeout and was cancelled, so no Dependabot PR ever auto-merged. The full test suite (lint, mypy, unit, integration, CodeQL) was green throughout — this was never a test or version-bump failure. Removed the self-referential gate; merging is delegated to `gh pr merge --auto`, which merges only once the branch's required `CI Success` check passes. Because `pull_request_target` always runs the workflow from the default branch, the already-fixed copy on `v2` never executed — the broken `main` copy ran for every PR.

### Changed

- **`dependabot/fetch-metadata` 2 → 3 (#115).**
- **`codecov/codecov-action` 5 → 7 (#116).**
- **Dependabot config** — Ignore `cbor2` `semver-major` updates on the **v2** target branch. v2 is intentionally pinned to `cbor2 <6`; 6.x changed the custom-tag encoding and breaks SurrealDB 2.x CBOR RPC (the 6.x compatibility fix only landed on the `main` / 3.x line in 0.31.2). This stops Dependabot from re-proposing the `<7` widening on v2.

---

## [0.31.5] - 2026-06-06

**Documentation & maintenance release.** Brings the changelog and README up to
date through 0.31.4, adds a consolidated summary of the 0.31.x line, and corrects
stale "last updated" dates. No library code changes — runtime behaviour is
identical to 0.31.4.

### Changed

- Documented the previously-unreleased 0.31.1–0.31.4 entries in this changelog and in the README "What's New" history.
- Renamed `CHANGELOG` → `CHANGELOG.md` so the README link resolves and the file follows convention.
- Refreshed the "last updated" date in `CLAUDE.md` (was stuck at "March 2026").

### 0.31.x at a glance

- **0.31.0** — First PyPI release for SurrealDB 3.0: `RebuildIndex`, `DefineGraphQLConfig` / `RemoveGraphQLConfig`, `DefineBearerAccess`, and `QuerySet.upsert()` / `bulk_upsert()` with `ON DUPLICATE KEY UPDATE`.
- **0.31.1** — Security: bumped `cbor2` 5.8.0 → 5.9.0; CI / release-automation fixes.
- **0.31.2** — Critical: fixed the cbor2 6.x incompatibility that produced 401 auth failures on every CBOR RPC; raised security dependency floors.
- **0.31.3** — Validated against SurrealDB 3.1.3 (test/CI target + Docker image).
- **0.31.4** — Fixed intermittent integration-suite 401s caused by JWT `nbf` clock skew (WSL2 backward clock jumps); upgraded locked dependencies, clearing the Pygments ReDoS advisory.

---

## [0.31.4] - 2026-06-06

**Fixes non-deterministic `401 Unauthorized` failures in the integration suite
(issue #101) and refreshes dependencies.**

### Fixed

- **Transient JWT `nbf` clock-skew 401s (#101 / #106)** — A freshly-minted SurrealDB JWT carries `iat == nbf == now`. When the server clock jumps *backwards* between minting and verifying a token (notably on WSL2, whose VM clock can jump back), the token's `nbf` claim is momentarily in the future and SurrealDB rejects the request with a transient `401` ("Token verification failed due to the 'nbf' claim containing a future time"). Under load this surfaced as non-deterministic, disjoint failure sets per run. `HTTPConnection` now retains its last signin arguments and `_send_rpc()` recovers by re-minting the token (a fresh `nbf` aligned with the current clock) and retrying with a short bounded backoff. The earlier "shared singleton auth-leak" hypothesis was disproven — every rejected token was a valid, unexpired root token.

### Changed

- **Dependency upgrade (#107)** — Refreshed `uv.lock` to the latest compatible versions: **pygments 2.19.2 → 2.20.0** (see Security below), plus mypy 2.1.0, pydantic 2.13.4, ruff 0.15.16 and assorted transitive bumps. Runtime floor pins in `pyproject.toml` are unchanged.

### Security

- **Pygments 2.19.2 → 2.20.0** — Resolves Dependabot alert #6 (GHSA-5239-wwwm-4pmq / CVE-2026-4539), a low-severity ReDoS in GUID matching (transitive dev dependency).

---

## [0.31.3] - 2026-06-06

**SurrealDB 3.1.3 support.**

### Changed

- Bumped the integration-test / CI target and Docker Compose image to `surrealdb/surrealdb:v3.1.3` (#104). No source changes — the SDK and ORM are compatible as-is.

---

## [0.31.2] - 2026-06-06

**Critical cbor2 6.x compatibility fix.**

### Fixed

- **cbor2 6+ compatibility (CRITICAL — fixes 401 auth failures)** — Under cbor2 6.x the decoder's `tag_hook` callback signature changed from `(decoder, tag)` to `(tag, immutable)`. Run with cbor2 6.x, the old code raised `CBORDecodeError: error decoding semantic tag 6` on every SurrealDB response containing a `NONE` value (i.e. virtually all of them), breaking all CBOR RPC operations — including authentication — and surfacing as 401 Unauthorized in applications (the workaround was pinning `cbor2<6`). `_cbor_tag_decoder(tag, immutable)` in `src/surreal_sdk/protocol/cbor.py` is fixed for cbor2 6.x, and the RecordId tag branch now accepts `list | tuple` (cbor2 6.x exposes a `CBORTag` array value as a `tuple` rather than a `list`).

### Security

- Raised dependency floors and pinned `cbor2>=6.1.2` (avoids the data-corruption bugs in 6.0.x / 6.1.0, fixed in 6.1.1 / 6.1.2): `aiohttp>=3.12` (drops CVE-affected 3.9.x), `pydantic>=2.11`, `httpx>=0.28`, `click>=8.1.8`.

---

## [0.31.1] - 2026-03-28

**Automated security-patch release.**

### Security

- Bumped `cbor2` 5.8.0 → 5.9.0 (Dependabot auto-merge).

### Changed

- CI / release automation: `tag-release` now uses `PAT_TOKEN` so tag pushes trigger the publish workflow; Dependabot v2 sync and version-bump PR flow; PyPI scoping fixes; version tests made dynamic instead of hardcoded.

---

## [0.31.0] - 2026-03-22

**First PyPI release for SurrealDB 3.0.** Previous 0.30.x versions were
internal alpha/beta iterations. This is the first version published to PyPI
that targets SurrealDB >= 3.0. For SurrealDB 2.X, use `surrealdb-orm<0.30`
(the `v2` branch).

Adds migration operations for GraphQL configuration, bearer access, index
rebuilding, and upsert conflict handling. The `DEFINE BUCKET` file storage
feature has been deferred to v0.35.0 to keep this release focused.

The project remains in **Beta** status (`Development Status :: 4 - Beta`).

### Added

- **`RebuildIndex` migration operation** — Rebuild indexes after bulk data imports or index definition changes. Supports `IF EXISTS` guard. Irreversible (idempotent no-op on rollback). Generates `REBUILD INDEX name ON table;`.

  ```python
  from surreal_orm import RebuildIndex

  RebuildIndex(table="documents", name="idx_embedding")
  RebuildIndex(table="articles", name="idx_fts", if_exists=True)
  ```

- **`DefineGraphQLConfig` / `RemoveGraphQLConfig` migration operations** — Enable and configure SurrealDB 3.0's built-in GraphQL endpoint via migrations. Supports four modes per clause: `TABLES AUTO|NONE|INCLUDE|EXCLUDE` and `FUNCTIONS AUTO|NONE|INCLUDE|EXCLUDE`. New `GraphQLConfigState` dataclass for schema tracking and diffing. New `parse_define_config_graphql()` parser for database introspection via `INFO FOR DB`.

  ```python
  from surreal_orm import DefineGraphQLConfig

  # Enable for all tables
  DefineGraphQLConfig(tables_mode="AUTO", functions_mode="AUTO")
  # Generates: DEFINE CONFIG GRAPHQL TABLES AUTO FUNCTIONS AUTO;

  # Include specific tables only
  DefineGraphQLConfig(tables_mode="INCLUDE", tables_list=["users", "orders"])
  ```

- **Bearer access support (SurrealDB 3.0)** — Machine-to-machine authentication via API keys. New `DefineBearerAccess` migration operation generates `DEFINE ACCESS ... TYPE BEARER DURATION FOR GRANT ..., FOR SESSION ...;`. New `AccessType` enum (`RECORD`, `BEARER`) in `types.py`. `parse_define_access()` parser updated to handle both TYPE RECORD and TYPE BEARER access definitions. New auth mixin methods on `AuthenticatedUserMixin`:
  - `grant_bearer_key(user_id=, access_name=)` — Issues bearer keys via `ACCESS ... GRANT`
  - `revoke_bearer_key(key_id, access_name=)` — Revokes keys via `ACCESS ... REVOKE`

  ```python
  from surreal_orm import DefineBearerAccess

  # Migration
  DefineBearerAccess(name="api_key", duration_grant="30d", duration_session="1h")

  # Runtime
  key_info = await ServiceAccount.grant_bearer_key(user_id="service_accounts:worker1")
  await ServiceAccount.revoke_bearer_key("key:abc123")
  ```

- **`QuerySet.upsert()` with `ON DUPLICATE KEY UPDATE`** — Insert a record or update it on conflict. Accepts `defaults` (field values for the new record), `id` (optional record ID), and `on_conflict` (dict of field -> value/SurrealFunc for the update clause on existing records). `SurrealFunc` expressions are supported in both `defaults` and `on_conflict`. All values are parameterized (`$_up_*` for defaults, `$_oc_*` for conflict) to prevent injection.

  ```python
  user = await User.objects().upsert(
      defaults={"name": "Alice", "login_count": 1},
      id="user:alice",
      on_conflict={"login_count": SurrealFunc("login_count + 1")},
  )
  ```

- **`QuerySet.bulk_upsert()`** — Batch upsert multiple instances with shared `on_conflict` handling. Supports `atomic=True` for transaction wrapping. Variables are namespaced by index (`$_bu{idx}_*`, `$_oc{idx}_*`) to avoid collisions across batch items.

  ```python
  results = await User.objects().bulk_upsert(
      users,
      on_conflict={"login_count": SurrealFunc("login_count + 1")},
      atomic=True,
  )
  ```

### Changed

- **`AccessState` schema** — Added `access_type` (default `"RECORD"`) and `duration_grant` fields. The `table`, `signup_fields`, and `signin_where` fields now have default values (empty string/dict) for BEARER access compatibility. Existing RECORD access definitions are fully backward-compatible.
- **`SchemaState` schema** — Added `graphql_config: GraphQLConfigState | None` field. `SchemaState.diff()` generates `DefineGraphQLConfig` when config is added or changed, and `RemoveGraphQLConfig` when config is removed. No operations when config is identical.
- **`DatabaseIntrospector`** — Now parses GraphQL configuration from `INFO FOR DB` (`configs` or `cg` key) and bearer access definitions. Bearer access states are recognized but not attached to a specific table (they are database-level).

### Fixed (validated against SurrealDB v3.0.4)

- **`DefineBearerAccess` SQL syntax** — SurrealDB 3.0 requires `FOR USER` or `FOR RECORD` after `TYPE BEARER`. Added `bearer_for` parameter (default `"USER"`). Generates `DEFINE ACCESS name ON DATABASE TYPE BEARER FOR USER DURATION FOR GRANT ..., FOR SESSION ...;`.
- **`RemoveGraphQLConfig` SQL syntax** — SurrealDB 3.0.4 does not support `REMOVE CONFIG GRAPHQL`. Changed to overwrite with `DEFINE CONFIG GRAPHQL TABLES NONE FUNCTIONS NONE;` to effectively disable it. `DefineGraphQLConfig.backwards()` updated to match.
- **`QuerySet.upsert()` SQL syntax** — SurrealDB uses `INSERT INTO ... ON DUPLICATE KEY UPDATE` (not `UPSERT ... ON DUPLICATE KEY UPDATE`). The `upsert()` method now generates `INSERT INTO` when `on_conflict` is provided, and plain `UPSERT SET` when not. `SurrealFunc` expressions with compound assignment operators (`+=`, `-=`) are output directly without `field =` wrapping.

### Deferred

- **`DEFINE BUCKET` / File Storage** — Moved to v0.35.0 to keep this release focused on query and auth features.

---

## [0.30.0b1] - 2026-03-10

### Added

- **Refresh token flow** — `signup()` and `signin()` now return `AuthResult` with `user`, `token`, and `refresh_token` (SurrealDB 3.0+). Backward-compatible with existing `user, token = await User.signup(...)` unpacking. New `refresh_access_token()` classmethod exchanges a refresh token for a new access token. Requires `WITH REFRESH` clause on `DEFINE ACCESS` (after `SIGNIN`, before `DURATION`).
- **DEFINE API migration support** — New `DefineApi` and `RemoveApi` migration operations for SurrealDB 3.0 REST API endpoint definitions. `ApiState` dataclass for schema tracking and `parse_define_api()` parser for introspection.
- **Record references field** — New `ReferencesField["table"]` type annotation for SurrealDB 3.0's `REFERENCE` clause on `DEFINE FIELD`. Supports `ON DELETE` strategies (CASCADE, REJECT, UNSET, IGNORE). Includes `is_references_field()`, `get_references_info()`, and `get_references_on_delete()` detection helpers.
- **Branch Guard CI check** — New `branch-guard` job in CI blocks PRs from v2-related branches (`v2`, `0.20.*`, `chore/surrealdb-2x-*`) into `main`. Prevents accidental cross-branch merges between the SurrealDB 3.X and 2.X codebases.

### Fixed

- **SDK: `AuthResponse.from_rpc_result()` — `"access"` key support** — SurrealDB 3.0 WITH REFRESH returns the JWT under the `"access"` key (not `"token"`). The RPC-level parser now checks `data.get("access") or data.get("token")` for both formats.
- **SDK: HTTP `signin()` — nested token dict** — The HTTP `/signin` endpoint returns `{"code": 200, "token": {"access": "JWT...", "refresh": "..."}}` (nested dict) when WITH REFRESH is enabled. The HTTP connection handler now correctly unpacks this nested structure.
- **`DefineApi` PERMISSIONS ordering** — SurrealDB 3.0 requires `PERMISSIONS` before `THEN` in `DEFINE API` statements. Fixed clause ordering in `DefineApi.forwards()`.
- **Refresh token prefix in docstrings** — Fixed `surreal-refresh_` (underscores) to `surreal-refresh-` (hyphens) to match SurrealDB 3.0.2 actual output.

---

## [0.30.0a2] - 2026-03-10

### Added

- **SurrealDB 2.X Version Monitor on main** — New workflow `surrealdb-v2-security.yml` runs daily from `main` (the default branch) to monitor SurrealDB 2.X releases for the `v2` LTS branch. GitHub Actions only executes scheduled (cron) workflows from the default branch, so this workflow lives on `main` but checks out `v2` code for testing and creates PRs targeting `v2`.

### Changed

- **Dual-branch security monitoring** — `main` now manages security monitoring for both SurrealDB 3.X (`surrealdb-security.yml`) and SurrealDB 2.X (`surrealdb-v2-security.yml`). The V2 monitor runs at 12:30 UTC (offset by 30 min from the V3 monitor) to avoid resource contention.

---

## [0.30.0-alpha] - 2026-03-09

### Breaking Changes (SurrealDB 3.0 Migration)

### Changed

- **SurrealDB 3.0 Support** — All code and tests now target SurrealDB >= 3.0. The `v2` branch preserves SurrealDB 2.x compatibility.
- **Docker Compose upgraded to SurrealDB v3** — All 4 services in `devops/docker-compose.yml` now use `surrealdb/surrealdb:v3` image.
- **Auth token format** — `signin`/`signup` now returns `{access, refresh}` dict (with `WITH REFRESH`) or `{token}` dict (without) instead of plain string token. New `AuthResponse` dataclass in `surreal_sdk/types.py` handles all formats with `refresh_token` field.
- **KNN vector search requires EF parameter** — SurrealDB 3.0 requires the search effort (EF) parameter for KNN queries. `similar_to()` now always generates `<|K,EF|>` syntax (default ef=100 when not specified). The old `<|K|>` syntax is no longer supported.
- **`SEARCH ANALYZER` renamed to `FULLTEXT ANALYZER`** — Migration operations and parsers updated. Both keywords are accepted during parsing for backward compatibility.
- **`MTREE` vector index type removed** — SurrealDB 3.0 only supports `HNSW` for vector indexes. Parser and migration code updated.
- **Time functions renamed** — `time::from::micros` → `time::from_micros`, `time::from::millis` → `time::from_millis`, etc. All `time::from::*` renamed to `time::from_*`. Also `time::is::leap_year` → `time::is_leap_year`.
- **`type::thing()` renamed to `type::record()`** — Auth module updated to use new function name.
- **Non-existent tables return errors** — SurrealDB 3.0 returns errors instead of empty arrays for queries on non-existent tables. `ConnectionManager` now auto-creates namespace/database with `DEFINE NAMESPACE/DATABASE IF NOT EXISTS` after signin.
- **Live queries require existing tables** — `LIVE SELECT` on non-existent tables now fails. Tables must be created before subscribing.
- **Schema introspection `none | T` format** — SurrealDB 3.0 reports nullable fields as `none | int` instead of `option<int>`. The `define_parser` now handles both formats.

### Fixed

- **Namespace/database auto-creation** — `SurrealDBConnectionManager.get_client()` and `get_ws_client()` now execute `DEFINE NAMESPACE/DATABASE IF NOT EXISTS` after signin to prevent "namespace does not exist" errors in SurrealDB 3.0.
- **Test infrastructure** — `conftest.py` now ensures namespace/database exist before tests run. All integration test fixtures updated with `DEFINE TABLE IF NOT EXISTS` before live queries and `try/except` wrappers for cleanup operations on non-existent tables.
- **`hybrid_search()` KNN syntax** — Updated to include EF parameter: `<|K,100|>` instead of `<|K|>`.

---

## [0.14.4] - 2026-02-19

### Fixed

- **Datetime Serialization Round-Trip** (HIGH) — Python `datetime` objects were serialized as plain ISO 8601 strings during `save()` and `merge()`, causing silent type mismatches with SurrealDB `TYPE datetime` and `TYPE option<datetime>` schema fields. Two fixes:
  - **`_restore_datetime_fields()`** — After `model_dump()` in `save()` and `update()`, re-injects original `datetime` objects from the model instance if Pydantic stringified them. Ensures datetime objects reach the CBOR encoder as native datetime values (properly encoded as `CBORTag(12, iso_string)`).
  - **`inline_dict_variables()` datetime support** — `_extract_datetime_values()` recursively replaces `datetime` objects with `__SURQL_DT_N__` placeholder markers before `json.dumps()`, then post-replaces them with `d"<iso>"` SurrealQL datetime literals.

### Added

- **Generic `QuerySet[T]`** — `QuerySet` is now a generic class parameterized by the model type. All terminal methods return properly typed instances:
  - `exec()` → `list[T]`, `get()` → `T`, `first()` → `T`, `all()` → `list[T]`
  - `bulk_create()` → `list[T]`, `live()` → `LiveModelStream[T]`, `changes()` → `ChangeModelStream[T]`
  - `objects()` returns `QuerySet[Self]` — full type inference from model to query result
- **Typed `get_related()` via `@overload`** — Return type is now inferred from the `model_class` parameter:
  - `get_related("wrote", model_class=Book)` → `list[Book]`
  - `get_related("wrote")` → `list[dict[str, Any]]`
- **`_restore_datetime_fields()`** — Instance method on `BaseSurrealModel` that re-injects datetime objects after `model_dump()`.
- **`_extract_datetime_values()`** — Utility function in `utils.py` for recursive datetime placeholder extraction in inline dict variables.
- **10 new integration tests** — `tests/test_features_0_14_4.py` covering datetime round-trips (save, merge, inline, None), QuerySet typed returns, and `get_related()` typed returns.
- **24 new unit tests** — Covering `Generic QuerySet[T]`, `get_related()` overloads, `_restore_datetime_fields()`, `_extract_datetime_values()`, inline dict datetime handling, and `_is_datetime_field()`.

### Changed

- **mypy strict mode** — Enabled `strict = true` in `pyproject.toml`. All 64 source files pass mypy strict with zero errors.
- **Docker Compose pinned to SurrealDB v2.6** — All images in `devops/docker-compose.yml` pinned from `surrealdb/surrealdb:latest` to `surrealdb/surrealdb:v2.6` to prevent accidental use of incompatible versions.
- `QuerySet` class now inherits from `Generic[T]` with `T = TypeVar("T", bound="BaseSurrealModel")`.
- `objects()` classmethod return type changed from `QuerySet` to `QuerySet[Self]`.
- `get_related()` now has two `@overload` stubs with `_M = TypeVar("_M", bound="BaseSurrealModel")`.
- Multiple files updated for mypy strict compliance: `cli/commands.py`, `auth/mixins.py`, `cache.py`, `live.py`, `signals.py`, `subquery.py`, `prefetch.py`, `fields/computed.py`, `migrations/executor.py`.
- Version bump to 0.14.4

---

## [0.14.3] - 2026-02-15

### Fixed

- **Large Nested Dict Parameter Binding** (Issue #55) — SurrealDB v2.6's CBOR parameter binding silently drops complex nested structures (dicts with nested dicts/lists, lists of dicts), receiving `{}` instead of actual data. Two fixes:
  - **`save()` auto-routing**: `_execute_save()` now detects complex nested data via `_has_complex_nested_data()` and routes through a SET-clause query path (`_execute_save_with_set_clause()`) where each field is bound as a separate `$_sv_field` variable, avoiding the problematic single-object CBOR binding.
  - **`raw_query(inline_dicts=True)`**: New parameter on `raw_query()` that serializes complex dict/list variables as inline JSON in the query string, bypassing CBOR parameter binding entirely.

### Added

- **`_has_complex_nested_data(data)`** — Static method on `BaseSurrealModel` that detects dicts containing nested dicts/lists or lists containing dicts.
- **`_execute_save_with_set_clause()`** — New internal method that builds explicit `SET field = $_sv_field` queries for complex data, reusing `_build_set_clause()`.
- **`inline_dicts` parameter on `raw_query()`** — When `True`, complex dict/list variables are inlined as JSON in the query string.
- **`inline_dict_variables(query, variables)`** — New utility function in `utils.py` that replaces `$var` references with inline JSON for complex variables.
- **`_is_complex_value(value)`** — New utility function detecting nested dicts/lists.
- **`_SurrealJSONEncoder`** — JSON encoder in `utils.py` handling datetime, Decimal, UUID for inline serialization.
- **Integration tests** — `tests/test_issue_55.py` covering SDK and ORM save/query paths against real SurrealDB.
- **Unit tests** — `tests/test_issue_55_unit.py` covering detection heuristics, inline-dict helpers, JSON encoder, and save routing logic.

### Changed

- `_execute_save()` now checks `_has_complex_nested_data()` before the direct RPC path and routes to SET-clause path when needed.
- Version bump to 0.14.3

---

## [0.14.2] - 2026-02-15

### Fixed

- **CBOR None → NONE Encoding** (HIGH) — Python `None` was encoded as CBOR `null` (SurrealDB `NULL`) instead of `CBORTag(TAG_NONE)` (SurrealDB `NONE`). This caused `option<T>` fields on SCHEMAFULL tables to reject updates with `None` values. `_preprocess_for_cbor()` now recursively converts `None` to `CBORTag(TAG_NONE, None)` before `cbor2.dumps()`.
- **JSON Protocol None Handling** (HIGH) — Added `_strip_none_values()` to recursively strip `None` keys from dicts before JSON encoding, since JSON `null` maps to SurrealDB `NULL` and JSON has no `NONE` concept.
- **`validate_token()` Connection Overhead** (HIGH) — Added in-memory TTL cache (default 300s) to avoid creating an ephemeral HTTP connection on every call. Under concurrent web traffic, this previously caused connection refusals.

### Added

- **`validate_token_local()`** — New classmethod that decodes JWT payload locally (base64, no crypto verification) and extracts record ID from claims. Checks `exp` for expiry. Zero network calls — for trusted backend scenarios only.
- **`configure_token_cache(ttl=)`** — Classmethod to configure token validation cache TTL.
- **`invalidate_token_cache(token=)`** — Classmethod to clear cached token(s).
- **`flexible_fields` Config** — `SurrealConfigDict(flexible_fields=["game_state"])` as discoverable alternative to `Field(json_schema_extra={"flexible": True})` for migration `FLEXIBLE TYPE` generation.
- **38 new unit tests** — Full coverage for CBOR NONE encoding, JSON None stripping, token cache, `validate_token_local()`, `flexible_fields` introspection, and `validate_assignment` behavior.

### Changed

- **`validate_assignment=True`** — Enabled on `BaseSurrealModel.model_config`. Pydantic now validates field assignments, auto-coercing ISO 8601 strings to `datetime` objects on `model.field = "2026-02-13T10:00:00Z"`.
- `_introspect_field()` now accepts `model` parameter and checks `model_config.flexible_fields` in addition to `json_schema_extra`.
- `test_null_roundtrip` updated to expect absent field (NONE) instead of NULL value.
- Version bump to 0.14.2

---

## [0.14.1] - 2026-02-11

### Added

- **Typed Functions API in Notebook** — Added Section 1 to `examples/08_computed_functions.ipynb`
  - `db.fn.math.*` — `sqrt`, `pow`, `abs`, `ceil` demos
  - `db.fn.string.*` — `uppercase`, `len`, `concat` demos
  - `db.fn.time.*` — `now()` server timestamp demo
  - `db.fn.crypto.*` — `sha256`, `md5`, `argon2.generate` demos
  - `db.fn.array.*` — `len`, `distinct`, `flatten` demos
  - Dynamic namespace resolution demo (`db.fn.rand.int`)
  - SQL inspection via `FunctionCall.to_sql()`

### Changed

- Notebook `08_computed_functions.ipynb` reordered from simple to complex (Typed Functions API first)
- Sections renumbered: Typed Functions (1) → Computed Fields (2) → SurrealFunc (3) → extra_vars (4) → call_function (5) → raw_query (6)
- Version bump to 0.14.1

---

## [0.14.0] - 2026-02-11

### Added

- **Test Fixtures** — Declarative test data with automatic cleanup
  - `SurrealFixture` base class for declaring fixtures as class attributes
  - `@fixture` decorator scans class attributes for `BaseSurrealModel` instances
  - `load()` async context manager: saves all instances on enter, deletes them on exit
  - Fixture instances accessible by attribute name (e.g., `fixtures.alice`)
  - Deep-copies templates on each `load()` to avoid state leakage across calls
  - Duck-type detection for cross-import-path compatibility
  - New file: `src/surreal_orm/testing/fixtures.py`

- **Model Factories** — Factory Boy-style data generation with zero external dependencies
  - `ModelFactory` with `_FactoryMeta` metaclass for declarative factory definitions
  - `build()` / `build_batch(n)` — create in-memory instances (unit tests, no DB)
  - `create()` / `create_batch(n)` — create and save to DB (integration tests)
  - Override any field at call time: `create(role="admin")`
  - Built-in `Faker` with 14 providers: `name`, `first_name`, `last_name`, `email`, `random_int`, `random_float`, `text`, `sentence`, `word`, `uuid`, `boolean`, `date`, `datetime`, `choice`
  - New file: `src/surreal_orm/testing/factories.py`

- **QueryLogger** — Profile and debug all ORM queries with timing
  - `QueryLog` dataclass: `sql`, `variables`, `duration_ms`, `timestamp`
  - `QueryLogger` async context manager with `total_queries` and `total_ms` properties
  - Async-safe activation via `contextvars.ContextVar`
  - All query paths instrumented: `QuerySet.exec()`, `save()`, `update()`, `merge()`, `delete()`, `raw_query()`, `atomic_append/remove/set_add()`
  - New file: `src/surreal_orm/debug.py`

- **Testing Subpackage** — `surreal_orm.testing` with public exports
  - `from surreal_orm.testing import SurrealFixture, fixture, ModelFactory, Faker`
  - New file: `src/surreal_orm/testing/__init__.py`

- **15 Themed Jupyter Notebooks** — Comprehensive examples in `examples/`
  - `00_setup` — Prerequisites, Docker, connection config
  - `01_models_crud` — Model definition, CRUD operations
  - `02_queries_filters` — Filters, Q objects, lookups
  - `03_aggregations` — Count, Sum, Avg, Min, Max, GROUP BY
  - `04_relations_graph` — ForeignKey, ManyToMany, graph traversal
  - `05_transactions_bulk` — Transactions, bulk operations, atomic ops
  - `06_signals` — Pre/post/around signals
  - `07_auth` — User tables, signup, signin, token validation
  - `08_computed_functions` — Computed fields, SurrealFunc, raw_query
  - `09_advanced_queries` — Subqueries, QueryCache
  - `10_search_vectors` — Vector search, full-text search, hybrid search
  - `11_geospatial` — GeoField, PointField, nearby(), GeoDistance
  - `12_realtime` — Live queries, change feeds
  - `13_migrations_schema` — Migration system, schema introspection
  - `14_testing_debug` — SurrealFixture, ModelFactory, QueryLogger

- **Shared Test Connection Constants** — Centralized in `tests/conftest.py`
  - `SURREALDB_URL`, `SURREALDB_WS_URL`, `SURREALDB_USER`, `SURREALDB_PASS`, `SURREALDB_NAMESPACE`
  - All env-variable-backed with sensible defaults
  - `SURREALDB_SKIP_CONTAINER` support for CI environments

### Changed

- Alpha → Beta transition (`Development Status :: 4 - Beta` classifier)
- `conftest.py` uses `/health` endpoint as primary readiness signal (no longer requires Docker container name check)
- `QuerySet.nearby()` validates coordinate types and tuple length
- `SurrealFixture.load()` uses `object.__new__(cls)` to avoid type checker warnings
- Version bump to 0.14.0

### Fixed

- **Notebook 05** — `async with SurrealDBConnectionManager.transaction()` → `async with await` (coroutine needs `await` before entering context manager)
- **Notebook 09** — `annotate()` with `Subquery` returns dicts via GROUP ALL path; rewrote to use `values().annotate()` pattern
- **Notebook 11** — SurrealDB cannot parse `$variable` references inside tuple constructors; geo coordinates now inlined in query string
- **Notebook 14** — `@fixture` decorator `isinstance()` failed across dual-import paths; added duck-type fallback check
- **CI pipeline** — Docker Compose service name `surrealdb-ci` → `surrealdb`
- Hardcoded URLs/credentials in 6 test files replaced with shared `conftest` constants

---

## [0.13.0] - 2026-02-10

### Added

- **DEFINE EVENT (Server-Side Triggers)** — Declarative server-side event triggers in migrations
  - `DefineEvent(name, table, when, then)` migration operation — generates `DEFINE EVENT ... WHEN ... THEN (...);`
  - `RemoveEvent(name, table)` migration operation
  - `EventState` dataclass for schema tracking
  - `parse_define_event()` parser for `DEFINE EVENT` statements
  - `SchemaState.diff()` detects event add/change/remove across tables
  - `DatabaseIntrospector` parses events from `INFO FOR TABLE`
  - `ModelCodeGenerator` adds informational event comments in generated models

- **Geospatial Fields & Queries** — Typed geometry fields and distance-based queries
  - `GeoField["point"]`, `GeoField["polygon"]`, `GeoField["linestring"]`, `GeoField["multipoint"]` type annotations
  - Convenience aliases: `PointField`, `PolygonField`, `LineStringField`, `MultiPointField`
  - `is_geo_field()` and `get_geo_info()` detection helpers
  - `QuerySet.nearby(field, point, max_distance)` — proximity filter using `geo::distance()`
  - `GeoDistance(field, point)` annotation helper — compatible with `annotate()` interface
  - `ModelCodeGenerator` generates `GeoField["X"]` for `geometry<X>` database types
  - New files: `src/surreal_orm/fields/geometry.py`, `src/surreal_orm/geo.py`

- **Materialized Views** — Read-only models backed by `DEFINE TABLE ... AS SELECT`
  - `SurrealConfigDict(view_query="SELECT ...")` for view configuration
  - Read-only guard: `save()`, `update()`, `merge()`, `delete()` raise `TypeError` on views
  - `CreateTable(view_query=...)` generates `DEFINE TABLE ... AS (...);`
  - `parse_define_table()` extracts `AS` clause as `view_query`
  - `SchemaState.diff()` detects view_query changes

- **TYPE RELATION Enforcement** — Graph edge constraints in migrations
  - `TableType.RELATION` and `TableType.ANY` added to enum
  - `SurrealConfigDict(relation_in=..., relation_out=..., enforced=True)` for relation config
  - `CreateTable` generates `TYPE RELATION IN table1 OUT table2 ENFORCED`
  - `parse_define_table()` extracts RELATION IN/OUT/ENFORCED clauses
  - `DatabaseIntrospector` populates relation fields from schema

### Changed

- `annotate()` type hint extended to accept `GeoDistance`
- `exec()` integrates geo distance WHERE clause in `_compile_query()`
- `ModelCodeGenerator` generates `view_query=`, `relation_in=`, `relation_out=`, `enforced=` in config
- Version bump to 0.13.0

---

## [0.12.0] - 2026-02-10

### Added

- **Vector Search** — First-class support for AI/RAG pipelines with SurrealDB HNSW indexes
  - `VectorField[dimension]` and `VectorField[dimension, "type"]` type annotations
  - `is_vector_field()` and `get_vector_info()` detection helpers
  - `QuerySet.similar_to(field, vector, limit, *, ef)` — KNN similarity search using `<|N|>` operator
  - Auto-adds `vector::distance::knn() AS _knn_distance` to SELECT and ORDER BY
  - Combined with `.filter()` for pre-filtered similarity search
  - New file: `src/surreal_orm/fields/vector.py`

- **Full-Text Search** — BM25 scoring, highlighting, and multi-field search
  - `QuerySet.search(**field_queries)` — FTS using `@N@` match operator
  - `SearchScore(ref)` — Annotation helper for `search::score(N)` BM25 relevance
  - `SearchHighlight(open_tag, close_tag, ref)` — Annotation helper for `search::highlight()`
  - Multi-field search with auto-incrementing match references
  - Compatible with `.filter()` and `.annotate()` for combined queries
  - New file: `src/surreal_orm/search.py`

- **Hybrid Search** — Reciprocal Rank Fusion combining vector + full-text search
  - `QuerySet.hybrid_search(vector_field, vector, text_field, text_query, ...)` — Uses `search::rrf()` for ranking fusion
  - Configurable `vector_limit`, `text_limit`, `rrf_k` parameters

- **HNSW Index Support** — Full migration support for vector indexes
  - `CreateIndex` expanded with `hnsw`, `dimension`, `dist`, `vector_type`, `efc`, `m`, `concurrently` parameters
  - Generates `DEFINE INDEX ... HNSW DIMENSION N DIST X TYPE Y EFC N M N;`
  - `IndexState` expanded with all HNSW parameters for schema diffing

- **Full-Text Index Support** — BM25 and highlights in migrations
  - `CreateIndex` expanded with `bm25` (bool or tuple) and `highlights` parameters
  - Generates `DEFINE INDEX ... SEARCH ANALYZER name BM25(k1, b) HIGHLIGHTS;`

- **Analyzer Operations** — Define and manage custom text analyzers
  - `DefineAnalyzer(name, tokenizers, filters)` migration operation
  - `RemoveAnalyzer(name)` migration operation
  - `AnalyzerState` dataclass for schema tracking
  - `SchemaState.analyzers` dict with full diff support (add/update/remove)
  - `parse_define_analyzer()` parser for `DEFINE ANALYZER` statements

- **Parser Enhancements** — `parse_define_index()` now parses HNSW, BM25, HIGHLIGHTS, CONCURRENTLY

- **Introspection Updates**
  - `DatabaseIntrospector` now parses analyzers from `INFO FOR DB`
  - `ModelCodeGenerator` generates `VectorField[dim]` annotations for HNSW-indexed fields

### Changed

- `annotate()` type hint extended to accept `SearchScore | SearchHighlight`
- `exec()` attaches extra fields (`_knn_distance`, search scores) to model instances
- Search-only annotations bypass GROUP BY path in `_execute_annotate()`
- Version bump to 0.12.0

---

## [0.11.0] - 2026-02-10

### Added

- **Subqueries** — Embed a QuerySet as a filter value in another QuerySet
  - `Subquery` class wrapping a `QuerySet` for inline sub-SELECT expressions
  - Parameterized variables remapped via shared counter to avoid collisions
  - Works with all filter operators (`__in`, `=`, etc.) and Q objects
  - Nested subqueries supported (subquery within a subquery)
  - Usable in `annotate()` for scalar sub-SELECT expressions
  - New file: `src/surreal_orm/subquery.py`

- **Query Cache** — TTL-based query result caching with auto-invalidation
  - `QueryCache` class-level singleton with `configure()`, `get()`, `set()`, `invalidate()`, `clear()`, `stats()`
  - SHA-256 cache keys from query string + variables + table name
  - FIFO eviction when `max_size` is reached
  - Auto-invalidation via `post_save`, `post_delete`, `post_update` signals
  - `QuerySet.cache(ttl=N)` method for opt-in caching per query
  - New file: `src/surreal_orm/cache.py`

- **Prefetch Objects** — Fine-grained control over `prefetch_related()` batching
  - `Prefetch` class with `relation_name`, `queryset`, and `to_attr` parameters
  - `prefetch_related()` now accepts both strings and `Prefetch` objects
  - `_execute_prefetch()` batch-fetches related records after main query
  - Results attached to parent instances via `object.__setattr__`
  - Custom QuerySet filters applied to the edge table query
  - New file: `src/surreal_orm/prefetch.py`

### Changed

- `annotate()` type hint extended to accept `Aggregation | Subquery`
- `_execute_annotate()` handles Subquery annotations as inline sub-SELECTs
- `_prefetch_related` type changed from `list[str]` to `list[str | Prefetch]`
- `exec()` now checks/stores cache and runs prefetch after main query
- Version bump to 0.11.0

---

## [0.10.0] - 2026-02-10

### Added

- **Schema Introspection** — Generate Python model code from an existing SurrealDB database
  - `generate_models_from_db(output_path=)` public API
  - `schema_diff(models=[...])` to compare Python models against live database
  - `DatabaseIntrospector` — Parses `INFO FOR DB` / `INFO FOR TABLE` into `SchemaState`
  - `ModelCodeGenerator` — Converts `SchemaState` to Python model source code
  - `define_parser` module — Parses DEFINE TABLE, FIELD, INDEX, ACCESS statements
  - Handles generic types, VALUE/ASSERT expressions, encrypted fields, FLEXIBLE, READONLY
  - CLI: `surreal-orm inspectdb` and `surreal-orm schemadiff` commands

- **Multi-Database Support** — Named connection registry for routing models to different databases
  - `ConnectionConfig` frozen dataclass for immutable connection settings
  - `add_connection("name", ...)` to register named connections
  - `using("name")` async context manager override (async-safe via `contextvars`)
  - `SurrealConfigDict(connection="name")` model-level connection routing
  - `get_connection_name()` priority: context var > model config > `"default"`
  - `list_connections()`, `get_config()`, `remove_connection()` registry management
  - Full backward compatibility: `set_connection()` delegates to `add_connection("default", ...)`

### Changed

- All query paths route through named connection (model, queryset, live, auth, relations)
- Version bump to 0.10.0

---

## [0.9.0] - 2026-02-10

### Added

- **Live Models** — ORM-level real-time subscriptions via WebSocket Live Queries
  - `QuerySet.live()` returns a typed async iterator yielding `ModelChangeEvent` instances
  - `ModelChangeEvent[T]` dataclass with `action`, `instance: T`, `record_id`, `changed_fields`, `raw`
  - `LiveModelStream[T]` async context manager + iterator wrapping SDK `LiveSelectStream`
  - Filter integration: QuerySet filters translate to live query WHERE clauses
  - `auto_resubscribe=True` for seamless WebSocket recovery
  - `diff=True` for receiving only changed fields (DIFF mode)

- **Change Feed Integration** — HTTP-based change data capture at the ORM level
  - `QuerySet.changes()` returns a typed async iterator for event streaming
  - `ChangeModelStream[T]` async iterator wrapping SDK `ChangeFeedStream`
  - Cursor tracking via `.cursor` property for resumable streaming
  - Configurable `poll_interval` and `batch_size` parameters

- **`post_live_change` Signal** — New signal fired on live query events

- **WebSocket Connection Manager** — Lazy WebSocket connection alongside HTTP
  - `SurrealDBConnectionManager.get_ws_client()` for manual access

### Changed

- Version bump to 0.9.0

---

## [0.8.0] - 2026-02-08

### Added

- **Computed Fields** — Server-side computed fields using `DEFINE FIELD ... VALUE <expression>`
  - `Computed[T] = Computed("expression")` — dual-use type annotation + default value
  - Auto-excluded from writes via `get_server_fields()`
  - Migration introspector generates VALUE clauses

### Fixed

- **Auth Module: Ephemeral Connections** (Critical) — `signup()`, `signin()`, `authenticate_token()` no longer mutate root singleton connection
- **Auth Module: Configurable Access Name** (High) — Access name configurable via `access_name` in `SurrealConfigDict`
- **Auth Module: `signup()` Returns Token** (High) — Now returns `tuple[Self, str]` (user + JWT)
- **Auth Module: `authenticate_token()` Fixed** (Medium) — Now returns `tuple[Self, str] | None`
  - Added `validate_token()` lightweight method returning `str | None`
  - SDK `authenticate()` RPC method added to `BaseSurrealConnection`

### Changed

- Version bump to 0.8.0

---

## [0.7.0] - 2026-02-08

### Added

- **`merge(refresh=False)`** — Skip the extra SELECT after UPDATE for fire-and-forget operations
- **`call_function()`** — Invoke custom SurrealDB stored functions from ORM
  - Available on `SurrealDBConnectionManager` and any model class
- **`extra_vars` on `save()` and `merge()`** — Bind additional query variables for SurrealFunc expressions
- **`fetch()` + FETCH clause** — Resolve record links inline to avoid N+1 queries
  - `select_related()` also maps to FETCH
- **`remove_all_relations()` with list support** — Remove multiple relation types in one call

### Changed

- Version bump to 0.7.0

---

## [0.6.0] - 2026-02-07

### Added

- **Q Objects for Complex Queries** - Django-style composable query expressions
  - `Q` class with `|` (OR), `&` (AND), and `~` (NOT) operators
  - Mix Q objects with regular kwargs in `filter()` calls
  - Recursive rendering of nested Q trees into SurrealQL WHERE clauses
  - New file: `src/surreal_orm/q.py`

- **Parameterized Filters (Security)** - All filter values bound as query variables
  - Filter values use `$_fN` auto-generated variable names instead of `repr()` string interpolation
  - Prevents SQL injection by never embedding values directly in query strings
  - Existing `$variable` references via `.variables()` remain fully backwards compatible
  - New shared `_render_condition()` method factored from `_compile_query()` and `_compile_where_clause()`

- **SurrealFunc for Server-Side Functions** - Embed raw SurrealQL expressions in save/update
  - `SurrealFunc(expression)` marker class for server-side function calls
  - `save(server_values={"field": SurrealFunc("time::now()")})` for save operations
  - `merge(field=SurrealFunc("time::now()"))` with automatic detection
  - `_build_set_clause()` helper generates `SET field = expression` with mixed parameterized/raw values
  - Falls back to raw `UPSERT/UPDATE ... SET ...` query when SurrealFunc values are present

- **`remove_all_relations()`** - Bulk relation deletion on model instances
  - `direction` parameter: `"out"` (default), `"in"`, or `"both"`
  - Transaction support via `tx` parameter
  - Identifier validation with `_SAFE_IDENTIFIER_RE`
  - Proper ID escaping with `format_thing()`

- **Django-style `-field` Ordering** - Shorthand for descending order
  - `order_by("-created_at")` is equivalent to `order_by("created_at", OrderBy.DESC)`
  - Fully backwards compatible: existing `order_by("field", OrderBy.DESC)` still works

### Fixed

- **`isnull` Lookup** - `filter(field__isnull=True)` now generates `field IS NULL` instead of `field IS True`
  - `filter(field__isnull=False)` correctly generates `field IS NOT NULL`

### Changed

- `_compile_query()` and `_compile_where_clause()` now use shared `_build_where_parts()` method
- Filter rendering extracted into `_render_condition()` static method (was duplicated)
- `_render_q()` added for recursive Q object compilation
- Version bump to 0.6.0

---

## [0.5.9] - 2026-02-07

### Added

- **Atomic Array Operations** — Server-side array mutations
  - `atomic_append(record_id, field, value)` — Append using `array::append()`
  - `atomic_remove(record_id, field, value)` — Remove using `-=` operator
  - `atomic_set_add(record_id, field, value)` — Add only if not present using `+=`

- **Transaction Conflict Retry** — `retry_on_conflict(max_retries=3)` decorator with exponential backoff + jitter
  - `TransactionConflictError` new exception subclass for conflict detection

- **Relation Direction Control** — `reverse` parameter for `relate()` and `remove_relation()`

- **New Query Lookup Operators**
  - `not_contains` → `CONTAINSNOT`
  - `containsall` → `CONTAINSALL`
  - `containsany` → `CONTAINSANY`
  - `not_in` → `NOT IN`

### Changed

- Version bump to 0.5.9

---

## [0.5.8] - 2026-02-05

### Added

- **Around Signals** — Generator-based middleware pattern for wrapping DB operations
  - `around_save`, `around_delete`, `around_update` signal types
  - Shared state between before/after logic via `yield`
  - Guaranteed cleanup with `try/finally`
  - Execution order: `pre_* → around(before) → DB operation → around(after) → post_*`

### Changed

- Version bump to 0.5.8

---

## [0.5.7] - 2026-02-05

### Added

- **Model Signals** — Django-style event hooks for model lifecycle operations
  - Signal types: `pre_save`, `post_save`, `pre_delete`, `post_delete`, `pre_update`, `post_update`
  - `Signal.connect(sender=None)` decorator and method for registering handlers
  - `Signal.send(sender, **kwargs)` for dispatching to handlers
  - `Signal.disconnect()`, `Signal.disconnect_all()`, `Signal.clear()` for cleanup

### Changed

- Version bump to 0.5.7

---

## [0.5.6] - 2026-02-04

### Fixed

- **Issue #8 bis: Numeric ID escaping in relation queries**
  - Fixed `get_related()`, `RelationQuerySet`, and `RelationInfo.get_traversal_query()` for IDs starting with digits
  - Now uses `escape_record_id()` for proper backtick escaping in graph traversal queries

### Changed

- Version bump to 0.5.6

---

## [0.5.5.3] - 2026-02-04

### Fixed

- **Issue #10: RecordId objects in non-id fields not converted to strings**
  - When using CBOR protocol, foreign key fields (like `user_id`, `table_id`) returned `RecordId` objects instead of strings
  - This caused Pydantic validation errors and application failures
  - Fix: `_preprocess_db_record()` now converts RecordId objects in ANY field to strings
  - For the `id` field: extracts just the id part (e.g., `"abc123"`)
  - For other fields: converts to "table:id" format (e.g., `"users:abc123"`)

### Changed

- `_convert_record_id_to_string()` - New helper function for converting RecordId objects
- `_update_from_db()` now also handles RecordId conversion in non-id fields
- Version bump to 0.5.5.3

---

## [0.5.5.2] - 2026-02-04

### Fixed

- **Issue #9 (CRITICAL): datetime_type Pydantic validation regression**
  - In v0.5.5.1, records with datetime fields failed Pydantic validation
  - `from_db()` silently returned dicts instead of model instances
  - Caused widespread application failures ("'dict' object has no attribute 'model_dump'")
  - Root cause: `from_db()` passed raw database data directly to Pydantic without preprocessing
  - Fix: Added `_preprocess_db_record()` method to handle datetime parsing and RecordId conversion

### Added

- **`BaseSurrealModel._preprocess_db_record()`** - New classmethod that preprocesses database records
  - Parses datetime fields in all formats:
    - ISO 8601 strings (JSON protocol)
    - Python datetime objects (CBOR protocol)
    - CBOR timestamp arrays `[seconds, nanoseconds]`
  - Converts RecordId objects from CBOR responses to string IDs
  - Called by `from_db()` before Pydantic validation

### Changed

- `from_db()` now uses `_preprocess_db_record()` for consistent handling with `_update_from_db()`
- Version bump to 0.5.5.2

---

## [0.5.5.1] - 2026-02-04

### Fixed

- **Issue #8 (CRITICAL): Record IDs starting with digits** - IDs like `7abc123` now properly escaped
  - SurrealDB interprets unquoted IDs starting with digits as number tokens, causing parse errors
  - New `escape_record_id()` utility escapes such IDs with backticks: `` `7abc123` ``
  - New `format_thing()` utility generates correct thing references: `` table:`7abc123` ``
  - All CRUD operations (`get()`, `save()`, `update()`, `merge()`, `delete()`) use proper escaping

- **Issue #3 (HIGH): CBOR protocol for HTTP connections**
  - HTTP connections now support and default to CBOR protocol
  - Fixes `data:` prefix strings being misinterpreted as record links
  - `HTTPConnection` accepts `protocol` parameter: `"cbor"` (default) or `"json"`
  - `SurrealDBConnectionManager.set_connection()` accepts `protocol` parameter

- **Issue #1 (HIGH): `QuerySet.get()` handles full record ID format**
  - `get("table:id")` now correctly extracts the ID part and queries the correct table
  - New `parse_record_id()` utility handles both formats: `"abc123"` and `"table:abc123"`
  - Handles escaped IDs correctly: `` "table:`7abc`" `` → `"7abc"`

- **Issue #7 (MEDIUM): `get_related()` with `direction="in"` returns correct results**
  - Changed from unreliable `SELECT ... FETCH` to `SELECT VALUE field.* FROM ... WHERE ...`
  - Both `direction="in"` and `direction="out"` now return full related records

- **`update()` table name bug** - Fixed incidental bug using class name instead of `table_name`
  - Was using `self.__class__.__name__` instead of `self.get_table_name()`
  - Models with custom `table_name` config now update correctly

### Added

- **New utility module** - `src/surreal_orm/utils.py` with record ID handling functions:
  - `needs_id_escaping(record_id)` - Check if ID needs backtick escaping
  - `escape_record_id(record_id)` - Escape ID with backticks if needed
  - `format_thing(table, record_id)` - Generate proper SurrealDB thing reference
  - `parse_record_id(full_id)` - Parse `table:id` format into components

### Changed

- HTTP connections default to CBOR protocol (same as WebSocket)
- All ORM CRUD methods use `format_thing()` for consistent ID handling
- Version bump to 0.5.5.1

---

## [0.5.5] - 2026-02-03

### Added

- **CBOR Protocol Support** - Binary protocol for WebSocket connections (Issue #3)
  - `cbor2` is now a **required dependency** (previously optional)
  - CBOR is the **default protocol** for WebSocket connections
  - Properly handles strings with `data:` prefix (no longer misinterpreted as record links)
  - Custom CBOR tags for SurrealDB types: `RecordId`, `Table`, `Duration`, `DateTime`, `UUID`, `Decimal`
  - `RPCRequest.to_cbor()` and `RPCResponse.from_cbor()` methods
  - Aligns with official SurrealDB SDK protocol behavior

- **`unset_connection_sync()` Method** (Issue #4)
  - Synchronous version of `unset_connection()` for non-async contexts
  - Useful in `atexit` handlers, `__del__` methods, and synchronous cleanup code
  - Clears connection settings without async `close()` call

- **Field Alias Support** (Issue #6)
  - Map Python field names to different database column names
  - Uses Pydantic `Field(alias="db_column_name")` syntax
  - `populate_by_name=True` set by default in `BaseSurrealModel`
  - All `model_dump()` calls use `by_alias=True` for database operations

### Changed

- **CBOR is now required** - `cbor2>=5.6.0` moved from optional to required dependency
- **CBOR is the default protocol** - WebSocket connections use `protocol="cbor"` by default
- Removed `[cbor]` optional extra from `pyproject.toml`
- Updated `WebSocketConnection` docstring to reflect CBOR as recommended protocol
- Version bump to 0.5.5

### Fixed

- **Issue #3** - Strings with `data:` prefix (like base64 images) no longer misinterpreted as record links
  - JSON protocol interprets `data:xxx` as `record<data>` type
  - CBOR protocol properly encodes strings without interpretation

---

## [0.5.4] - 2026-02-03

### Added

- **Record ID format handling** - `QuerySet.get()` now automatically handles both formats:
  - Just the ID: `get("abc123")`
  - Full SurrealDB format: `get("players:abc123")`

- **`remove_relation()` accepts string IDs** - Now accepts both model instances and string IDs:
  - Model instance: `await table.remove_relation("has_player", player_instance)`
  - String ID (full format): `await table.remove_relation("has_player", "players:abc123")`
  - String ID (just ID): `await table.remove_relation("has_player", "abc123")`

- **`raw_query()` class method** - Execute arbitrary SurrealQL queries from model class

### Changed

- Version bump to 0.5.4

---

## [0.5.3.3] - 2026-02-02

### Fixed

- **`from_db()` fields_set** — Fixed bug where `from_db()` marked all fields as "user-set", causing `exclude_unset=True` to include DB-loaded fields in subsequent saves. Now `from_db()` clears `__pydantic_fields_set__`.

### Changed

- Version bump to 0.5.3.3

---

## [0.5.3.2] - 2026-02-02

### Fixed

- **QuerySet table name** — Fixed critical bug where QuerySet used class name instead of configured `table_name` from `model_config`

### Changed

- `QuerySet.get()` signature now accepts both `id=` keyword and positional `id_item` parameter
- Version bump to 0.5.3.2

---

## [0.5.3.1] - 2026-02-02

### Fixed

- **Partial updates for persisted records** — `save()` now uses `merge()` for already-persisted records, only sending explicitly modified fields
- **datetime parsing from DB** — `_update_from_db()` auto-parses ISO 8601 datetime strings
- **`_db_persisted` flag** — Tracks new vs persisted records for correct save behavior

### Changed

- Version bump to 0.5.3.1

---

## [0.5.3] - 2026-02-02

### Added

- **`upsert()` Method** - New SDK method for create-or-update operations
  - Added to `BaseSurrealConnection` and all transaction types
  - Enables idempotent save behavior for existing records

- **`server_fields` Config Option** - Exclude server-generated fields from saves
  - New `server_fields` option in `SurrealConfigDict`
  - Automatically excludes fields like `created_at`, `updated_at` from save/update
  - Prevents datetime serialization errors on subsequent saves

- **`_update_from_db()` Helper** - Internal method for DB data loading
  - Updates model fields without marking them as user-set
  - Preserves `__pydantic_fields_set__` for correct `exclude_unset` behavior

### Fixed

- **Bug #2: NULL Values** - `exclude_unset=True` now works after loading from DB
  - `_update_from_db()` preserves original `__pydantic_fields_set__`
  - Subsequent saves don't include DB-loaded fields as user-set

- **Bug #7: save() Returns New Instance** - Now updates original instance
  - `save()` updates `self` attributes in place instead of returning new object
  - Uses `_update_from_db()` for consistent field handling

- **Bug #8: datetime Serialization for UPDATE** - Fixed via `server_fields`
  - Server-generated datetime fields excluded from save operations
  - No more "expected datetime" errors on second save

- **Bug #9: merge() Returns None** - Now returns `self`
  - `merge()` method signature updated to return `Self`
  - Updates local instance with merged data before returning

### Changed

- **`save()` Uses Upsert** - Idempotent behavior for existing records
  - When ID is set, `save()` now uses `upsert` instead of `create`
  - No more "record already exists" errors on duplicate saves
  - **Breaking:** save() with duplicate ID now updates instead of failing

- Version bump to 0.5.3

---

## [0.5.2] - 2026-02-01

### Added

- **FieldType Enum Improvements** - Enhanced type system for migrations
  - Added `NUMBER`, `SET`, `REGEX` types to `FieldType` enum
  - `generic(inner_type)` method for parameterized types (`array<string>`, `record<users>`)
  - `from_python_type(type)` class method for automatic type mapping
  - Comprehensive docstrings with SurrealDB type documentation

- **Migration Type Validation** - Strict type checking for field definitions
  - `AddField` and `AlterField` now accept `FieldType | str`
  - Validation rejects invalid type strings
  - Support for generic types and union types

### Fixed

- **datetime Serialization** (Bug #1) - Custom JSON encoder for RPC requests
  - `SurrealJSONEncoder` handles `datetime`, `date`, `time`, `Decimal`, `UUID`
  - HTTP connection now uses pre-encoded JSON to preserve custom types

- **NULL Values Override** (Bug #2) - Optional fields no longer override DB defaults
  - Changed `model_dump()` to use `exclude_unset=True` in save/update operations
  - Unset optional fields are not sent to the database

- **Fluent API** (Bug #3) - `connect()` now returns `Self` for method chaining
  - `HTTPConnection.connect()` and `WebSocketConnection.connect()` return `self`
  - Enables patterns like `await (await conn.connect()).signin(...)`

- **Session Cleanup** (Bug #5) - WebSocket callback tasks properly managed
  - Added `_callback_tasks` set to track active tasks
  - Tasks are cancelled and cleared in `_cleanup()` method

- **Parameter Alias** (Bug #4) - `username` alias for `user` parameter
  - `SurrealDBConnectionManager.set_connection()` accepts both `user` and `username`
  - Raises `ValueError` if neither is provided

- **Patch Version Increment** - Fixed version calculation for security patches
  - Now extracts semantic base version (x.y.z) from current version
  - Correctly increments to x.y.z.1, x.y.z.2, etc. instead of x.y.z.1.1

### Changed

- Version bump to 0.5.2

---

## [0.5.1] - 2026-02-01

### Added

- **Dependabot Security Workflows** - Automated dependency updates
  - Auto-merge for Dependabot PRs after CI passes
  - Patch version tagging (x.x.x.1, x.x.x.2, etc.) for security updates
  - Automatic GitHub releases for security patches

- **SurrealDB Security Monitoring** - Database vulnerability tracking
  - Daily checks for new SurrealDB releases
  - Automatic integration tests with new DB versions
  - Auto-update of DB requirements on security patches
  - Issue creation for compatibility failures

### Changed

- Version bump to 0.5.1

---

## [0.5.0] - 2026-02-01

### Added

- **Live Select Stream** - Async iterator pattern for real-time change notifications
  - `LiveSelectStream` class with `async with` context manager and `async for` iteration
  - `LiveChange` dataclass with `record_id`, `action`, `result`, `changed_fields`
  - `LiveAction` enum: `CREATE`, `UPDATE`, `DELETE`
  - WHERE clause support with parameterized queries
  - DIFF mode support for receiving only changed fields

- **Live Select Manager** - Callback-based multi-stream management
  - `LiveSelectManager` for managing multiple concurrent subscriptions
  - `watch(table, callback, where, params)` method for callback-based watching
  - `stop(live_id)` and `stop_all()` for subscription management
  - `active_streams` and `count` properties

- **Auto-Resubscribe** - Automatic Live Query reconnection
  - `auto_resubscribe` parameter for automatic reconnection after WebSocket disconnect
  - `LiveSubscriptionParams` dataclass for storing subscription state
  - `on_reconnect(old_id, new_id)` callback for resubscription notifications
  - Seamless recovery on K8s pod restarts and network interruptions

- **Typed Function Calls** - Pydantic/dataclass return type support
  - `call(function, params, return_type)` method on connections
  - Automatic conversion to Pydantic models and dataclasses
  - `fn::` prefix auto-normalization for user-defined functions
  - Support for built-in function namespaces (math::, time::, etc.)

### Changed

- Version bump to 0.5.0
- SDK `__init__.py` updated with new exports

---

## [0.4.0] - 2026-02-01

### Added

- **Relation Field Types** - Declarative relation definitions
  - `ForeignKey(to, on_delete, related_name)` - Single record reference
  - `ManyToMany(to, through, related_name)` - Multiple record reference
  - `Relation(edge, to, reverse)` - SurrealDB graph edges

- **Relation Operations** - Django-style relation management
  - `RelationManager` class for lazy loading and operations
  - `add(*objects)`, `remove(*objects)`, `set(objects)`, `clear()` methods
  - `all()`, `filter(**kwargs)`, `count()`, `contains(obj)` query methods

- **Graph Traversal** - SurrealDB graph capabilities
  - `RelationQuerySet` for chained multi-hop traversal
  - `graph_query(traversal)` for raw graph queries on QuerySet
  - `traverse(path)` method for graph path queries

- **Prefetch Related** - N+1 query prevention
  - `select_related(*relations)` - Eager load in same query
  - `prefetch_related(*relations)` - Separate optimized queries

- **Model Methods** - Low-level graph operations
  - `relate(relation, to, tx, **edge_data)` - Create graph edge
  - `remove_relation(relation, to, tx)` - Delete graph edge
  - `get_related(relation, direction, model_class)` - Query edges

- **Relation Helpers**
  - `is_relation_field()`, `is_foreign_key()`, `is_many_to_many()`, `is_graph_relation()`
  - `get_relation_info()` for metadata extraction
  - `RelationInfo` dataclass for relation metadata

### Changed

- Version bump to 0.4.0

---

## [0.3.1] - 2026-02-01

### Added

- **Bulk Operations** - Efficient batch operations
  - `bulk_create(instances, atomic=False, batch_size=None)` - Create multiple records
  - `bulk_update(data, atomic=False)` - Update matching records
  - `bulk_delete(atomic=False)` - Delete matching records
  - Transaction support with `atomic=True` for all-or-nothing operations

### Fixed

- ORDER BY clause position (now correctly placed before LIMIT/START in SurrealQL)
- Typo in error message: "primirary_key" corrected to "primary_key"

---

## [0.3.0] - 2026-02-01

### Added

- **ORM Transactions** - Transaction support integrated into ORM layer
  - `tx` parameter added to `save()`, `update()`, `merge()`, `delete()` methods
  - `Model.transaction()` class method shortcut for creating transactions
  - `SurrealDBConnectionManager.transaction()` context manager

- **Aggregations** - Django-style aggregation classes
  - `Count()` - Count records
  - `Sum(field)` - Sum field values
  - `Avg(field)` - Average field values
  - `Min(field)` - Minimum field value
  - `Max(field)` - Maximum field value

- **GROUP BY Support**
  - `values(*fields)` method for specifying GROUP BY fields
  - `annotate(**aggregations)` method for computed aggregations
  - GROUP ALL behavior when `annotate()` used without `values()`

- **QuerySet Aggregation Methods**
  - `count()` - Count matching records
  - `sum(field)` - Sum field values
  - `avg(field)` - Average field values
  - `min(field)` - Minimum field value
  - `max(field)` - Maximum field value

### Changed

- Version bump to 0.3.0

---

## [0.2.2] - 2026-02-01

### Added

- **Atomic Transactions** - Full transaction support with context manager
  - `HTTPTransaction` - Batched execution for stateless connections
  - `WebSocketTransaction` - Immediate execution with server-side transactions
  - Context manager with auto-commit on success, auto-rollback on exception
  - Transaction methods: `query()`, `create()`, `update()`, `delete()`, `relate()`
  - `TransactionError` exception with rollback status tracking

- **Typed Functions API** - Fluent interface for SurrealDB functions
  - `db.fn.namespace.function()` syntax for built-in functions
  - `db.fn.custom_function()` syntax for user-defined functions
  - Support for all built-in namespaces: `array`, `crypto`, `duration`, `geo`, `http`, `math`, `meta`, `object`, `parse`, `rand`, `session`, `string`, `time`, `type`, `vector`
  - Automatic parameter binding with variable namespacing

- **Test Infrastructure Improvements**
  - Automatic SurrealDB container lifecycle management in `conftest.py`
  - Container detection to avoid duplicate instances
  - Graceful container cleanup only when started by tests

### Changed

- Test coverage improved to 68.5%
- Updated SDK documentation with transactions and functions guides

### Fixed

- Mypy `no-any-return` errors in `functions.py`
- Unused imports and variables in test files
- Module path resolution for SDK exceptions in tests

---

## [0.2.1] - 2026-02-01

### Added

- **Django-style Migration System** - Complete migration framework for schema versioning
  - `MigrationGenerator` - Generates Python migration files from model changes
  - `MigrationExecutor` - Applies/rolls back migrations to database
  - `ModelIntrospector` - Extracts schema state from Pydantic models
  - `SchemaState` with diff algorithm to compute operations between states
  - Migration operations: `CreateTable`, `DropTable`, `AddField`, `DropField`, `AlterField`, `CreateIndex`, `DropIndex`, `DefineAccess`, `RemoveAccess`, `DataMigration`, `RawSQL`

- **CLI for Schema Management** (`surreal-orm` command, requires `pip install surrealdb-orm[cli]`)
  - `makemigrations` - Generate migration files from model changes
  - `migrate` - Apply pending schema migrations
  - `upgrade` - Apply data migrations (record transformations)
  - `rollback <target>` - Rollback to a specific migration
  - `status` - Show migration status
  - `sqlmigrate <migration>` - Show SQL without executing
  - `shell` - Interactive SurrealQL shell

- **Table Type Classification** (`TableType` enum)
  - `NORMAL` - Standard table (default)
  - `USER` - Authentication table (enforces SCHEMAFULL, generates DEFINE ACCESS)
  - `STREAM` - Real-time table with CHANGEFEED
  - `HASH` - Lookup/cache table (defaults to SCHEMALESS)

- **Encrypted Field Type** for password hashing
  - `Encrypted` - Type alias for encrypted string fields
  - `EncryptedField(algorithm)` - Factory for custom encryption algorithms
  - Supported algorithms: `argon2` (default), `bcrypt`, `pbkdf2`, `scrypt`
  - Auto-generates `crypto::<algorithm>::generate()` in schema

- **JWT Authentication Support** using SurrealDB's `DEFINE ACCESS ... TYPE RECORD`
  - `AuthenticatedUserMixin` - Mixin providing `signup()`, `signin()`, `authenticate_token()`, `change_password()`
  - `AccessDefinition` - Dataclass for access configuration
  - `AccessGenerator` - Generates DEFINE ACCESS from USER models
  - Configurable token/session durations

- **Extended Model Configuration** (`SurrealConfigDict`)
  - `table_type` - USER, STREAM, HASH, NORMAL
  - `schema_mode` - SCHEMAFULL / SCHEMALESS
  - `changefeed` - Duration string (e.g., "7d")
  - `permissions` - Row-level security expressions
  - `identifier_field` - Field for signin (default: "email")
  - `password_field` - Field for password (default: "password")
  - `token_duration` / `session_duration` - JWT lifetimes

- **Model Registry** - Auto-registration of models via `__init_subclass__`
  - `get_registered_models()` - Get all registered model classes
  - `clear_model_registry()` - Clear for testing

- **New Test Suites**
  - `test_types.py` - Tests for enums (14 tests)
  - `test_encrypted_field.py` - Tests for Encrypted type (18 tests)
  - `test_migrations_operations.py` - Tests for operations (40 tests)
  - `test_migrations_state.py` - Tests for state/diff (25 tests)
  - `test_migrations_introspector.py` - Tests for introspection (15 tests)
  - `test_migrations_generator.py` - Tests for file generation (17 tests)
  - `test_cli.py` - CLI command tests (21 tests)
  - `test_auth_unit.py` - Auth unit tests (37 tests)
  - `test_migrations_integration.py` - Integration tests (requires SurrealDB)
  - `test_auth_integration.py` - Auth integration tests (requires SurrealDB)

### Changed

- Test coverage improved from ~44% to 56%
- Added `click>=8.1.0` as optional dependency for CLI

### Fixed

- Mypy type errors in auth mixins and migration modules
- Ruff linting issues across codebase
- Dataclass property override issue in `AlterField` operation

---

## [0.2.0] - 2026-02-01

### Added

- **New `surreal_sdk` package** - Standalone SDK that can be published separately to PyPI as `surreal-sdk`
  - Has its own `pyproject.toml` and `README.md`
  - Can be installed independently: `pip install surreal-sdk`

- **New `surreal_sdk` module** - Custom Python SDK for SurrealDB independent of the official `surrealdb` package
  - `HTTPConnection` - Stateless HTTP-based connection, ideal for microservices and serverless
  - `WebSocketConnection` - Stateful WebSocket connection for real-time features and Live Queries
  - `ConnectionPool` - Connection pooling for high-throughput scenarios
  - `LiveQuery` and `LiveQueryManager` - Real-time Live Query subscriptions (WebSocket only)
  - `ChangeFeedStream` and `MultiTableChangeFeed` - CDC pattern for stateless microservices (HTTP)
  - `RPCRequest`, `RPCResponse`, `RPCError` - RPC protocol layer

- **Strongly-typed response classes** (replacing `Any` returns)
  - `QueryResponse` / `QueryResult` - For query operations
  - `RecordResponse` / `RecordsResponse` - For record CRUD operations
  - `AuthResponse` - For signin/signup authentication
  - `InfoResponse` - For database info operations
  - `DeleteResponse` - For delete operations
  - `LiveQueryId` - For live query UUID handling
  - `ResponseStatus` - Enum for OK/ERR status

- **Exception classes** for SDK
  - `SurrealDBError`, `ConnectionError`, `AuthenticationError`
  - `QueryError`, `TimeoutError`, `LiveQueryError`, `ChangeFeedError`

### Changed

- **🚀 ORM now uses custom SDK** - Removed dependency on official `surrealdb` package
  - `SurrealDBConnectionManager` now uses `HTTPConnection` from `surreal_sdk`
  - All ORM methods adapted to use typed responses (`RecordResponse`, `RecordsResponse`, etc.)
  - Removed `surrealdb>=0.4.1` from dependencies

- **Python version support**: Now requires Python 3.12+ (dropped 3.11, added 3.14)
- Updated `pyproject.toml` classifiers and tool configurations for Python 3.12/3.13/3.14
- Coverage threshold reduced to 35% to accommodate new SDK code

### Removed

- **Dependency on `surrealdb` package** - No longer required, fully replaced by custom `surreal_sdk`

### Fixed

- ID parsing from SurrealDB format (`table:id` → `id`)
- Test isolation with dedicated model classes per test

---

## [0.1.5] - 2025-01-20

- Add
  - Support Model.DoesNotExist Error

## [0.1.4] - 2025-01-20

- Improvement
  - New class init based on the pydantic 2.0 validators.

- Changed
  - Required id or primary_key model_config

- Fixed
  - Packaging setup.
  - Pydantic validators can be uses on the class
  - Error message on primary key with use of SurrealConfigDict

---

## [0.1.3] - 2025-01-07

- Fixed
  - Packaging fix.

---

## [0.1.0] - 2025-01-07

- Added
  - Init version with basic "Django" like ORM.
  - Basic test and standard form has been set.

---
*Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and compliant with [SemVer](https://semver.org/).*
