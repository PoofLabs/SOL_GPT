**main.py**

import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

import requests
import base58  # for decoding base58 addresses
import base64  # for decoding base64 transaction

# Solana-specific imports
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed

from dotenv import load_dotenv

# Load environment variables from `.env`
load_dotenv()
RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY")
PRIVATE_KEY_STR = os.environ.get("PRIVATE_KEY")  # optional for enabling Jupiter swap signing

# Initialize FastAPI
app = FastAPI(title="Solana Token API", description="API for token information, wallet balances, and swaps on Solana.")

# Enable CORS for all origins (adjust in production as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class TokenInfo(BaseModel):
    symbol: Optional[str] = None
    name: Optional[str] = None
    address: str
    decimals: Optional[int] = None
    logoURI: Optional[str] = None
    tags: Optional[List[str]] = None
    price: Optional[float] = None  # include price in token info

class BalanceInfo(BaseModel):
    token: str
    amount: float

class SwapRequest(BaseModel):
    input_token: str
    output_token: str
    amount: float  # amount of input_token to swap (in human units)
    slippage_bps: int = 50

# Load Solana token list (covering obscure tokens)
TOKEN_LIST_URL = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
tokens_data = []
try:
    resp = requests.get(TOKEN_LIST_URL, timeout=10)
    resp.raise_for_status()
    token_list_json = resp.json()
    tokens_data = token_list_json.get("tokens", [])
except Exception as e:
    print(f"Warning: Could not fetch token list: {e}")
# Index tokens by symbol and address
tokens_mainnet = [t for t in tokens_data if t.get("chainId") == 101]
tokens_by_symbol = {}
tokens_by_address = {}
for token in tokens_mainnet:
    symbol = token.get("symbol")
    address = token.get("address")
    if symbol:
        tokens_by_symbol.setdefault(symbol.upper(), []).append(token)
    if address:
        tokens_by_address[address] = token

# Initialize Solana RPC client and optional Keypair
solana_client = Client(RPC_URL)
solana_keypair = None
solana_pubkey_str = None
if PRIVATE_KEY_STR:
    try:
        # Accept either base58-encoded or list of ints (like Solana CLI)
        if PRIVATE_KEY_STR.strip().startswith("["):
            secret_key = [int(x) for x in PRIVATE_KEY_STR.strip("[]").split(",") if x.strip()]
            solana_keypair = Keypair.from_bytes(bytes(secret_key))
        else:
            solana_keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_STR))
        solana_pubkey_str = str(solana_keypair.pubkey())
    except Exception as e:
        raise RuntimeError(f"Invalid PRIVATE_KEY. Could not initialize Keypair: {e}")

@app.get("/token", response_model=List[TokenInfo])
def get_token_info(query: str):
    """
    Get token information by symbol, name, or mint address. Includes obscure tokens.
    """
    query_str = query.strip()
    if not query_str:
        raise HTTPException(status_code=400, detail="Query parameter is required.")
    token_matches: List[TokenInfo] = []
    # Check if query is a valid mint address (32-byte base58)
    is_address = False
    try:
        decoded = base58.b58decode(query_str)
        if len(decoded) == 32:
            is_address = True
    except Exception:
        is_address = False

    if is_address:
        token = tokens_by_address.get(query_str)
        if token:
            # Use token list info
            info = TokenInfo(**token)
            # Fetch price if possible (using Coingecko ID if available)
            coingecko_id = token.get("extensions", {}).get("coingeckoId")
            if coingecko_id:
                try:
                    price_resp = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd", timeout=5)
                    price_resp.raise_for_status()
                    price_data = price_resp.json()
                    info.price = price_data.get(coingecko_id, {}).get("usd")
                except Exception:
                    info.price = None
            token_matches.append(info)
        else:
            # Not found in list, attempt to fetch decimals (no price if not in list)
            try:
                supply_info = solana_client.get_token_supply(query_str)
                decimals = supply_info.get("result", {}).get("value", {}).get("decimals")
            except Exception:
                raise HTTPException(status_code=404, detail="Token mint address not found or invalid.")
            token_matches.append(TokenInfo(symbol=None, name=None, address=query_str, decimals=decimals or 0, logoURI=None, tags=None, price=None))
    else:
        q_upper = query_str.upper()
        q_lower = query_str.lower()
        if q_upper in tokens_by_symbol:
            for token in tokens_by_symbol[q_upper]:
                info = TokenInfo(**token)
                coingecko_id = token.get("extensions", {}).get("coingeckoId")
                if coingecko_id:
                    try:
                        price_resp = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd", timeout=5)
                        price_resp.raise_for_status()
                        price_data = price_resp.json()
                        info.price = price_data.get(coingecko_id, {}).get("usd")
                    except Exception:
                        info.price = None
                token_matches.append(info)
        else:
            for token in tokens_mainnet:
                name = token.get("name")
                if name and name.lower() == q_lower:
                    info = TokenInfo(**token)
                    coingecko_id = token.get("extensions", {}).get("coingeckoId")
                    if coingecko_id:
                        try:
                            price_resp = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd", timeout=5)
                            price_resp.raise_for_status()
                            price_data = price_resp.json()
                            info.price = price_data.get(coingecko_id, {}).get("usd")
                        except Exception:
                            info.price = None
                    token_matches.append(info)
    if not token_matches:
        raise HTTPException(status_code=404, detail="Token not found.")
    return token_matches

