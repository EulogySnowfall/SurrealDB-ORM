"""Tests for ORM v0.4.0 features: relations and graph traversal."""

from pydantic import Field
from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_orm.fields.relation import (
    ForeignKey,
    ManyToMany,
    Relation,
    RelationInfo,
    get_relation_info,
    is_foreign_key,
    is_graph_relation,
    is_many_to_many,
    is_relation_field,
)


# ==================== Test Models ====================


class User(BaseSurrealModel):
    """Test model for relation tests."""

    id: str | None = None
    name: str = Field(...)
    active: bool = Field(default=True)


class Profile(BaseSurrealModel):
    """Test model for ForeignKey relation."""

    id: str | None = None
    bio: str = Field(default="")


class Group(BaseSurrealModel):
    """Test model for ManyToMany relation."""

    id: str | None = None
    name: str = Field(...)


# ==================== ForeignKey Field Tests ====================


def test_foreign_key_creates_annotated_type() -> None:
    """Test that ForeignKey creates an Annotated type."""
    fk_type = ForeignKey("User")
    assert is_relation_field(fk_type)
    assert is_foreign_key(fk_type)


def test_foreign_key_with_on_delete() -> None:
    """Test ForeignKey with on_delete parameter."""
    fk_cascade = ForeignKey("User", on_delete="CASCADE")
    fk_set_null = ForeignKey("User", on_delete="SET_NULL")
    fk_protect = ForeignKey("User", on_delete="PROTECT")

    info_cascade = get_relation_info(fk_cascade)
    info_set_null = get_relation_info(fk_set_null)
    info_protect = get_relation_info(fk_protect)

    assert info_cascade is not None
    assert info_cascade.on_delete == "CASCADE"
    assert info_set_null is not None
    assert info_set_null.on_delete == "SET_NULL"
    assert info_protect is not None
    assert info_protect.on_delete == "PROTECT"


def test_foreign_key_with_related_name() -> None:
    """Test ForeignKey with related_name parameter."""
    fk_type = ForeignKey("User", related_name="posts")
    info = get_relation_info(fk_type)

    assert info is not None
    assert info.related_name == "posts"


def test_foreign_key_relation_info() -> None:
    """Test RelationInfo extraction from ForeignKey."""
    fk_type = ForeignKey("Profile", on_delete="SET_NULL", related_name="owner")
    info = get_relation_info(fk_type)

    assert info is not None
    assert info.to_model == "Profile"
    assert info.relation_type == "foreign_key"
    assert info.on_delete == "SET_NULL"
    assert info.related_name == "owner"


# ==================== ManyToMany Field Tests ====================


def test_many_to_many_creates_annotated_type() -> None:
    """Test that ManyToMany creates an Annotated type."""
    m2m_type = ManyToMany("Group")
    assert is_relation_field(m2m_type)
    assert is_many_to_many(m2m_type)


def test_many_to_many_with_through() -> None:
    """Test ManyToMany with through table."""
    m2m_type = ManyToMany("Group", through="membership")
    info = get_relation_info(m2m_type)

    assert info is not None
    assert info.through == "membership"


def test_many_to_many_with_related_name() -> None:
    """Test ManyToMany with related_name."""
    m2m_type = ManyToMany("Group", related_name="members")
    info = get_relation_info(m2m_type)

    assert info is not None
    assert info.related_name == "members"


def test_many_to_many_relation_info() -> None:
    """Test RelationInfo extraction from ManyToMany."""
    m2m_type = ManyToMany("Tag", through="post_tags", related_name="posts")
    info = get_relation_info(m2m_type)

    assert info is not None
    assert info.to_model == "Tag"
    assert info.relation_type == "many_to_many"
    assert info.through == "post_tags"
    assert info.related_name == "posts"


# ==================== Relation (Graph) Field Tests ====================


def test_relation_creates_annotated_type() -> None:
    """Test that Relation creates an Annotated type."""
    rel_type = Relation("follows", "User")
    assert is_relation_field(rel_type)
    assert is_graph_relation(rel_type)


def test_relation_forward_direction() -> None:
    """Test Relation in forward direction (default)."""
    rel_type = Relation("follows", "User")
    info = get_relation_info(rel_type)

    assert info is not None
    assert info.reverse is False
    assert info.traversal_direction == "->"


