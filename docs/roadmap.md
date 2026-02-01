# SurrealDB-ORM Roadmap

> Planning document for future ORM features - Last updated: February 2026

---

## Version History

| Version   | Status       | Focus                                 |
| --------- | ------------ | ------------------------------------- |
| 0.1.x     | Released     | Basic ORM (Models, QuerySet, CRUD)    |
| 0.2.x     | Released     | Custom SDK, Migrations, JWT Auth, CLI |
| **0.3.0** | **Released** | **ORM Transactions + Aggregations**   |
| 0.3.1     | Planned      | Bulk Operations                       |
| 0.4.x     | Planned      | Relations & Graph Traversal           |
| 0.5.x     | Planned      | Real-time Features (Live Models)      |

---

## v0.3.0 - Transactions & Aggregations (Released)

**Goal:** Integrate SDK transactions into ORM and add Django-style aggregations.

**Status:** Implemented and released.

### 1. Model-level Transactions

Allow atomic operations across multiple model instances:

```python
# Via ConnectionManager
async with SurrealDBConnectionManager.transaction() as tx:
    user = User(name="Alice", balance=1000)
    await user.save(tx=tx)

    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)

    user.balance -= 100
    await user.save(tx=tx)
    # All-or-nothing: commit on success, rollback on exception

# Via Model class method
async with User.transaction() as tx:
    await user1.save(tx=tx)
    await user2.delete(tx=tx)
```

**Implementation:**

- Add `tx` parameter to `save()`, `delete()`, `merge()`, `update()`
- Add `transaction()` context manager to `SurrealDBConnectionManager`
- Add `Model.transaction()` class method shortcut

### 2. QuerySet Aggregations

Django-style aggregation methods using SDK functions:

```python
# Simple aggregations
count = await User.objects().count()
count = await User.objects().filter(active=True).count()

# Field aggregations
avg_age = await User.objects().avg("age")
total = await Order.objects().filter(status="paid").sum("amount")
min_val = await Product.objects().min("price")
max_val = await Product.objects().max("price")

# With filters
avg = await User.objects().filter(role="premium").avg("lifetime_value")
```

**Implementation:**

- Add `count()`, `sum()`, `avg()`, `min()`, `max()` to QuerySet
- Use SDK's typed functions internally (`fn.count()`, `fn.math.sum()`, etc.)

### 3. GROUP BY Support

Aggregations with grouping:

```python
# Group by single field
stats = await Order.objects().values("status").annotate(
    count=Count(),
    total=Sum("amount"),
)
# Result: [{"status": "paid", "count": 42, "total": 5000}, ...]

# Group by multiple fields
monthly = await Order.objects().values("status", "month").annotate(
    count=Count(),
)
```

**Implementation:**

- Add `values(*fields)` for grouping
- Add `annotate(**aggregations)` for computed values
- Create `Count`, `Sum`, `Avg`, `Min`, `Max` aggregation classes

---

## v0.3.1 - Bulk Operations

**Goal:** Efficient batch operations with transaction support.

### Bulk Create

```python
users = [User(name=f"User{i}") for i in range(1000)]

# Simple bulk create
created = await User.objects().bulk_create(users)

# Atomic bulk create
created = await User.objects().bulk_create(users, atomic=True)

# With batch size (for large datasets)
created = await User.objects().bulk_create(users, batch_size=100)
```

### Bulk Update

```python
# Update all matching records
updated = await User.objects().filter(
    last_login__lt="2025-01-01"
).bulk_update({"status": "inactive"})

# Atomic update
updated = await User.objects().filter(role="guest").bulk_update(
    {"verified": True},
    atomic=True
)
```

### Bulk Delete

```python
# Delete all matching records
deleted = await User.objects().filter(status="deleted").bulk_delete()

# Atomic delete
deleted = await Order.objects().filter(
    created_at__lt="2024-01-01"
).bulk_delete(atomic=True)
```

---

## v0.4.0 - Relations & Graph Traversal

**Goal:** Leverage SurrealDB's graph capabilities with declarative relations.

### Relation Field Types

```python
from surreal_orm import BaseSurrealModel, ForeignKey, ManyToMany, Relation

class User(BaseSurrealModel):
    name: str

    # Graph relations (SurrealDB edges)
    followers = Relation("follows", "User", reverse=True)
    following = Relation("follows", "User")

    # Traditional relations
    profile = ForeignKey("Profile", on_delete="CASCADE")
    groups = ManyToMany("Group", through="membership")

class Post(BaseSurrealModel):
    title: str
    author = ForeignKey("User", related_name="posts")
    tags = ManyToMany("Tag")
```

### Relation Operations

```python
# Add/remove relations
await alice.following.add(bob)
await alice.following.remove(bob)
await alice.following.set([bob, charlie])
await alice.following.clear()

# Query relations
followers = await alice.followers.all()
followers = await alice.followers.filter(active=True)
count = await alice.followers.count()

# Check membership
is_following = await alice.following.contains(bob)
```

### Graph Traversal

