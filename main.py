from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
from decimal import Decimal

app = FastAPI(
    title="SolanaGPT",
    description="Poof Labs Solana degen trading assistant",
    version="1.0",
    servers=[{"url": "https://sol-gpt.onrender.com"}]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

RPC_ENDPOINTS = [
    "https://rpc.helius.xyz/?api-key=YOUR_API_KEY",
    "https://mainnet.helius-rpc.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.rpcpool.com",
    "https://solana-mainnet.g.alchemy.com/v2/YOUR_API_KEY",
    "https://mainnet.rpc.solana.com",
    "https://solana-mainnet.rpc.extrnode.com",
    "https://api.metaplex.solana.com",
    "https://solana-api.syndica.io/access-token/YOUR_API_KEY/rpc"
]

JUPITER_TOKEN_INFO_URL = "https://tokens.jup.ag/token/"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2?ids="
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def get_rpc_response(payload):
    for url in RPC_ENDPOINTS:
        try:
            resp = requests.post(url, json=payload, timeout=5)
            data = resp.json()
            if 'result' in data and data['result'] is not None:
                return data
        except Exception:
            continue
    raise Exception("All RPC endpoints failed or timed out")


@app.get("/balances/{address}")
def get_balances(address: str):
    result = {"sol": None, "tokens": []}

    balance_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [address]
    }
    try:
        balance_data = get_rpc_response(balance_payload)
    except Exception as e:
        return {"error": "Unable to fetch SOL balance from RPC", "details": str(e)}
    lamports = balance_data.get("result", {}).get("value", 0)
    sol_amount = lamports / 1e9

    token_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            address,
            {"programId": TOKEN_PROGRAM_ID},
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        token_data = get_rpc_response(token_payload)
    except Exception as e:
        result["sol"] = {"amount": sol_amount, "price": None, "usd_value": None}
        result["tokens"] = []
        result["error"] = "Token account lookup failed"
        return result

    accounts = token_data.get("result", {}).get("value", [])
    token_list = []
    for acct in accounts:
        info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        token_amount = info.get("tokenAmount", {})
        amount = None
        if token_amount.get("uiAmountString"):
            amount = Decimal(token_amount["uiAmountString"])
        elif token_amount.get("uiAmount") is not None:
            amount = Decimal(str(token_amount["uiAmount"]))
        else:
            try:
                raw = Decimal(token_amount.get("amount", "0"))
                decimals = int(token_amount.get("decimals", 0))
                amount = raw / (Decimal(10) ** decimals)
            except Exception:
                amount = Decimal(0)
        if amount is None or amount == 0:
            continue
        token_list.append({
            "mint": info.get("mint"),
            "amount": amount
        })

    mint_addresses = [t["mint"] for t in token_list]
    WSOL_MINT = "So11111111111111111111111111111111111111112"
    mint_addresses.append(WSOL_MINT)

    prices = {}
    if mint_addresses:
        ids_param = ",".join(mint_addresses)
        try:
            price_resp = requests.get(f"{JUPITER_PRICE_URL}{ids_param}", timeout=5)
            price_data = price_resp.json()
            prices = price_data.get("data", {})
        except Exception:
            prices = {}

    tokens_output = []
    for token in token_list:
        mint = token["mint"]
        amount = token["amount"]
        name = "Unknown Token"
        symbol = mint[:4] + "..." + mint[-4:]
        volume = None

        try:
            meta_resp = requests.get(f"{JUPITER_TOKEN_INFO_URL}{mint}", timeout=5)
            if meta_resp.status_code == 200:
                meta = meta_resp.json()
                name = meta.get("name") or name
                symbol = meta.get("symbol") or symbol
                volume = meta.get("daily_volume")
        except Exception:
            pass

        price = None
        usd_value = None
        if mint in prices:
            price_str = prices[mint].get("price")
            if price_str is not None:
                try:
                    price = float(price_str)
                except:
                    price = float(Decimal(price_str))
        if price is not None:
            usd_value = float(Decimal(str(price)) * amount)

        tokens_output.append({
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "amount": float(amount),
            "price": price,
            "usd_value": usd_value,
            "daily_volume": volume
        })

    sol_price = None
    sol_usd_value = None
    if WSOL_MINT in prices:
        price_str = prices[WSOL_MINT].get("price")
        if price_str is not None:
            try:
                sol_price = float(price_str)
            except:
                sol_price = float(Decimal(price_str))
    if sol_price is not None:
        sol_usd_value = sol_price * sol_amount

    result["sol"] = {
        "amount": sol_amount,
        "price": sol_price,
        "usd_value": sol_usd_value
    }
    result["tokens"] = tokens_output
    return result


@app.get("/transaction/{signature}")
def get_transaction(signature: str):
    tx_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed"}]
    }
    try:
        tx_data = get_rpc_response(tx_payload)
    except Exception as e:
        return {"error": "Unable to fetch transaction", "details": str(e)}
    if tx_data.get("result") is None:
        return {"error": "Transaction not found"}

    tx = tx_data["result"]
    summary_lines = []
    if "meta" in tx:
        if tx["meta"].get("err"):
            summary_lines.append("**Transaction Status**: Failed")
        else:
            summary_lines.append("**Transaction Status**: Success")
        fee = tx["meta"].get("fee")
        if fee is not None:
            summary_lines.append(f"**Fee Paid**: {fee} lamports")

    if "transaction" in tx and "message" in tx["transaction"]:
        instructions = tx["transaction"]["message"].get("instructions", [])
        for idx, instr in enumerate(instructions, start=1):
            if instr.get("program") == "spl-token" and "parsed" in instr:
                parsed = instr["parsed"]
                instr_type = parsed.get("type")
                info = parsed.get("info", {})
                if instr_type == "transfer":
                    amt = info.get("amount")
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    mint = info.get("mint", "")
                    summary_lines.append(f"Instruction {idx}: Transfer of {amt} tokens (mint {mint}) from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                elif instr_type == "mintTo":
                    amt = info.get("amount")
                    mint = info.get("mint", "")
                    acct = info.get("account", "")
                    summary_lines.append(f"Instruction {idx}: Minted {amt} new tokens of {mint} to {acct[:4]}...{acct[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: SPL token instruction **{instr_type}**.")
            elif instr.get("program") == "system" and "parsed" in instr:
                parsed = instr["parsed"]
                if parsed.get("type") == "transfer":
                    info = parsed.get("info", {})
                    lamports = info.get("lamports", 0)
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    sol_amount = int(lamports) / 1e9
                    summary_lines.append(f"Instruction {idx}: SOL transfer of {sol_amount:.9f} SOL from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: System program instruction **{parsed.get('type')}**.")
            else:
                prog_id = instr.get("programId") or instr.get("programIdIndex")
                summary_lines.append(f"Instruction {idx}: Instruction by program {prog_id} (details not parsed).")

    summary = "\n".join(summary_lines) if summary_lines else "No parsed instruction details available."
    return {"signature": signature, "summary": summary}


@app.get("/")
def root():
    return {"message": "SolanaGPT online — try /balances/{address} or /transaction/{signature}"}
