# Surreal SDK

A custom Python SDK for SurrealDB with HTTP and WebSocket support.

**No dependency on the official `surrealdb` package** - this is a standalone implementation.

## Installation

```bash
pip install surreal-sdk
```

## Quick Start

### HTTP Connection (Stateless)

```python
from surreal_sdk import SurrealDB

async with SurrealDB.http("http://localhost:8000", "namespace", "database") as db:
    await db.signin("root", "root")

    # Query
    result = await db.query("SELECT * FROM users WHERE age > $min_age", {"min_age": 18})
    print(result.all_records)

    # CRUD
    user = await db.create("users", {"name": "Alice", "age": 30})
    await db.update("users:alice", {"age": 31})
    await db.delete("users:alice")
```

### WebSocket Connection (Stateful, Real-time)

```python
from surreal_sdk import SurrealDB

async with SurrealDB.ws("ws://localhost:8000", "namespace", "database") as db:
    await db.signin("root", "root")

    # Live Query
    async def on_change(data):
        print(f"Change detected: {data}")

    live_id = await db.live("users", on_change)
    # ... do work ...
    await db.kill(live_id)
```

### Connection Pool

```python
from surreal_sdk import SurrealDB

async with SurrealDB.pool("http://localhost:8000", "ns", "db", size=10) as pool:
    await pool.set_credentials("root", "root")

    async with pool.acquire() as conn:
        result = await conn.query("SELECT * FROM users")
```

## Features

- **HTTPConnection** - Stateless, ideal for microservices and serverless
- **WebSocketConnection** - Stateful, for real-time features and Live Queries
- **ConnectionPool** - Connection pooling for high-throughput scenarios
- **Typed Responses** - `QueryResponse`, `RecordResponse`, `RecordsResponse`, etc.
- **Live Queries** - Real-time subscriptions (WebSocket only)
- **Change Feeds** - CDC pattern for stateless architectures (HTTP)

## Requirements

- Python 3.12+
- httpx
- aiohttp

## License

MIT
