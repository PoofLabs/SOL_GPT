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
# We will derive the Keypair and public key below if PRIVATE_KEY is provided

# Initialize FastAPI app
app = FastAPI(title="Solana Token API", description="Query token info and perform token swaps on Solana.")

# (Optional) Enable CORS if needed for web clients (allow all origins here for simplicity)
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
    address: str  # mint address
    decimals: Optional[int] = None
    logoURI: Optional[str] = None
    tags: Optional[List[str]] = None

# Data model for swap request (request body)
class SwapRequest(BaseModel):
    input_token: str   # symbol or mint address of input token
    output_token: str  # symbol or mint address of output token
    amount: float      # amount of input token (in human-readable units, e.g., 1.5 means 1.5 tokens)
    slippage_bps: int = 50  # slippage in basis points (default 50 = 0.5% slippage)

# Pre-load the Solana token list (to support symbol/name queries for even obscure tokens)
TOKEN_LIST_URL = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
tokens_data = []
try:
    resp = requests.get(TOKEN_LIST_URL, timeout=10)
    resp.raise_for_status()
    token_list_json = resp.json()
    tokens_data = token_list_json.get("tokens", [])
except Exception as e:
    # If the token list fails to load, we proceed with an empty list and rely on on-chain queries for info
    print(f"Warning: Could not load token list: {e}")

# Filter tokens for Solana mainnet (chainId 101) and index them by symbol and address for quick lookup
mainnet_tokens = [t for t in tokens_data if t.get("chainId") == 101]
# Create lookup dictionaries for symbol->list of tokens and address->token
tokens_by_symbol = {}
tokens_by_address = {}
for token in mainnet_tokens:
    symbol = token.get("symbol")
    address = token.get("address")
    if symbol:
        symbol_upper = symbol.upper()
        tokens_by_symbol.setdefault(symbol_upper, []).append(token)
    if address:
        tokens_by_address[address] = token

# Prepare Solana RPC client and Keypair if private key is provided
solana_client = Client(endpoint=RPC_URL)
solana_keypair = None
solana_pubkey_str = None
if PRIVATE_KEY_STR:
    try:
        # If the private key is in JSON array format (e.g., "[18, 54, ...]"), parse it
        if PRIVATE_KEY_STR.strip().startswith("["):
            key_array = list(map(int, PRIVATE_KEY_STR.strip().strip("[]").split(",")))
            solana_keypair = Keypair.from_bytes(bytes(key_array))
        else:
            # Assume the private key is a base58-encoded secret key
            solana_keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_STR))
    except Exception as e:
        # If key parsing fails, raise an error at startup (so deployment fails fast rather than at swap time)
        raise RuntimeError("Failed to parse PRIVATE_KEY from environment. Please provide a valid Solana private key.") from e

    # Derive the public key (string) from the Keypair
    solana_pubkey_str = str(solana_keypair.pubkey())
    # Log (or print) to verify loaded key (avoid printing the actual private key)
    print(f"Loaded wallet public key: {solana_pubkey_str}")

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

    # Determine if query is a mint address (base58) or a token symbol/name.
    token_matches: List[TokenInfo] = []
    is_address = False
    # Heuristic: if query is valid base58 and decodes to 32 bytes, treat it as a mint address
    try:
        decoded = base58.b58decode(query_str)
        if len(decoded) == 32:
            is_address = True
    except Exception:
        is_address = False

    if is_address:
        # Exact address lookup
        token = tokens_by_address.get(query_str)
        if token:
            # Found in token list
            token_matches.append(TokenInfo(**{
                "symbol": token.get("symbol"),
                "name": token.get("name"),
                "address": token.get("address"),
                "decimals": token.get("decimals"),
                "logoURI": token.get("logoURI"),
                "tags": token.get("tags")
            }))
        else:
            # Not found in static list, attempt on-chain lookup for decimals (name/symbol might not be available)
            try:
                supply_info = solana_client.get_token_supply(query_str)
                value = supply_info.get("result", {}).get("value")
                if value and "decimals" in value:
                    decimals = value["decimals"]
                else:
                    # If RPC response doesn't contain decimals, we cannot retrieve info
                    decimals = None
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
        # Not an address: treat query as symbol or name (case-insensitive exact match)
        q_upper = query_str.upper()
        q_lower = query_str.lower()
        # Try symbol exact match (case-insensitive)
        if q_upper in tokens_by_symbol:
            # If multiple tokens share the same symbol, include them all
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
            # Try name exact match (case-insensitive)
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
        # No token found for the query
        raise HTTPException(status_code=404, detail="Token not found.")
    return token_matches

