"""Typed async wrapper for the Woolix Order API."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import time
from typing import Any

import aiohttp

from .models import SETTLED_PAYMENT_STATUSES, TERMINAL_JOB_STATUSES


UpdateCallback = Callable[[dict[str, Any]], Awaitable[None]]
logger = logging.getLogger(__name__)


class WoolixApiError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        if isinstance(payload, dict):
            self.code = str(payload.get("code") or "")
            message = str(payload.get("error") or payload.get("message") or f"HTTP {status}")
        else:
            self.code = ""
            message = str(payload or f"HTTP {status}")
        super().__init__(message)


class WoolixApiClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WoolixApiClient":
        await self.open()
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()

    async def open(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "direct-aco-woolix/1.0",
                },
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.open()
        assert self._session is not None
        started = time.monotonic()
        logger.info("Order service request | method=%s path=%s", method, path)
        try:
            async with self._session.request(method, f"{self.base_url}{path}", json=body) as response:
                raw = await response.text()
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    data = {"error": raw[:1000] or f"HTTP {response.status}"}
                if not 200 <= response.status < 300:
                    logger.warning(
                        "Order service response | method=%s path=%s status=%s duration_ms=%s",
                        method,
                        path,
                        response.status,
                        round((time.monotonic() - started) * 1000),
                    )
                    raise WoolixApiError(response.status, data)
                if not isinstance(data, dict):
                    logger.warning(
                        "Order service returned invalid data | method=%s path=%s status=%s",
                        method,
                        path,
                        response.status,
                    )
                    raise WoolixApiError(response.status, {"error": "The ordering service returned an invalid response"})
                logger.info(
                    "Order service response | method=%s path=%s status=%s duration_ms=%s",
                    method,
                    path,
                    response.status,
                    round((time.monotonic() - started) * 1000),
                )
                return data
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Order service timeout | method=%s path=%s duration_ms=%s",
                method,
                path,
                round((time.monotonic() - started) * 1000),
            )
            raise WoolixApiError(0, {"error": "The ordering service timed out"}) from exc
        except aiohttp.ClientError as exc:
            logger.warning(
                "Order service connection failed | method=%s path=%s duration_ms=%s",
                method,
                path,
                round((time.monotonic() - started) * 1000),
            )
            raise WoolixApiError(0, {"error": "Could not reach the ordering service"}) from exc

    async def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/jobs", payload)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/jobs/{job_id}")

    async def configure_job(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/jobs/{job_id}/configure", changes)

    async def proceed_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/jobs/{job_id}/proceed")

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/jobs/{job_id}/cancel")

    async def set_dropoff(self, job_id: str, dropoff_type: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/jobs/{job_id}/dropoff", {"type": dropoff_type})

    async def set_fulfillment(self, job_id: str, fulfillment_type: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/jobs/{job_id}/fulfillment", {"type": fulfillment_type})

    async def rebuild_job(self, job_id: str, group_cart: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/jobs/{job_id}/rebuild", {"group_cart": group_cart})

    async def _poll(
        self,
        job_id: str,
        done: Callable[[dict[str, Any]], bool],
        *,
        timeout_seconds: float,
        interval_seconds: float,
        on_update: UpdateCallback | None = None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        previous: tuple[str, str, str] | None = None
        while True:
            job = await self.get_job(job_id)
            fingerprint = (
                str(job.get("status") or ""),
                str(job.get("substatus") or ""),
                str(job.get("payment_status") or ""),
            )
            if on_update and fingerprint != previous:
                await on_update(job)
            previous = fingerprint
            if done(job):
                return job
            if loop.time() >= deadline:
                raise WoolixApiError(0, {"error": "This is taking longer than expected. The main panel will keep updating."})
            await asyncio.sleep(interval_seconds)

    async def wait_for_draft(
        self,
        job_id: str,
        *,
        timeout_seconds: float,
        interval_seconds: float,
        on_update: UpdateCallback | None = None,
        awaiting_config_done: bool = False,
    ) -> dict[str, Any]:
        return await self._poll(
            job_id,
            lambda job: (
                job.get("status") in TERMINAL_JOB_STATUSES
                or job.get("status") == "draft_ready"
                or (awaiting_config_done and job.get("status") == "awaiting_config")
                or bool(job.get("card_error"))
                or bool(job.get("place_order_error"))
                or bool(job.get("error"))
            ),
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            on_update=on_update,
        )

    async def wait_for_settlement(
        self,
        job_id: str,
        *,
        timeout_seconds: float,
        interval_seconds: float,
        on_update: UpdateCallback | None = None,
    ) -> dict[str, Any]:
        return await self._poll(
            job_id,
            lambda job: (
                job.get("payment_status") in SETTLED_PAYMENT_STATUSES
                or job.get("status") in {"cancelled", "expired", "failed"}
                or bool(job.get("card_error"))
                or bool(job.get("place_order_error"))
                or bool(job.get("error"))
            ),
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            on_update=on_update,
        )
