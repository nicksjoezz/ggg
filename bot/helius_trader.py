"""
helius_trader.py — Builds and sends pump.fun buy/sell transactions via Helius RPC.

Implements:
  - Bonding curve PDA derivation from mint address
  - Associated token account (ATA) creation if needed
  - Buy instruction with correct Anchor discriminator + accounts
  - Sell instruction
  - Priority fee estimation via Helius getPriorityFeeEstimate
  - Transaction send with retry via Helius sendSmartTransaction equivalent
"""

import asyncio
import base64
import hashlib
import logging
import struct
from typing import Optional, Tuple

import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.hash import Hash
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

logger = logging.getLogger("helius_trader")

# ── Program & account constants ───────────────────────────────────────────────

PUMP_PROGRAM      = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL       = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5zP9QkVSvtsFGAH")
PUMP_FEE_RECIPIENT= Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgznyQHebiSmwjYM7pj6hs")
PUMP_EVENT_AUTH   = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
TOKEN_PROGRAM     = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM       = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe8bv")
SYSVAR_RENT       = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
SYS_PROGRAM       = Pubkey.from_string("11111111111111111111111111111111")
MPL_TOKEN_META    = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

# pump.fun token decimals
TOKEN_DECIMALS = 6

# Pre-calculated Anchor discriminators (sha256("global:<name>")[:8])
# buy:  [102, 6, 61, 18, 1, 218, 235, 234]
# sell: [51, 230, 133, 164, 1, 127, 131, 173]
BUY_DISCRIMINATOR  = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])


def _anchor_discriminator(name: str) -> bytes:
    """Calculate 8-byte Anchor discriminator for instruction name."""
    h = hashlib.sha256(f"global:{name}".encode()).digest()
    return h[:8]


def get_bonding_curve_pda(mint: Pubkey) -> Pubkey:
    """Derive the bonding curve PDA for a given mint."""
    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)],
        PUMP_PROGRAM
    )
    return pda


