# SurrealDB-ORM - Contexte de Développement

> Document de contexte pour Claude AI - Dernière mise à jour: Janvier 2026

## Vision du Projet

**Objectif initial:** ORM style Django pour SurrealDB utilisant le SDK Python officiel.

**Nouvelle direction:** Créer un SDK complet qui se connecte directement via WebSocket ou autres protocoles supportés par SurrealDB, car le SDK actuel (`surrealdb` package) est jugé trop basique.

---

## Architecture Actuelle (v0.1.5 Alpha)

```text
src/surreal_orm/
├── __init__.py              # Exports publics de l'API
├── connection_manager.py    # Singleton de connexion à SurrealDB
├── model_base.py            # Classe de base ORM (style Django)
├── query_set.py             # Builder de requêtes fluent
├── constants.py             # Mapping des opérateurs lookup
├── enum.py                  # Enums OrderBy et Operator
├── utils.py                 # Utilitaires pour les requêtes
├── surreal_function.py      # Enums des fonctions SurrealDB (NEW)
└── surreal_ql.py            # Builder SurrealQL bas niveau (NEW)
```

---

## Composants Clés

### 1. ConnectionManager (`connection_manager.py`)

**Pattern:** Singleton statique avec classmethods

**Fonctionnement:**

- Connexion lazy (créée au premier `get_client()`)
- Stocke URL, user, password, namespace, database
- Utilise `AsyncSurrealDB` du SDK officiel
- Support context manager async

**Méthodes principales:**

```python
set_connection(url, user, password, namespace, database)
get_client() -> AsyncSurrealDB
close_connection()
validate_connection() -> bool
```

**Limitations actuelles:**

- Une seule connexion globale
- Pas de pooling
- Commentaires en français dans le code

---

### 2. BaseSurrealModel (`model_base.py`)

**Pattern:** Héritage Pydantic BaseModel + méthodes CRUD async

**Caractéristiques:**

- Requiert `id: Optional[str]` OU `primary_key` dans `model_config`
- Exception personnalisée `Model.DoesNotExist`
- Conversion automatique des RecordID SurrealDB

**Méthodes d'instance:**

```python
async save() -> Self           # Insert ou update
async update() -> Any          # Update tous les champs
async delete() -> None         # Supprime le record
async merge(**data) -> Any     # Update partiel
async refresh() -> None        # Recharge depuis la DB (BUG: ne met pas à jour l'instance)
get_id() -> str | RecordID | None
```

**Méthodes de classe:**

```python
get_table_name() -> str        # Nom de la table (défaut: nom de la classe)
objects() -> QuerySet          # Retourne le query builder
from_db(record) -> Self        # Parse la réponse SurrealDB
```

**BUG connu:** `refresh()` ligne ~105 ne réassigne pas les données à l'instance

---

### 3. QuerySet (`query_set.py`)

**Pattern:** Builder fluent chainable

**Construction de requête:**

```python
.select(*fields)              # Colonnes à sélectionner
.filter(**kwargs)             # WHERE avec lookups (__gt, __contains, etc.)
.limit(n)                     # LIMIT
.offset(n)                    # START (offset SurrealQL)
.order_by(field, OrderBy)     # ORDER BY
.variables(**kwargs)          # Variables paramétrées
```

**Exécution:**

```python
async .exec() -> List[Model]   # Exécute filter
async .all() -> List[Model]    # Tous les records
async .first() -> Model        # Premier résultat (raise DoesNotExist)
async .get(id) -> Model        # Par ID ou résultat unique
async .query(sql, vars)        # SurrealQL brut
async .delete_table()          # Supprime la table
```

**Opérateurs lookup supportés:**

```python
exact, gt, gte, lt, lte, in, like, ilike, contains, icontains,
startswith, istartswith, endswith, iendswith, match, regex, iregex, isnull
```

**BUG connu:** ORDER BY mal positionné dans le SQL généré (après LIMIT/START)

---

### 4. Nouveaux Fichiers (Non intégrés)

#### `surreal_function.py`

Enums pour les fonctions built-in SurrealDB:

- `SurrealArrayFunction` - opérations sur tableaux
- `SurrealTimeFunction` - fonctions temporelles
- `SurrealMathFunction` - fonctions mathématiques (50+)

#### `surreal_ql.py`

Builder SurrealQL bas niveau - incomplet/placeholder:

- Méthodes: `select()`, `related()`, `from_tables()`
- Pas d'exécution, juste construction de string

---

## Dépendances

**Runtime:**

- `pydantic >= 2.10.5`
- `surrealdb >= 0.4.1` (SDK officiel - limité)

**Dev:**

- pytest, pytest-asyncio, pytest-cov
- mypy, black, ruff, isort
- docker (pour tests e2e)

---

## Limitations Majeures à Adresser

### Fonctionnalités Manquantes

1. **Relations** - Pas de ForeignKey, ManyToMany, OneToOne
2. **Agrégations** - Pas de count(), sum(), avg(), GROUP BY
3. **Transactions** - Non supportées
4. **Multi-connexion** - Une seule connexion globale
5. **Pooling** - Pas de pool de connexions

### Bugs Connus

