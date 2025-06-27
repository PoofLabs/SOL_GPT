import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import requests
from typing import List, Optional
from pydantic import BaseModel

# Environment variables for any API keys or endpoints
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", f"https://rpc.helius.xyz/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else None)
# If no Helius key is provided, fallback to public Solana RPC
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Known token mint constants (for convenience in code)
WSOL_MINT = "So11111111111111111111111111111111111111112"  # Wrapped SOL mint address
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC mint address (Solana)

app = FastAPI(
    title="SolanaGPT API",
    description="API for Solana-based utilities such as token prices, wallet balances, and token swaps.",
    version="1.0.0",
    servers=[{"url": "/"}]  # Use relative root path for OpenAPI (important for Railway deployment)
)

# Enable CORS for all origins (adjust in production as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic response models for clean output
class TokenPriceResponse(BaseModel):
    symbol: Optional[str] = None
    mint: Optional[str] = None
    price: float
    volume_24h: Optional[float] = None
    market_cap: Optional[float] = None

class TokenBalance(BaseModel):
    mint: str
    symbol: Optional[str] = None
    balance: float              # token balance in human-readable units
    usd_value: Optional[float] = None

class WalletBalanceResponse(BaseModel):
    address: str
    sol_balance: float
    tokens: List[TokenBalance]

class SwapQuoteResponse(BaseModel):
    inputMint: str
    outputMint: str
    inAmount: str
    outAmount: str
    slippageBps: int
    priceImpactPct: Optional[float] = None
    routePlan: Optional[list] = None
    swap_link: Optional[str] = None

def is_solana_address(token: str) -> bool:
    """Basic check for Solana public key format (base58, length 32-44)."""
    if len(token) < 32 or len(token) > 44:
        return False
    allowed_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(c in allowed_chars for c in token)

def fetch_price_from_coingecko_by_id(coingecko_id: str) -> Optional[TokenPriceResponse]:
    """Fetch price info from CoinGecko by coin ID."""
    url = (f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}"
           f"&vs_currencies=usd&include_24hr_vol=true&include_market_cap=true")
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if coingecko_id in data:
            info = data[coingecko_id]
            return TokenPriceResponse(price=info.get("usd"),
                                      volume_24h=info.get("usd_24h_vol"),
                                      market_cap=info.get("usd_market_cap"))
    except Exception:
        return None

def fetch_price_from_coingecko_by_contract(mint_address: str) -> Optional[TokenPriceResponse]:
    """Fetch price info from CoinGecko using a Solana token contract address."""
    url = (f"https://api.coingecko.com/api/v3/simple/token_price/solana?"
           f"contract_addresses={mint_address}&vs_currencies=usd&include_24hr_vol=true&include_market_cap=true")
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if mint_address in data:
            info = data[mint_address]
            return TokenPriceResponse(mint=mint_address,
                                      price=info.get("usd"),
                                      volume_24h=info.get("usd_24h_vol"),
                                      market_cap=info.get("usd_market_cap"))
    except Exception:
        return None

def search_coingecko_symbol(symbol: str) -> Optional[str]:
    """Find CoinGecko coin ID by symbol or name."""
    url = f"https://api.coingecko.com/api/v3/search?query={symbol}"
    try:
        resp = requests.get(url, timeout=5)
        results = resp.json().get("coins", [])
        # Try exact match on symbol or name
        for coin in results:
            if coin.get("symbol", "").lower() == symbol.lower() or coin.get("name", "").lower() == symbol.lower():
                return coin.get("id")
        # Fallback to first result if no exact match
        if results:
            return results[0].get("id")
    except Exception:
        return None

def fetch_price_from_helius(mint_address: str) -> Optional[TokenPriceResponse]:
    """Fetch token price using Helius getAsset (if API key is available)."""
    if not HELIUS_RPC_URL:
        return None
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getAsset",
        "params": {"id": mint_address}
    }
    try:
        resp = requests.post(HELIUS_RPC_URL, json=payload, timeout=5)
        result = resp.json().get("result", {})
        token_info = result.get("content", {}).get("token_info", {})
        price_info = token_info.get("price_info", {})
        if price_info:
            return TokenPriceResponse(
                symbol=token_info.get("symbol"),
                mint=mint_address,
                price=price_info.get("price_per_token"),
                volume_24h=None,
                market_cap=None
            )
    except Exception:
        return None

