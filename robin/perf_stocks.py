import threading
import time
import os
import sys
import json
import pickle
import logging
import requests
from fastapi import FastAPI
import pandas as pd
import yfinance as yf
import robin_stocks.robinhood as r

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='[%(levelname)s] %(message)s')
log = logging.getLogger("stocks-backend")

username = os.environ["ROBINHOOD_USER"]
password = os.environ["ROBINHOOD_PASS"]
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

TOKEN_LIFETIME = 23 * 3600  # re-login every 23 hours (token lasts ~7 days)
last_login_time = 0
LOGIN_TIMESTAMP_PATH = os.path.expanduser("~/.tokens/last_login.json")


def load_login_timestamp():
    """Load last_login_time from disk so restarts don't force unnecessary re-login."""
    global last_login_time
    try:
        if os.path.exists(LOGIN_TIMESTAMP_PATH):
            with open(LOGIN_TIMESTAMP_PATH, 'r') as f:
                last_login_time = json.load(f).get("last_login", 0)
    except Exception:
        pass


def save_login_timestamp():
    """Persist last_login_time to disk."""
    try:
        with open(LOGIN_TIMESTAMP_PATH, 'w') as f:
            json.dump({"last_login": last_login_time}, f)
    except Exception:
        pass


load_login_timestamp()

app = FastAPI()

# Global Thread-Safe Storage for C++ Bot consumption
RECOMMENDATIONS_CACHE = {
    "status": "processing",
    "daily": [],
    "weekly": [],
    "monthly": [],
    "last_updated": 0
}

# Lock to ensure we don't read the cache while the background thread is overwriting it
cache_lock = threading.Lock()

# Lock to protect Robinhood session API requests
rh_api_lock = threading.RLock()

# True only after the initial background login completes — prevents race condition
# where /portfolio is served before Robinhood session is established
session_ready = threading.Event()


