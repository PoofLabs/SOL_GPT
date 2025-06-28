import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from decimal import Decimal, getcontext

import requests
import base64
import base58
import json

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

# Initialize FastAPI app
app = FastAPI(
    title="Solana Token API",
    description="Query Solana wallet, token info, and perform token swaps.",
    version="1.0.0",
    servers=[{"url": API_BASE_URL}]
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
solana_client = Client(endpoint=RPC_URL)

# Load token list
TOKEN_LIST_URL = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
tokens_data = []
mainnet_tokens = []
tokens_by_symbol = {}
tokens_by_address = {}

try:
    resp = requests.get(TOKEN_LIST_URL, timeout=10)
    resp.raise_for_status()
    token_list_json = resp.json()
    tokens_data = token_list_json.get("tokens", [])
    
    # Filter mainnet tokens
    mainnet_tokens = [t for t in tokens_data if t.get("chainId") == 101]
    
    # Index tokens
    for token in mainnet_tokens:
        symbol = token.get("symbol")
        address = token.get("address")
        if symbol:
            tokens_by_symbol.setdefault(symbol.upper(), []).append(token)
        if address:
            tokens_by_address[address] = token
            
    print(f"Loaded {len(mainnet_tokens)} mainnet tokens")
except Exception as e:
    print(f"Warning: Could not load token list: {e}")

# Initialize keypair
solana_keypair = None
solana_pubkey_str = None

if PRIVATE_KEY_STR:
    try:
        # Handle different private key formats
        if PRIVATE_KEY_STR.strip().startswith("["):
            # JSON array format
            key_array = json.loads(PRIVATE_KEY_STR.strip())
            solana_keypair = Keypair.from_bytes(bytes(key_array))
        else:
            # Base58 format
            solana_keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_STR))
        
        solana_pubkey_str = str(solana_keypair.pubkey())
        print(f"Loaded wallet public key: {solana_pubkey_str}")
    except Exception as e:
        print(f"Warning: Failed to parse PRIVATE_KEY: {e}")

# Helper functions
def get_token_price(mint_address: str) -> Optional[float]:
    """Get token price from Jupiter Price API"""
    try:
        url = f"https://price.jup.ag/v6/price?ids={mint_address}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            price_data = data.get("data", {}).get(mint_address)
            if price_data:
                return price_data.get("price")
    except Exception:
        pass
    return None

def resolve_token(token_str: str) -> tuple[str, int]:
    """Resolve token identifier to mint address and decimals"""
    token_str = token_str.strip()
    if not token_str:
        raise HTTPException(status_code=400, detail="Token identifier cannot be empty.")
    
    # Check symbol first
    token_info_list = tokens_by_symbol.get(token_str.upper())
    if token_info_list:
        if len(token_info_list) > 1:
            # For ambiguous symbols, prefer the most common one
            # You could add logic here to prefer certain tokens
            pass
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
                    supply_resp = solana_client.get_token_supply(token_str)
                    if isinstance(supply_resp, dict):
                        value = supply_resp.get("result", {}).get("value", {})
                        decimals = value.get("decimals", 0)
                except Exception:
                    decimals = 0
            
            return token_str, decimals
    except Exception:
        pass
    
    # Try exact name match
    q_lower = token_str.lower()
    name_matches = [t for t in mainnet_tokens if t.get("name") and t.get("name").lower() == q_lower]
    if name_matches:
        if len(name_matches) > 1:
            # Use the first match
            pass
        token = name_matches[0]
        return token["address"], token.get("decimals", 0)
    
    raise HTTPException(status_code=404, detail=f"Token '{token_str}' not found.")

# API Endpoints
@app.get("/")
def root():
    """Health check endpoint"""
    info = {
        "message": "Solana Token API is running",
        "version": "1.0.0",
        "endpoints": {
            "health": "/",
            "token_info": "/token?query={symbol_or_address}",
            "token_price": "/price/{token}",
            "wallet_info": "/wallet",
            "wallet_check": "/wallet/{address}",
            "swap": "/swap (POST)"
        }
    }
    if solana_pubkey_str:
        info["configured_wallet"] = solana_pubkey_str
    return info

