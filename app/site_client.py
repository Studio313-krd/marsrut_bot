from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import quote

import httpx


class SiteApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class SiteClient:
    def __init__(self, base_url: str, key_id: str, secret: str) -> None:
        self._site_base_url = base_url.rstrip("/")
        self._key_id = key_id
        self._secret = secret.encode()
        self._client = httpx.AsyncClient(
            base_url=f"{self._site_base_url}/api/integrations/bot",
            timeout=httpx.Timeout(20.0, connect=7.0),
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> httpx.Response:
        raw = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else b""
        )
        request = self._client.build_request(
            method,
            path,
            params={key: value for key, value in (params or {}).items() if value not in {None, ""}},
            content=raw,
            headers={"Content-Type": "application/json"} if body is not None else None,
            timeout=httpx.Timeout(timeout, connect=7.0),
        )
        timestamp = str(int(time.time()))
        content_hash = hashlib.sha256(raw).hexdigest()
        canonical_path = request.url.raw_path.decode("ascii")
        canonical = "\n".join([timestamp, method.upper(), canonical_path, content_hash])
        signature = hmac.new(self._secret, canonical.encode(), hashlib.sha256).hexdigest()
        request.headers.update(
            {
                "X-Bot-Key-Id": self._key_id,
                "X-Bot-Timestamp": timestamp,
                "X-Bot-Content-Sha256": content_hash,
                "X-Bot-Signature": signature,
            }
        )
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            raise SiteApiError("Сайт временно недоступен. Попробуйте ещё раз позже.") from exc
        if response.is_error:
            try:
                error = response.json()
                message = error.get("statusMessage") or error.get("message") or error.get("error")
            except (ValueError, AttributeError):
                message = None
            raise SiteApiError(str(message or "Ошибка API сайта"), response.status_code)
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Any:
        response = await self._send(method, path, params=params, body=body, timeout=timeout)
        return response.json() if response.content else None

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def events(self, after: int, limit: int = 100) -> dict[str, Any]:
        return await self._request("GET", "/events", params={"after": after, "limit": limit})

    async def create_request(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/requests", body=body)

    async def requests(self, **filters: Any) -> dict[str, Any]:
        return await self._request("GET", "/requests", params=filters)

    async def request(self, request_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/requests/{quote(request_id, safe='')}")

    async def update_request(self, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", f"/requests/{quote(request_id, safe='')}", body=body)

    async def add_activity(self, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/requests/{quote(request_id, safe='')}/activities", body=body)

    async def cancel_request(self, request_id: str, source: str, external_user_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/requests/{quote(request_id, safe='')}/cancel",
            body={"source": source, "externalUserId": external_user_id},
        )

    async def link_request(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/requests/link", body=body)

    async def content(self, kind: str, *, limit: int = 6, offset: int = 0, city: str = "") -> dict[str, Any]:
        if kind == "videos":
            return await self.videos(limit=limit, offset=offset)
        return await self._request(
            "GET", f"/content/{quote(kind, safe='')}", params={"limit": limit, "offset": offset, "city": city}
        )

    async def videos(self, *, limit: int = 6, offset: int = 0) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self._site_base_url}/api/videos")
        except httpx.HTTPError as exc:
            raise SiteApiError("Сайт временно недоступен. Попробуйте ещё раз позже.") from exc
        if response.is_error:
            raise SiteApiError("Не удалось загрузить новые выпуски", response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SiteApiError("Сайт вернул некорректный список выпусков") from exc
        if not isinstance(payload, list):
            raise SiteApiError("Сайт вернул некорректный список выпусков")

        start = max(0, offset)
        size = max(1, limit)
        rows = payload[start : start + size]
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("slug") or "").strip()
            if not slug:
                continue
            items.append(
                {
                    "id": str(row.get("id") or slug),
                    "kind": "videos",
                    "slug": slug,
                    "title": str(row.get("name") or row.get("title") or "Новый выпуск"),
                    "subtitle": row.get("description") or row.get("title") or None,
                    "image": row.get("coverImage") or None,
                    "path": f"/videos?play={quote(slug, safe='')}",
                    "publishedAt": None,
                    "city": None,
                }
            )
        return {
            "items": items,
            "pagination": {
                "limit": size,
                "offset": start,
                "hasMore": start + size < len(payload),
            },
        }

    async def cities(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/cities")

    async def delete_user_data(self, source: str, external_user_id: str) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/user-data/{quote(source, safe='')}/{quote(external_user_id, safe='')}"
        )

    async def export_requests(self) -> bytes:
        response = await self._send("GET", "/exports/requests.csv")
        return response.content

    async def request_statistics(self, *, from_date: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", "/exports/request-statistics", params={"from": from_date}, timeout=60.0
        )

    async def close(self) -> None:
        await self._client.aclose()