def get_token_decimals(mint_address: str) -> Optional[int]:
    """Get token decimal count via RPC (using getTokenSupply)."""
    rpc_url = HELIUS_RPC_URL if HELIUS_RPC_URL else SOLANA_RPC_URL
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint_address]}
    try:
        resp = requests.post(rpc_url, json=payload, timeout=5)
        return resp.json().get("result", {}).get("value", {}).get("decimals")
    except Exception:
        return None

@app.get("/price", response_model=TokenPriceResponse)
def get_token_price(token: str):
    """
    Get the current price of a token by symbol or mint address.
    Returns price (USD), 24h volume, and market cap if available.
    """
    # Determine if input is a symbol or a mint address
    symbol_query = None
    mint_address = None
    if is_solana_address(token):
        mint_address = token
    else:
        symbol_query = token.strip().upper()
        if symbol_query == "SOL":
            # Solana native token – use CoinGecko ID
            coingecko_id = "solana"
        else:
            coingecko_id = search_coingecko_symbol(symbol_query)
        if coingecko_id:
            price_data = fetch_price_from_coingecko_by_id(coingecko_id)
            if price_data:
                price_data.symbol = symbol_query  # attach the symbol for reference
                return price_data
        # If not found on CoinGecko, try to resolve via Helius (symbol to mint)
        if HELIUS_RPC_URL:
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "searchAssets",
                "params": {"query": symbol_query, "tokenType": "fungible"}
            }
            try:
                resp = requests.post(HELIUS_RPC_URL, json=payload, timeout=5)
                assets = resp.json().get("result", [])
                if assets:
                    # Take the first matching asset's mint address
                    mint_address = assets[0].get("id")
            except Exception:
                mint_address = None
    if mint_address:
        # First try CoinGecko by contract address
        price_data = fetch_price_from_coingecko_by_contract(mint_address)
        if price_data:
            if symbol_query:
                price_data.symbol = symbol_query
            return price_data
        # Next try Helius direct
        price_data = fetch_price_from_helius(mint_address)
        if price_data:
            return price_data
    # If we reach here, no price was found
    raise HTTPException(status_code=404, detail="Token price not found.")

