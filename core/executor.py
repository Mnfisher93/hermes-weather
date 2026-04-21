"""
Order execution wrapper around py-clob-client.

In SIM mode, no network calls are made — orders are logged and returned
as a synthetic dict. In LIVE mode:
  1. Fetch the order book to confirm the target price is reachable.
  2. Apply a hard max-slippage check against the current best ask.
  3. Submit the GTC limit order.
"""
from __future__ import annotations

import os
import time
from typing import Optional

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    ClobClient = None  # type: ignore
    OrderArgs = None   # type: ignore
    OrderType = None   # type: ignore


# Minimum Polymarket order price / tick.
MIN_PRICE = 0.01
# How much worse than the signal price we'll accept at fill time, as a
# safety net on top of the scanner's liquidity filter.
DEFAULT_MAX_SLIPPAGE = 0.03


# ──────────────────────────────────────────────────────────────────
# Client construction
# ──────────────────────────────────────────────────────────────────

def build_client() -> "ClobClient":
    """Build an authenticated CLOB client from .env."""
    if not CLOB_AVAILABLE:
        raise RuntimeError(
            "py-clob-client is not installed — pip install py-clob-client"
        )
    host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    pk = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    funder = os.getenv("POLYMARKET_FUNDER") or os.getenv("FUNDER")

    if not pk:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY / PK not set in .env")

    client = ClobClient(
        host,
        key=pk,
        chain_id=chain_id,
        funder=funder,
        signature_type=0 if not funder else 1,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


# ──────────────────────────────────────────────────────────────────
# Order placement
# ──────────────────────────────────────────────────────────────────

def place_limit_order(
    client: Optional["ClobClient"],
    signal: dict,
    sim_mode: bool = True,
    max_slippage: float = DEFAULT_MAX_SLIPPAGE,
) -> Optional[dict]:
    """
    Place a GTC limit order for a signal produced by edge.py.

    In SIM mode, logs the intended order and returns a synthetic record.
    In LIVE mode, performs a book-depth check and submits the order.
    Returns a result dict, or None if the order was rejected at pre-flight.
    """
    side = signal["side"]
    token_id = (
        signal["yes_token_id"] if side == "YES" else signal["no_token_id"]
    )

    # The edge pipeline pre-computes the price we intend to submit at.
    target_price = float(signal.get("exec_price") or (
        signal["yes_price"] if side == "YES" else (1 - signal["yes_price"])
    ))
    target_price = max(MIN_PRICE, round(target_price, 2))

    size_usd = float(signal["position_usd"])
    size_shares = max(1.0, round(size_usd / target_price, 1))

    log_entry = {
        "market": signal["question"],
        "city": signal["city"],
        "condition_id": signal.get("condition_id"),
        "side": side,
        "type": signal["type"],
        "price": target_price,
        "size_shares": size_shares,
        "size_usd": size_usd,
        "model_prob": signal["model_prob"],
        "edge": signal["edge"],
        "ev": signal["ev"],
        "sim": sim_mode,
        "timestamp": time.time(),
    }

    if sim_mode:
        print(
            f"[SIM] {side} {size_shares:.1f} shares @ ${target_price:.2f} "
            f"on {signal['city']} | type={signal['type']} "
            f"edge={signal['edge']:.3f} ev={signal['ev']:.3f}"
        )
        return {**log_entry, "order_id": "sim", "status": "simulated"}

    # ── LIVE path ─────────────────────────────────────────────────
    if client is None:
        print("[EXEC] No client — skipping")
        return None

    # Pre-flight: confirm the book actually has liquidity at or near our price
    try:
        book_ok, book_info = _check_book_fillable(
            client, token_id, side, target_price, max_slippage
        )
    except Exception as e:
        print(f"[EXEC] Book check failed: {e} — skipping order")
        return None

    if not book_ok:
        print(f"[EXEC] Book not fillable: {book_info} — skipping")
        return {**log_entry, "order_id": None, "status": f"skipped:{book_info}"}

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=target_price,
            size=size_shares,
            side=side,
        )
        order = client.create_order(order_args)
        resp = client.post_order(order, OrderType.GTC)
        order_id = resp.get("orderID") if isinstance(resp, dict) else None
        print(
            f"[LIVE] Placed {side} {size_shares:.1f} @ ${target_price:.2f} "
            f"on {signal['city']} | order_id={order_id}"
        )
        return {**log_entry, "order_id": order_id, "status": "placed", "raw": resp}
    except Exception as e:
        print(f"[EXEC] Order failed: {e}")
        return {**log_entry, "order_id": None, "status": f"error:{e}"}


def _check_book_fillable(
    client: "ClobClient",
    token_id: str,
    side: str,
    target_price: float,
    max_slippage: float,
) -> tuple[bool, str]:
    """
    Fetch the order book and decide whether we can plausibly fill at
    target_price (allowing up to max_slippage worse).

    For BUY YES: we need an ask ≤ target_price + max_slippage
    For BUY NO:  we need an ask ≤ target_price + max_slippage on the NO token
    """
    book = client.get_order_book(token_id)
    asks = getattr(book, "asks", None) or []
    if not asks:
        return False, "empty asks"

    # py-clob-client returns OrderSummary-like objects with .price and .size
    try:
        best_ask = min(float(a.price) for a in asks if float(a.size) > 0)
    except Exception:
        # Fallback for dict-style responses
        try:
            best_ask = min(float(a["price"]) for a in asks if float(a.get("size", 0)) > 0)
        except Exception:
            return False, "unparseable book"

    ceiling = target_price + max_slippage
    if best_ask > ceiling:
        return False, f"best_ask ${best_ask:.3f} > ceiling ${ceiling:.3f}"
    return True, f"best_ask ${best_ask:.3f} within ceiling ${ceiling:.3f}"


# ──────────────────────────────────────────────────────────────────
# Read & cancel helpers
# ──────────────────────────────────────────────────────────────────

def get_positions(client: "ClobClient") -> list[dict]:
    try:
        return client.get_orders()
    except Exception as e:
        print(f"[EXEC] get_orders failed: {e}")
        return []


def cancel_all(client: "ClobClient") -> None:
    try:
        client.cancel_all()
        print("All open orders cancelled.")
    except Exception as e:
        print(f"[EXEC] cancel_all failed: {e}")
