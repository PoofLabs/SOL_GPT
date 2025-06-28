import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Tuple
import json

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
from solders.pubkey import Pubkey

from dotenv import load_dotenv
from decimal import Decimal, getcontext

# Load environment variables (e.g., RPC_URL, PRIVATE_KEY) from a .env file if present
load_dotenv()

# Set decimal precision for token calculations
getcontext().prec = 20

# Configuration from environment
RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_STR = os.environ.get("PRIVATE_KEY")  # Base58-encoded or JSON-array string of 64-byte private key

# Initialize FastAPI app
app = FastAPI(title="Solana Token API", description="Query token info and perform token swaps on Solana.")

# Enable CORS if needed for web clients
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

# Global variables for token data and client setup
tokens_data = []
mainnet_tokens = []
tokens_by_symbol = {}
tokens_by_address = {}
solana_client = None
solana_keypair = None
solana_pubkey_str = None

def initialize_token_list():
    """Load and process token list data"""
    global tokens_data, mainnet_tokens, tokens_by_symbol, tokens_by_address
    
    TOKEN_LIST_URL = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
    try:
        resp = requests.get(TOKEN_LIST_URL, timeout=10)
        resp.raise_for_status()
        token_list_json = resp.json()
        tokens_data = token_list_json.get("tokens", [])
        print(f"Loaded {len(tokens_data)} tokens from token list")
    except Exception as e:
        print(f"Warning: Could not load token list: {e}")
        return

    # Filter tokens for Solana mainnet (chainId 101) and index them
    mainnet_tokens = [t for t in tokens_data if t.get("chainId") == 101]
    
    # Create lookup dictionaries
    for token in mainnet_tokens:
        symbol = token.get("symbol")
        address = token.get("address")
        if symbol:
            symbol_upper = symbol.upper()
            tokens_by_symbol.setdefault(symbol_upper, []).append(token)
        if address:
            tokens_by_address[address] = token
    
    print(f"Indexed {len(mainnet_tokens)} mainnet tokens")

def initialize_solana_client():
    """Initialize Solana client and keypair"""
    global solana_client, solana_keypair, solana_pubkey_str
    
    # Initialize Solana RPC client
    solana_client = Client(endpoint=RPC_URL)
    print(f"Initialized Solana client with RPC: {RPC_URL}")
    
    # Initialize keypair if private key is provided
    if PRIVATE_KEY_STR:
        try:
            # Handle JSON array format
            if PRIVATE_KEY_STR.strip().startswith("["):
                key_data = json.loads(PRIVATE_KEY_STR.strip())
                if not isinstance(key_data, list) or len(key_data) != 64:
                    raise ValueError("Private key array must contain exactly 64 integers")
                solana_keypair = Keypair.from_bytes(bytes(key_data))
            else:
                # Handle base58 encoded format
                decoded_key = base58.b58decode(PRIVATE_KEY_STR)
                if len(decoded_key) != 64:
                    raise ValueError("Private key must be 64 bytes")
                solana_keypair = Keypair.from_bytes(decoded_key)
            
            solana_pubkey_str = str(solana_keypair.pubkey())
            print(f"Loaded wallet public key: {solana_pubkey_str}")
            
        except Exception as e:
            raise RuntimeError(f"Failed to parse PRIVATE_KEY from environment: {e}")
    else:
        print("No private key provided - swap transactions will be returned unsigned")

def is_valid_solana_address(address: str) -> bool:
    """Validate if a string is a valid Solana address"""
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except Exception:
        return False

def get_token_decimals_from_chain(mint_address: str) -> Optional[int]:
    """Fetch token decimals from chain"""
    try:
        # Use the correct method to get mint info
        mint_pubkey = Pubkey.from_string(mint_address)
        mint_info = solana_client.get_account_info(mint_pubkey)
        
        if mint_info.value is None:
            return None
            
        # Parse mint account data to get decimals (byte 44 contains decimals)
        account_data = mint_info.value.data
        if len(account_data) >= 45:
            return int(account_data[44])
        
        return None
    except Exception as e:
        print(f"Error fetching decimals for {mint_address}: {e}")
        return None

# Initialize everything at startup
initialize_token_list()
initialize_solana_client()

@app.get("/")
def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "message": "Solana Token API is running",
        "tokens_loaded": len(mainnet_tokens),
        "wallet_configured": solana_keypair is not None
    }

@app.get("/token", response_model=List[TokenInfo])
def get_token_info(query: str):
    """
    Query Solana token information by symbol, name, or mint address.
    """
    query_str = query.strip()
    if not query_str:
        raise HTTPException(status_code=400, detail="Query parameter is required.")

    token_matches: List[TokenInfo] = []
    
    # Check if query is a valid Solana address
    if is_valid_solana_address(query_str):
        # Look up by address
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
            # Try to get decimals from chain
            decimals = get_token_decimals_from_chain(query_str)
            if decimals is not None:
                token_matches.append(TokenInfo(**{
                    "symbol": None,
                    "name": None,
                    "address": query_str,
                    "decimals": decimals,
                    "logoURI": None,
                    "tags": None
                }))
            else:
                raise HTTPException(status_code=404, detail="Token mint address not found or invalid.")
    else:
        # Search by symbol (case-insensitive)
        q_upper = query_str.upper()
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
            # Search by name (case-insensitive)
            q_lower = query_str.lower()
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

