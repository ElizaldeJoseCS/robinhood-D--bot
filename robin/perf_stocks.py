import threading
import time
import os
from fastapi import FastAPI
import pandas as pd
import yfinance as yf
import robin_stocks.robinhood as r
from credentials import username, password

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


@app.on_event("startup")
def start_background_pipeline():
    """Fires when FastAPI starts up; logs into Robinhood and spawns our analysis engine."""
    with rh_api_lock:
        print("Logging into Robinhood...")
        r.login(username=os.getenv("ROBINHOOD_USER", username), 
                password=os.getenv("ROBINHOOD_PASS", password), 
                expiresIn=86400)
        print("Logged in successfully.")

    # Spawn the background picker thread
    ticker_thread = threading.Thread(target=analysis_pipeline_worker, daemon=True)
    ticker_thread.start()

def initialization_and_pipeline_worker():
"""Handles async authentication on boot, then transitions into the 24-hour analysis loop."""
    # 1. Handle Robinhood Login asynchronously in the background
    with rh_api_lock:
        try:
            print("Logging into Robinhood in background thread...")
            r.login(username=os.getenv("ROBINHOOD_USER", username), 
                    password=os.getenv("ROBINHOOD_PASS", password), 
                    expiresIn=86400)
            print("✅ Logged into Robinhood successfully.")
        except Exception as auth_err:
            print(f"❌ Critical error logging into Robinhood: {auth_err}")
            return  # Stop the thread if credentials fail completely
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


def analysis_pipeline_worker():
    """Runs indefinitely in the background, updating calculations every 24 hours."""
    while True:
        print("⚡ Starting Stock Evaluation Pipeline...")
        try:
            tickers = get_sp500_tickers()
            
            # Fetch 6 months of historical data to calculate 50-day and 200-day SMAs
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
                    
                    # Calculate performance/momentum score (e.g., 1-month returns)
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
            
            # Sort by highest 1-month return performance and take top 25 for deep analysis
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
                    
                    # Extract fundamental indicators safely using dictionary defaults
                    roe = info.get('returnOnEquity', 0.0)
                    debt_to_equity = info.get('debtToEquity', 150.0) # Assume high debt if missing
                    pe_ratio = info.get('trailingPE', 999.0)
                    
                    # Pass criteria verification filter:
                    # Healthy ROE (> 12%), safe debt ratio (< 120%), and has a real P/E track
                    if (roe and roe >= 0.12) and (debt_to_equity and debt_to_equity < 120.0) and (pe_ratio and pe_ratio < 80.0):
                        final_winners.append(ticker)
                        if len(final_winners) >= 5: # We found our Top 5 strongest recommendations
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
        
        # Sleep thread for 24 hours before refreshing calculations
        time.sleep(86400)

@app.on_event("startup")
def start_background_pipeline():
    """Fires when FastAPI starts up; immediately frees the main loop by deferring initialization."""
    # Move EVERYTHING to a background thread so the web server boots in milliseconds
    ticker_thread = threading.Thread(target=initialization_and_pipeline_worker, daemon=True)
    ticker_thread.start()


def initialization_and_pipeline_worker():
    """Handles async authentication on boot, then transitions into the 24-hour analysis loop."""
    # 1. Handle Robinhood Login asynchronously in the background
    with rh_api_lock:
        try:
            print("Logging into Robinhood in background thread...")
            r.login(username=os.getenv("ROBINHOOD_USER", username), 
                    password=os.getenv("ROBINHOOD_PASS", password), 
                    expiresIn=86400)
            print("Logged into Robinhood successfully.")
        except Exception as auth_err:
            print(f"Critical error logging into Robinhood: {auth_err}")
            return  # Stop the thread if credentials fail completely

    # 2. Transition straight into your infinite market scanning loop
    while True:
        print("⚡ Starting Stock Evaluation Pipeline...")
        try:
            tickers = get_sp500_tickers()
            
            # --- Keep the rest of your original Stage 1 & Stage 2 processing code exactly as is ---
            data = yf.download(tickers, period="6mo", group_by='ticker', progress=False)
            
            # ... processing loops ...
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
                    
                    # Calculate performance/momentum score (e.g., 1-month returns)
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
            
            # Sort by highest 1-month return performance and take top 25 for deep analysis
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
                    
                    # Extract fundamental indicators safely using dictionary defaults
                    roe = info.get('returnOnEquity', 0.0)
                    debt_to_equity = info.get('debtToEquity', 150.0) # Assume high debt if missing
                    pe_ratio = info.get('trailingPE', 999.0)
                    
                    # Pass criteria verification filter:
                    # Healthy ROE (> 12%), safe debt ratio (< 120%), and has a real P/E track
                    if (roe and roe >= 0.12) and (debt_to_equity and debt_to_equity < 120.0) and (pe_ratio and pe_ratio < 80.0):
                        final_winners.append(ticker)
                        if len(final_winners) >= 5: # We found our Top 5 strongest recommendations
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
        
        # Sleep thread for 24 hours before refreshing calculations
        time.sleep(86400)

# FASTAPI ENDPOINTS (Accessed instantly by D++ bot)  
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
        # Fetch profile data using robin_stocks
            profile_stocks = r.profiles.load_portfolio_profile()
            profile_positions = r.crypto.get_crypto_positions() # returns crypto positions for my account in the form of a list of dict (key : value pairs where the key is the crypto symbol and value is diff options for that position )
        
            total_crypto_equity = 0.0
            for position in profile_positions:
            # filter out assets i own
                quantity = float(position['quantity'])
                if quantity > 0: # if positive then that means i own some of this coin 
                    name = position['currency']['code']
                    curr_price = float(r.crypto.get_crypto_quote(name)['mark_price'])
                    total_crypto_equity += (quantity * curr_price)
        # structuring the data nicely for the D++ bot 
            return {
                "status": "success",
                "equity": float(profile_stocks['equity']),
                "crypto_equity":total_crypto_equity,
                "total_equity": float(profile_stocks['equity']) + total_crypto_equity,
                "extended_hours_equity": float(profile_stocks['extended_hours_equity']) if profile_stocks['extended_hours_equity'] else None,
                "market_value": float(profile_stocks['market_value'])
        }
        except Exception as e:
            return {"status": "error", "message": str(e)}
