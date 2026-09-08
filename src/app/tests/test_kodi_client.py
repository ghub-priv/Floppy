from unittest.mock import MagicMock

import pytest
import requests

from app.kodi_client import (
    KodiAuthenticationError,
    KodiClient,
    KodiConfigurationError,
    KodiConnectionError,
    KodiProtocolError,
    KodiRPCError,
)


def test_client_validates_configuration():
    with pytest.raises(KodiConfigurationError):
        KodiClient(host="")
    with pytest.raises(KodiConfigurationError):
        KodiClient(host="kodi", scheme="ftp")
    with pytest.raises(KodiConfigurationError):
        KodiClient(host="kodi", port=70000)
    with pytest.raises(KodiConfigurationError):
        KodiClient(host="kodi", timeout=0)


def test_call_uses_json_rpc_contract(monkeypatch):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "pong"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr("app.kodi_client.requests.post", post)

    client = KodiClient(host="192.168.1.10", username="u", password="p")
    assert client.ping() is True

    post.assert_called_once_with(
        "http://192.168.1.10:8080/jsonrpc",
        json={"jsonrpc": "2.0", "method": "JSONRPC.Ping", "id": 1},
        auth=("u", "p"),
        timeout=6.0,
    )


def test_call_rejects_authentication_failure(monkeypatch):
    response = MagicMock(status_code=401)
    monkeypatch.setattr("app.kodi_client.requests.post", MagicMock(return_value=response))
    with pytest.raises(KodiAuthenticationError):
        KodiClient(host="kodi").ping()


def test_call_maps_connection_failure(monkeypatch):
    monkeypatch.setattr(
        "app.kodi_client.requests.post",
        MagicMock(side_effect=requests.ConnectionError("down")),
    )
    with pytest.raises(KodiConnectionError):
        KodiClient(host="kodi").ping()


def test_call_rejects_wrong_request_id(monkeypatch):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"jsonrpc": "2.0", "id": 99, "result": "pong"}
    monkeypatch.setattr("app.kodi_client.requests.post", MagicMock(return_value=response))
    with pytest.raises(KodiProtocolError):
        KodiClient(host="kodi").ping()


def test_call_surfaces_json_rpc_error(monkeypatch):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32601, "message": "Method not found", "data": {"x": 1}},
    }
    monkeypatch.setattr("app.kodi_client.requests.post", MagicMock(return_value=response))

    with pytest.raises(KodiRPCError) as exc:
        KodiClient(host="kodi").call("Missing.Method")

    assert exc.value.code == -32601
    assert exc.value.data == {"x": 1}


def test_seek_seconds_builds_absolute_time(monkeypatch):
    client = KodiClient(host="kodi")
    client.call = MagicMock(return_value={"percentage": 50})

    client.seek_seconds(1, 3661)

    client.call.assert_called_once_with(
        "Player.Seek",
        {
            "playerid": 1,
            "value": {
                "time": {
                    "hours": 1,
                    "minutes": 1,
                    "seconds": 1,
                    "milliseconds": 0,
                }
            },
        },
    )
