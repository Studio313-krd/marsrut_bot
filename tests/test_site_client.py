from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest
import respx

from app.site_client import SiteClient


@pytest.mark.asyncio
@respx.mock
async def test_site_request_signature_covers_exact_path_and_body(monkeypatch) -> None:
    monkeypatch.setattr("app.site_client.time.time", lambda: 1_788_444_000)
    secret = "s" * 32
    route = respx.get("https://site.example.test/api/integrations/bot/events?after=4&limit=10").mock(
        return_value=httpx.Response(200, json={"events": [], "nextCursor": 4})
    )
    client = SiteClient("https://site.example.test", "key-1", secret)
    try:
        result = await client.events(4, 10)
    finally:
        await client.close()

    request = route.calls[0].request
    empty_hash = hashlib.sha256(b"").hexdigest()
    canonical = "\n".join(
        [
            "1788444000",
            "GET",
            "/api/integrations/bot/events?after=4&limit=10",
            empty_hash,
        ]
    )
    assert request.headers["x-bot-key-id"] == "key-1"
    assert (
        request.headers["x-bot-signature"]
        == hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    )
    assert result["nextCursor"] == 4


@pytest.mark.asyncio
@respx.mock
async def test_videos_content_comes_from_public_videos_page_source() -> None:
    route = respx.get("https://site.example.test/api/videos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "person-1",
                    "slug": "angelina-stipidi",
                    "name": "Ангелина Стипиди",
                    "title": "Продюсер культурных проектов",
                    "description": "Описание выпуска",
                    "coverImage": "/uploads/angelina.webp",
                },
                {
                    "id": "person-2",
                    "slug": "vyacheslav-markovskiy",
                    "name": "Вячеслав Марковский",
                    "description": "Второй выпуск",
                },
            ],
        )
    )
    client = SiteClient("https://site.example.test", "key-1", "s" * 32)
    try:
        result = await client.content("videos", limit=1, offset=0, city="krasnodar")
    finally:
        await client.close()

    assert route.called
    assert result == {
        "items": [
            {
                "id": "person-1",
                "kind": "videos",
                "slug": "angelina-stipidi",
                "title": "Ангелина Стипиди",
                "subtitle": "Описание выпуска",
                "image": "/uploads/angelina.webp",
                "path": "/videos?play=angelina-stipidi",
                "publishedAt": None,
                "city": None,
            }
        ],
        "pagination": {"limit": 1, "offset": 0, "hasMore": True},
    }
