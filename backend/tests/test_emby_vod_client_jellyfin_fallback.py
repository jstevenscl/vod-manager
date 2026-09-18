"""GH#27: some Jellyfin installs don't alias /emby/* at all, so a call like
/emby/Library/VirtualFolders 404s even though the equivalent native
(unprefixed) Jellyfin path works fine and the server is otherwise reachable.
EmbyVodClient._get must fall back to the native path on a 404 and remember
that choice for the rest of the client's lifetime.
"""

import asyncio

import httpx

import emby_vod_client


def _client_with_transport(provider, handler):
    client = emby_vod_client.EmbyVodClient(provider)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_falls_back_to_native_path_when_emby_prefix_missing():
    provider = {"base_url": "http://jellyfin.example", "password": "key"}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/emby/Library/VirtualFolders":
            return httpx.Response(404)
        if request.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "Movies", "CollectionType": "movies"}])
        return httpx.Response(500)

    client = _client_with_transport(provider, handler)
    result = asyncio.run(client._get("/emby/Library/VirtualFolders"))

    assert result == [{"Name": "Movies", "CollectionType": "movies"}]
    assert calls == ["/emby/Library/VirtualFolders", "/Library/VirtualFolders"]
    assert client._emby_prefix_unsupported is True


def test_remembers_fallback_for_later_calls_on_the_same_client():
    provider = {"base_url": "http://jellyfin.example", "password": "key"}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/emby/Library/VirtualFolders":
            return httpx.Response(404)
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(provider, handler)
    asyncio.run(client._get("/emby/Library/VirtualFolders"))
    calls.clear()

    asyncio.run(client._get("/emby/System/Info"))

    # No 404 round-trip this time -- goes straight to the native path.
    assert calls == ["/System/Info"]


def test_no_fallback_needed_when_emby_prefix_works():
    provider = {"base_url": "http://emby.example", "password": "key"}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=[{"Name": "Movies", "CollectionType": "movies"}])

    client = _client_with_transport(provider, handler)
    result = asyncio.run(client._get("/emby/Library/VirtualFolders"))

    assert result == [{"Name": "Movies", "CollectionType": "movies"}]
    assert calls == ["/emby/Library/VirtualFolders"]
    assert client._emby_prefix_unsupported is False


def test_real_404_unrelated_to_emby_prefix_still_raises():
    provider = {"base_url": "http://emby.example", "password": "key"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client_with_transport(provider, handler)
    try:
        asyncio.run(client._get("/emby/Library/VirtualFolders"))
        assert False, "expected an HTTPStatusError"
    except httpx.HTTPStatusError:
        pass
    assert client._emby_prefix_unsupported is False
