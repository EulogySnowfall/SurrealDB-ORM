"""Tests for the RPC protocol module."""

from src.surreal_sdk.protocol.rpc import RPCRequest, RPCResponse, RPCError, RPCMethod


class TestRPCRequest:
    """Tests for RPCRequest class."""

    def test_basic_request(self) -> None:
        """Test creating a basic RPC request."""
        request = RPCRequest(method="query", params=["SELECT * FROM users"])
        assert request.method == "query"
        assert request.params == ["SELECT * FROM users"]
        assert request.id == 1

    def test_to_dict(self) -> None:
        """Test converting request to dictionary."""
        request = RPCRequest(method="select", params=["users"], id=5)
        data = request.to_dict()

        assert data["id"] == 5
        assert data["method"] == "select"
        assert data["params"] == ["users"]

    def test_to_json(self) -> None:
        """Test JSON serialization."""
        request = RPCRequest(method="ping", params=[], id=1)
        json_str = request.to_json()

        assert '"method": "ping"' in json_str
        assert '"id": 1' in json_str

    def test_query_factory(self) -> None:
        """Test query factory method."""
        request = RPCRequest.query("SELECT * FROM users WHERE age > 21", {"limit": 10})

        assert request.method == "query"
        assert request.params[0] == "SELECT * FROM users WHERE age > 21"
        assert request.params[1] == {"limit": 10}

    def test_select_factory(self) -> None:
        """Test select factory method."""
        request = RPCRequest.select("users:123")

        assert request.method == "select"
        assert request.params == ["users:123"]

    def test_create_factory(self) -> None:
        """Test create factory method."""
        request = RPCRequest.create("users", {"name": "Alice", "age": 30})

        assert request.method == "create"
        assert request.params[0] == "users"
        assert request.params[1] == {"name": "Alice", "age": 30}

    def test_signin_factory(self) -> None:
        """Test signin factory method."""
        request = RPCRequest.signin(user="root", password="root", namespace="test")

        assert request.method == "signin"
        assert request.params["user"] == "root"
        assert request.params["pass"] == "root"
        assert request.params["ns"] == "test"

    def test_use_factory(self) -> None:
        """Test use factory method."""
        request = RPCRequest.use("my_namespace", "my_database")

        assert request.method == "use"
        assert request.params == ["my_namespace", "my_database"]

    def test_live_factory(self) -> None:
        """Test live query factory method."""
        request = RPCRequest.live("orders", diff=True)

        assert request.method == "query"
        assert "LIVE SELECT * FROM orders DIFF" in request.params[0]

    def test_kill_factory(self) -> None:
        """Test kill factory method."""
        request = RPCRequest.kill("some-uuid-here")

        assert request.method == "kill"
        assert request.params == ["some-uuid-here"]


class TestRPCResponse:
    """Tests for RPCResponse class."""

    def test_success_response(self) -> None:
        """Test parsing successful response."""
        data = {"id": 1, "result": [{"id": "users:1", "name": "Alice"}]}
        response = RPCResponse.from_dict(data)

        assert response.id == 1
        assert response.is_success
        assert not response.is_error
        assert response.result == [{"id": "users:1", "name": "Alice"}]

    def test_error_response(self) -> None:
        """Test parsing error response."""
        data = {"id": 1, "error": {"code": -32000, "message": "Table 'users' does not exist"}}
        response = RPCResponse.from_dict(data)

        assert response.id == 1
        assert response.is_error
        assert not response.is_success
        assert response.error is not None
        assert response.error.code == -32000
        assert "does not exist" in response.error.message

    def test_from_json(self) -> None:
        """Test parsing from JSON string."""
        json_str = '{"id": 5, "result": "ok"}'
        response = RPCResponse.from_json(json_str)

        assert response.id == 5
        assert response.result == "ok"


class TestRPCError:
    """Tests for RPCError class."""

    def test_from_dict(self) -> None:
        """Test creating error from dict."""
        data = {"code": -32600, "message": "Invalid request"}
        error = RPCError.from_dict(data)

        assert error.code == -32600
        assert error.message == "Invalid request"

    def test_from_dict_defaults(self) -> None:
        """Test default values."""
        error = RPCError.from_dict({})

        assert error.code == -1
        assert error.message == "Unknown error"


class TestRPCMethod:
    """Tests for RPCMethod constants."""

    def test_method_constants(self) -> None:
        """Test method name constants."""
        assert RPCMethod.SIGNIN == "signin"
        assert RPCMethod.QUERY == "query"
        assert RPCMethod.SELECT == "select"
        assert RPCMethod.CREATE == "create"
        assert RPCMethod.UPDATE == "update"
        assert RPCMethod.DELETE == "delete"
        assert RPCMethod.LIVE == "live"
        assert RPCMethod.KILL == "kill"
