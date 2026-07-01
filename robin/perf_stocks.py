from fastapi import FastAPI
import robin_stocks.robinhood as r
from credentials import username, password
import os

app = FastAPI()

# Configuration (Use environment variables or secure storage for credentials)
ROBINHOOD_USER = os.getenv("ROBINHOOD_USER", username)
ROBINHOOD_PASS = os.getenv("ROBINHOOD_PASS", password)

@app.on_event("startup")
def login_to_robinhood():
    # Note: If you have MFA enabled, robin_stocks will prompt you in the 
    # terminal the VERY FIRST time you run this to enter your SMS/auth code.
    # It will cache the token locally for subsequent starts.
    print("Logging into Robinhood...")
    r.login(username=ROBINHOOD_USER, password=ROBINHOOD_PASS, expiresIn=86400)
    print("Logged in successfully.")

@app.get("/portfolio")
def get_portfolio():
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

        # Structure the data clean and simple for your C++ bot to parse
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

# Optional endpoint for top performing recommendations
@app.get("/recommendations")
def get_recommendations():
    # You can customize this logic using r.stocks or r.markets functions
    return {
        "daily": ["AAPL", "NVDA"],
        "weekly": ["MSFT", "AMD"],
        "monthly": ["TSLA", "INTC"]
    }