def test_relation_reverse_direction() -> None:
    """Test Relation in reverse direction."""
    rel_type = Relation("follows", "User", reverse=True)
    info = get_relation_info(rel_type)

    assert info is not None
    assert info.reverse is True
    assert info.traversal_direction == "<-"


def test_relation_info() -> None:
    """Test RelationInfo extraction from Relation."""
    rel_type = Relation("likes", "Post", reverse=False)
    info = get_relation_info(rel_type)

    assert info is not None
    assert info.to_model == "Post"
    assert info.relation_type == "relation"
    assert info.edge_table == "likes"
    assert info.reverse is False


# ==================== Type Detection Tests ====================


def test_is_relation_field_detects_foreign_key() -> None:
    """Test is_relation_field detects ForeignKey."""
    fk_type = ForeignKey("User")
    assert is_relation_field(fk_type) is True


def test_is_relation_field_detects_many_to_many() -> None:
    """Test is_relation_field detects ManyToMany."""
    m2m_type = ManyToMany("Group")
    assert is_relation_field(m2m_type) is True


def test_is_relation_field_detects_relation() -> None:
    """Test is_relation_field detects Relation."""
    rel_type = Relation("follows", "User")
    assert is_relation_field(rel_type) is True


def test_is_relation_field_rejects_non_relation() -> None:
    """Test is_relation_field rejects non-relation types."""
    assert is_relation_field(str) is False
    assert is_relation_field(int) is False
    assert is_relation_field(list) is False


def test_is_foreign_key_specific() -> None:
    """Test is_foreign_key only matches ForeignKey."""
    fk_type = ForeignKey("User")
    m2m_type = ManyToMany("Group")
    rel_type = Relation("follows", "User")

    assert is_foreign_key(fk_type) is True
    assert is_foreign_key(m2m_type) is False
    assert is_foreign_key(rel_type) is False


def test_is_many_to_many_specific() -> None:
    """Test is_many_to_many only matches ManyToMany."""
    fk_type = ForeignKey("User")
    m2m_type = ManyToMany("Group")
    rel_type = Relation("follows", "User")

    assert is_many_to_many(fk_type) is False
    assert is_many_to_many(m2m_type) is True
    assert is_many_to_many(rel_type) is False


def test_is_graph_relation_specific() -> None:
    """Test is_graph_relation only matches Relation."""
    fk_type = ForeignKey("User")
    m2m_type = ManyToMany("Group")
    rel_type = Relation("follows", "User")

    assert is_graph_relation(fk_type) is False
    assert is_graph_relation(m2m_type) is False
    assert is_graph_relation(rel_type) is True


# ==================== RelationInfo Tests ====================


def test_relation_info_traversal_direction_forward() -> None:
    """Test RelationInfo.traversal_direction for forward relations."""
    info = RelationInfo(
        to_model="User",
        relation_type="relation",
        edge_table="follows",
        reverse=False,
    )
    assert info.traversal_direction == "->"


def test_relation_info_traversal_direction_reverse() -> None:
    """Test RelationInfo.traversal_direction for reverse relations."""
    info = RelationInfo(
        to_model="User",
        relation_type="relation",
        edge_table="follows",
        reverse=True,
    )
    assert info.traversal_direction == "<-"


def test_relation_info_traversal_query_forward() -> None:
    """Test RelationInfo.get_traversal_query for forward relations."""
    info = RelationInfo(
        to_model="User",
        relation_type="relation",
        edge_table="follows",
        reverse=False,
    )
    query = info.get_traversal_query("users", "alice")
    assert "users:alice->follows->User" in query


def test_relation_info_traversal_query_reverse() -> None:
    """Test RelationInfo.get_traversal_query for reverse relations."""
    info = RelationInfo(
        to_model="User",
        relation_type="relation",
        edge_table="follows",
        reverse=True,
    )
    query = info.get_traversal_query("users", "alice")
    assert "users:alice<-follows<-User" in query


# ==================== QuerySet Extension Tests ====================


def test_queryset_has_select_related() -> None:
    """Test that QuerySet has select_related method."""
    qs = User.objects()
    assert hasattr(qs, "select_related")
    assert callable(getattr(qs, "select_related"))


