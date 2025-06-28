import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from decimal import Decimal, getcontext

import requests
import base64
import base58
import json
import logging

# Solana SDK imports
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY_STR = os.environ.get("PRIVATE_KEY")
API_BASE_URL = os.environ.get("API_BASE_URL", "https://sol-gpt.onrender.com")

# Set decimal precision
getcontext().prec = 20

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app with servers list
app = FastAPI(
    title="Solana Token API",
    description="Query Solana wallet, token info, and perform token swaps.",
    version="1.0.0",
    servers=[{"url": API_BASE_URL, "description": "Production server"}]
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
    price_usd: Optional[float] = None

class WalletInfo(BaseModel):
    public_key: str
    sol_balance: Optional[float] = None
    lamports: Optional[int] = None
    tokens: Optional[List[Dict[str, Any]]] = None

class SwapRequest(BaseModel):
    input_token: str
    output_token: str
    amount: float
    slippage_bps: int = 50

class PriceInfo(BaseModel):
    token: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    price_usd: Optional[float] = None
    price_change_24h: Optional[float] = None
    market_cap: Optional[float] = None
    volume_24h: Optional[float] = None

# Initialize Solana client
try:
    solana_client = Client(endpoint=RPC_URL)
    logger.info(f"Connected to Solana RPC: {RPC_URL}")
except Exception as e:
    logger.error(f"Failed to connect to Solana RPC: {e}")
    solana_client = None

# Load token list from Solana Labs token registry
TOKEN_LIST_URL = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
tokens_data = []
mainnet_tokens = []
tokens_by_symbol = {}
tokens_by_address = {}

try:
    logger.info("Loading token list...")
    resp = requests.get(TOKEN_LIST_URL, timeout=10)
    resp.raise_for_status()
    token_list_json = resp.json()
    tokens_data = token_list_json.get("tokens", [])
    # Filter mainnet tokens
    mainnet_tokens = [t for t in tokens_data if t.get("chainId") == 101]
    # Index tokens by symbol and address
    for token in mainnet_tokens:
        symbol = token.get("symbol")
        address = token.get("address")
        if symbol:
            tokens_by_symbol.setdefault(symbol.upper(), []).append(token)
        if address:
            tokens_by_address[address] = token
    logger.info(f"Loaded {len(mainnet_tokens)} mainnet tokens")
except Exception as e:
    logger.warning(f"Could not load token list: {e}")

# Initialize keypair from PRIVATE_KEY if provided
solana_keypair = None
solana_pubkey_str = None

if PRIVATE_KEY_STR:
    try:
        if PRIVATE_KEY_STR.strip().startswith("["):
            key_array = json.loads(PRIVATE_KEY_STR.strip())
            solana_keypair = Keypair.from_bytes(bytes(key_array))
        else:
            solana_keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_STR))
        solana_pubkey_str = str(solana_keypair.pubkey())
        logger.info(f"Loaded wallet public key: {solana_pubkey_str}")
    except Exception as e:
        logger.warning(f"Failed to parse PRIVATE_KEY: {e}")

# Helper: Get token price from Jupiter
def get_token_price(mint_address: str) -> Optional[float]:
    try:
        url = f"https://price.jup.ag/v6/price?ids={mint_address}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            price_data = data.get("data", {}).get(mint_address)
            if price_data:
                return price_data.get("price")
    except Exception as e:
        logger.error(f"Error getting price for {mint_address}: {e}")
    return None

# Helper: resolve token symbol/name/address to mint and decimals
def resolve_token(token_str: str) -> tuple[str, int]:
    token_str = token_str.strip()
    if not token_str:
        raise HTTPException(status_code=400, detail="Token identifier cannot be empty.")
    # Symbol lookup
    token_info_list = tokens_by_symbol.get(token_str.upper())
    if token_info_list:
        token = token_info_list[0]
        return token["address"], token.get("decimals", 0)
    # Address lookup
    try:
        decoded = base58.b58decode(token_str)
        if len(decoded) == 32:
            decimals = None
            if token_str in tokens_by_address:
                decimals = tokens_by_address[token_str].get("decimals")
            if decimals is None and solana_client:
                try:
                    supply_resp = solana_client.get_token_supply(token_str)
                    if isinstance(supply_resp, dict):
                        value = supply_resp.get("result", {}).get("value", {})
                        decimals = value.get("decimals", 0)
                except Exception:
                    decimals = 0
            return token_str, decimals or 0
    except Exception:
        pass
    # Name lookup
    q_lower = token_str.lower()
    name_matches = [t for t in mainnet_tokens if t.get("name") and t.get("name").lower() == q_lower]
    if name_matches:
        token = name_matches[0]
        return token["address"], token.get("decimals", 0)
    raise HTTPException(status_code=404, detail=f"Token '{token_str}' not found.")

# Root/health endpoint
@app.get("/")
async def root():
    info = {
        "message": "Solana Token API is running",
        "version": "1.0.0",
        "endpoints": {
            "health": "/",
            "token_info": "/token?query={symbol_or_address}",
            "token_price": "/price/{token}",
            "wallet_info": "/wallet",
            "wallet_check": "/wallet/{address}",
            "swap": "/swap (POST)",
            "openapi": "/openapi.json"
        }
    }
    if solana_pubkey_str:
        info["configured_wallet"] = solana_pubkey_str
    return info