@app.get("/balance", response_model=List[BalanceInfo])
def get_wallet_balances(address: str):
    """
    Get wallet balance (SOL and SPL tokens) for a given wallet address using Helius API.
    """
    addr = address.strip()
    if not addr or len(addr) != 44:
        raise HTTPException(status_code=400, detail="Invalid wallet address.")
    if not HELIUS_API_KEY:
        raise HTTPException(status_code=500, detail="HELIUS_API_KEY not configured.")
    try:
        # Use Helius to fetch token balances
        url = f"https://api.helius.xyz/v0/addresses/{addr}/balances?api-key={HELIUS_API_KEY}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        balances_data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch balances: {e}")
    # Parse the balances into BalanceInfo entries
    balance_list: List[BalanceInfo] = []
    tokens_balances = balances_data.get("tokens", [])
    native_balance = balances_data.get("nativeBalance", 0)
    # Add SOL (native) balance
    sol_amount = native_balance / 1e9  # convert lamports to SOL
    balance_list.append(BalanceInfo(token="SOL", amount=sol_amount))
    # Add SPL token balances (non-zero only)
    for token_bal in tokens_balances:
        amount_raw = token_bal.get("amount", 0)
        decimals = token_bal.get("decimals", 0)
        token_amount = float(amount_raw) / (10 ** decimals) if decimals > 0 else float(amount_raw)
        # The mint address for the token
        mint_addr = token_bal.get("tokenAccount", {}).get("accountInfo", {}).get("data", {}).get("parsed", {}).get("info", {}).get("mint")
        symbol = mint_addr
        if mint_addr and mint_addr in tokens_by_address:
            symbol = tokens_by_address[mint_addr].get("symbol", mint_addr)
        if token_amount > 0:
            balance_list.append(BalanceInfo(token=symbol, amount=token_amount))
    return balance_list

@app.post("/swap")
def swap_tokens(req: SwapRequest):
    """
    Swap tokens using Jupiter aggregator. If no server signing key is configured, returns unsigned tx.
    """
    def resolve_token(token: str) -> (str, int):
        token = token.strip()
        if not token:
            raise HTTPException(status_code=400, detail="Token identifier cannot be empty.")
        # Check symbol
        token_list = tokens_by_symbol.get(token.upper())
        if token_list:
            if len(token_list) > 1:
                raise HTTPException(status_code=400, detail=f"Ambiguous symbol '{token}', use mint address instead.")
            t = token_list[0]
            return t["address"], t.get("decimals", 0)
        # Check if it's a valid mint address
        try:
            decoded = base58.b58decode(token)
            if len(decoded) == 32:
                decimals = None
                if token in tokens_by_address:
                    decimals = tokens_by_address[token].get("decimals")
                if decimals is None:
                    try:
                        supply_info = solana_client.get_token_supply(token)
                        decimals = supply_info.get("result", {}).get("value", {}).get("decimals", 0)
                    except Exception:
                        decimals = 0
                return token, decimals or 0
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Token '{token}' not found.")

    input_mint, in_decimals = resolve_token(req.input_token)
    output_mint, out_decimals = resolve_token(req.output_token)
    # Compute amount in smallest units (lamports or minor units)
    from decimal import Decimal, getcontext
    getcontext().prec = 18
    amount = Decimal(str(req.amount))
    if in_decimals is None:
        in_decimals = 0
    amount_smallest = int(amount * (Decimal(10) ** in_decimals))
    if amount_smallest <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0.")
    # Jupiter quote
    quote_url = (
        f"https://quote-api.jup.ag/v6/quote"
        f"?inputMint={input_mint}"
        f"&outputMint={output_mint}"
        f"&amount={amount_smallest}"
        f"&slippageBps={req.slippage_bps}"
        f"&swapMode=ExactIn"
    )
    try:
        quote_resp = requests.get(quote_url, timeout=10)
        quote_resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to get quote from Jupiter: {e}")
    quote_data = quote_resp.json()
    if quote_data.get("outAmount") is None or int(quote_data.get("outAmount", 0)) == 0:
        raise HTTPException(status_code=400, detail="No swap route found or output amount is zero.")
    # Prepare swap transaction payload
    payload = {
        "quoteResponse": quote_data,
        "userPublicKey": solana_pubkey_str or "INSERT_PUBLIC_KEY_HERE",
        "wrapAndUnwrapSol": True
    }
    try:
        swap_resp = requests.post("https://quote-api.jup.ag/v6/swap", json=payload, timeout=10)
        swap_resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create swap transaction: {e}")
    swap_result = swap_resp.json()
    swap_tx = swap_result.get("swapTransaction")
    if not swap_tx:
        raise HTTPException(status_code=500, detail="Failed to get swap transaction from Jupiter.")
    # If no signing key, return unsigned transaction
    if solana_keypair is None:
        return {"swap_transaction": swap_tx, "message": "Unsigned transaction returned. Please sign and send it."}
    # Sign the transaction with the server's keypair
    try:
        raw_tx_bytes = base64.b64decode(swap_tx)
        txn = VersionedTransaction.from_bytes(raw_tx_bytes)
        msg_bytes = message.to_bytes_versioned(txn.message)
        signature = solana_keypair.sign_message(msg_bytes)
        signed_tx = VersionedTransaction.populate(txn.message, [signature])
        # Send the signed transaction to the network
        send_opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
        send_result = solana_client.send_raw_transaction(bytes(signed_tx), opts=send_opts)
        txid = send_result.get("result") if isinstance(send_result, dict) else None
        if not txid:
            raise Exception("No transaction ID returned.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Swap signing or send failed: {e}")
    return {"transaction_id": txid, "explorer_url": f"https://explorer.solana.com/tx/{txid}"}
