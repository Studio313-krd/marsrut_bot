"""Dedicated read-only report endpoint. Does not grant normal bot administration."""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from app.site_client import SiteApiError
from app.statistics import build_statistics_workbook

router = APIRouter(prefix="/api/leadership/v1", include_in_schema=False)
PROMPT = Path(__file__).resolve().parents[1] / "resources/leadership-prompt.txt"


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fromUtc: AwareDatetime | None
    toUtc: AwareDatetime
    timeZone: str = "Europe/Moscow"
    filters: dict[str, str] = Field(default_factory=dict)


def authorize(request: Request):
    key = request.app.state.settings.leadership_report_key
    if len(key) < 32:
        raise HTTPException(404)
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + key).encode()):
        raise HTTPException(401)


@router.get("/catalog")
async def catalog(request: Request):
    authorize(request)
    return JSONResponse(
        {
            "schemaVersion": 1,
            "project": "marshrut",
            "title": "Маршрут построен",
            "filters": [],
            "periods": [
                "today",
                "yesterday",
                "last7",
                "last30",
                "last90",
                "month_current",
                "month_previous",
                "all",
                "custom",
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/report")
async def report(request: Request):
    authorize(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            raise HTTPException(413)
    try:
        args = ReportRequest.model_validate_json(bytes(body))
        zone = ZoneInfo(args.timeZone)
        end = min(args.toUtc, datetime.now(UTC))
        if args.filters or (args.fromUtc and not timedelta(0) < end - args.fromUtc <= timedelta(days=366)):
            raise ValueError()
    except (ValidationError, ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Invalid range or filters") from None
    service = request.app.state.service
    if service._statistics_lock.locked():
        raise HTTPException(429)
    async with service._statistics_lock:
        try:
            payload = await service.site.request_statistics(
                from_date=args.fromUtc.isoformat() if args.fromUtc else None, to_date=end.isoformat()
            )
            workbook = await asyncio.to_thread(
                build_statistics_workbook, payload, service.storage.list_admins(), zone
            )
        except SiteApiError as exc:
            raise HTTPException(413 if exc.status_code == 413 else 503) from None
        except ValueError:
            raise HTTPException(422, "Report range unavailable") from None
    manifest = {
        "schemaVersion": 1,
        "project": "marshrut",
        "title": "Маршрут построен",
        "fileName": f"marshrut-statistics-{end:%Y%m%d-%H%M%S}.xlsx",
        "fromUtc": args.fromUtc.isoformat() if args.fromUtc else None,
        "toUtc": end.isoformat(),
        "generatedAt": payload["generatedAt"],
        "timeZone": args.timeZone,
        "filters": {},
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("report.xlsx", workbook)
        archive.writestr("prompt.txt", PROMPT.read_text(encoding="utf-8"))
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return Response(output.getvalue(), media_type="application/zip", headers={"Cache-Control": "no-store"})
