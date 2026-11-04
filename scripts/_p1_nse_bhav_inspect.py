"""P1 step 7f - inspect the official NSE F&O bhavcopy that was just retrieved.

Confirms the real column set (ISO-20022 style abbreviated names), the contract
identifier convention, and which columns can be compared against the
Hugging Face 1-minute archive.
"""

from __future__ import annotations

import glob
import io
import zipfile

import pandas as pd

path = sorted(glob.glob("data/raw/p1_nse/*BhavCopy_NSE_FO*20260825*.zip"))[0]
archive = zipfile.ZipFile(path)
raw = archive.read(archive.namelist()[0]).decode("utf-8", "replace")
df = pd.read_csv(io.StringIO(raw))

print("shape:", df.shape)
print("FinInstrmTp:", df["FinInstrmTp"].value_counts().to_dict())
print("Sgmt:", df["Sgmt"].value_counts().to_dict())
print("SctySrs non-null:", int(df["SctySrs"].notna().sum()), df["SctySrs"].dropna().unique()[:6])
print("OptnTp:", df["OptnTp"].value_counts().to_dict())
print("top TckrSymb:", df["TckrSymb"].value_counts().head(8).to_dict())

key = df[(df["TckrSymb"] == "NIFTY") & (df["FinInstrmTp"] == "IDO")].copy()
print(f"\nNIFTY IDO (index option) rows: {len(key)}   expiries: {sorted(key['XpryDt'].astype(str).unique())}")
print("lot sizes present:", sorted(key["NewBrdLotQty"].dropna().unique()))
print("strike spacing sample:", sorted(key["StrkPric"].round(2).unique())[:6], "...", sorted(key["StrkPric"].round(2).unique())[-3:])

cols = ["TckrSymb", "SctySrs", "OptnTp", "StrkPric", "XpryDt", "NewBrdLotQty", "UndrlygPric",
        "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric", "SttlmPric", "PrvsClsgPric",
        "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlNbOfTxsExctd", "FinInstrmNm"]
cols = [c for c in cols if c in key.columns]
atm = key[(key["OptnTp"] == "CE") & (key["XpryDt"] == key["XpryDt"].max())].copy()
atm["dist"] = (atm["StrkPric"] - atm["UndrlygPric"].max()).abs()
with pd.option_context("display.width", 250, "display.max_columns", 40):
    print("\nmonthly-expiry CE rows nearest ATM:")
    print(atm.nsmallest(6, "dist")[cols].to_string(index=False))
    print("\nunderlying print:", atm["UndrlygPric"].dropna().unique()[:5])
    print("\nFinInstrmNm sample:", key["FinInstrmNm"].dropna().unique()[:4])
    print("\nfull expiry list with contract counts:")
    print(key.groupby(["XpryDt", "OptnTp"]).size().unstack(fill_value=0).to_string())
