"""
Factory Boy-style model factories for SurrealDB ORM.

Provides ``ModelFactory`` and ``Faker`` for generating test data with
realistic random values. No external dependencies — uses Python's
built-in ``random`` and ``uuid`` modules.

Example::

    from surreal_orm.testing import ModelFactory, Faker

    class UserFactory(ModelFactory):
        class Meta:
            model = User

        name = Faker("name")
        email = Faker("email")
        age = Faker("random_int", min=18, max=80)
        role = "player"

    # Build without saving (unit tests)
    user = UserFactory.build()

    # Create and save to DB (integration tests)
    user = await UserFactory.create()
    users = await UserFactory.create_batch(50)

    # Override fields
    admin = await UserFactory.create(role="admin")
"""

from __future__ import annotations

import random
import string
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from surreal_orm.model_base import BaseSurrealModel

# ---------------------------------------------------------------------------
# Name / word pools for realistic fake data
# ---------------------------------------------------------------------------

_FIRST_NAMES = [
    "Alice",
    "Bob",
    "Charlie",
    "Diana",
    "Eve",
    "Frank",
    "Grace",
    "Hank",
    "Ivy",
    "Jack",
    "Karen",
    "Leo",
    "Mia",
    "Nathan",
    "Olivia",
    "Paul",
    "Quinn",
    "Rose",
    "Sam",
    "Tina",
    "Uma",
    "Victor",
    "Wendy",
    "Xavier",
    "Yuki",
    "Zoe",
    "Liam",
    "Emma",
    "Noah",
    "Sophia",
]

_LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
]

_DOMAINS = [
    "example.com",
    "test.org",
    "demo.net",
    "sample.io",
    "mock.dev",
]

_WORDS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
]


# ---------------------------------------------------------------------------
# Faker — lightweight fake data provider
# ---------------------------------------------------------------------------


