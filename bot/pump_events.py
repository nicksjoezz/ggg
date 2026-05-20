"""
pump_events.py — Parses pump.fun Anchor TradeEvent from Solana transaction logs.

pump.fun emits a TradeEvent for every buy/sell via Anchor's event macro.
It appears in logs as a "Program data: <base64>" line.

TradeEvent struct layout (after 8-byte discriminator):
  offset  8 : mint                Pubkey  32 bytes
  offset 40 : solAmount           u64      8 bytes  (lamports)
  offset 48 : tokenAmount         u64      8 bytes  (raw units, 6 decimals)
  offset 56 : isBuy               bool     1 byte
  offset 57 : user                Pubkey  32 bytes
  offset 89 : timestamp           i64      8 bytes
  offset 97 : virtualSolReserves  u64      8 bytes  (lamports)
  offset 105: virtualTokenReserves u64     8 bytes  (raw units)
  total: 113 bytes
"""

import base64
import hashlib
import struct

from solders.pubkey import Pubkey

# sha256("event:TradeEvent")[:8]
TRADE_EVENT_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]


def parse_trade_event(logs: list[str]) -> dict | None:
    """
    Scan transaction log lines for a pump.fun TradeEvent.

    Returns a dict compatible with the rest of the bot:
      mint, txType, solAmount, traderPublicKey, marketCapSol

    Returns None if no valid TradeEvent is found.
    """
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            data = base64.b64decode(line[len("Program data: "):])
        except Exception:
            continue

        if len(data) < 113 or data[:8] != TRADE_EVENT_DISC:
            continue

        try:
            mint       = str(Pubkey.from_bytes(data[8:40]))
            sol_amount = struct.unpack_from("<Q", data, 40)[0] / 1e9
            is_buy     = bool(data[56])
            user       = str(Pubkey.from_bytes(data[57:89]))
            v_sol      = struct.unpack_from("<Q", data, 97)[0]   # lamports
            v_tok      = struct.unpack_from("<Q", data, 105)[0]  # raw token units
            # marketCapSol = price_per_token_sol × 1B supply
            # price = v_sol_lamports / v_tok_raw  →  adjust decimals:
            # marketCapSol = (v_sol/1e9) / (v_tok/1e6) × 1e9 = v_sol/v_tok × 1e6
            mcap_sol = (v_sol / v_tok * 1_000_000) if v_tok > 0 else 0
        except Exception:
            continue

        return {
            "mint":            mint,
            "txType":          "buy" if is_buy else "sell",
            "solAmount":       sol_amount,
            "traderPublicKey": user,
            "marketCapSol":    mcap_sol,
        }

    return None
