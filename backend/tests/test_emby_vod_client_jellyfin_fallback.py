import asyncio

import httpx

import emby_vod_client


def _client_with_transport(provider, handler):
    client = emby_vod_client.EmbyVodClient(provider)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_falls_back_to_native_path_and_remembers_it():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/emby/Library/VirtualFolders":
            return httpx.Response(404)
        return httpx.Response(200, json=[{"Name": "Movies", "CollectionType": "movies"}])

    client = _client_with_transport({"base_url": "http://jellyfin.example", "password": "key"}, handler)
    assert asyncio.run(client._get("/emby/Library/VirtualFolders")) == [
        {"Name": "Movies", "CollectionType": "movies"}
    ]
    assert calls == ["/emby/Library/VirtualFolders", "/Library/VirtualFolders"]
    assert client._emby_prefix_unsupported is True

    calls.clear()
    asyncio.run(client._get("/emby/System/Info"))
    assert calls == ["/System/Info"]


def test_failed_native_fallback_is_not_latched():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/emby/Library/VirtualFolders":
            return httpx.Response(404)
        return httpx.Response(401)

    client = _client_with_transport({"base_url": "http://jellyfin.example", "password": "key"}, handler)
    try:
        asyncio.run(client._get("/emby/Library/VirtualFolders"))
        assert False, "expected an HTTPStatusError"
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 401
    assert client._emby_prefix_unsupported is False


def test_requests_include_emby_token_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Emby-Token")
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport({"base_url": "http://jellyfin.example", "password": "secret"}, handler)
    asyncio.run(client._get("/emby/System/Info"))
    assert seen["token"] == "secret"