def resolve_token(token_str: str) -> Tuple[str, int]:
    """Helper to resolve a token symbol/name/address to a mint address and decimals."""
    token_str = token_str.strip()
    if not token_str:
        raise HTTPException(status_code=400, detail="Token identifier cannot be empty.")
    
    # Check if it's a known symbol
    token_info_list = tokens_by_symbol.get(token_str.upper())
    if token_info_list:
        if len(token_info_list) > 1:
            symbols_info = [f"{t['symbol']} ({t['address']})" for t in token_info_list]
            raise HTTPException(
                status_code=400, 
                detail=f"Symbol '{token_str}' is ambiguous. Multiple tokens found: {', '.join(symbols_info)}. Please use the mint address instead."
            )
        token_info = token_info_list[0]
        return token_info["address"], token_info.get("decimals", 0)
    
    # Check if it's a valid address
    if is_valid_solana_address(token_str):
        # Check if we have it in our token list
        if token_str in tokens_by_address:
            decimals = tokens_by_address[token_str].get("decimals")
        else:
            # Try to get decimals from chain
            decimals = get_token_decimals_from_chain(token_str)
        
        if decimals is None:
            decimals = 0  # Default to 0 if we can't determine decimals
            
        return token_str, decimals
    
    # Token not found
    raise HTTPException(status_code=404, detail=f"Token '{token_str}' not found. Please use a valid symbol or mint address.")

@app.post("/swap")
def swap_tokens(request: SwapRequest):
    """
    Perform a token swap from input_token to output_token using Jupiter aggregator.
    """
    if not solana_client:
        raise HTTPException(status_code=500, detail="Solana client not initialized")
    
    # Validate request
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")
    
    if request.slippage_bps < 0 or request.slippage_bps > 10000:
        raise HTTPException(status_code=400, detail="Slippage must be between 0 and 10000 basis points (0-100%)")
    
    # Resolve input and output tokens
    try:
        input_address, input_decimals = resolve_token(request.input_token)
        output_address, output_decimals = resolve_token(request.output_token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error resolving tokens: {e}")
    
    if input_address == output_address:
        raise HTTPException(status_code=400, detail="Input and output tokens cannot be the same")
    
    # Calculate amount in base units with proper precision
    try:
        amount_decimal = Decimal(str(request.amount))
        amount_in_base_units = int(amount_decimal * (Decimal(10) ** input_decimals))
        if amount_in_base_units <= 0:
            raise HTTPException(status_code=400, detail="Amount is too small for the given token decimals")
    except (ValueError, OverflowError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid amount: {e}")
    
    # Get quote from Jupiter
    quote_url = (
        f"https://quote-api.jup.ag/v6/quote"
        f"?inputMint={input_address}"
        f"&outputMint={output_address}"
        f"&amount={amount_in_base_units}"
        f"&slippageBps={request.slippage_bps}"
        f"&swapMode=ExactIn"
    )
    
    try:
        quote_resp = requests.get(quote_url, timeout=15)
        quote_resp.raise_for_status()
        quote_data = quote_resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to get quote from Jupiter: {e}")
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Invalid response from Jupiter quote API: {e}")
    
    # Validate quote response
    if "error" in quote_data:
        raise HTTPException(status_code=400, detail=f"Jupiter quote error: {quote_data['error']}")
    
    out_amount = quote_data.get("outAmount")
    if not out_amount or int(out_amount) == 0:
        raise HTTPException(status_code=400, detail="No swap route found or output amount is zero")
    
    # Get swap transaction from Jupiter
    user_pubkey = solana_pubkey_str if solana_pubkey_str else "11111111111111111111111111111112"  # System program as placeholder
    
    swap_payload = {
        "quoteResponse": quote_data,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "computeUnitPriceMicroLamports": "auto"  # Let Jupiter set compute price
    }
    
    try:
        swap_resp = requests.post(
            "https://quote-api.jup.ag/v6/swap", 
            json=swap_payload, 
            timeout=15,
            headers={"Content-Type": "application/json"}
        )
        swap_resp.raise_for_status()
        swap_result = swap_resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to get swap transaction from Jupiter: {e}")
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Invalid response from Jupiter swap API: {e}")
    
    if "error" in swap_result:
        raise HTTPException(status_code=400, detail=f"Jupiter swap error: {swap_result['error']}")
    
    swap_tx_b64 = swap_result.get("swapTransaction")
    if not swap_tx_b64:
        raise HTTPException(status_code=500, detail="Failed to retrieve swap transaction from Jupiter")
    
    # If no private key configured, return unsigned transaction
    if solana_keypair is None:
        return {
            "swap_transaction": swap_tx_b64,
            "message": "No server signing key configured. Please sign this transaction and send it to the Solana network.",
            "quote_info": {
                "input_amount": str(request.amount),
                "output_amount": str(Decimal(out_amount) / (Decimal(10) ** output_decimals)),
                "input_token": input_address,
                "output_token": output_address
            }
        }
    
    # Sign and send transaction
    try:
        # Decode and create versioned transaction
        raw_tx_bytes = base64.b64decode(swap_tx_b64)
        versioned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        
        # Sign the transaction
        versioned_tx.sign([solana_keypair])
        
        # Send transaction
        opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
        result = solana_client.send_raw_transaction(bytes(versioned_tx), opts=opts)
        
        # Extract transaction signature
        if hasattr(result, 'value'):
            tx_signature = str(result.value)
        else:
            tx_signature = str(result)
        
        return {
            "transaction_id": tx_signature,
            "explorer_url": f"https://explorer.solana.com/tx/{tx_signature}",
            "quote_info": {
                "input_amount": str(request.amount),
                "output_amount": str(Decimal(out_amount) / (Decimal(10) ** output_decimals)),
                "input_token": input_address,
                "output_token": output_address
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sign or send transaction: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