def get_associated_bonding_curve(bonding_curve: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the ATA of the bonding curve (holds the token reserves)."""
    ata, _ = Pubkey.find_program_address(
        [bytes(bonding_curve), bytes(TOKEN_PROGRAM), bytes(mint)],
        ATA_PROGRAM
    )
    return ata


def get_user_ata(user: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the user's associated token account for a given mint."""
    ata, _ = Pubkey.find_program_address(
        [bytes(user), bytes(TOKEN_PROGRAM), bytes(mint)],
        ATA_PROGRAM
    )
    return ata


class HeliusTrader:
    """Builds and sends pump.fun transactions using Helius RPC."""

    def __init__(self, rpc_url: str, keypair: Keypair):
        self.rpc_url = rpc_url
        self.keypair = keypair
        self.client  = AsyncClient(rpc_url)
        self.pubkey  = keypair.pubkey()

    # ── Priority fee estimation ──────────────────────────────────────────────

    async def _get_priority_fee(self, account_keys: list[str]) -> int:
        """
        Estimate priority fee via Helius getPriorityFeeEstimate.
        Returns micro-lamports per compute unit.
        Falls back to a safe default if the call fails.
        """
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.post(self.rpc_url, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getPriorityFeeEstimate",
                    "params": [{
                        "accountKeys": account_keys,
                        "options": {"priorityLevel": "High"}
                    }]
                })
                data = resp.json()
                fee = data["result"]["priorityFeeEstimate"]
                logger.info(f"Priority fee estimate: {fee} micro-lamports/CU")
                return int(fee)
        except Exception as e:
            logger.warning(f"Priority fee estimate failed ({e}) — using default 100000")
            return 100_000  # safe default: 0.0001 SOL per 200k CU

    # ── Buy transaction ──────────────────────────────────────────────────────

    async def build_buy_instruction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        associated_bonding_curve: Pubkey,
        user_ata: Pubkey,
        sol_amount: float,
        slippage_bps: int = 1000,
    ) -> Instruction:
        """
        Build the pump.fun buy instruction.

        sol_amount: SOL to spend (e.g. 0.05)
        slippage_bps: basis points tolerance (1000 = 10%)
        """
        # Convert SOL to lamports
        lamports = int(sol_amount * 1_000_000_000)

        # Fetch bonding curve state to calculate max_sol_cost
        # The bonding curve state gives us current token price
        # We apply slippage tolerance to the expected SOL amount
        max_sol_cost = int(lamports * (1 + slippage_bps / 10_000))

        # Token amount = approximate tokens we expect to receive
        # pump.fun buy instruction takes: amount (tokens), max_sol_cost (lamports)
        # We pass a large token amount (all remaining supply) and let max_sol_cost cap it
        token_amount = 1_000_000_000 * (10 ** TOKEN_DECIMALS)  # buy as many as sol_amount allows

        # Instruction data: discriminator + amount (u64) + max_sol_cost (u64)
        data = BUY_DISCRIMINATOR + struct.pack("<QQ", token_amount, max_sol_cost)

        accounts = [
            AccountMeta(pubkey=PUMP_GLOBAL,                is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FEE_RECIPIENT,         is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint,                       is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_bonding_curve,   is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_ata,                   is_signer=False, is_writable=True),
            AccountMeta(pubkey=self.pubkey,                is_signer=True,  is_writable=True),
            AccountMeta(pubkey=SYS_PROGRAM,                is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM,              is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSVAR_RENT,                is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_EVENT_AUTH,            is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_PROGRAM,               is_signer=False, is_writable=False),
        ]

        return Instruction(program_id=PUMP_PROGRAM, accounts=accounts, data=data)

    def build_create_ata_instruction(self, mint: Pubkey, user_ata: Pubkey) -> Instruction:
        """Build create-associated-token-account instruction if ATA doesn't exist yet."""
        accounts = [
            AccountMeta(pubkey=self.pubkey, is_signer=True,  is_writable=True),
            AccountMeta(pubkey=user_ata,   is_signer=False, is_writable=True),
            AccountMeta(pubkey=self.pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint,        is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYS_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSVAR_RENT, is_signer=False, is_writable=False),
        ]
        return Instruction(program_id=ATA_PROGRAM, accounts=accounts, data=bytes())

    async def _ata_exists(self, ata: Pubkey) -> bool:
        """Check if an ATA already exists on-chain."""
        try:
            resp = await self.client.get_account_info(ata)
            return resp.value is not None
        except Exception:
            return False

    async def execute_buy(
        self,
        mint_str: str,
        sol_amount: float,
        slippage_bps: int = 1000,
        compute_unit_limit: int = 200_000,
    ) -> Tuple[bool, str]:
        """
        Execute a buy transaction on pump.fun.
        Returns (success, tx_signature_or_error_message).
        """
        try:
            mint = Pubkey.from_string(mint_str)
            bonding_curve = get_bonding_curve_pda(mint)
            associated_bonding_curve = get_associated_bonding_curve(bonding_curve, mint)
            user_ata = get_user_ata(self.pubkey, mint)

            logger.info(f"BUY {mint_str[:8]}... | {sol_amount} SOL | curve: {str(bonding_curve)[:8]}...")

            # Priority fee
            account_keys = [str(mint), str(bonding_curve), str(self.pubkey)]
            priority_fee = await self._get_priority_fee(account_keys)

            # Get latest blockhash
            blockhash_resp = await self.client.get_latest_blockhash()
            blockhash = blockhash_resp.value.blockhash

            # Build instructions
            instructions = [
                set_compute_unit_limit(compute_unit_limit),
                set_compute_unit_price(priority_fee),
            ]

            # Create ATA if it doesn't exist
            if not await self._ata_exists(user_ata):
                logger.info("Creating ATA for token...")
                instructions.append(self.build_create_ata_instruction(mint, user_ata))

            # Buy instruction
            buy_ix = await self.build_buy_instruction(
                mint=mint,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user_ata=user_ata,
                sol_amount=sol_amount,
                slippage_bps=slippage_bps,
            )
            instructions.append(buy_ix)

            # Build + sign transaction
            msg = MessageV0.try_compile(
                payer=self.pubkey,
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self.keypair])

            # Send with retries
            sig = await self._send_with_retry(tx)
            logger.info(f"BUY confirmed: {sig}")
            return True, sig

        except Exception as e:
            logger.error(f"BUY failed for {mint_str[:8]}: {e}")
            return False, str(e)

    # ── Sell transaction ─────────────────────────────────────────────────────

    async def execute_sell(
        self,
        mint_str: str,
        token_amount: int,          # raw token units (include 6 decimals)
        slippage_bps: int = 1000,
        compute_unit_limit: int = 200_000,
    ) -> Tuple[bool, str]:
        """
        Execute a sell transaction on pump.fun.
        token_amount: raw units including 6 decimals (e.g. 1_000_000 = 1 token)
        """
        try:
            mint = Pubkey.from_string(mint_str)
            bonding_curve = get_bonding_curve_pda(mint)
            associated_bonding_curve = get_associated_bonding_curve(bonding_curve, mint)
            user_ata = get_user_ata(self.pubkey, mint)

            logger.info(f"SELL {mint_str[:8]}... | {token_amount / 10**TOKEN_DECIMALS:.2f} tokens")

            # min_sol_output: accept anything above 0 (slippage applied)
            min_sol_output = 0

            # Instruction data: discriminator + amount (u64) + min_sol_output (u64)
            data = SELL_DISCRIMINATOR + struct.pack("<QQ", token_amount, min_sol_output)

            accounts = [
                AccountMeta(pubkey=PUMP_GLOBAL,              is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FEE_RECIPIENT,       is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint,                     is_signer=False, is_writable=False),
                AccountMeta(pubkey=bonding_curve,            is_signer=False, is_writable=True),
                AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_ata,                 is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.pubkey,              is_signer=True,  is_writable=True),
                AccountMeta(pubkey=SYS_PROGRAM,              is_signer=False, is_writable=False),
                AccountMeta(pubkey=ATA_PROGRAM,              is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM,            is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_EVENT_AUTH,          is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_PROGRAM,             is_signer=False, is_writable=False),
            ]

            sell_ix = Instruction(program_id=PUMP_PROGRAM, accounts=accounts, data=data)

            # Priority fee
            account_keys = [str(mint), str(bonding_curve), str(self.pubkey)]
            priority_fee = await self._get_priority_fee(account_keys)

            blockhash_resp = await self.client.get_latest_blockhash()
            blockhash = blockhash_resp.value.blockhash

            instructions = [
                set_compute_unit_limit(compute_unit_limit),
                set_compute_unit_price(priority_fee),
                sell_ix,
            ]

            msg = MessageV0.try_compile(
                payer=self.pubkey,
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self.keypair])
            sig = await self._send_with_retry(tx)
            logger.info(f"SELL confirmed: {sig}")
            return True, sig

        except Exception as e:
            logger.error(f"SELL failed for {mint_str[:8]}: {e}")
            return False, str(e)

    # ── Send with retry ──────────────────────────────────────────────────────

    async def _send_with_retry(self, tx: VersionedTransaction, max_retries: int = 3) -> str:
        """Send transaction and retry up to max_retries times on failure."""
        serialized = bytes(tx)

        for attempt in range(max_retries):
            try:
                resp = await self.client.send_raw_transaction(
                    serialized,
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                )
                sig = str(resp.value)

                # Confirm
                await self._confirm_transaction(sig)
                return sig

            except Exception as e:
                logger.warning(f"Send attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))

        raise RuntimeError(f"Transaction failed after {max_retries} attempts")

    async def _confirm_transaction(self, sig: str, timeout: int = 30) -> bool:
        """Poll for transaction confirmation."""
        from solders.signature import Signature
        signature = Signature.from_string(sig)
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await self.client.get_signature_statuses([signature])
                status = resp.value[0]
                if status and status.confirmation_status:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.5)

        logger.warning(f"Transaction {sig[:16]}... not confirmed within {timeout}s")
        return False

    async def get_token_balance(self, mint_str: str) -> int:
        """Get user's token balance in raw units."""
        try:
            mint = Pubkey.from_string(mint_str)
            user_ata = get_user_ata(self.pubkey, mint)
            resp = await self.client.get_token_account_balance(user_ata)
            return int(resp.value.amount)
        except Exception:
            return 0

    async def close(self):
        await self.client.close()


def load_keypair_from_base58(private_key_b58: str) -> Optional[Keypair]:
    """Load a Keypair from a base58 private key string."""
    try:
        import base58
        decoded = base58.b58decode(private_key_b58)
        return Keypair.from_bytes(decoded)
    except ImportError:
        # fallback: try raw bytes interpretation
        try:
            decoded = base64.b58decode(private_key_b58)
            return Keypair.from_bytes(decoded)
        except Exception as e:
            logger.error(f"Failed to load keypair: {e}")
            return None
    except Exception as e:
        logger.error(f"Failed to load keypair: {e}")
        return None
