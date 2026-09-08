import csv
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

filename = "company_map.csv"
rows = []
updated_rows = []

def fetch_market_cap(row):
    ticker_symbol = row.get("Ticker", "").strip()
    lei = row.get("LEI", "").strip()
    
    if not ticker_symbol:
        logger.debug(f"Skipping LEI {lei}: No ticker symbol")
        return row
    
    try:
        # logger.info(f"Fetching data for ticker: {ticker_symbol} (LEI: {lei})")
        ticker = yf.Ticker(ticker_symbol)
        market_cap = ticker.fast_info.get("marketCap")
        
        if market_cap is not None:
            row["MarketCap"] = market_cap
            # logger.info(f"✓ Success: {ticker_symbol} - Market Cap: {market_cap:,}")
        else:
            logger.warning(f"⚠ No market cap data for {ticker_symbol} (LEI: {lei})")
            
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        
        # More specific error messages
        if "404" in error_msg or "Not Found" in error_msg:
            logger.error(f"✗ HTTP 404: Ticker '{ticker_symbol}' not found (LEI: {lei}). "
                        f"Verify ticker symbol is correct.")
        elif "429" in error_msg or "Too Many Requests" in error_msg:
            logger.error(f"✗ Rate limit exceeded for {ticker_symbol}. Consider adding delays.")
        elif "connection" in error_msg.lower() or "timeout" in error_msg.lower():
            logger.error(f"✗ Connection error for {ticker_symbol}: {error_msg}")
        else:
            logger.error(f"✗ {error_type} for ticker '{ticker_symbol}' (LEI: {lei}): {error_msg}")
    
    return row

# Läs in
try:
    with open(filename, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows = list(reader)
    
    logger.info(f"Loaded {len(rows)} rows from {filename}")
    
except FileNotFoundError:
    logger.error(f"File not found: {filename}")
    exit(1)

# Uppdatera värden
logger.info("Starting market cap fetch...")
with ThreadPoolExecutor(max_workers=5) as executor:
    updated_rows = list(executor.map(fetch_market_cap, rows))

# Skriv tillbaka
try:
    with open(filename, 'w', newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(updated_rows)
    
    logger.info(f"Successfully wrote updated data to {filename}")
    
    # Summary statistics
    successful = sum(1 for row in updated_rows if row.get("MarketCap"))
    total_with_ticker = sum(1 for row in updated_rows if row.get("Ticker", "").strip())
    logger.info(f"Summary: {successful}/{total_with_ticker} tickers successfully updated")
    
except Exception as e:
    logger.error(f"Error writing to file: {e}")
    exit(1)