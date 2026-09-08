# ['0, Publiceringsdatum', '1, Emittent', '2, LEI-kod', '3, Anmälningsskyldig', '4, Person i ledande ställning', 
# '5, Befattning', '6, Närstående', '7, Korrigering', '8, Beskrivning av korrigering', ',9 Är förstagångsrapportering', 
# '10, Är kopplad till aktieprogram', '11, Karaktär', '12, Instrumenttyp', '13, Instrumentnamn', '14, ISIN', '15, Transaktionsdatum', 
# '16, Volym', '17, Volymsenhet', '18, Pris', '19, Valuta', '20, Handelsplats', '21, Status', '']

import os
import csv
import subprocess
import sys



res = dict()
name_variations = dict()
isin = set()
filename = next(f for f in os.listdir(".") if f.startswith("Insyn") and f.endswith(".csv"))

with open("isin.csv", newline="",encoding="utf-8") as f:
    next(f)  # hoppa headern "ISIN"
    isin = {line.strip() for line in f if line.strip()}


with open(filename, encoding="utf-16le") as f:
    header = f.readline().strip().split(";")

    for line in f:
        cur_line = line.strip().split(";")
        if cur_line[19] != "SEK":
            continue

        karak = cur_line[11]
        name = cur_line[1].strip()
        volym = float(cur_line[16].replace(',', '.'))
        pris = float(cur_line[18].replace(',', '.'))
        status = cur_line[21]
        lei = cur_line[2]
        isin.add(cur_line[14])

        if status != "Aktuell":
            continue

        if lei not in name_variations:
            name_variations[lei] = set()
        name_variations[lei].add(name)

        if karak in ("Förvärv", "00Teckning", "00Tilldelning"):
            res[lei] = res.setdefault(lei, 0) + pris * volym
        elif karak == "Avyttring":
            res[lei] = res.setdefault(lei, 0) - pris * volym
        elif karak in ("Lösen minskning", "Lösen ökning", "Utbyte ökning", "Utbyte minskning", "Lån mottaget", "Lån utlåning", "Inlösen egenutfärdat instrument", "Pantsättning", "Konvertering minskning", "Konvertering ökning", "Lån återgång ökning", "Utdelning lämnad", "Gåva lämnad", "Utfärdande av instrument"):
            continue
        else:
            #print(karak)
            continue

with open("isin.csv", 'w', newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["ISIN"])
    for code in isin:
        if code:
            writer.writerow([code.strip()])

sorted_list = sorted(res.items(), key=lambda x: x[1], reverse=True)

#proc1 = subprocess.Popen([sys.executable, "lei_isin.py"])
#proc1.wait()

#proc2 = subprocess.Popen([sys.executable, "marketCap.py"])
#proc2.wait()

def load_market_caps(path="company_map.csv"):
    lei_to_mcap = {}
    try:
        with open(path, encoding="utf-8", newline="") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                lei = (row.get("LEI") or "").strip()
                mcap_raw = (row.get("MarketCap") or "").strip()
                if not lei:
                    continue

                try:
                    mcap = float(mcap_raw.replace(" ", "").replace("\xa0","").replace(",", ""))
                except ValueError:
                    mcap = 0.0
                lei_to_mcap[lei] = mcap
    except FileNotFoundError:
        pass
    return lei_to_mcap

lei_to_mcap = load_market_caps()

print("FÖRETAG".ljust(50) + "NETTOINSYNSHANDEL".ljust(20) + "ANDEL AV MCAP")
print("-" * 83)

for lei, netto in sorted_list:
    display_name = sorted(name_variations[lei])[0] if name_variations[lei] else lei
    formatted = f"{netto:,.0f}".replace(",", " ")

    mcap = lei_to_mcap.get(lei, 0.0)
    if mcap > 0:
        pct_str = f"{(netto / mcap) * 100:.3f}%"
    else:
        pct_str = "(saknar mcap)"

    print(f"{display_name.ljust(50)}{(formatted + ' kr').ljust(20)}{pct_str}")
   