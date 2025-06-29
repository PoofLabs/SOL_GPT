from fastapi import FastAPI, HTTPException
import requests
import os

app = FastAPI()

# Load Helius API key from environment (required for Helius DAS endpoints)
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
if not HELIUS_API_KEY:
    raise RuntimeError("HELIUS_API_KEY must be set in environment.")

# Utility: format large numbers with suffixes (k, M, B) and one decimal place
def format_amount(amount: float) -> str:
    if amount >= 1_000_000_000:      # billions
        return f"{amount/1_000_000_000:.1f} B"
    elif amount >= 1_000_000:        # millions
        return f"{amount/1_000_000:.1f} M"
    elif amount >= 1_000:           # thousands
        # Use one decimal for <100k, no decimal for >=100k for cleaner output
        if amount >= 100_000:
            return f"{amount/1_000:.0f} k"
        else:
            return f"{amount/1_000:.1f} k"
    else:
        # For amounts less than 1000, format with up to one decimal if needed
        if amount < 10:
            return f"{amount:.2f}"      # two decimals for very small numbers
        elif amount < 100:
            return f"{amount:.1f}"      # one decimal for two-digit numbers
        else:
            return f"{amount:.0f}"      # no decimal for 100-999
@app.get("/wallet/{address}")
def get_wallet(address: str):
    """Get SOL balance and top SPL token holdings for the wallet."""
    # Prepare Helius DAS searchAssets request for fungible tokens
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "searchAssets",
        "params": {
            "ownerAddress": address,
            "tokenType": "fungible",   # only fungible tokens (SPL tokens)
            "page": 1,
            "limit": 500,             # fetch up to 500 assets (to cover most wallets)
        }
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except requests.RequestException:
        # Propagate as an internal error for the agent to handle
        raise HTTPException(status_code=502, detail="Helius API request failed")
    if resp.status_code != 200 or not resp.json():
        raise HTTPException(status_code=502, detail="Helius API returned error")
    data = resp.json().get("result") or resp.json()  # Helius returns {"result": {...}} on success
    if data is None:
        raise HTTPException(status_code=502, detail="Invalid Helius API response")
    
    # Extract native SOL balance
    native_balance_info = data.get("nativeBalance") or data.get("native_balance")
    sol_balance = 0.0
    if native_balance_info:
        lamports = native_balance_info.get("lamports")
        if lamports is not None:
            sol_balance = lamports / 1e9  # convert lamports to SOL
    # Format SOL balance to 3 decimal places (typical for SOL)
    sol_balance_str = f"{sol_balance:.3f}"
    
    # Extract fungible token holdings
    items = data.get("assets", {}).get("items") if data.get("assets") else data.get("items")
    token_list = []
    if items:
        # Each item includes token_info with balance and price if available:contentReference[oaicite:6]{index=6}:contentReference[oaicite:7]{index=7}
        for item in items:
            info = item.get("token_info") or {}
            symbol = info.get("symbol") or item.get("content", {}).get("metadata", {}).get("symbol")
            # Ensure symbol is in uppercase for display
            if symbol:
                symbol = symbol.upper()
            balance_raw = info.get("balance")  # raw token balance (integer, base units)
            decimals = info.get("decimals")
            # Calculate human-readable token amount
            amount = None
            if balance_raw is not None and decimals is not None:
                try:
                    amount = balance_raw / (10 ** decimals)
                except (TypeError, ValueError):
                    amount = None
            # Fallback: if balance not provided directly (very unlikely), skip
            if amount is None:
                continue
            # Format token amount with k/M/B notation
            amount_str = format_amount(amount)
            # Get token mint address (for reference/truncation in output)
            mint = item.get("id") or info.get("mint")  # 'id' is the mint address in Helius result
            mint_str = mint or ""
            # Truncate mint for display (first 4 and last 3 chars by default)
            if len(mint_str) > 7:
                if mint_str.lower().endswith("pump"):
                    # If mint ends in 'pump', show full 'pump' suffix (e.g., ...pump)
                    mint_display = f"{mint_str[:4]}…{mint_str[-4:]}"
                else:
                    mint_display = f"{mint_str[:4]}…{mint_str[-3:]}"
            else:
                mint_display = mint_str
            # Determine USD value (if available via price_info or known stablecoin)
            usd_value_str = None
            price_info = info.get("price_info")
            if price_info and "total_price" in price_info:
                total_price = price_info["total_price"]
                # Format USD value as $X.X (or $X if >100)
                if total_price >= 100:
                    usd_value_str = f"${total_price:.0f}"
                elif total_price >= 10:
                    usd_value_str = f"${total_price:.1f}"
                else:
                    usd_value_str = f"${total_price:.2f}"
            else:
                # If no price_info, check if token is USDC/USDT (stablecoins) by symbol
                if symbol in ("USDC", "USDT"):
                    # Use amount as USD value for stablecoins
                    usd_value_str = f"${amount:.1f}"
            token_list.append({
                "symbol": symbol or "", 
                "mint": mint_display, 
                "amount": amount_str, 
                "usd_value": usd_value_str
            })
        # Sort tokens by USD value if available, otherwise by raw amount (descending)
        token_list.sort(key=lambda t: (t["usd_value"] is None, 0) if t["usd_value"] is None 
                        else (False, -float(t["usd_value"].strip('$'))))
        # If many tokens, limit to top 10 for brevity
        if len(token_list) > 10:
            token_list = token_list[:10]
    # Prepare response JSON
    return {
        "address": f"{address[:4]}…{address[-3:]}" if len(address) > 7 else address,
        "sol_balance": sol_balance_str,
        "tokens": token_list
    }