def send_discord_alert(message):
    """Send an alert to the Discord channel via the bot token."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    try:
        requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"content": message},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Failed to send Discord alert: {e}")


def refresh_robinhood_token():
    """Use the stored refresh_token to get a new access_token without full re-login."""
    pickle_path = os.path.expanduser("~/.tokens/robinhood.pickle")
    if not os.path.exists(pickle_path):
        log.warning("No pickle file found at %s", pickle_path)
        return False
    try:
        with open(pickle_path, 'rb') as f:
            data = pickle.load(f)
        missing = [k for k in ("token_type", "access_token", "refresh_token", "device_token") if k not in data]
        if missing:
            log.warning("Pickle missing keys: %s", missing)
            return False
        resp = requests.post("https://api.robinhood.com/oauth2/token/", data={
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "scope": "internal",
            "client_id": "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS",
            "device_token": data["device_token"],
            "expires_in": 86400,
        }, timeout=15)
        if resp.status_code == 200:
            new_data = resp.json()
            if "verification_workflow" in new_data:
                log.warning("Robinhood returned verification_workflow — refresh token expired")
                return False
            r.update_session('Authorization', f'{new_data["token_type"]} {new_data["access_token"]}')
            with open(pickle_path, 'wb') as f:
                pickle.dump({
                    "token_type": new_data["token_type"],
                    "access_token": new_data["access_token"],
                    "refresh_token": new_data["refresh_token"],
                    "device_token": data["device_token"],
                }, f)
            log.info("Robinhood token refreshed successfully")
            return True
        else:
            log.error("Refresh failed — HTTP %d: %s", resp.status_code, resp.text[:300])
    except Exception as e:
        log.error("Refresh exception: %s", e)
    return False


def ensure_authenticated():
    """Proactively re-authenticate before the token expires."""
    global last_login_time
    with rh_api_lock:
        if time.time() - last_login_time < TOKEN_LIFETIME:
            return  # Still valid, skip
        # Try refresh first (no MFA needed)
        if refresh_robinhood_token():
            last_login_time = time.time()
            save_login_timestamp()
            return
        # Refresh failed — full re-login
        send_discord_alert("Robinhood refresh token expired — performing full re-login. You may get a security notification.")
        try:
            log.info("Proactive re-login to Robinhood...")
            r.login(username=username, password=password, expiresIn=604800)
            last_login_time = time.time()
            save_login_timestamp()
            log.info("Re-logged into Robinhood successfully")
        except Exception as e:
            log.error("Proactive re-login failed: %s", e)
            send_discord_alert(f"Robinhood re-login FAILED: {e}")


@app.on_event("startup")
def start_background_pipeline():
    """Fires when FastAPI starts up; immediately frees the main loop by deferring initialization."""
    # Spawns everything asynchronously so Gunicorn/Uvicorn can open port 8000 instantly
    ticker_thread = threading.Thread(target=initialization_and_pipeline_worker, daemon=True)
    ticker_thread.start()


def get_sp500_tickers():
    """Scrapes Wikipedia cleanly to fetch the live 500 S&P tickers."""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        tickers = tables[0]['Symbol'].tolist()
        return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        log.error("Error scraping S&P 500 list: %s", e)
        return ["AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "TSLA"] # Fallback subset


def initialization_and_pipeline_worker():
    """Handles async authentication on boot, then transitions into the 24-hour analysis loop."""
    global last_login_time
    # 1. Handle Robinhood Login asynchronously in the background thread
    with rh_api_lock:
        try:
            # Always try silent refresh first — avoids notifications on restarts
            if refresh_robinhood_token():
                log.info("Restored Robinhood session from pickle (no re-login).")
            else:
                log.info("Refresh token expired or missing, doing full login...")
                send_discord_alert("Robinhood session invalid — performing full re-login. You may get a security notification.")
                r.login(username=username, password=password, expiresIn=604800)
                last_login_time = time.time()
                save_login_timestamp()
                log.info("Logged into Robinhood successfully.")
        except Exception as auth_err:
            log.error("Critical error logging into Robinhood: %s", auth_err)
            send_discord_alert(f"Robinhood login FAILED: {auth_err}")
            return  # Kill the thread if credentials fail completely

    session_ready.set()

    # 2. Transition straight into your infinite market scanning loop
    while True:
        ensure_authenticated()
        log.info("Starting Stock Evaluation Pipeline...")
        try:
            tickers = get_sp500_tickers()
            
            # Fetch 6 months of historical data to calculate 50-day SMAs
            data = yf.download(tickers, period="6mo", group_by='ticker', progress=False)
            
            momentum_pool = []
            
            for ticker in tickers:
                try:
                    if ticker not in data.columns.levels[0]:
                        continue
                    
                    hist = data[ticker].dropna()
                    if len(hist) < 50:
                        continue
                        
                    close_prices = hist['Close']
                    latest_close = float(close_prices.iloc[-1])
                    
                    # Calculate simple moving averages
                    sma_50 = float(close_prices.rolling(window=50).mean().iloc[-1])
                    
                    # Calculate performance/momentum score (1-month returns)
                    one_month_ago = float(close_prices.iloc[-21]) if len(close_prices) > 21 else float(close_prices.iloc[0])
                    one_month_return = (latest_close - one_month_ago) / one_month_ago

                    # Criteria: Upward price trend (price > 50 SMA)
                    if latest_close > sma_50:
                        momentum_pool.append({
                            "ticker": ticker,
                            "return_1m": one_month_return,
                            "latest_close": latest_close
                        })
                except Exception:
                    continue # Skip troublesome tickers smoothly
            
            # Sort by highest 1-month return performance and take top 25 for fundamental check
            momentum_pool = sorted(momentum_pool, key=lambda x: x['return_1m'], reverse=True)[:25]
            log.info("Found %d high momentum stocks. Entering Stage 2 Fundamental analysis...", len(momentum_pool))

            # ----------------------------------------------------
            # STAGE 2: FUNDAMENTAL HEALTH & DEEP VALUATION SCREEN
            # ----------------------------------------------------
            final_winners = []
            
            for item in momentum_pool:
                ticker = item['ticker']
                try:
                    ticker_obj = yf.Ticker(ticker)
                    info = ticker_obj.info
                    
                    # Extract indicators safely using dictionary defaults
                    roe = info.get('returnOnEquity', 0.0)
                    debt_to_equity = info.get('debtToEquity', 150.0) # Assume high debt if missing
                    pe_ratio = info.get('trailingPE', 999.0)
                    
                    # Pass criteria verification filter:
                    if (roe and roe >= 0.12) and (debt_to_equity and debt_to_equity < 120.0) and (pe_ratio and pe_ratio < 80.0):
                        final_winners.append(ticker)
                        if len(final_winners) >= 5: # We found our Top 5 recommendations
                            break
                except Exception as e:
                    log.error("Error checking fundamentals for %s: %s", ticker, e)
                    continue

            # Update Global Cache safely using the Mutex Lock
            with cache_lock:
                global RECOMMENDATIONS_CACHE
                RECOMMENDATIONS_CACHE = {
                    "status": "success",
                    "daily": final_winners[:2],    # Short-term speed plays
                    "weekly": final_winners[2:4],  # Structural swings
                    "monthly": final_winners[-1:], # Solid long-term hold pick
                    "last_updated": int(time.time())
                }
            log.info("Pipeline complete. Top picks: %s", final_winners)

        except Exception as global_err:
            log.error("Critical error in background pipeline: %s", global_err)
        
        # Sleep 6 hours before refreshing (token is valid for ~7 days)
        time.sleep(21600)


# ----------------------------------------------------
# FASTAPI ENDPOINTS (Accessed instantly by C++ Bot)
# ----------------------------------------------------

@app.get("/recommendations")
def get_recommendations():
    """Serves the pre-calculated recommendations list instantly via safe read locking."""
    with cache_lock:
        return RECOMMENDATIONS_CACHE


@app.get("/portfolio")
def get_portfolio():
    """Fetches user active portfolio metrics dynamically using Robinhood session."""
    if not session_ready.is_set():
        return {"status": "error", "message": "Robinhood session is still initializing, try again in a few seconds."}
    global last_login_time
    with rh_api_lock:
        ensure_authenticated()
        try:
            profile_stocks = r.profiles.load_portfolio_profile()
            if profile_stocks is None or not isinstance(profile_stocks, dict) or 'equity' not in profile_stocks:
                log.warning("Robinhood session expired: Attempting silent refresh...")
                if not refresh_robinhood_token():
                    log.warning("Refresh failed, doing full re-login...")
                    send_discord_alert("Robinhood session expired on /portfolio — full re-login triggered.")
                    r.login(username=username, password=password, expiresIn=604800)
                last_login_time = time.time()
                save_login_timestamp()
                profile_stocks = r.profiles.load_portfolio_profile()
                if profile_stocks is None:
                    return {"status": "error", "message": "Robinhood authentication token expired and re-login failed."}
                log.info("Logged into Robinhood successfully.")
                


            profile_positions = r.crypto.get_crypto_positions()
        
            total_crypto_equity = 0.0
            if profile_positions:
                for position in profile_positions:
                    quantity = float(position['quantity'])
                    if quantity > 0: 
                        name = position['currency']['code']
                        curr_price = float(r.crypto.get_crypto_quote(name)['mark_price'])
                        total_crypto_equity += (quantity * curr_price)

            return {
                "status": "success",
                "equity": float(profile_stocks['equity']),
                "crypto_equity": total_crypto_equity,
                "total_equity": float(profile_stocks['equity']) + total_crypto_equity,
                "extended_hours_equity": float(profile_stocks['extended_hours_equity']) if profile_stocks['extended_hours_equity'] else None,
                "market_value": float(profile_stocks['market_value'])
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