# Token info endpoint
@app.get("/token", response_model=List[TokenInfo])
async def get_token_info(query: str):
    try:
        query_str = query.strip()
        if not query_str:
            raise HTTPException(status_code=400, detail="Query parameter is required.")
        token_matches: List[TokenInfo] = []
        # Check if query is an address (base58 32-byte)
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
                price = get_token_price(query_str)
                token_matches.append(TokenInfo(
                    symbol=token.get("symbol"),
                    name=token.get("name"),
                    address=token.get("address"),
                    decimals=token.get("decimals"),
                    logoURI=token.get("logoURI"),
                    tags=token.get("tags"),
                    price_usd=price
                ))
            else:
                # On-chain lookup for unknown address
                if solana_client:
                    try:
                        supply_resp = solana_client.get_token_supply(query_str)
                        decimals = None
                        if isinstance(supply_resp, dict):
                            value = supply_resp.get("result", {}).get("value", {})
                            decimals = value.get("decimals")
                        price = get_token_price(query_str)
                        token_matches.append(TokenInfo(
                            address=query_str,
                            decimals=decimals,
                            price_usd=price
                        ))
                    except Exception:
                        pass
        else:
            # Symbol or name search
            q_upper = query_str.upper()
            q_lower = query_str.lower()
            # Symbol match
            if q_upper in tokens_by_symbol:
                for token in tokens_by_symbol[q_upper]:
                    price = get_token_price(token.get("address"))
                    token_matches.append(TokenInfo(
                        symbol=token.get("symbol"),
                        name=token.get("name"),
                        address=token.get("address"),
                        decimals=token.get("decimals"),
                        logoURI=token.get("logoURI"),
                        tags=token.get("tags"),
                        price_usd=price
                    ))
            else:
                # Name match
                for token in mainnet_tokens:
                    if token.get("name") and token.get("name").lower() == q_lower:
                        price = get_token_price(token.get("address"))
                        token_matches.append(TokenInfo(
                            symbol=token.get("symbol"),
                            name=token.get("name"),
                            address=token.get("address"),
                            decimals=token.get("decimals"),
                            logoURI=token.get("logoURI"),
                            tags=token.get("tags"),
                            price_usd=price
                        ))
        if not token_matches:
            raise HTTPException(status_code=404, detail=f"Token '{query_str}' not found.")
        return token_matches
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_token_info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Detailed price endpoint
@app.get("/price/{token}", response_model=PriceInfo)
async def get_token_price_info(token: str):
    try:
        mint_address, _ = resolve_token(token)
        url = f"https://price.jup.ag/v6/price?ids={mint_address}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch price data")
        data = resp.json()
        price_data = data.get("data", {}).get(mint_address)
        if not price_data:
            raise HTTPException(status_code=404, detail="Price data not available for this token")
        token_info = tokens_by_address.get(mint_address, {})
        return PriceInfo(
            token=mint_address,
            symbol=token_info.get("symbol"),
            name=token_info.get("name"),
            price_usd=price_data.get("price"),
            price_change_24h=price_data.get("price24hChange"),
            market_cap=price_data.get("marketCap"),
            volume_24h=price_data.get("volume24h")
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_token_price_info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Wallet info (configured or by address)
@app.get("/wallet", response_model=WalletInfo)
async def get_configured_wallet_info():
    if not solana_pubkey_str:
        raise HTTPException(status_code=404, detail="No wallet configured on server.")
    return await get_wallet_info_by_address(solana_pubkey_str)

@app.get("/wallet/{address}", response_model=WalletInfo)
async def get_wallet_info_by_address(address: str):
    try:
        # Validate address
        try:
            decoded = base58.b58decode(address)
            if len(decoded) != 32:
                raise ValueError("Invalid address length")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid Solana address")
        if not solana_client:
            raise HTTPException(status_code=503, detail="Solana RPC not available")
        # Get SOL balance
        lamports = None
        sol_balance = None
        try:
            balance_resp = solana_client.get_balance(address)
            if isinstance(balance_resp, dict):
                lamports = balance_resp.get("result", {}).get("value")
            sol_balance = (lamports / 1_000_000_000) if lamports is not None else None
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
        # Get SPL token accounts
        token_accounts = []
        try:
            token_accounts_resp = solana_client.get_token_accounts_by_owner(
                address,
                opts={"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
            )
            if isinstance(token_accounts_resp, dict):
                accounts = token_accounts_resp.get("result", {}).get("value", [])
                for account in accounts:
                    try:
                        account_data = account.get("account", {}).get("data", {})
                        parsed_data = account_data.get("parsed", {}).get("info", {})
                        if parsed_data:
                            mint = parsed_data.get("mint")
                            token_amount = parsed_data.get("tokenAmount", {})
                            amount = token_amount.get("amount", "0")
                            decimals = token_amount.get("decimals", 0)
                            ui_amount = token_amount.get("uiAmount", 0)
                            if ui_amount == 0:
                                continue
                            token_info = tokens_by_address.get(mint, {})
                            price = get_token_price(mint)
                            value_usd = (ui_amount * price) if price else None
                            token_accounts.append({
                                "mint": mint,
                                "symbol": token_info.get("symbol"),
                                "name": token_info.get("name"),
                                "amount": amount,
                                "decimals": decimals,
                                "uiAmount": ui_amount,
                                "price_usd": price,
                                "value_usd": value_usd
                            })
                    except Exception as e:
                        logger.error(f"Error parsing token account: {e}")
                        continue
        except Exception as e:
            logger.error(f"Error fetching token accounts: {e}")
        return WalletInfo(
            public_key=address,
            sol_balance=sol_balance,
            lamports=lamports,
            tokens=token_accounts
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_wallet_info_by_address: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Swap endpoint
@app.post("/swap")
async def swap_tokens(request: SwapRequest):
    try:
        # Resolve tokens
        input_address, input_decimals = resolve_token(request.input_token)
        output_address, output_decimals = resolve_token(request.output_token)
        # Calculate input amount in base units
        amount_decimal = Decimal(str(request.amount))
        amount_in_base_units = int(amount_decimal * (Decimal(10) ** input_decimals))
        if amount_in_base_units <= 0:
            raise HTTPException(status_code=400, detail="Amount too small")
        # Get quote from Jupiter
        quote_url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={input_address}"
            f"&outputMint={output_address}"
            f"&amount={amount_in_base_units}"
            f"&slippageBps={request.slippage_bps}"
            f"&swapMode=ExactIn"
        )
        logger.info(f"Getting quote: {quote_url}")
        quote_resp = requests.get(quote_url, timeout=10)
        if quote_resp.status_code != 200:
            detail = f"Jupiter quote error: {quote_resp.status_code}"
            try:
                err = quote_resp.json()
                if err.get("error"):
                    detail += f" - {err.get('error')}"
            except:
                pass
            raise HTTPException(status_code=502, detail=detail)
        quote_data = quote_resp.json()
        out_amt = quote_data.get("outAmount")
        if not out_amt or int(out_amt) == 0:
            raise HTTPException(status_code=400, detail="No route found")
        # Get swap transaction from Jupiter
        user_pubkey = solana_pubkey_str if solana_pubkey_str else "11111111111111111111111111111111"
        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": "auto",
            "dynamicComputeUnitLimit": True
        }
        swap_resp = requests.post(
            "https://quote-api.jup.ag/v6/swap",
            json=payload,
            timeout=10
        )
        if swap_resp.status_code != 200:
            detail = f"Jupiter swap error: {swap_resp.status_code}"
            try:
                err = swap_resp.json()
                if err.get("error"):
                    detail += f" - {err.get('error')}"
            except:
                pass
            raise HTTPException(status_code=502, detail=detail)
        swap_result = swap_resp.json()
        swap_tx_b64 = swap_result.get("swapTransaction")
        if not swap_tx_b64:
            raise HTTPException(status_code=500, detail="No transaction returned")
        estimated_out = float(int(out_amt) / (10 ** output_decimals))
        input_symbol = tokens_by_address.get(input_address, {}).get("symbol", input_address[:8])
        output_symbol = tokens_by_address.get(output_address, {}).get("symbol", output_address[:8])
        # Return unsigned transaction if no keypair
        if not solana_keypair:
            return {
                "status": "unsigned",
                "input_token": input_symbol,
                "output_token": output_symbol,
                "input_amount": request.amount,
                "estimated_output_amount": estimated_out,
                "swap_transaction": swap_tx_b64,
                "message": "Transaction ready. Sign with your wallet and submit to network."
            }
        # Sign and send transaction
        try:
            raw_tx_bytes = base64.b64decode(swap_tx_b64)
            raw_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            # Sign transaction
            msg_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = solana_keypair.sign_message(msg_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            signed_tx_bytes = bytes(signed_tx)
            base64_tx = base64.b64encode(signed_tx_bytes).decode('utf-8')
            opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
            result = solana_client.send_raw_transaction(base64_tx, opts=opts)
            tx_result = result if isinstance(result, dict) else {}
            if "error" in tx_result and tx_result["error"]:
                raise HTTPException(status_code=502, detail=f"RPC error: {tx_result['error']}")
            tx_signature = tx_result.get("result")
            if not tx_signature:
                raise HTTPException(status_code=502, detail="No signature returned")
            return {
                "status": "success",
                "transaction_id": tx_signature,
                "explorer_url": f"https://explorer.solana.com/tx/{tx_signature}",
                "input_token": input_symbol,
                "output_token": output_symbol,
                "input_amount": request.amount,
                "estimated_output_amount": estimated_out
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Transaction error: {e}")
            raise HTTPException(status_code=500, detail=f"Transaction error: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Swap error: {e}")
        raise HTTPException(status_code=500, detail=f"Swap error: {str(e)}")

# Error handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=404,
        content={"error": "Not found", "detail": str(exc.detail)}
    )

@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": "An unexpected error occurred"}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)}
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
