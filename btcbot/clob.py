"""Polymarket CLOB REST client (read paths + order submission).

Read endpoints are plain HTTP. Order submission requires a signed EIP-712
payload, which we delegate to the official `py-clob-client` -- imported lazily
so that paper mode runs with no wallet, no keys, and no extra dependency.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from .models import Book, Level, Order

log = logging.getLogger(__name__)


class ClobClient:
    def __init__(self, base_url: str = "https://clob.polymarket.com", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "btcintervaltrader/0.1"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ClobClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> Any:
        resp = self._http.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_book(self, token_id: str) -> Book:
        raw = self._get("/book", token_id=token_id)
        return _parse_book(raw)

    def get_books(self, token_ids: list[str]) -> dict[str, Book]:
        """Batch book fetch. Falls back to serial gets if the batch route fails."""
        try:
            resp = self._http.post(
                f"{self.base_url}/books",
                json=[{"token_id": t} for t in token_ids],
            )
            resp.raise_for_status()
            out: dict[str, Book] = {}
            for entry in resp.json():
                tid = str(entry.get("asset_id") or entry.get("token_id"))
                out[tid] = _parse_book(entry)
            if all(t in out for t in token_ids):
                return out
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("batch book fetch failed, falling back to serial: %s", exc)
        return {t: self.get_book(t) for t in token_ids}

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            raw = self._get("/midpoint", token_id=token_id)
            return float(raw["mid"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None


def _parse_book(raw: dict[str, Any]) -> Book:
    def levels(key: str, reverse: bool) -> list[Level]:
        out = [
            Level(price=float(lv["price"]), size=float(lv["size"]))
            for lv in (raw.get(key) or [])
        ]
        out.sort(key=lambda lv: lv.price, reverse=reverse)
        return out

    # Polymarket returns bids ascending; normalise to best-first on both sides.
    return Book(bids=levels("bids", reverse=True), asks=levels("asks", reverse=False))


class LiveOrderClient:
    """Thin wrapper over py-clob-client for real order submission.

    Constructed only when mode=live. Import is deferred so paper users never
    need the dependency or a private key on disk.
    """

    def __init__(self, host: str, chain_id: int = 137):
        try:
            from py_clob_client.client import ClobClient as _Clob  # type: ignore
            from py_clob_client.clob_types import ApiCreds  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "live mode needs the optional dependency: pip install py-clob-client"
            ) from exc

        key = os.getenv("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is not set")

        self._client = _Clob(host=host, key=key, chain_id=chain_id)
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)
        self._ApiCreds = ApiCreds

    def submit(self, token_id: str, order: Order) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs  # type: ignore
        from py_clob_client.order_builder.constants import BUY  # type: ignore

        args = OrderArgs(
            token_id=token_id,
            price=round(order.limit_price, 3),
            size=round(order.shares, 2),
            side=BUY,
        )
        signed = self._client.create_order(args)
        return self._client.post_order(signed)
