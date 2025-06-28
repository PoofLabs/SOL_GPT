import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

import requests
import base64
import base58  # For base58 decoding of Solana addresses and private keys

# Solana SDK and Solders for transaction signing
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message  # Used for obtaining transaction message bytes for signing
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed

from dotenv import load_dotenv

# Load environment variables (e.g., RPC_URL, PRIVATE_KEY) from a .env file if present
load_dotenv()

# Configuration from environment
RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_STR = os.environ.get("PRIVATE_KEY")  # Base58-encoded or JSON-array string of 64-byte private key

# Initialize FastAPI app
app = FastAPI(title="Solana Token API", 
              description="Query Solana wallet, token info, and perform token swaps.")

# Enable CORS for all origins (optional, for client flexibility)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data model for token information (response model)
class TokenInfo(BaseModel):
    symbol: Optional[str] = None
    name: Optional[str] = None
    address: str   # mint address
    decimals: Optional[int] = None
    logoURI: Optional[str] = None
    tags: Optional[List[str]] = None

# Data model for swap request (request body)
class SwapRequest(BaseModel):
    input_token: str   # symbol, name, or mint address of input token
    output_token: str  # symbol, name, or mint address of output token
    amount: float      # amount of input token in human units (e.g. 1.5 means 1.5 tokens)
    slippage_bps: int = 50  # slippage in basis points (default 50 = 0.5%)

# Pre-load the Solana token list to support symbol/name queries for obscure tokens
TOKEN_LIST_URL = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
tokens_data = []
try:
    resp = requests.get(TOKEN_LIST_URL, timeout=10)
    resp.raise_for_status()
    token_list_json = resp.json()
    tokens_data = token_list_json.get("tokens", [])
except Exception as e:
    print(f"Warning: Could not load token list: {e}")
# Filter for Solana mainnet tokens and index by symbol and address
mainnet_tokens = [t for t in tokens_data if t.get("chainId") == 101]
tokens_by_symbol = {}
tokens_by_address = {}
for token in mainnet_tokens:
    symbol = token.get("symbol")
    address = token.get("address")
    if symbol:
        tokens_by_symbol.setdefault(symbol.upper(), []).append(token)
    if address:
        tokens_by_address[address] = token

# Prepare Solana RPC client and Keypair if private key is provided
solana_client = Client(endpoint=RPC_URL)
solana_keypair = None
solana_pubkey_str = None
if PRIVATE_KEY_STR:
    try:
        # Handle JSON-array format vs base58 format for the private key
        if PRIVATE_KEY_STR.strip().startswith("["):
            key_array = list(map(int, PRIVATE_KEY_STR.strip().strip("[]").split(",")))
            solana_keypair = Keypair.from_bytes(bytes(key_array))
        else:
            solana_keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_STR))
    except Exception as e:
        raise RuntimeError("Failed to parse PRIVATE_KEY from environment. Please provide a valid Solana private key.") from e
    solana_pubkey_str = str(solana_keypair.pubkey())
    print(f"Loaded wallet public key: {solana_pubkey_str}")

# Data model for wallet information (response model)
class WalletInfo(BaseModel):
    public_key: str
    sol_balance: Optional[float] = None  # SOL balance in SOL units
    lamports: Optional[int] = None      # SOL balance in lamports

@app.get("/token", response_model=List[TokenInfo])
def get_token_info(query: str):
    """
    Query Solana token information by symbol, name, or mint address.
    - `query`: Token symbol (e.g. "SOL"), token name (e.g. "Wrapped Solana"), or mint address.
    Returns a list of matching tokens (could be multiple if name matches several), or 404 if none found.
    """
    query_str = query.strip()
    if not query_str:
        raise HTTPException(status_code=400, detail="Query parameter is required.")
    token_matches: List[TokenInfo] = []
    # Check if the query is a mint address (32-byte base58)
    is_address = False
    try:
        decoded = base58.b58decode(query_str)
        if len(decoded) == 32:
            is_address = True
    except Exception:
        is_address = False

    if is_address:
        # Exact address lookup in token list
        token = tokens_by_address.get(query_str)
        if token:
            token_matches.append(TokenInfo(**{
                "symbol": token.get("symbol"),
                "name": token.get("name"),
                "address": token.get("address"),
                "decimals": token.get("decimals"),
                "logoURI": token.get("logoURI"),
                "tags": token.get("tags")
            }))
        else:
            # If not in the list, try on-chain lookup for decimals
            try:
                supply_info = solana_client.get_token_supply(query_str)
                value = supply_info.get("result", {}).get("value")
                decimals = value.get("decimals") if (value and "decimals" in value) else None
            except Exception as e:
                raise HTTPException(status_code=404, detail="Token mint address not found or invalid.") from e
            token_matches.append(TokenInfo(**{
                "symbol": None,
                "name": None,
                "address": query_str,
                "decimals": decimals,
                "logoURI": None,
                "tags": None
            }))
    else:
        # Not an address: treat query as symbol or exact name
        q_upper = query_str.upper()
        q_lower = query_str.lower()
        # Try exact symbol match
        if q_upper in tokens_by_symbol:
            for token in tokens_by_symbol[q_upper]:
                token_matches.append(TokenInfo(**{
                    "symbol": token.get("symbol"),
                    "name": token.get("name"),
                    "address": token.get("address"),
                    "decimals": token.get("decimals"),
                    "logoURI": token.get("logoURI"),
                    "tags": token.get("tags")
                }))
        else:
            # Try exact name match (case-insensitive)
            for token in mainnet_tokens:
                if token.get("name") and token.get("name").lower() == q_lower:
                    token_matches.append(TokenInfo(**{
                        "symbol": token.get("symbol"),
                        "name": token.get("name"),
                        "address": token.get("address"),
                        "decimals": token.get("decimals"),
                        "logoURI": token.get("logoURI"),
                        "tags": token.get("tags")
                    }))
    if not token_matches:
        raise HTTPException(status_code=404, detail="Token not found.")
    return token_matches