1. `refresh()` ne met pas à jour l'instance
2. ORDER BY mal positionné dans les requêtes générées
3. Typo: "primirary_key" dans un message d'erreur (model_base.py:208)

### Limitations du SDK Officiel

Le SDK `surrealdb` Python est considéré comme:

- Trop basique
- API limitée
- Manque de fonctionnalités avancées

**Direction future:** Créer notre propre couche de connexion WebSocket/HTTP.

---

## Protocoles SurrealDB Supportés

Pour le nouveau SDK à développer:

| Protocole  | Port Défaut | Usage                             |
| ---------- | ----------- | --------------------------------- |
| HTTP/HTTPS | 8000        | REST API, requêtes simples        |
| WebSocket  | 8000        | Connexion persistante, temps réel |
| RPC        | 8000        | Appels de procédures              |

**Endpoints principaux:**

- `/sql` - Exécution de requêtes
- `/rpc` - WebSocket RPC
- `/signup`, `/signin` - Authentification
- `/key/:table/:id` - CRUD REST

---

## Structure des Tests

```text
tests/
├── test_unit.py      # Tests QuerySet, Model
├── test_manager.py   # Tests ConnectionManager
└── test_e2e.py       # Tests d'intégration (nécessite Docker/SurrealDB)
```

**Exécution:**

```bash
uv run pytest                    # Tous les tests
uv run pytest tests/test_unit.py # Tests unitaires
tox                              # Tests multi-versions Python
```

---

## Prochaines Étapes Suggérées

### Phase 1: Correction des Bugs

- [ ] Fix `refresh()` pour mettre à jour l'instance
- [ ] Fix ordre ORDER BY dans le SQL généré
- [ ] Fix typos dans les messages d'erreur

### Phase 2: Nouveau SDK de Connexion

- [ ] Implémenter connexion WebSocket native
- [ ] Implémenter connexion HTTP REST
- [ ] Pool de connexions
- [ ] Support multi-database

### Phase 3: Fonctionnalités ORM

- [ ] Relations (graph traversal SurrealDB)
- [ ] Agrégations
- [ ] Transactions
- [ ] Migrations

---

## Conventions de Code

- **Langue:** Anglais (retirer les commentaires français)
- **Style:** Black + isort + ruff
- **Types:** mypy strict
- **Async:** Tout I/O doit être async
- **Tests:** pytest-asyncio pour les tests async

---

## Commandes Utiles

```bash
# Installation
uv sync

# Tests
make test              # Tests unitaires (sans SurrealDB)
make test-sdk          # Tests SDK uniquement
make test-integration  # Tests d'intégration (démarre SurrealDB)
make test-all          # Tous les tests

# Docker/SurrealDB
make db-up             # Démarre SurrealDB test (port 8001)
make db-dev            # Démarre SurrealDB dev (port 8000, persistant)
make db-cluster        # Démarre cluster 3 noeuds (ports 8002-8004)
make db-shell          # Shell SQL interactif

# Lint
uv run ruff check src/
uv run black src/ --check
uv run mypy src/

# Build
uv build
```

---

## Nouveau SDK Custom (`src/surreal_sdk/`)

### Architecture

```text
src/surreal_sdk/
├── __init__.py              # Factory SurrealDB.http() / .ws() / .pool()
├── exceptions.py            # Exceptions personnalisées
├── connection/
│   ├── base.py              # Interface abstraite BaseSurrealConnection
│   ├── http.py              # HTTPConnection (stateless, httpx)
│   ├── websocket.py         # WebSocketConnection (stateful, aiohttp)
│   └── pool.py              # ConnectionPool
├── protocol/
│   └── rpc.py               # RPCRequest, RPCResponse, RPCError
└── streaming/
    ├── change_feed.py       # ChangeFeedStream (CDC, HTTP)
    └── live_query.py        # LiveQuery (WebSocket temps réel)
```

### Usage Basique

```python
from surreal_sdk import SurrealDB

# HTTP (stateless, microservices)
async with SurrealDB.http("http://localhost:8000", "ns", "db") as conn:
    await conn.signin("root", "root")
    users = await conn.query("SELECT * FROM users")

# WebSocket (stateful, temps réel)
async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as conn:
    await conn.signin("root", "root")
    await conn.live("orders", callback=on_order_change)

# Connection Pool
async with SurrealDB.pool("http://localhost:8000", "ns", "db", size=10) as pool:
    await pool.set_credentials("root", "root")
    async with pool.acquire() as conn:
        await conn.query("SELECT * FROM users")
```

### Streaming

```python
# Change Feeds (HTTP, stateless, microservices)
from surreal_sdk import ChangeFeedStream

stream = ChangeFeedStream(conn, "orders")
async for change in stream.stream():
    print(f"{change['changes']}")

# Live Queries (WebSocket, temps réel)
from surreal_sdk import LiveQuery

live = LiveQuery(ws_conn, "orders")
await live.subscribe(on_change_callback)
```

### Dépendances

- `httpx>=0.27.0` - Client HTTP async
- `aiohttp>=3.9.0` - Client WebSocket
- **Zéro dépendance au SDK officiel `surrealdb`!**

---

## Contact

**Auteur:** Yannick Croteau
**GitHub:** EulogySnowfall