@app.get("/price/{symbol}")
def get_price(symbol: str):
    """Get current price, 24h change, volume (in SOL), and market cap for a token symbol."""
    symbol_query = symbol.strip().lower()
    # Use CoinGecko's search to find the coin ID (to handle cases where symbol isn't unique)
    search_url = f"https://api.coingecko.com/api/v3/search?query={symbol_query}"
    try:
        sresp = requests.get(search_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko search request failed")
    if sresp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko search error")
    search_data = sresp.json()
    coin_id = None
    if search_data and "coins" in search_data:
        # Find an exact symbol match if possible
        for coin in search_data["coins"]:
            if coin.get("symbol", "").lower() == symbol_query:
                coin_id = coin.get("id")
                break
        if not coin_id and search_data["coins"]:
            # Fallback to first search result
            coin_id = search_data["coins"][0].get("id")
    if not coin_id:
        raise HTTPException(status_code=404, detail="Token not found on CoinGecko")
    # Fetch market data for the coin and for Solana in one call
    market_url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={coin_id},solana&price_change_percentage=24h"
    )
    try:
        mresp = requests.get(market_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko market data request failed")
    if mresp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko market data error")
    market_data = mresp.json()
    if not isinstance(market_data, list):
        raise HTTPException(status_code=502, detail="Invalid market data response")
    # Parse the coin and Solana data from the response list
    coin_data = None
    sol_data = None
    for entry in market_data:
        if entry.get("id") == coin_id:
            coin_data = entry
        elif entry.get("id") == "solana":
            sol_data = entry
    if not coin_data:
        raise HTTPException(status_code=502, detail="Coin data not found in response")
    # Ensure we have Solana price for volume conversion; if not present, fetch separately
    sol_price = None
    if sol_data and "current_price" in sol_data:
        sol_price = sol_data["current_price"]
    else:
        # Fallback: get SOL price via simple endpoint
        try:
            sol_resp = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=3)
            sol_price = sol_resp.json().get("solana", {}).get("usd") if sol_resp.status_code == 200 else None
        except:
            sol_price = None
    
    # Extract required fields from coin_data
    symbol_out = coin_data.get("symbol", symbol).upper()
    price_usd = coin_data.get("current_price")
    change_pct = coin_data.get("price_change_percentage_24h")
    volume_usd = coin_data.get("total_volume")  # 24h trading volume in USD
    market_cap = coin_data.get("market_cap")
    if price_usd is None or change_pct is None or volume_usd is None or market_cap is None:
        raise HTTPException(status_code=502, detail="Incomplete data from CoinGecko")
    # Format price with appropriate decimals
    if price_usd >= 1:
        price_str = f"${price_usd:.2f}"
    elif price_usd >= 0.1:
        price_str = f"${price_usd:.4f}"
    else:
        price_str = f"${price_usd:.6f}"
    # Format change with one decimal and sign
    change_str = f"{change_pct:+.1f}%"
    # Convert volume USD to volume in SOL, and format with suffix
    vol_sol = None
    vol_str = None
    if sol_price and sol_price > 0:
        vol_sol = volume_usd / sol_price
        # Format volume in SOL with k/M suffix
        vol_str = format_amount(vol_sol) + " SOL"
    else:
        # If SOL price unavailable, fall back to USD volume with suffix and $ (though not expected)
        vol_str = format_amount(volume_usd) + " $"
    # Format market cap with k/M/B (in USD, omit currency symbol as per style)
    mc_str = None
    if market_cap is not None:
        if market_cap >= 1_000_000_000:
            mc_str = f"{market_cap/1_000_000_000:.1f} B"
        elif market_cap >= 1_000_000:
            mc_str = f"{market_cap/1_000_000:.1f} M"
        elif market_cap >= 1_000:
            mc_str = f"{market_cap/1_000:.1f} k"
        else:
            mc_str = str(int(market_cap))
    # Prepare response data
    return {
        "symbol": symbol_out,
        "price": price_str,
        "change_24h": change_str,
        "volume": vol_str,
        "market_cap": mc_str
    }