@app.get("/token", response_model=List[TokenInfo])
def get_token_info(query: str):
    """Get token information by symbol, name, or mint address"""
    query_str = query.strip()
    if not query_str:
        raise HTTPException(status_code=400, detail="Query parameter is required.")
    
    token_matches: List[TokenInfo] = []
    
    # Check if query is an address
    is_address = False
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
            # Try on-chain lookup
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
                raise HTTPException(status_code=404, detail="Token not found.")
    else:
        # Symbol or name search
        q_upper = query_str.upper()
        q_lower = query_str.lower()
        
        # Try symbol match
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
            # Try name match
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
        raise HTTPException(status_code=404, detail="Token not found.")
    
    return token_matches

@app.get("/price/{token}", response_model=PriceInfo)
def get_token_price_info(token: str):
    """Get detailed price information for a token"""
    try:
        # Resolve token to address
        mint_address, _ = resolve_token(token)
        
        # Get price data from Jupiter
        url = f"https://price.jup.ag/v6/price?ids={mint_address}"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch price data")
        
        data = resp.json()
        price_data = data.get("data", {}).get(mint_address)
        
        if not price_data:
            raise HTTPException(status_code=404, detail="Price data not available for this token")
        
        # Get token info
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
        raise HTTPException(status_code=500, detail=f"Error fetching price: {str(e)}")

@app.get("/wallet", response_model=WalletInfo)
def get_configured_wallet_info():
    """Get information about the configured wallet"""
    if not solana_pubkey_str:
        raise HTTPException(status_code=404, detail="No wallet configured on server.")
    
    return get_wallet_info_by_address(solana_pubkey_str)

@app.get("/wallet/{address}", response_model=WalletInfo)
def get_wallet_info_by_address(address: str):
    """Get information about any wallet by address"""
    # Validate address
    try:
        decoded = base58.b58decode(address)
        if len(decoded) != 32:
            raise ValueError("Invalid address length")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Solana address")
    
    # Get SOL balance
    lamports = None
    sol_balance = None
    try:
        balance_resp = solana_client.get_balance(address)
        if isinstance(balance_resp, dict):
            lamports = balance_resp.get("result", {}).get("value")
        sol_balance = (lamports / 1_000_000_000) if lamports is not None else None
    except Exception as e:
        print(f"Error fetching balance: {e}")
    
    # Get token accounts
    token_accounts = []
    try:
        # Get all token accounts for this wallet
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
                        
                        # Get token info
                        token_info = tokens_by_address.get(mint, {})
                        
                        # Get price
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
                    print(f"Error parsing token account: {e}")
                    continue
    except Exception as e:
        print(f"Error fetching token accounts: {e}")
    
    return WalletInfo(
        public_key=address,
        sol_balance=sol_balance,
        lamports=lamports,
        tokens=token_accounts
    )

@app.post("/swap")
def swap_tokens(request: SwapRequest):
    """Perform a token swap using Jupiter aggregator"""
    try:
        # Resolve tokens
        input_address, input_decimals = resolve_token(request.input_token)
        output_address, output_decimals = resolve_token(request.output_token)
        
        # Calculate amount in base units
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
        
        # Get swap transaction
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
        
        # Calculate output amount
        estimated_out = float(int(out_amt) / (10 ** output_decimals))
        
        # Get token symbols
        input_symbol = tokens_by_address.get(input_address, {}).get("symbol", input_address[:8])
        output_symbol = tokens_by_address.get(output_address, {}).get("symbol", output_address[:8])
        
        # If no keypair configured, return unsigned transaction
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
            # Decode transaction
            raw_tx_bytes = base64.b64decode(swap_tx_b64)
            raw_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            
            # Sign transaction
            msg_bytes = message.to_bytes_versioned(raw_tx.message)
            signature = solana_keypair.sign_message(msg_bytes)
            signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
            
            # Send transaction
            signed_tx_bytes = bytes(signed_tx)
            base64_tx = base64.b64encode(signed_tx_bytes).decode('utf-8')
            
            opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
            result = solana_client.send_raw_transaction(base64_tx, opts=opts)
            
            # Handle response
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
            raise HTTPException(status_code=500, detail=f"Transaction error: {str(e)}")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Swap error: {str(e)}")

# Error handlers
@app.exception_handler(404)
async def not_found_handler(request, exc):
    return {"error": "Not found", "detail": str(exc.detail)}

@app.exception_handler(500)
async def server_error_handler(request, exc):
    return {"error": "Internal server error", "detail": "An unexpected error occurred"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
