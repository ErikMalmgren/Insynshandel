#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import csv
import json
import time
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# --------------------------- Konfiguration ---------------------------

ISIN_FILE = "isin.csv"                    # dina ISIN (header: ISIN)
GLEIF_FILE = "lei-isin-20251108.csv"      # GLEIF-mappning (header: LEI,ISIN) — redan filtrerad till SE
COMPANY_MAP = "company_map.csv"           # målfil (header: LEI,Ticker,MarketCap)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
#OPENFIGI_KEY = os.getenv("OPENFIGI_API_KEY")  # valfri
PREFER_MIC = "XSTO"                        # Nasdaq Stockholm
MIC_TO_YAHOO = {
    "XSTO": ".ST",  # Stockholm
    "XHEL": ".HE",  # Helsingfors
    "XCSE": ".CO",  # Köpenhamn
    "XICE": ".IC",  # Island
}

# ---------------------------- Hjälpfunktioner ----------------------------

def read_my_isins(path: str) -> set:
    """Läs dina ISIN från CSV (en kolumn, header ISIN)."""
    with open(path, encoding="utf-8") as f:
        next(f)  # hoppa header
        return {line.strip() for line in f if line.strip()}

def read_lei_to_isin_for_my_isins(path: str, my_isins: set) -> dict:
    """Läs GLEIF LEI,ISIN och returnera {LEI: valfritt_ISIN} för de ISIN du har."""
    leis = set()
    lei_to_isin = {}
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r)  # hoppa header
        for lei, isin in r:  # ordning: LEI,ISIN
            if isin in my_isins:
                leis.add(lei)
                if lei not in lei_to_isin:          # räcker med första ISIN per LEI
                    lei_to_isin[lei] = isin
    return lei_to_isin

def read_existing_leis_from_company_map(path: str) -> set:
    """Läs redan existerande LEI från company_map.csv (om den finns)."""
    existing = set()
    if not os.path.exists(path):
        return existing
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r, None)
        for row in r:
            if row:
                existing.add(row[0].strip())
    return existing

def yahoo_ticker_from_figi(isin: str) -> str:
    """
    Slå OpenFIGI för ISIN på XSTO och returnera Yahoo-format 'TICKER.ST'.
    Hanterar fall där OpenFIGI returnerar 'NCCB' (gör om till 'NCC-B').
    Returnerar "" om inget hittas eller vid fel.
    """
    if not isin:
        return ""

    body = json.dumps([{
        "idType": "ID_ISIN",
        "idValue": isin,
        "micCode": "XSTO"  # Stockholm
    }]).encode("utf-8")

    headers = {"Content-Type": "application/json"}

    try:
        req = Request(OPENFIGI_URL, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        hits = (data[0].get("data") or [])
        if not hits:
            return ""

        h = hits[0]
        raw = (h.get("ticker") or "").strip().upper()
        name = (h.get("name") or "").upper()

        if not raw:
            return ""

        # 1) SDB (depository shares), t.ex. 'ALIV SDB' -> 'ALIV-SDB.ST'
        if " SDB" in raw or " SDB" in name or raw.endswith("SDB"):
            s = raw.replace(" SDB", "-SDB").replace(" ", "-")
            if s.endswith("SDB") and not s.endswith("-SDB"):
                s = s[:-3] + "-SDB"
            return s + ".ST"

        # 2) Byt mellanslag mot '-' om det redan finns (vanligt 'INVE B' -> 'INVE-B.ST')
        if " " in raw:
            return raw.replace(" ", "-") + ".ST"

        # 3) Försök sätta '-' före aktieslag med hjälp av namnet ('ser. B'/'class B')
        m = re.search(r'\b(?:SER|SERIES|CLASS)\.?\s*([A-D])\b', name, re.I)
        if m and raw.endswith(m.group(1)):
            return raw[:-1] + "-" + raw[-1] + ".ST"

        # 4) Sista försiktiga heuristiken: om raw slutar på A/B/C/D och inte redan har '-'
        #    undvik uppenbara falska positiva som 'ABB' eller 'ALFA'
        if raw.endswith(tuple("ABCD")) and raw not in {"ABB", "ALFA"}:
            # hantera t.ex. 'SSABB' -> 'SSAB-B'
            if len(raw) >= 4 and raw[-2] != raw[-1]:
                return raw[:-1] + "-" + raw[-1] + ".ST"

        # 5) Inga förändringar behövs
        return raw + ".ST"

    except Exception:
        return ""


# ------------------------------- Huvudflöde -------------------------------

def main():
    # 1) Läs dina ISIN
    my_isins = read_my_isins(ISIN_FILE)

    # 2) Bygg LEI->ISIN (för just dina ISIN) från GLEIF
    lei_to_isin = read_lei_to_isin_for_my_isins(GLEIF_FILE, my_isins)
    leis = set(lei_to_isin.keys())

    # 3) Hämta redan existerande LEI i company_map
    existing = read_existing_leis_from_company_map(COMPANY_MAP)

    # 4) Räkna ut vilka som saknas
    missing = sorted(leis - existing)
    if not missing:
        print("Inga nya LEI att lägga till.")
        return

    # 5) Skriv (append eller skapa) company_map.csv och fyll Ticker via OpenFIGI
    mode = "a" if os.path.exists(COMPANY_MAP) else "w"
    with open(COMPANY_MAP, mode, newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if mode == "w":
            w.writerow(["LEI", "Ticker", "MarketCap"])
        for lei in missing:
            isin = lei_to_isin.get(lei, "")
            ticker = yahoo_ticker_from_figi(isin) if isin else ""
            # MarketCap lämnas tomt tills du uppdaterar med yfinance
            w.writerow([lei, ticker, ""])
            print(f"Lade till: LEI={lei}  Ticker={ticker or '(okänd)'}")
            time.sleep(2.5)

if __name__ == "__main__":
    main()
