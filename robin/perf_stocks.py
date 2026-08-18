import threading
import time
import os
import pickle
import requests
from fastapi import FastAPI
import pandas as pd
import yfinance as yf
import robin_stocks.robinhood as r

username = os.environ["ROBINHOOD_USER"]
password = os.environ["ROBINHOOD_PASS"]

TOKEN_LIFETIME = 23 * 3600  # re-login every 23 hours (token lasts ~7 days)
last_login_time = 0

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
rh_api_lock = threading.Lock()


def refresh_robinhood_token():
    """Use the stored refresh_token to get a new access_token without full re-login."""
    pickle_path = os.path.expanduser("~/.tokens/robinhood.pickle")
    if not os.path.exists(pickle_path):
        return False
    try:
        with open(pickle_path, 'rb') as f:
            data = pickle.load(f)
        resp = requests.post("https://api.robinhood.com/oauth2/token/", data={
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "scope": "internal",
            "client_id": "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS",
            "device_token": data["device_token"],
            "expires_in": 86400,
        })
        if resp.status_code == 200:
            new_data = resp.json()
            if "verification_workflow" in new_data:
                print("Refresh Token has expired - full relogin required")
                return False
            r.helpers.update_session('Authorization', f'{new_data["token_type"]} {new_data["access_token"]}')
            with open(pickle_path, 'wb') as f:
                pickle.dump({
                    "token_type": new_data["token_type"],
                    "access_token": new_data["access_token"],
                    "refresh_token": new_data["refresh_token"],
                    "device_token": data["device_token"],
                }, f)
            print("✅ Robinhood token refreshed successfully.")
            return True
        else:
            print(f" Refresh failed (HTTP {resp.status_code}): {resp.text}")
    except Exception as e:
        print(f" Token refresh failed: {e}")
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
            return
        # Refresh failed — full re-login
        try:
            print("Proactive re-login to Robinhood...")
            r.login(username=username, password=password, expiresIn=604800)
            last_login_time = time.time()
            print("✅ Re-logged into Robinhood successfully.")
        except Exception as e:
            print(f"❌ Proactive re-login failed: {e}")


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
        print(f"Error scraping S&P 500 list: {e}")
        return ["AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "TSLA"] # Fallback subset


def initialization_and_pipeline_worker():
    """Handles async authentication on boot, then transitions into the 24-hour analysis loop."""
    # 1. Handle Robinhood Login asynchronously in the background thread
    with rh_api_lock:
        try:
            print("Logging into Robinhood in background thread...")
            r.login(username=username, 
                    password=password, 
                    expiresIn=604800)
            last_login_time = time.time()
            print("✅ Logged into Robinhood successfully.")
        except Exception as auth_err:
            print(f"❌ Critical error logging into Robinhood: {auth_err}")
            return  # Kill the thread if credentials fail completely

    # 2. Transition straight into your infinite market scanning loop
    while True:
        ensure_authenticated()
        print("⚡ Starting Stock Evaluation Pipeline...")
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
            print(f"Found {len(momentum_pool)} high momentum stocks. Entering Stage 2 Fundamental analysis...")

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
                    print(f"Error checking fundamentals for {ticker}: {e}")
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
            print(f"Pipeline complete. Top picks cached successfully: {final_winners}")

        except Exception as global_err:
            print(f"Critical error encountered in background pipeline: {global_err}")
        
        # Sleep 6 hours before refreshing (token is valid for ~7 days)
        time.sleep(21600)


# ----------------------------------------------------
# FASTAPI ENDPOINTS (Accessed instantly by your C++ Bot)
# ----------------------------------------------------

@app.get("/recommendations")
def get_recommendations():
    """Serves the pre-calculated recommendations list instantly via safe read locking."""
    with cache_lock:
        return RECOMMENDATIONS_CACHE


@app.get("/portfolio")
def get_portfolio():
    """Fetches user active portfolio metrics dynamically using Robinhood session."""
    with rh_api_lock:
        try:
            profile_stocks = r.profiles.load_portfolio_profile()
            if profile_stocks is None or not isinstance(profile_stocks, dict) or 'equity' not in profile_stocks:
                print("Robinhood session expired: Attemping to re-log in")
                r.login(username=username, 
                        password=password, 
                        expiresIn=604800)
                profile_stocks = r.profiles.load_portfolio_profile()
                if profile_stocks is None:
                    return {"status": "error", "message": "Robinhood authentication token expired and re-login failed."}
                print("Logged into Robinhood successfully.")
                


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
