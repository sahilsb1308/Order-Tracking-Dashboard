"""Rebuild Summary tab from existing Orders tab — no API calls needed."""
import os, tempfile
from collections import defaultdict
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────
_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
_SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
if _SA_JSON:
    _f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _f.write(_SA_JSON); _f.close()
    SERVICE_ACCOUNT = _f.name
elif _SA_FILE:
    SERVICE_ACCOUNT = _SA_FILE
else:
    raise EnvironmentError("Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")

SHEET_ID = os.getenv("SHEET_ID") or ""
if not SHEET_ID:
    raise EnvironmentError("Set SHEET_ID")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds  = Credentials.from_service_account_file(SERVICE_ACCOUNT, scopes=SCOPES)
gc     = gspread.authorize(creds)
ss     = gc.open_by_key(SHEET_ID)

# ── Status mappings (must match sync_clickpost.py) ────────────────────────────
STATUS_MAP = {
    "In Transit":       {3, 4, 5, 17, 18, 19, 20, 25, 28, 1004, 1005, 1006},
    "Out for delivery": {6, 44},
    "Delivered":        {8, 48},
    "Undelivered":      {9, 43},
    "Lost":             {16},
    "RTO":              {11, 12, 13, 14, 15, 21, 26, 27, 45, 46, 47, 50, 52},
}
POST_DISPATCH = set(STATUS_MAP.keys())

def get_category(code):
    for cat, codes in STATUS_MAP.items():
        if code in codes:
            return cat
    return None

STATUS_LABELS = ["Cancelled", "Confirmed", "Shopify Confirmed", "PFD", "In Transit",
                 "Out for delivery", "Delivered", "Undelivered", "Lost", "RTO"]
SUMMARY_COLS  = (["Date", "Grand Total"] + STATUS_LABELS +
                 [""] + [s + " %" for s in STATUS_LABELS if s != "Confirmed"])

# ── Read Orders tab ───────────────────────────────────────────────────────────
print("Reading Orders tab...", flush=True)
ws_orders = ss.worksheet("Orders")
rows = ws_orders.get_all_values()
if not rows:
    print("Orders tab is empty."); exit(1)

headers    = rows[0]
col        = {h: i for i, h in enumerate(headers)}
date_col     = col["Order Date (IST)"]
category_col = col["Category"]

daily = defaultdict(lambda: defaultdict(int))

VALID_CATS = {"Cancelled", "Shopify Confirmed", "PFD",
              "In Transit", "Out for delivery", "Delivered",
              "Undelivered", "Lost", "RTO"}

for row in rows[1:]:
    if len(row) <= max(date_col, category_col):
        continue
    date = (row[date_col] or "")[:10]
    cat  = (row[category_col] or "").strip()
    if not date or cat not in VALID_CATS:
        continue
    daily[date][cat] += 1

# ── Build Summary rows ────────────────────────────────────────────────────────
def fmt_date(d):
    return f"{d.day} {d.strftime('%b')} {d.strftime('%y')}"

out   = [SUMMARY_COLS]
grand = defaultdict(int)

for date_str in sorted(daily.keys(), reverse=True):
    c         = daily[date_str]
    total     = sum(c.values()) or 1
    confirmed = total - c["Cancelled"]
    denom     = confirmed or 1

    def pct(v, base=denom):
        return round(v / base, 4)

    try:
        label = fmt_date(datetime.strptime(date_str, "%Y-%m-%d"))
    except Exception:
        label = date_str

    out.append([
        label, total,
        c["Cancelled"], confirmed, c["Shopify Confirmed"], c["PFD"],
        c["In Transit"], c["Out for delivery"],
        c["Delivered"], c["Undelivered"], c["Lost"], c["RTO"],
        "",
        round(c["Cancelled"] / total, 4),
        pct(c["Shopify Confirmed"]), pct(c["PFD"]),
        pct(c["In Transit"]), pct(c["Out for delivery"]),
        pct(c["Delivered"]), pct(c["Undelivered"]), pct(c["Lost"]), pct(c["RTO"]),
    ])

    for k in ["Cancelled", "Shopify Confirmed", "PFD", "In Transit",
              "Out for delivery", "Delivered", "Undelivered", "Lost", "RTO"]:
        grand[k] += c[k]
    grand["total"] += total

gt  = grand["total"] or 1
gc_ = (gt - grand["Cancelled"]) or 1
out.append([
    "TOTAL", gt,
    grand["Cancelled"], gc_, grand["Shopify Confirmed"], grand["PFD"],
    grand["In Transit"], grand["Out for delivery"],
    grand["Delivered"], grand["Undelivered"], grand["Lost"], grand["RTO"],
    "",
    round(grand["Cancelled"]           / gt,  4),
    round(grand["Shopify Confirmed"]   / gc_, 4),
    round(grand["PFD"]                 / gc_, 4),
    round(grand["In Transit"]          / gc_, 4),
    round(grand["Out for delivery"]    / gc_, 4),
    round(grand["Delivered"]           / gc_, 4),
    round(grand["Undelivered"]         / gc_, 4),
    round(grand["Lost"]                / gc_, 4),
    round(grand["RTO"]                 / gc_, 4),
])

# ── Write Summary tab ─────────────────────────────────────────────────────────
print("Writing Summary tab...", flush=True)
try:
    ws_sum = ss.worksheet("Summary")
except gspread.WorksheetNotFound:
    ws_sum = ss.add_worksheet(title="Summary", rows=200, cols=30)

ws_sum.clear()
ws_sum.update(out, value_input_option="USER_ENTERED")
print(f"Done — {len(out)-2} date rows written.", flush=True)