@app.get("/token")
def find_token(query: str):
    """Find a token by name or symbol and return its symbol, name, and Solana mint address."""
    q = query.strip()
    if not q:
        raise HTTPException(status_code=422, detail="Query parameter cannot be empty")
    # Use CoinGecko search to find the token
    url = f"https://api.coingecko.com/api/v3/search?query={q}"
    try:
        resp = requests.get(url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko search request failed")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko search error")
    data = resp.json()
    if not data or "coins" not in data or not data["coins"]:
        raise HTTPException(status_code=404, detail="Token not found")
    # Take the top search result for simplicity (or the first exact symbol match)
    result = None
    for coin in data["coins"]:
        if coin.get("symbol", "").lower() == q.lower():
            result = coin
            break
    if result is None:
        result = data["coins"][0]
    coin_id = result.get("id")
    symbol = result.get("symbol", "").upper()
    name = result.get("name")
    if not coin_id:
        raise HTTPException(status_code=404, detail="Token not found")
    # Fetch coin details to get contract addresses (mint) on Solana
    detail_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false"
    try:
        dresp = requests.get(detail_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko token detail request failed")
    if dresp.status_code != 200:
        raise HTTPException(status_code=502, detail="Error fetching token details")
    detail_data = dresp.json()
    platforms = detail_data.get("platforms", {})
    sol_mint = platforms.get("solana")
    if not sol_mint:
        # If the token is not on Solana (no Solana mint address), return not found
        raise HTTPException(status_code=404, detail="Token not available on Solana")
    return {
        "symbol": symbol,
        "name": name,
        "mint": sol_mint
    }
@app.get("/swap")
def simulate_swap(input_mint: str, output_mint: str, amount: float):
    """Simulate a token swap using Jupiter aggregator and return quote details."""
    # Construct Jupiter quote API URL. Amount for Jupiter must be in smallest units (integer).
    # We assume 'amount' is given in the input token's natural unit (e.g., SOL).
    # For SOL (mint So111...12) Jupiter expects amount in lamports.
    # For SPL tokens, amount should be in base units (according to their decimals).
    # **Note**: This simple implementation cannot dynamically fetch token decimals,
    # so it assumes the amount is already in base units if not SOL.
    # If input is SOL, convert to lamports:
    input_mint_addr = input_mint
    output_mint_addr = output_mint
    raw_amount = None
    # Special-case: if input mint is the canonical SOL address, multiply by 1e9
    if input_mint_addr == "So11111111111111111111111111111111111111112":
        raw_amount = int(amount * 1e9)
    else:
        # Otherwise, assume amount is already in the smallest unit for simplicity
        # (In a real scenario, we would lookup the input token's decimals and do scaling)
        try:
            raw_amount = int(amount)
        except Exception:
            raw_amount = None
    if raw_amount is None:
        raise HTTPException(status_code=422, detail="Invalid amount")
    jup_url = (
        "https://lite-api.jup.ag/quote"
        f"?inputMint={input_mint_addr}&outputMint={output_mint_addr}"
        f"&amount={raw_amount}&slippageBps=50&restrictIntermediateTokens=true"
    )
    try:
        jresp = requests.get(jup_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Jupiter quote request failed")
    if jresp.status_code != 200:
        raise HTTPException(status_code=502, detail="Jupiter API returned error")
    quote = jresp.json()
    if not quote or "outAmount" not in quote:
        raise HTTPException(status_code=502, detail="Invalid quote response")
    # Parse the quote data
    in_amount_user = amount  # the input amount as provided (in SOL or token units)
    out_amount = int(quote["outAmount"])
    # If possible, convert out_amount to token units by applying output token decimals.
    # (Here we don't know decimals of output; assume it is already smallest unit if from Jupiter.)
    out_amount_str = f"{out_amount:,}"  # raw output amount with commas (if no decimal info)
    # Route parsing:
    route_steps = []
    route_plan = quote.get("routePlan", [])
    for step in route_plan:
        swap_info = step.get("swapInfo", {})
        imint = swap_info.get("inputMint")
        omint = swap_info.get("outputMint")
        # Use known common tokens for names, otherwise abbreviate mint
        def mint_to_symbol(mint_addr: str) -> str:
            # Known tokens mapping (extend as needed)
            known = {
                "So11111111111111111111111111111111111111112": "SOL",
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
                "Es9vMFrzaCjLwSX5ae4Ew9WVeEKXZotPwX3hPJJrEvDw": "USDT"
            }
            if mint_addr in known:
                return known[mint_addr]
            # If not known, fallback to a short form of mint (e.g., first4…last3)
            return f"{mint_addr[:4]}…{mint_addr[-3:]}" if mint_addr else "UNK"
        symbol_in = mint_to_symbol(imint)
        symbol_out = mint_to_symbol(omint)
        route_steps.append(symbol_in)
        # Only append the final output token in the very end (outside loop)
        # (Intermediate tokens will be added in each iteration)
    if route_steps:
        final_out_symbol = mint_to_symbol(output_mint_addr)
        route_steps.append(final_out_symbol)
    route_str = " -> ".join(route_steps)
    # Slippage (from the query parameter we fixed at 0.5%)
    slippage_pct = 0.5
    # Prepare simulation result
    return {
        "input_amount": f"{in_amount_user} {route_steps[0]}",
        "output_estimate": f"{out_amount_str} {route_steps[-1]}",
        "slippage": f"{slippage_pct}%",
        "route": route_str,
        "platform": "Jupiter Aggregator"
    }
