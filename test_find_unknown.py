#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import os
import json
import time
import re
import logging
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from typing import Optional

# ---- Konfig ----
COMPANY_MAP = "company_map.csv"          # header: LEI,Ticker,MarketCap
GLEIF_FILE  = "lei-isin-20251108.csv"    # header: LEI,ISIN (endast SE)
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_KEY = os.getenv("OPENFIGI_API_KEY")  # valfri
RATE_PER_MIN = 25
INTERVAL = 60.0 / RATE_PER_MIN  # ~2.4 s
MAX_RETRIES = 2
DIAGNOSTIC_MODE = os.getenv("DIAGNOSTIC_MODE", "false").lower() == "true"  # Set to "true" for detailed debugging
TRY_WITHOUT_MIC = True  # Try without micCode if XSTO fails

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def yahoo_ticker_from_figi(isin: str, lei: str = "", retries: int = 0) -> Optional[str]:
    """
    Slå OpenFIGI för ISIN på XSTO och returnera Yahoo-format 'TICKER.ST'.
    Hanterar SDB och aktieslag (ser./class A–D), t.ex. 'INVE B'->'INVE-B.ST', 'NCCB'->'NCC-B.ST'.
    """
    if not isin:
        return None
    
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
    
    # First try with XSTO micCode
    body = json.dumps([{"idType": "ID_ISIN", "idValue": isin, "micCode": "XSTO"}]).encode("utf-8")

    try:
        req = Request(OPENFIGI_URL, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # Check for error response
        if not data or not isinstance(data, list):
            logger.warning(f"Unexpected response format for ISIN {isin}")
            return None
        
        if "error" in data[0]:
            error_msg = data[0].get("error", "Unknown error")
            
            # If no results with XSTO, try without micCode
            if TRY_WITHOUT_MIC and "No identifier found" in error_msg:
                logger.debug(f"No results with XSTO for {isin}, trying without micCode...")
                return _try_without_mic(isin, lei, headers)
            
            logger.warning(f"OpenFIGI error for ISIN {isin} (LEI: {lei}): {error_msg}")
            return None

        hits = data[0].get("data") or []
        if not hits:
            # Try without micCode restriction
            if TRY_WITHOUT_MIC:
                logger.debug(f"No XSTO results for {isin}, trying without micCode...")
                return _try_without_mic(isin, lei, headers)
            
            logger.warning(f"No FIGI data found for ISIN {isin} (LEI: {lei}) - ISIN may not be in OpenFIGI database")
            return None

        # Debug: log what we received
        if DIAGNOSTIC_MODE:
            logger.info(f"FIGI response for {isin}: {json.dumps(hits[0], indent=2)}")
        
        # Plocka ut både ticker & namn från första träffen
        raw = (hits[0].get("ticker") or "").strip().upper()
        name = (hits[0].get("name") or "").strip().upper()
        market_sector = hits[0].get("marketSector", "")
        exchange_code = hits[0].get("exchCode", "")
        
        # Log detailed info för debugging
        logger.debug(f"  Raw ticker: '{raw}', Name: '{name}', Market: {market_sector}, Exchange: {exchange_code}")
        
        if not raw:
            logger.warning(f"Empty ticker in FIGI response for ISIN {isin} (LEI: {lei}) - Name: '{name}', Market: {market_sector}")
            return None

        ticker = _format_ticker(raw, name)
        logger.info(f"Converted '{raw}' -> '{ticker}' for ISIN {isin}")
        return ticker

    except HTTPError as e:
        if e.code == 429:  # Rate limit
            if retries < MAX_RETRIES:
                wait_time = (retries + 1) * 5
                logger.warning(f"Rate limit hit for {isin}, waiting {wait_time}s before retry {retries+1}/{MAX_RETRIES}")
                time.sleep(wait_time)
                return yahoo_ticker_from_figi(isin, lei, retries + 1)
            else:
                logger.error(f"Rate limit exceeded for ISIN {isin} (LEI: {lei}) after {MAX_RETRIES} retries")
        elif e.code == 404:
            logger.warning(f"ISIN {isin} not found in OpenFIGI (LEI: {lei})")
        else:
            logger.error(f"HTTP {e.code} error for ISIN {isin} (LEI: {lei}): {e.reason}")
        return None
    
    except URLError as e:
        logger.error(f"Network error for ISIN {isin} (LEI: {lei}): {e.reason}")
        return None
    
    except TimeoutError:
        logger.error(f"Timeout error for ISIN {isin} (LEI: {lei})")
        return None
    
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for ISIN {isin}: {e}")
        return None
    
    except Exception as e:
        logger.error(f"Unexpected error for ISIN {isin} (LEI: {lei}): {type(e).__name__}: {e}")
        return None

def _try_without_mic(isin: str, lei: str, headers: dict) -> Optional[str]:
    """Try fetching without micCode restriction, useful for ISINs not listed on XSTO."""
    body = json.dumps([{"idType": "ID_ISIN", "idValue": isin}]).encode("utf-8")
    
    try:
        req = Request(OPENFIGI_URL, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        
        if not data or not isinstance(data, list) or "error" in data[0]:
            return None
        
        hits = data[0].get("data") or []
        if not hits:
            return None
        
        # Priority 1: Look for Stockholm exchange (XSTO, SS, ST)
        for hit in hits:
            exch = (hit.get("exchCode") or "").upper()
            mic = (hit.get("micCode") or "").upper()
            
            if DIAGNOSTIC_MODE:
                logger.info(f"Alternative result: {json.dumps(hit, indent=2)}")
            
            if exch in ["SS", "ST"] or mic == "XSTO":
                raw = (hit.get("ticker") or "").strip().upper()
                name = (hit.get("name") or "").strip().upper()
                if raw:
                    ticker = _format_ticker(raw, name)
                    logger.info(f"Found via alternative search: '{raw}' -> '{ticker}' (Exchange: {exch}, MIC: {mic})")
                    return ticker
        
        # Priority 2: For Swedish ISINs (SE*), try to use any available ticker with .ST suffix
        # This handles cases where companies are listed elsewhere but still Swedish
        if isin.startswith("SE"):
            # Look for reasonable ticker candidates
            for hit in hits:
                ticker_raw = (hit.get("ticker") or "").strip().upper()
                name = (hit.get("name") or "").strip().upper()
                exch = hit.get("exchCode", "?")
                mic = hit.get("micCode", "?")
                market_sector = hit.get("marketSector", "")
                
                # Skip clearly non-equity instruments
                if market_sector and market_sector not in ["Equity"]:
                    continue
                
                # Prefer simpler tickers (avoid those with SEK suffix as they're often currency-specific)
                if ticker_raw and not ticker_raw.endswith("SEK"):
                    ticker = _format_ticker(ticker_raw, name)
                    logger.info(f"Using Swedish ISIN ticker from {exch}/{mic}: '{ticker_raw}' -> '{ticker}'")
                    logger.info(f"  Company: {name}")
                    return ticker
            
            # If only SEK-suffixed tickers available, use the shortest one
            for hit in hits:
                ticker_raw = (hit.get("ticker") or "").strip().upper()
                name = (hit.get("name") or "").strip().upper()
                exch = hit.get("exchCode", "?")
                
                if ticker_raw:
                    # Remove SEK suffix and apply formatting
                    if ticker_raw.endswith("SEK"):
                        ticker_raw = ticker_raw[:-3]
                    ticker = _format_ticker(ticker_raw, name)
                    logger.info(f"Using Swedish ISIN ticker (SEK variant) from {exch}: '{ticker_raw}' -> '{ticker}'")
                    return ticker
        
        # If no Stockholm exchange found and not Swedish ISIN, log for manual review
        logger.warning(f"ISIN {isin} (LEI: {lei}) found but not on Stockholm exchange:")
        for hit in hits[:3]:  # Show first 3 results
            exch = hit.get("exchCode", "?")
            mic = hit.get("micCode", "?")
            ticker = hit.get("ticker", "?")
            name = hit.get("name", "?")
            logger.warning(f"  - {ticker} on {exch}/{mic}: {name}")
        
        return None
        
    except Exception:
        return None

def _format_ticker(raw: str, name: str) -> str:
    """
    Format ticker according to Yahoo Finance conventions for Stockholm Stock Exchange.
    Returns ticker with .ST suffix.
    """
    # 1) SDB (depository shares), t.ex. 'ALIV SDB' -> 'ALIV-SDB.ST'
    if " SDB" in raw or " SDB" in name or raw.endswith("SDB"):
        s = raw.replace(" SDB", "-SDB").replace(" ", "-")
        if s.endswith("SDB") and not s.endswith("-SDB"):
            s = s[:-3] + "-SDB"
        return s + ".ST"

    # 2) Byt mellanslag mot '-' om det redan finns (vanligt 'INVE B' -> 'INVE-B.ST')
    if " " in raw:
        return raw.replace(" ", "-") + ".ST"

    # 3) Sätt '-' före aktieslag med hjälp av namnet ('ser. B'/'class B')
    m = re.search(r'\b(?:SER|SERIES|CLASS)\.?\s*([A-D])\b', name, re.I)
    if m and raw.endswith(m.group(1)):
        return raw[:-1] + "-" + raw[-1] + ".ST"

    # 4) Heuristik: om raw slutar på A/B/C/D och inte redan har '-' 
    # (undvik falska som 'ABB', 'ALFA')
    EXCLUDE_TICKERS = {"ABB", "ALFA", "ACA", "ACSA", "DIOS"}
    if raw.endswith(tuple("ABCD")) and raw not in EXCLUDE_TICKERS:
        if len(raw) >= 4 and raw[-2] != raw[-1]:
            return raw[:-1] + "-" + raw[-1] + ".ST"

    # 5) Inga förändringar behövs
    return raw + ".ST"

def load_company_map(filepath: str) -> tuple[list, list, list]:
    """Load company map and return header, all rows, and rows needing tickers."""
    if not os.path.exists(filepath):
        logger.error(f"File not found: {filepath}")
        raise FileNotFoundError(f"Cannot find {filepath}")
    
    rows = []
    with open(filepath, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        for row in r:
            if row:
                rows.append(row)
    
    target_leis = [row[0].strip() for row in rows if not (row[1] or "").strip()]
    logger.info(f"Loaded {len(rows)} companies, {len(target_leis)} missing tickers")
    return header, rows, target_leis

def load_gleif_mapping(filepath: str, target_leis: set) -> dict:
    """Load GLEIF file and create LEI->ISIN mapping for target LEIs."""
    if not os.path.exists(filepath):
        logger.error(f"File not found: {filepath}")
        raise FileNotFoundError(f"Cannot find {filepath}")
    
    lei_to_isin = {}
    with open(filepath, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)  # skip header
        for lei, isin in r:
            lei = lei.strip()
            if lei in target_leis and lei not in lei_to_isin:
                lei_to_isin[lei] = (isin or "").strip()
    
    logger.info(f"Found {len(lei_to_isin)} ISIN mappings for missing tickers")
    return lei_to_isin

def main():
    logger.info("Starting ticker finder...")
    
    if not OPENFIGI_KEY:
        logger.warning("OPENFIGI_API_KEY not set - using unauthenticated requests (lower rate limit)")
    
    if DIAGNOSTIC_MODE:
        logger.setLevel(logging.DEBUG)
        logger.info("DIAGNOSTIC_MODE enabled - will show detailed API responses")
    
    try:
        # 1) Load company map
        header, rows, target_leis = load_company_map(COMPANY_MAP)
        
        if not target_leis:
            logger.info("No empty tickers to fill - all done!")
            return
        
        # 2) Load GLEIF mapping
        lei_to_isin = load_gleif_mapping(GLEIF_FILE, set(target_leis))
        
        if not lei_to_isin:
            logger.warning("No ISIN mappings found for any target LEIs")
            return
        
        # 3) Fill missing tickers with rate limiting
        next_ok = 0.0
        updated = 0
        failed = 0
        failed_records = []  # Track failures for export
        
        # Build a quick lookup from LEI → row
        lei_to_row = {row[0].strip(): row for row in rows}
        
        for lei in target_leis:  # ✅ iterate only over LEIs missing ticker
            row = lei_to_row[lei]
            isin = lei_to_isin.get(lei, "")
            if not isin:
                logger.debug(f"No ISIN found for LEI {lei}")
                continue
            
            # Rate limiting
            now = time.monotonic()
            if now < next_ok:
                time.sleep(next_ok - now)
            
            ticker = yahoo_ticker_from_figi(isin, lei)
            next_ok = time.monotonic() + INTERVAL
            
            if ticker:
                row[1] = ticker
                updated += 1
                logger.info(f"✓ Updated: LEI {lei} -> {ticker} (ISIN: {isin})")
            else:
                failed += 1
                failed_records.append({"LEI": lei, "ISIN": isin})
                logger.warning(f"✗ Failed: LEI {lei} (ISIN: {isin})")
        
        # 4) Write back only if something changed
        if updated:
            with open(COMPANY_MAP, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            logger.info(f"✓ Complete! Updated {updated} tickers in {COMPANY_MAP}")
        else:
            logger.info("No updates made - no matches found from OpenFIGI")
        
        # 5) Export failed ISINs for manual review
        if failed_records:
            failed_file = "failed_isins.csv"
            with open(failed_file, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["LEI", "ISIN", "Ticker"])
                w.writeheader()
                for record in failed_records:
                    record["Ticker"] = ""  # Empty for manual filling
                    w.writerow(record)
            logger.info(f"Exported {len(failed_records)} failed ISINs to {failed_file} for manual review")
            logger.info(f"You can manually add tickers to this file and use it to update company_map.csv")
        
        # Summary
        logger.info(f"Summary: {updated} succeeded, {failed} failed, {len(target_leis)-(updated+failed)} skipped (no ISIN)")
    
    except FileNotFoundError as e:
        logger.error(f"File error: {e}")
        return
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return
    except Exception as e:
        logger.error(f"Unexpected error: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()