```python
# Multi-hop traversal
friends_of_friends = await alice.following.following.all()

# With filters at each level
active_fof = await alice.following.filter(active=True).following.filter(active=True).all()

# Raw graph query
result = await User.objects().graph_query(
    "->follows->User->follows->User WHERE active = true"
)
```

### Prefetch Related

```python
# Eager loading to avoid N+1
users = await User.objects().prefetch_related("followers", "posts").all()

for user in users:
    print(user.followers)  # Already loaded, no extra query
    print(user.posts)      # Already loaded
```

---

## v0.4.1 - Computed Fields

**Goal:** Server-side computed fields using SurrealDB functions.

### Computed Field Definition

```python
from surreal_orm import BaseSurrealModel, Computed

class Order(BaseSurrealModel):
    items: list[dict]  # [{"price": 10, "qty": 2}, ...]
    discount: float = 0.0

    # Computed at read time
    subtotal: Computed[float] = Computed("math::sum(items.*.price * items.*.qty)")
    total: Computed[float] = Computed("subtotal * (1 - discount)")
    item_count: Computed[int] = Computed("array::len(items)")

class User(BaseSurrealModel):
    first_name: str
    last_name: str

    # String computation
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

    # Using meta functions
    created_date: Computed[str] = Computed("time::format(meta::created(), '%Y-%m-%d')")
```

### Computed with Functions API

```python
from surreal_orm.functions import fn

class Product(BaseSurrealModel):
    name: str
    price: float

    # Using typed function builders
    price_formatted: Computed[str] = Computed(
        fn.string.concat("$", fn.string.format(price, "%.2f"))
    )
```

---

## v0.5.0 - Real-time Features

**Goal:** Live model synchronization and event-driven architecture.

### Live Models

```python
from surreal_orm import LiveAction

# Async iterator for changes
async for event in User.objects().filter(role="admin").live():
    if event.action == LiveAction.CREATE:
        print(f"New admin: {event.instance.name}")
    elif event.action == LiveAction.UPDATE:
        print(f"Admin updated: {event.instance}")
    elif event.action == LiveAction.DELETE:
        print(f"Admin removed: {event.instance.id}")

# Callback style
async def on_user_change(event):
    await notify_admin(event)

subscription = await User.objects().live(callback=on_user_change)
# ... later
await subscription.unsubscribe()
```

### Model Signals

```python
from surreal_orm.signals import pre_save, post_save, pre_delete, post_delete

@post_save.connect(User)
async def on_user_saved(sender, instance, created):
    if created:
        await send_welcome_email(instance.email)
    await update_search_index(instance)

@pre_delete.connect(Order)
async def on_order_deleting(sender, instance):
    await archive_order(instance)

@post_delete.connect(User)
async def on_user_deleted(sender, instance):
    await cleanup_user_data(instance.id)
```

### Change Feed Integration

```python
# For event-driven microservices
async for change in User.objects().changes(since="2026-01-01"):
    event = {
        "type": f"user.{change.action.lower()}",
        "data": change.record,
        "timestamp": change.timestamp,
    }
    await publish_to_queue(event)
```

---

## v0.6.0 - Advanced Features (Future)

### Schema Introspection

```python
# Generate models from existing database
await generate_models_from_db(
    output_dir="models/",
    tables=["users", "orders", "products"],
)
```

### Multi-Database Support

```python
class User(BaseSurrealModel):
    class Meta:
        database = "users_db"

class Order(BaseSurrealModel):
    class Meta:
        database = "orders_db"

# Or runtime switching
async with SurrealDBConnectionManager.using("analytics_db"):
    stats = await AnalyticsEvent.objects().all()
```

### Query Optimization

```python
# Explain query plan
plan = await User.objects().filter(age__gt=18).explain()
print(plan.indexes_used)
print(plan.estimated_cost)

# Query hints
users = await User.objects().filter(age__gt=18).using_index("idx_age").all()
```

---

## Implementation Priority

| Feature                      | Version | Priority | Complexity | Dependencies     |
| ---------------------------- | ------- | -------- | ---------- | ---------------- |
| Model Transactions           | 0.3.0   | Critical | Medium     | SDK transactions |
| Aggregations (count/sum/avg) | 0.3.0   | High     | Low        | SDK functions    |
| GROUP BY                     | 0.3.0   | High     | Medium     | Aggregations     |
| Bulk Operations              | 0.3.1   | Medium   | Low        | Transactions     |
| Relations (ForeignKey)       | 0.4.0   | High     | High       | -                |
| Graph Traversal              | 0.4.0   | High     | High       | Relations        |
| Computed Fields              | 0.4.1   | Medium   | Low        | SDK functions    |
| Live Models                  | 0.5.0   | Medium   | Medium     | SDK live queries |
| Signals                      | 0.5.0   | Low      | Medium     | Live Models      |

---

## Contributing

Want to help implement these features? Check out:

1. [Contributing Guide](../CONTRIBUTING.md)
2. [GitHub Issues](https://github.com/EulogySnowfall/SurrealDB-ORM/issues)
3. [Discussion Board](https://github.com/EulogySnowfall/SurrealDB-ORM/discussions)

---

_This roadmap is subject to change based on community feedback and SurrealDB updates._