@app.get("/wallet/{address}", response_model=WalletBalanceResponse)
def get_wallet_balance(address: str):
    """
    Get SOL balance and SPL token balances for a given wallet address.
    Returns SOL balance and list of tokens with balances and USD values.
    """
    if not is_solana_address(address):
        raise HTTPException(status_code=400, detail="Invalid Solana wallet address.")
    # Fetch native SOL balance
    rpc_url = HELIUS_RPC_URL if HELIUS_RPC_URL else SOLANA_RPC_URL
    try:
        resp = requests.post(rpc_url, json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address]}, timeout=5)
        lamports = resp.json().get("result", {}).get("value", 0)
        sol_balance = lamports / 1e9  # convert lamports to SOL
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch SOL balance.")
    tokens: List[TokenBalance] = []
    # Try using Helius to get all token holdings (with prices)
    if HELIUS_RPC_URL:
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "searchAssets",
            "params": {"ownerAddress": address, "tokenType": "fungible"}
        }
        try:
            resp = requests.post(HELIUS_RPC_URL, json=payload, timeout=8)
            assets = resp.json().get("result", [])
            for asset in assets:
                token_info = asset.get("content", {}).get("token_info", {})
                mint = asset.get("id")
                symbol = token_info.get("symbol")
                decimals = token_info.get("decimals", 0)
                balance_raw = token_info.get("balance", 0)  # raw balance (smallest units)
                balance = balance_raw / (10 ** decimals) if decimals else balance_raw
                usd_value = None
                price_info = token_info.get("price_info")
                if price_info and price_info.get("price_per_token") is not None:
                    usd_value = price_info["price_per_token"] * balance
                tokens.append(TokenBalance(mint=mint, symbol=symbol, balance=balance, usd_value=usd_value))
        except Exception:
            tokens = []  # if Helius fails, we will do manual method
    if not tokens:  # Helius not used or failed, fallback to RPC method
        try:
            resp = requests.post(SOLANA_RPC_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [
                    address,
                    {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},  # Token Program ID
                    {"encoding": "jsonParsed"}
                ]
            }, timeout=8)
            accounts = resp.json().get("result", {}).get("value", [])
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to fetch token accounts.")
        for acct in accounts:
            try:
                info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                mint = info.get("mint")
                token_amount = info.get("tokenAmount", {})
                balance = float(token_amount.get("uiAmount", 0))
                if balance is None or balance == 0:
                    continue  # skip zero-balance accounts
                # Fetch price for this token (contract or helius)
                price = None
                pd = fetch_price_from_coingecko_by_contract(mint) or fetch_price_from_helius(mint)
                if pd:
                    price = pd.price
                usd_value = price * balance if (price is not None) else None
                symbol = pd.symbol if (pd and pd.symbol) else None
                tokens.append(TokenBalance(mint=mint, symbol=symbol, balance=balance, usd_value=usd_value))
            except Exception:
                continue
    # Sort tokens by USD value (if available), descending
    tokens.sort(key=lambda t: (t.usd_value or 0), reverse=True)
    return WalletBalanceResponse(address=address, sol_balance=sol_balance, tokens=tokens)

@app.get("/swap", response_model=SwapQuoteResponse)
def get_swap_quote(input_mint: str, output_mint: str, amount: float, slippage_bps: int = 50):
    """
    Simulate a token swap via Jupiter aggregator.
    Returns the quote (route details, expected output, price impact) and a link to execute the swap.
    """
    # Handle "SOL" keyword by substituting with wSOL mint address
    in_mint = WSOL_MINT if input_mint.strip().upper() == "SOL" else input_mint
    out_mint = WSOL_MINT if output_mint.strip().upper() == "SOL" else output_mint
    if not is_solana_address(in_mint) or not is_solana_address(out_mint):
        raise HTTPException(status_code=400, detail="Invalid input or output mint address.")
    # Determine decimals of input token to convert amount to minor units
    decimals = 9 if input_mint.strip().upper() == "SOL" else get_token_decimals(in_mint)
    if decimals is None:
        raise HTTPException(status_code=400, detail="Unable to fetch token decimals for input mint.")
    minor_amount = int(amount * (10 ** decimals))
    if minor_amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0.")
    # Fetch quote from Jupiter API
    quote_url = (f"https://quote-api.jup.ag/v6/quote?inputMint={in_mint}"
                 f"&outputMint={out_mint}&amount={minor_amount}&slippageBps={slippage_bps}")
    try:
        resp = requests.get(quote_url, timeout=10)
        quote = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch quote from Jupiter API.")
    if "error" in quote:
        # Jupiter API can return an "error" key for invalid pairs, etc.
        raise HTTPException(status_code=502, detail=f"Jupiter API error: {quote.get('error')}")
    # Build response object with relevant fields
    result = {
        "inputMint": quote.get("inputMint", in_mint),
        "outputMint": quote.get("outputMint", out_mint),
        "inAmount": quote.get("inAmount"),
        "outAmount": quote.get("outAmount"),
        "slippageBps": slippage_bps,
        "priceImpactPct": quote.get("priceImpactPct"),
        "routePlan": quote.get("routePlan")
    }
    # Construct a Jupiter swap UI link for convenience
    ui_in = "SOL" if in_mint == WSOL_MINT else in_mint
    ui_out = "SOL" if out_mint == WSOL_MINT else out_mint
    result["swap_link"] = (f"https://jup.ag/swap?inputMint={ui_in}&outputMint={ui_out}"
                           f"&amount={amount}&slippage={slippage_bps/100:.2f}")
    return result
