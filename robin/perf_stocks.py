import threading
import time
import os
import sys
import json
import pickle
import logging
import requests
import numpy as np
from fastapi import FastAPI
from fastapi.responses import Response
import pandas as pd
import yfinance as yf
import robin_stocks.robinhood as r
import io
import matplotlib
matplotlib.use("Agg")  # headless backend — the droplet has no display server
import matplotlib.pyplot as plt

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

# ----------------------------------------------------
# MONTE CARLO RISK ENGINE — shared state
# ----------------------------------------------------
RISK_CACHE = {"status": "processing", "last_updated": 0}
risk_lock = threading.Lock()
RISK_CHART_PATH = os.path.expanduser("~/.cache/risk_surface.png")

N_PATHS = 10000    # simulated portfolio paths
N_STEPS = 126      # trading days forward (~6 months)
CHUNK = 2000       # paths per batch — bounds peak memory on a small droplet
N_BINS = 60        # value buckets for the density surface
TRADING_DAYS = 252

# Drift handling. The sample mean of daily returns has standard error sigma/sqrt(n),
# which over one year of daily data is roughly +/-20% annualised — the estimate is
# mostly noise, and feeding it into a 6-month projection swamps the result. Covariance
# converges far faster, so the risk model simulates with zero drift and reports the
# sample drift separately as a diagnostic. This is the usual choice for VaR, where the
# quantity of interest is dispersion rather than expected return.
DRIFT_MODE = "zero"   # "zero" | "sample"


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
            from robin_stocks.robinhood.helper import set_login_state
            set_login_state(True)
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
        html = requests.get(
            url, headers={"User-Agent": "portfolio-bot/1.0"}, timeout=10
        ).text
        tables = pd.read_html(io.StringIO(html))
        tickers = tables[0]['Symbol'].tolist()
        return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        log.error("Error scraping S&P 500 list: %s", e)
        return ["AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "TSLA"]


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

            # Refresh the Monte Carlo risk model off the same tick
            try:
                risk = compute_risk_model()
                with risk_lock:
                    global RISK_CACHE
                    RISK_CACHE = risk
                if risk.get("status") == "success":
                    log.info("Risk model updated: 95%% VaR $%.2f over %d days",
                             risk["var_95"], risk["horizon_days"])
                else:
                    log.warning("Risk model skipped: %s", risk.get("message"))
            except Exception as risk_err:
                log.error("Risk model failed: %s", risk_err)

        except Exception as global_err:
            log.error("Critical error in background pipeline: %s", global_err)
        
        # Sleep 6 hours before refreshing (token is valid for ~7 days)
        time.sleep(21600)


# ----------------------------------------------------
# MONTE CARLO RISK ENGINE
# ----------------------------------------------------

def get_portfolio_weights():
    """Return (tickers, weights, total_value) for the live portfolio, stocks + crypto.

    Crypto symbols are mapped to Yahoo's CODE-USD convention so both asset classes
    can be priced from one historical source.
    """
    with rh_api_lock:
        ensure_authenticated()
        holdings = r.account.build_holdings() or {}
        positions = {}
        for sym, h in holdings.items():
            try:
                eq = float(h.get("equity", 0.0))
                if eq > 0:
                    positions[sym] = eq
            except (TypeError, ValueError):
                continue

        try:
            for pos in (r.crypto.get_crypto_positions() or []):
                qty = float(pos["quantity"])
                if qty <= 0:
                    continue
                code = pos["currency"]["code"]
                price = float(r.crypto.get_crypto_quote(code)["mark_price"])
                positions[f"{code}-USD"] = qty * price
        except Exception as e:
            log.warning("Could not include crypto in risk model: %s", e)

    if not positions:
        return [], np.array([]), 0.0

    tickers = sorted(positions)
    values = np.array([positions[t] for t in tickers], dtype=np.float64)
    total = float(values.sum())
    return tickers, values / total, total


def nearest_psd(cov):
    """Clip negative eigenvalues so an almost-singular covariance still factors.

    Sample covariance from short or gappy history is often not positive definite,
    which makes np.linalg.cholesky raise. Clipping to a small positive floor is the
    standard repair and keeps the matrix symmetric.
    """
    vals, vecs = np.linalg.eigh((cov + cov.T) / 2.0)
    vals = np.clip(vals, 1e-10, None)
    return (vecs * vals) @ vecs.T


def compute_risk_model():
    """Simulate the portfolio forward with correlated shocks and cache the result.

    Pipeline: holdings -> aligned daily log returns -> covariance -> Cholesky factor
    -> chunked Monte Carlo -> VaR / CVaR / drawdown + a density surface PNG.
    """
    tickers, weights, total_value = get_portfolio_weights()
    if len(tickers) < 2:
        return {"status": "error",
                "message": "Need at least 2 holdings to estimate a covariance matrix."}

    hist = yf.download(tickers, period="1y", progress=False, auto_adjust=True)
    close = hist["Close"] if "Close" in hist else hist
    close = close.reindex(columns=tickers).dropna(axis=1, how="all")

    live = [t for t in tickers if t in close.columns]
    if len(live) < 2:
        return {"status": "error", "message": "Not enough price history to build a risk model."}

    # Drop tickers with no history and renormalise the remaining weights
    idx = [tickers.index(t) for t in live]
    weights = weights[idx]
    weights = weights / weights.sum()
    close = close[live].dropna()
    if len(close) < 60:
        return {"status": "error", "message": "Fewer than 60 overlapping trading days of history."}

    log_ret = np.log(close / close.shift(1)).dropna().to_numpy(dtype=np.float64)
    n_obs = log_ret.shape[0]
    mu_sample = log_ret.mean(axis=0)
    cov = np.cov(log_ret, rowvar=False)
    mu = np.zeros_like(mu_sample) if DRIFT_MODE == "zero" else mu_sample

    try:
        chol = np.linalg.cholesky(cov)
    except np.linalg.LinAlgError:
        log.warning("Covariance not positive definite — repairing via eigenvalue clipping")
        chol = np.linalg.cholesky(nearest_psd(cov))

    # Analytic portfolio moments, used to bound the histogram and to report vol
    sigma_p = float(np.sqrt(weights @ cov @ weights))
    ann_vol = sigma_p * np.sqrt(TRADING_DAYS)

    # How trustworthy the discarded drift estimate actually was
    sample_drift_ann = float(weights @ mu_sample) * TRADING_DAYS
    drift_stderr_ann = float(sigma_p * TRADING_DAYS / np.sqrt(n_obs))

    # Expected SIMPLE return per asset: E[e^X]-1 for X ~ N(mu, var), then weight.
    # Needed because the portfolio aggregates in simple-return space, not log space.
    exp_simple = np.expm1(mu + 0.5 * np.diag(cov))
    drift_p = float(np.log1p(weights @ exp_simple))

    span = 4.0 * sigma_p * np.sqrt(N_STEPS)
    lo = total_value * np.exp(drift_p * N_STEPS - span)
    hi = total_value * np.exp(drift_p * N_STEPS + span)
    edges = np.linspace(lo, hi, N_BINS + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    bin_width = edges[1] - edges[0]

    counts = np.zeros((N_STEPS, N_BINS), dtype=np.int64)
    terminal = np.empty(N_PATHS, dtype=np.float64)
    max_dd = np.empty(N_PATHS, dtype=np.float64)

    rng = np.random.default_rng(12345)
    chol_t = chol.T.astype(np.float32)
    mu_f = mu.astype(np.float32)
    w_f = weights.astype(np.float32)

    # Chunked so peak memory stays bounded regardless of N_PATHS
    for start in range(0, N_PATHS, CHUNK):
        n = min(CHUNK, N_PATHS - start)
        z = rng.standard_normal((n, N_STEPS, len(live))).astype(np.float32)
        asset_log = z @ chol_t                   # independent -> correlated shocks
        asset_log += mu_f                        # add per-asset drift

        # Log returns compound over TIME but do not aggregate across ASSETS: the
        # portfolio return is the weighted sum of SIMPLE returns. Summing log
        # returns here would compute a geometric mean and bias every path low.
        np.expm1(asset_log, out=asset_log)       # log return -> simple return
        port_simple = asset_log @ w_f            # daily-rebalanced constant weights
        values = total_value * np.cumprod(1.0 + port_simple, axis=1, dtype=np.float32)

        running_max = np.maximum.accumulate(values, axis=1)
        max_dd[start:start + n] = ((values - running_max) / running_max).min(axis=1)
        terminal[start:start + n] = values[:, -1]

        binned = np.clip(np.searchsorted(edges, values, side="right") - 1, 0, N_BINS - 1)
        for t in range(N_STEPS):
            counts[t] += np.bincount(binned[:, t], minlength=N_BINS)

        del z, asset_log, port_simple, values, running_max, binned

    p05, p50, p95 = np.percentile(terminal, [5, 50, 95])
    tail = terminal[terminal <= p05]
    density = counts / (N_PATHS * bin_width)

    chart_ok = render_risk_surface(density, centers, total_value, p05, p50, p95)

    return {
        "status": "success",
        "holdings": len(live),
        "tickers": live,
        "total_value": round(total_value, 2),
        "annual_vol_pct": round(ann_vol * 100, 2),
        "var_95": round(total_value - float(p05), 2),
        "var_95_pct": round((1 - p05 / total_value) * 100, 2),
        "cvar_95": round(total_value - float(tail.mean()), 2),
        "median_terminal": round(float(p50), 2),
        "p05_terminal": round(float(p05), 2),
        "p95_terminal": round(float(p95), 2),
        "prob_loss_pct": round(float((terminal < total_value).mean()) * 100, 2),
        "median_max_drawdown_pct": round(float(np.median(max_dd)) * 100, 2),
        "horizon_days": N_STEPS,
        "paths": N_PATHS,
        "drift_mode": DRIFT_MODE,
        "obs_days": int(n_obs),
        "sample_drift_annual_pct": round(sample_drift_ann * 100, 2),
        "drift_stderr_annual_pct": round(drift_stderr_ann * 100, 2),
        "chart": chart_ok,
        "last_updated": int(time.time()),
    }


def render_risk_surface(density, centers, start_value, p05, p50, p95):
    """Draw the simulated value distribution as a 3D surface and save it atomically."""
    try:
        os.makedirs(os.path.dirname(RISK_CHART_PATH), exist_ok=True)

        # Smooth along the value axis. 10k paths spread over 60 bins leaves sampling
        # noise that reads as surface roughness rather than as signal; a binomial
        # kernel is the cheap standard fix and preserves the mode.
        kernel = np.array([1.0, 4.0, 6.0, 4.0, 1.0])
        kernel /= kernel.sum()
        density = np.vstack([np.convolve(row, kernel, mode="same") for row in density])

        # Open at one month rather than day 1. Density scales as 1/sqrt(t), so day 1 is
        # ~11x the height of day 126 and swamps the plot; from day 21 the range is only
        # ~2.4x, which renders as a readable fan with the true densities intact.
        first = min(20, N_STEPS // 4)
        step = max(1, (N_STEPS - first) // 42)
        rows = np.arange(first, N_STEPS, step)
        days = rows + 1
        grid_x, grid_y = np.meshgrid(days, centers, indexing="ij")
        grid_z = density[rows].astype(np.float64)

        fig = plt.figure(figsize=(11, 6.5), dpi=110)
        fig.patch.set_facecolor("#1a1d21")
        ax = fig.add_subplot(projection="3d")
        ax.set_facecolor("#1a1d21")

        ax.plot_surface(grid_x, grid_y, grid_z, cmap="magma",
                        rstride=1, cstride=1, linewidth=0, antialiased=True, alpha=0.95)

        # Reference markers on the floor of the plot
        floor = np.zeros_like(days, dtype=float)
        for value, color, label in ((p05, "#ff5252", "5th pct"),
                                    (p50, "#f5f5f5", "median"),
                                    (p95, "#4ade80", "95th pct")):
            ax.plot(days, np.full_like(floor, value), floor,
                    color=color, lw=2.0, ls="--", label=f"{label}  ${value:,.0f}")

        ax.set_xlabel(f"Trading days forward (from day {int(days[0])})",
                      color="#c9d1d9", labelpad=10, fontsize=9)
        ax.set_ylabel("Portfolio value ($)", color="#c9d1d9", labelpad=14, fontsize=9)
        ax.set_zlabel("Probability density", color="#c9d1d9", labelpad=10, fontsize=9)
        ax.set_zlim(0, float(grid_z.max()) * 1.05)
        drift_note = "zero drift" if DRIFT_MODE == "zero" else "sample drift"
        ax.set_title(f"Simulated portfolio distribution — {N_PATHS:,} correlated paths\n"
                     f"start ${start_value:,.0f}  ·  {N_STEPS} days  ·  Cholesky-correlated GBM, {drift_note}",
                     color="#f0f6fc", fontsize=11, pad=18)

        for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
            axis.set_pane_color((0.11, 0.12, 0.13, 1.0))
            axis._axinfo["grid"]["color"] = (0.3, 0.32, 0.35, 1.0)
        ax.tick_params(colors="#8b949e", labelsize=8)
        leg = ax.legend(loc="upper right", fontsize=8, facecolor="#22262b",
                        edgecolor="#30363d", labelcolor="#c9d1d9",
                        framealpha=0.9, borderpad=0.7)
        ax.view_init(elev=28, azim=-122)

        tmp = RISK_CHART_PATH + ".tmp"
        fig.savefig(tmp, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        os.replace(tmp, RISK_CHART_PATH)  # atomic — never serve a half-written PNG
        return True
    except Exception as e:
        log.error("Failed to render risk surface: %s", e)
        plt.close("all")
        return False


# ----------------------------------------------------
# FASTAPI ENDPOINTS (Accessed instantly by C++ Bot)
# ----------------------------------------------------

@app.get("/recommendations")
def get_recommendations():
    """Serves the pre-calculated recommendations list instantly via safe read locking."""
    with cache_lock:
        return RECOMMENDATIONS_CACHE


@app.get("/risk")
def get_risk():
    """Serve the cached Monte Carlo risk metrics."""
    if not session_ready.is_set():
        return {"status": "error",
                "message": "Robinhood session is still initializing, try again in a few seconds."}
    with risk_lock:
        return RISK_CACHE


@app.get("/risk/chart.png")
def get_risk_chart():
    """Serve the rendered 3D density surface as raw PNG bytes."""
    if not os.path.exists(RISK_CHART_PATH):
        return Response(content=b"", status_code=503, media_type="image/png")
    with open(RISK_CHART_PATH, "rb") as f:
        return Response(content=f.read(), media_type="image/png")


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