class Faker:
    """
    Lightweight fake data field descriptor.

    Generates random data for common types without external dependencies.

    Supported providers:
        - ``name`` — Random full name (e.g., "Alice Smith")
        - ``first_name`` — Random first name
        - ``last_name`` — Random last name
        - ``email`` — Random email address
        - ``random_int`` — Random integer (kwargs: ``min``, ``max``)
        - ``random_float`` — Random float (kwargs: ``min``, ``max``)
        - ``text`` — Random text paragraph (kwargs: ``max_length``)
        - ``sentence`` — Random sentence
        - ``word`` — Random single word
        - ``uuid`` — Random UUID4 string
        - ``boolean`` — Random bool
        - ``date`` — Random date within last year
        - ``datetime`` — Random datetime within last year
        - ``choice`` — Random choice from list (kwargs: ``items``)

    Example::

        name = Faker("name")
        age = Faker("random_int", min=18, max=80)
        tag = Faker("choice", items=["tech", "science", "art"])
    """

    def __init__(self, provider: str, **kwargs: Any) -> None:
        self.provider = provider
        self.kwargs = kwargs

    def generate(self) -> Any:
        """Generate a random value using the configured provider."""
        method = getattr(self, f"_gen_{self.provider}", None)
        if method is None:
            raise ValueError(f"Unknown Faker provider: {self.provider!r}")
        return method()

    def _gen_name(self) -> str:
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    def _gen_first_name(self) -> str:
        return random.choice(_FIRST_NAMES)

    def _gen_last_name(self) -> str:
        return random.choice(_LAST_NAMES)

    def _gen_email(self) -> str:
        first = random.choice(_FIRST_NAMES).lower()
        last = random.choice(_LAST_NAMES).lower()
        suffix = "".join(random.choices(string.digits, k=3))
        domain = random.choice(_DOMAINS)
        return f"{first}.{last}{suffix}@{domain}"

    def _gen_random_int(self) -> int:
        lo = self.kwargs.get("min", 0)
        hi = self.kwargs.get("max", 100)
        return random.randint(lo, hi)

    def _gen_random_float(self) -> float:
        lo = self.kwargs.get("min", 0.0)
        hi = self.kwargs.get("max", 1.0)
        return round(random.uniform(lo, hi), 4)

    def _gen_text(self) -> str:
        max_length = self.kwargs.get("max_length", 200)
        words = random.choices(_WORDS, k=max_length // 5)
        text = " ".join(words)
        return text[:max_length]

    def _gen_sentence(self) -> str:
        length = random.randint(5, 12)
        words = random.choices(_WORDS, k=length)
        return " ".join(words).capitalize() + "."

    def _gen_word(self) -> str:
        return random.choice(_WORDS)

    def _gen_uuid(self) -> str:
        return str(uuid.uuid4())

    def _gen_boolean(self) -> bool:
        return random.choice([True, False])

    def _gen_date(self) -> date:
        days_back = random.randint(0, 365)
        return date.today() - timedelta(days=days_back)

    def _gen_datetime(self) -> datetime:
        seconds_back = random.randint(0, 365 * 24 * 3600)
        return datetime.now(tz=UTC) - timedelta(seconds=seconds_back)

    def _gen_choice(self) -> Any:
        items = self.kwargs.get("items")
        if not items:
            raise ValueError("Faker('choice') requires 'items' kwarg")
        return random.choice(items)

    def __repr__(self) -> str:
        if self.kwargs:
            kw = ", ".join(f"{k}={v!r}" for k, v in self.kwargs.items())
            return f"Faker({self.provider!r}, {kw})"
        return f"Faker({self.provider!r})"


# ---------------------------------------------------------------------------
# ModelFactory — Factory Boy-style model factory
# ---------------------------------------------------------------------------


class _FactoryMeta(type):
    """Metaclass that collects field descriptors from the factory class."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
    ) -> type:
        # Collect field definitions (Faker instances and static values)
        field_defs: dict[str, Any] = {}
        for base in bases:
            if hasattr(base, "_field_defs"):
                field_defs.update(base._field_defs)

        for attr_name, attr_value in namespace.items():
            if attr_name.startswith("_") or attr_name == "Meta":
                continue
            if isinstance(attr_value, (Faker, str, int, float, bool, list, dict, tuple, set, bytes)) or attr_value is None:
                field_defs[attr_name] = attr_value

        cls = super().__new__(mcs, name, bases, namespace)
        cls._field_defs = field_defs  # type: ignore[attr-defined]
        return cls


class ModelFactory(metaclass=_FactoryMeta):
    """
    Factory Boy-style factory for generating ORM model instances.

    Subclasses define field values as class attributes — either static
    values or ``Faker`` descriptors for random data.

    Example::

        class UserFactory(ModelFactory):
            class Meta:
                model = User

            name = Faker("name")
            email = Faker("email")
            age = Faker("random_int", min=18, max=80)
            role = "player"  # static default

        # Build without saving (for unit tests)
        user = UserFactory.build()

        # Create and save (for integration tests)
        user = await UserFactory.create(role="admin")
        users = await UserFactory.create_batch(10)
    """

    _field_defs: dict[str, Any]

    class Meta:
        model: type[BaseSurrealModel]

    @classmethod
    def _get_model(cls) -> type[BaseSurrealModel]:
        """Get the model class from Meta."""
        model = getattr(cls.Meta, "model", None)
        if model is None:
            raise ValueError(f"{cls.__name__}.Meta.model is not set. Define a Meta class with a model attribute.")
        return model  # type: ignore[no-any-return]

    @classmethod
    def _resolve_fields(cls, **overrides: Any) -> dict[str, Any]:
        """Resolve all field values, applying overrides."""
        import copy

        data: dict[str, Any] = {}
        for field_name, field_def in cls._field_defs.items():
            if field_name in overrides:
                data[field_name] = overrides[field_name]
            elif isinstance(field_def, Faker):
                data[field_name] = field_def.generate()
            elif isinstance(field_def, (list, dict, tuple, set)):
                data[field_name] = copy.deepcopy(field_def)
            else:
                data[field_name] = field_def
        # Apply any overrides for fields not in _field_defs
        for key, value in overrides.items():
            if key not in data:
                data[key] = value
        return data

    @classmethod
    def build(cls, **overrides: Any) -> BaseSurrealModel:
        """
        Build a model instance without saving to the database.

        Returns:
            A new model instance with generated data.
        """
        model = cls._get_model()
        data = cls._resolve_fields(**overrides)
        return model(**data)

    @classmethod
    def build_batch(cls, count: int, **overrides: Any) -> list[BaseSurrealModel]:
        """
        Build multiple model instances without saving.

        Args:
            count: Number of instances to build.

        Returns:
            A list of model instances.
        """
        return [cls.build(**overrides) for _ in range(count)]

    @classmethod
    async def create(cls, **overrides: Any) -> BaseSurrealModel:
        """
        Build a model instance and save it to the database.

        Returns:
            The saved model instance.
        """
        instance = cls.build(**overrides)
        await instance.save()
        return instance

    @classmethod
    async def create_batch(cls, count: int, **overrides: Any) -> list[BaseSurrealModel]:
        """
        Build and save multiple model instances.

        Args:
            count: Number of instances to create.

        Returns:
            A list of saved model instances.
        """
        instances = []
        for _ in range(count):
            instance = await cls.create(**overrides)
            instances.append(instance)
        return instances


__all__ = ["Faker", "ModelFactory"]
