import logging
import httpx
from config import get_config

logger = logging.getLogger(__name__)


class DispatcharrClient:
    def __init__(self):
        url, token    = get_config()
        self._base    = url
        self._headers = {"X-API-Key": token}

    async def get(self, path: str, params: dict | None = None):
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{self._base}{path}", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()

    async def post(self, path: str, data: dict):
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{self._base}{path}", headers=self._headers, json=data)
            if not r.is_success:
                logger.error("[DispatcharrClient] POST %s → %d: %s", path, r.status_code, r.text[:500])
            r.raise_for_status()
            return r.json()

    async def patch(self, path: str, data: dict):
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.patch(f"{self._base}{path}", headers=self._headers, json=data)
            r.raise_for_status()
            return r.json()

    async def delete(self, path: str):
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.delete(f"{self._base}{path}", headers=self._headers)
            r.raise_for_status()
            return r.status_code

    async def get_bytes(self, path: str) -> tuple[bytes, dict]:
        """Fetch a path on the Dispatcharr base URL with auth, returning raw bytes."""
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(f"{self._base}{path}", headers=self._headers)
            r.raise_for_status()
            return r.content, dict(r.headers)

    async def download_bytes(self, url: str) -> tuple[bytes, dict]:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content, dict(r.headers)
