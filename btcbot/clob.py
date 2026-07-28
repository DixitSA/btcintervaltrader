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


# Polymarket signature types. This is the single most common reason a
# correctly-written bot fails to trade: if you funded your account through the
# Polymarket website, your USDC lives in a PROXY wallet, not in the EOA that
# owns your private key. Signing as the EOA (type 0) then produces orders from
# an address with no balance.
SIG_TYPE_EOA = 0        # private key holds the USDC directly
SIG_TYPE_EMAIL = 1      # Polymarket email/Magic login -> proxy wallet
SIG_TYPE_BROWSER = 2    # browser wallet via Polymarket UI -> Gnosis Safe


class LiveOrderClient:
    """Thin wrapper over py-clob-client for real order submission.

    Constructed only when mode=live. Import is deferred so paper users never
    need the dependency or a private key on disk.

    UNVERIFIED: this path has never been executed against the real venue from
    this repo. Place one minimum-size order by hand and confirm it appears in
    the Polymarket UI before letting the bot run unattended.
    """

    def __init__(self, host: str, chain_id: int = 137):
        try:
            from py_clob_client.client import ClobClient as _Clob  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "live mode needs the optional dependency: pip install py-clob-client"
            ) from exc

        key = os.getenv("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is not set")

        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", SIG_TYPE_EOA))
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS") or None

        if sig_type in (SIG_TYPE_EMAIL, SIG_TYPE_BROWSER) and not funder:
            raise RuntimeError(
                f"POLYMARKET_SIGNATURE_TYPE={sig_type} means your USDC is held in a "
                "Polymarket proxy wallet, so POLYMARKET_FUNDER_ADDRESS must be set to "
                "that proxy address (copy it from your Polymarket profile). Without it "
                "orders are signed from an address with no balance."
            )
        if sig_type == SIG_TYPE_EOA and funder:
            log.warning(
                "POLYMARKET_FUNDER_ADDRESS is set but signature type is EOA (0); "
                "the funder address will be ignored. If you deposited via the "
                "Polymarket website, set POLYMARKET_SIGNATURE_TYPE=1 or 2."
            )

        kwargs: dict[str, Any] = {"host": host, "key": key, "chain_id": chain_id}
        if sig_type != SIG_TYPE_EOA:
            kwargs["signature_type"] = sig_type
            kwargs["funder"] = funder

        self._client = _Clob(**kwargs)
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        self.signature_type = sig_type
        self.funder = funder
        log.info("live order client ready (signature_type=%s, funder=%s)", sig_type, funder)

    def submit(self, token_id: str, order: Order) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
        from py_clob_client.order_builder.constants import BUY  # type: ignore

        args = OrderArgs(
            token_id=token_id,
            price=round(order.limit_price, 3),
            size=round(order.shares, 2),
            side=BUY,
        )
        signed = self._client.create_order(args)
        # Fill-and-kill: we priced this against the book we just read, so any
        # unfilled remainder should be cancelled rather than left resting into
        # a window that expires in minutes.
        return self._client.post_order(signed, OrderType.FAK)