@app.get("/wallet", response_model=WalletInfo)
def get_wallet_info():
    """
    Get information about the configured wallet (public key and SOL balance).
    """
    if not solana_pubkey_str:
        raise HTTPException(status_code=404, detail="No wallet configured on server.")
    # Fetch SOL balance
    lamports = None
    try:
        balance_resp = solana_client.get_balance(solana_pubkey_str)
        # solana_client.get_balance returns a dict with {'result': {'value': <lamports>}, ...}
        if isinstance(balance_resp, dict):
            lamports = balance_resp.get("result", {}).get("value")
        else:
            # If the response is an object (depending on solana-py version), convert to dict
            balance_json = balance_resp.to_json() if hasattr(balance_resp, "to_json") else balance_resp
            if isinstance(balance_json, dict):
                lamports = balance_json.get("result", {}).get("value")
    except Exception:
        lamports = None  # In case of RPC error, we'll return None for balance
    sol_balance = (lamports / 1_000_000_000) if (lamports is not None) else None
    return WalletInfo(public_key=solana_pubkey_str, sol_balance=sol_balance, lamports=lamports)

@app.post("/swap")
def swap_tokens(request: SwapRequest):
    """
    Perform a token swap using Jupiter aggregator. Returns a transaction signature if executed, 
    or an unsigned transaction (and quote info) if no signing key is configured.
    """
    # Helper to resolve a token identifier (symbol, name, or address) to mint address and decimals
    def resolve_token(token_str: str) -> tuple[str, int]:
        token_str = token_str.strip()
        if not token_str:
            raise HTTPException(status_code=400, detail="Token identifier cannot be empty.")
        # Check symbol first
        token_info_list = tokens_by_symbol.get(token_str.upper())
        if token_info_list:
            if len(token_info_list) > 1:
                raise HTTPException(status_code=400, detail=f"Symbol '{token_str}' is ambiguous, please use the mint address or full token name instead.")
            token = token_info_list[0]
            return token["address"], token.get("decimals", 0)
        # Check if it's a valid mint address
        try:
            decoded = base58.b58decode(token_str)
            if len(decoded) == 32:
                decimals = None
                if token_str in tokens_by_address:
                    decimals = tokens_by_address[token_str].get("decimals")
                if decimals is None:
                    try:
                        supply_info = solana_client.get_token_supply(token_str)
                        value = supply_info.get("result", {}).get("value")
                        decimals = value.get("decimals") if (value and "decimals" in value) else 0
                    except Exception:
                        decimals = 0
                return token_str, decimals
        except Exception:
            pass
        # Finally, try exact name match
        q_lower = token_str.lower()
        name_matches = [t for t in mainnet_tokens if t.get("name") and t.get("name").lower() == q_lower]
        if name_matches:
            if len(name_matches) > 1:
                raise HTTPException(status_code=400, detail=f"Token name '{token_str}' is ambiguous, please use the mint address instead.")
            token = name_matches[0]
            return token["address"], token.get("decimals", 0)
        # If nothing matched:
        raise HTTPException(status_code=404, detail=f"Token '{token_str}' not found. Please use a valid symbol, name, or mint address.")

    # Resolve input and output tokens to mint addresses and decimals
    input_address, input_decimals = resolve_token(request.input_token)
    output_address, output_decimals = resolve_token(request.output_token)
    if input_decimals is None:
        input_decimals = 0
    from decimal import Decimal, getcontext
    getcontext().prec = 20  # high precision for multiplication
    # Calculate input amount in the smallest unit (lamports or minor units)
    amount_decimal = Decimal(str(request.amount))
    amount_in_base_units = int(amount_decimal * (Decimal(10) ** input_decimals))
    if amount_in_base_units <= 0:
        raise HTTPException(status_code=400, detail="Amount is too small or invalid for the given token decimals.")

    # Jupiter quote for the swap
    quote_url = (
        f"https://quote-api.jup.ag/v6/quote"
        f"?inputMint={input_address}"
        f"&outputMint={output_address}"
        f"&amount={amount_in_base_units}"
        f"&slippageBps={request.slippage_bps}"
        f"&swapMode=ExactIn"
    )
    try:
        quote_resp = requests.get(quote_url, timeout=10)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch quote from Jupiter: {e}")
    if quote_resp.status_code != 200:
        # Propagate Jupiter error message if possible
        detail_msg = f"Jupiter quote API error (status {quote_resp.status_code})"
        try:
            err_body = quote_resp.json()
            if isinstance(err_body, dict) and err_body.get("error"):
                detail_msg += f" - {err_body.get('error')}"
        except ValueError:
            detail_msg += f" - {quote_resp.text[:200]}"
        raise HTTPException(status_code=502, detail=detail_msg)
    quote_data = quote_resp.json()
    out_amt = quote_data.get("outAmount")
    if out_amt is None or int(out_amt) == 0:
        raise HTTPException(status_code=400, detail="No swap route found or output amount is zero. Swap cannot be performed.")

    # Prepare Jupiter swap transaction request
    payload = {
        "quoteResponse": quote_data,
        "userPublicKey": solana_pubkey_str if solana_pubkey_str else "INSERT_PUBLIC_KEY_HERE",
        "wrapAndUnwrapSol": True
    }
    try:
        swap_resp = requests.post("https://quote-api.jup.ag/v6/swap", json=payload, timeout=10)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch swap transaction from Jupiter: {e}")
    if swap_resp.status_code != 200:
        detail_msg = f"Jupiter swap API error (status {swap_resp.status_code})"
        try:
            err_json = swap_resp.json()
            if isinstance(err_json, dict) and err_json.get("error"):
                detail_msg += f" - {err_json.get('error')}"
        except ValueError:
            detail_msg += f" - {swap_resp.text[:200]}"
        raise HTTPException(status_code=502, detail=detail_msg)
    swap_result = swap_resp.json()
    swap_tx_b64 = swap_result.get("swapTransaction")
    if not swap_tx_b64:
        raise HTTPException(status_code=500, detail="Failed to retrieve swap transaction from Jupiter.")

    # If no server key, return unsigned transaction and quote info for user
    if solana_keypair is None:
        # Calculate human-readable output amount from out_amt and output_decimals
        estimated_out = None
        if out_amt is not None and output_decimals is not None:
            try:
                estimated_out = float(int(out_amt) / (10 ** output_decimals))
            except Exception:
                estimated_out = None
        # Determine token symbols if available
        input_token_symbol = tokens_by_address.get(input_address, {}).get("symbol")
        output_token_symbol = tokens_by_address.get(output_address, {}).get("symbol")
        return {
            "input_token": input_token_symbol or input_address,
            "output_token": output_token_symbol or output_address,
            "input_amount": request.amount,
            "estimated_output_amount": estimated_out,
            "swap_transaction": swap_tx_b64,
            "message": "No signing key configured on server. This is an unsigned swap transaction. Please sign it with your wallet and submit to the network."
        }

    # Sign the transaction using the server's Keypair
    try:
        raw_tx_bytes = base64.b64decode(swap_tx_b64)
        raw_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        msg_bytes = message.to_bytes_versioned(raw_tx.message)  # transaction message bytes
        signature = solana_keypair.sign_message(msg_bytes)
        signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sign the transaction: {e}")

    # Send the signed transaction to the Solana network
    try:
        opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
        # Encode transaction to base64 for sending
        signed_tx_bytes = bytes(signed_tx)
        base64_tx = base64.b64encode(signed_tx_bytes).decode('utf-8')
        result = solana_client.send_raw_transaction(base64_tx, opts=opts)
        # Ensure result is a dictionary
        tx_result = result if isinstance(result, dict) else (result.to_json() if hasattr(result, "to_json") else {})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to send transaction to Solana network: {e}")

    # Check for success or error in the RPC response
    if "error" in tx_result and tx_result["error"]:
        raise HTTPException(status_code=502, detail=f"Solana RPC error: {tx_result['error']}")
    tx_signature = tx_result.get("result")
    if not tx_signature:
        raise HTTPException(status_code=502, detail="Unknown error from Solana RPC (no signature returned).")

    # Successful swap – return transaction signature, explorer link, and details
    input_token_symbol = tokens_by_address.get(input_address, {}).get("symbol")
    output_token_symbol = tokens_by_address.get(output_address, {}).get("symbol")
    estimated_out = None
    try:
        if out_amt is not None and output_decimals is not None:
            estimated_out = float(int(out_amt) / (10 ** output_decimals))
    except Exception:
        estimated_out = None
    return {
        "transaction_id": tx_signature,
        "explorer_url": f"https://explorer.solana.com/tx/{tx_signature}",
        "input_token": input_token_symbol or input_address,
        "output_token": output_token_symbol or output_address,
        "input_amount": request.amount,
        "estimated_output_amount": estimated_out
    }

@app.get("/")
def root():
    # Basic health check endpoint, include wallet pubkey if loaded
    info = {"message": "SolanaGPT backend is running."}
    if solana_pubkey_str:
        info["wallet_public_key"] = solana_pubkey_str
    return info