def test_queryset_select_related_is_chainable() -> None:
    """Test that select_related returns self for chaining."""
    qs = User.objects()
    result = qs.select_related("profile")
    assert result is qs


def test_queryset_select_related_stores_relations() -> None:
    """Test that select_related stores the relation names."""
    qs = User.objects().select_related("profile", "settings")
    assert qs._select_related == ["profile", "settings"]


def test_queryset_has_prefetch_related() -> None:
    """Test that QuerySet has prefetch_related method."""
    qs = User.objects()
    assert hasattr(qs, "prefetch_related")
    assert callable(getattr(qs, "prefetch_related"))


def test_queryset_prefetch_related_is_chainable() -> None:
    """Test that prefetch_related returns self for chaining."""
    qs = User.objects()
    result = qs.prefetch_related("followers")
    assert result is qs


def test_queryset_prefetch_related_stores_relations() -> None:
    """Test that prefetch_related stores the relation names."""
    qs = User.objects().prefetch_related("followers", "posts")
    assert qs._prefetch_related == ["followers", "posts"]


def test_queryset_has_traverse() -> None:
    """Test that QuerySet has traverse method."""
    qs = User.objects()
    assert hasattr(qs, "traverse")
    assert callable(getattr(qs, "traverse"))


def test_queryset_traverse_is_chainable() -> None:
    """Test that traverse returns self for chaining."""
    qs = User.objects()
    result = qs.traverse("->follows->users")
    assert result is qs


def test_queryset_traverse_stores_path() -> None:
    """Test that traverse stores the traversal path."""
    qs = User.objects().traverse("->follows->users->likes->posts")
    assert qs._traversal_path == "->follows->users->likes->posts"


def test_queryset_has_graph_query() -> None:
    """Test that QuerySet has graph_query method."""
    qs = User.objects()
    assert hasattr(qs, "graph_query")
    assert callable(getattr(qs, "graph_query"))


def test_queryset_chain_filter_and_traverse() -> None:
    """Test chaining filter with traverse."""
    qs = User.objects().filter(active=True).traverse("->follows->users")
    assert qs._filters == [("active", "exact", True)]
    assert qs._traversal_path == "->follows->users"


# ==================== Model Method Tests ====================


def test_model_has_relate_method() -> None:
    """Test that BaseSurrealModel has relate method."""
    assert hasattr(User, "relate")
    assert callable(getattr(User, "relate"))


def test_model_has_remove_relation_method() -> None:
    """Test that BaseSurrealModel has remove_relation method."""
    assert hasattr(User, "remove_relation")
    assert callable(getattr(User, "remove_relation"))


def test_model_has_get_related_method() -> None:
    """Test that BaseSurrealModel has get_related method."""
    assert hasattr(User, "get_related")
    assert callable(getattr(User, "get_related"))


def test_model_relate_signature() -> None:
    """Test relate method signature."""
    import inspect

    sig = inspect.signature(User.relate)
    params = list(sig.parameters.keys())
    assert "relation" in params
    assert "to" in params
    assert "tx" in params


def test_model_remove_relation_signature() -> None:
    """Test remove_relation method signature."""
    import inspect

    sig = inspect.signature(User.remove_relation)
    params = list(sig.parameters.keys())
    assert "relation" in params
    assert "to" in params
    assert "tx" in params


def test_model_get_related_signature() -> None:
    """Test get_related method signature."""
    import inspect

    sig = inspect.signature(User.get_related)
    params = list(sig.parameters.keys())
    assert "relation" in params
    assert "direction" in params
    assert "model_class" in params


# ==================== Import/Export Tests ====================


def test_relation_types_exported_from_fields() -> None:
    """Test that relation types are exported from fields module."""
    from src.surreal_orm.fields import ForeignKey, ManyToMany, Relation, RelationInfo

    assert ForeignKey is not None
    assert ManyToMany is not None
    assert Relation is not None
    assert RelationInfo is not None


def test_helper_functions_exported_from_fields() -> None:
    """Test that helper functions are exported from fields module."""
    from src.surreal_orm.fields import (
        get_relation_info,
        is_foreign_key,
        is_graph_relation,
        is_many_to_many,
        is_relation_field,
    )

    assert is_relation_field is not None
    assert is_foreign_key is not None
    assert is_many_to_many is not None
    assert is_graph_relation is not None
    assert get_relation_info is not None