@app.post("/swap")
def swap_tokens(request: SwapRequest):
        """
    Swap tokens using the Jupiter aggregator. Provide input/output tokens, amount, and slippage. Returns a signed or unsigned transaction.
    """

    # Determine input and output mint addresses from symbols or addresses
    def resolve_token(token_str: str) -> (str, int):
        """Helper to resolve a token symbol/name/address to a mint address and decimals."""
        token_str = token_str.strip()
        if not token_str:
            raise HTTPException(status_code=400, detail="Token identifier cannot be empty.")
        # Check if it's a known symbol
        token_info_list = tokens_by_symbol.get(token_str.upper())
        if token_info_list:
            # If multiple tokens share the symbol, require unambiguous input
            if len(token_info_list) > 1:
                raise HTTPException(status_code=400, detail=f"Symbol '{token_str}' is ambiguous, please use the mint address instead.")
            token_info = token_info_list[0]
            return token_info["address"], token_info.get("decimals", 0)
        # If not a symbol, maybe it's an address (or name, but we'll assume direct address for swap)
        try:
            decoded = base58.b58decode(token_str)
            if len(decoded) == 32:
                # It's a valid 32-byte address. Check if we know its decimals from list or chain.
                decimals = None
                if token_str in tokens_by_address:
                    decimals = tokens_by_address[token_str].get("decimals", None)
                if decimals is None:
                    # Fetch decimals via RPC if not in list
                    try:
                        supply_info = solana_client.get_token_supply(token_str)
                        value = supply_info.get("result", {}).get("value")
                        if value and "decimals" in value:
                            decimals = value["decimals"]
                    except Exception:
                        # If this fails, we'll just set decimals as 0 (to avoid crash; the swap might fail if incorrect)
                        decimals = 0
                return token_str, decimals if decimals is not None else 0
        except Exception:
            # Not a valid base58 address
            pass
        # If we reach here, the token_str was not resolved
        raise HTTPException(status_code=404, detail=f"Token '{token_str}' not found. Please use a valid symbol or mint address.")

    input_address, input_decimals = resolve_token(request.input_token)
    output_address, output_decimals = resolve_token(request.output_token)

    # Calculate the input amount in the smallest units (lamports for SOL or minor units for SPL tokens)
    if input_decimals is None:
        input_decimals = 0  # If still None, treat as 0 to avoid errors (though this is unlikely if token exists)
    # Use Decimal for precise multiplication to avoid floating-point issues
    from decimal import Decimal, getcontext
    getcontext().prec = 20  # sufficient precision for token amounts
    amount_decimal = Decimal(str(request.amount))
    amount_in_base_units = int(amount_decimal * (Decimal(10) ** input_decimals))
    if amount_in_base_units <= 0:
        raise HTTPException(status_code=400, detail="Amount is too small or invalid for the given token decimals.")

    # Prepare the Jupiter quote API request
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
        # Forward the error from Jupiter if available
        detail_msg = f"Jupiter quote API error (status {quote_resp.status_code})"
        try:
            error_body = quote_resp.json()
            # If Jupiter provided an error message, include it
            if isinstance(error_body, dict) and error_body.get("error"):
                detail_msg += f" - {error_body.get('error')}"
        except ValueError:
            # Not JSON
            detail_msg += f" - {quote_resp.text[:200]}"
        raise HTTPException(status_code=502, detail=detail_msg)
    # Parse quote response JSON
    quote_data = quote_resp.json()
    # If no route found or output amount is zero, handle as error
    out_amt = quote_data.get("outAmount")
    if out_amt is None or int(out_amt) == 0:
        raise HTTPException(status_code=400, detail="No swap route found or output amount is zero. Swap cannot be performed.")

    # Prepare the swap API request payload
    payload = {
        "quoteResponse": quote_data,
        "userPublicKey": solana_pubkey_str if solana_pubkey_str else "INSERT_PUBLIC_KEY_HERE",
        "wrapAndUnwrapSol": True  # auto wrap/unwrap SOL if SOL is involved
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

    # If no private key configured on the server, return the unsigned transaction for the user to sign
    if solana_keypair is None:
        return {"swap_transaction": swap_tx_b64, "message": "No server signing key configured. Please sign this transaction and send it to the Solana network."}

    # Sign the transaction using the server's Keypair
    try:
        raw_tx_bytes = base64.b64decode(swap_tx_b64)
        raw_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        # Obtain the message bytes for signing (for versioned transactions)
        msg_bytes = message.to_bytes_versioned(raw_tx.message)
        # Sign the message bytes with our private key
        signature = solana_keypair.sign_message(msg_bytes)
        # Create a signed transaction using the original message and the signature
        signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sign the transaction: {e}")

    # Send the signed transaction to the Solana RPC node
    try:
        opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
        result = solana_client.send_raw_transaction(bytes(signed_tx), opts=opts)
        # The result might be an RPC response object; convert to dict if needed
        if hasattr(result, "to_json"):
            result_json = result.to_json()
        else:
            # If already a dict or similar, use it directly
            result_json = result
        tx_result = result_json if isinstance(result_json, dict) else {}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to send transaction to Solana network: {e}")

    # Extract transaction signature (ID) from the RPC response
    tx_signature = None
    if "result" in tx_result and tx_result["result"]:
        tx_signature = tx_result["result"]
    elif "error" in tx_result and tx_result["error"]:
        # RPC node returned an error
        raise HTTPException(status_code=502, detail=f"Solana RPC error: {tx_result['error']}")
    else:
        # Unexpected response format
        raise HTTPException(status_code=502, detail="Unknown error from Solana RPC.")

    # Return the transaction signature (and an explorer link for convenience)
    return {
        "transaction_id": tx_signature,
        "explorer_url": f"https://explorer.solana.com/tx/{tx_signature}"
    }


@app.get("/")
def root():
    return {"message": "SolanaGPT backend is running."}

# OpenAPI schema will use FastAPI's default configuration
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Solana Token API",
        version="1.0.0",
        description="Solana wallet, token, and swap backend",
        routes=app.routes,
    )
    openapi_schema["servers"] = [
        {
            "url": "https://solgpt-production.up.railway.app",  # ✅ your deployed URL
            "description": "Production Server"
        }
    ]
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
