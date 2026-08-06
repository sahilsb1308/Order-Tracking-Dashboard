import requests
import time
import os
import json
import tempfile
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import gspread
from google.oauth2.service_account import Credentials

# ── Config (all sensitive values must be set as environment variables) ─────────
def _require(name):
    val = os.getenv(name)
    if not val:
        raise EnvironmentError(f"Missing required env var: {name}")
    return val

CLICKPOST_KEY      = os.getenv("CLICKPOST_KEY")      or _require("CLICKPOST_KEY")
CLICKPOST_USERNAME = os.getenv("CLICKPOST_USERNAME") or _require("CLICKPOST_USERNAME")
SHOPIFY_STORE      = os.getenv("SHOPIFY_STORE")      or _require("SHOPIFY_STORE")
SHOPIFY_TOKEN      = os.getenv("SHOPIFY_TOKEN")      or _require("SHOPIFY_TOKEN")
SHEET_ID           = os.getenv("SHEET_ID")           or _require("SHEET_ID")

# Service account JSON: from env var (GitHub Actions) or local file path
_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
_SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
if _SA_JSON:
    _sa_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _sa_tmp.write(_SA_JSON)
    _sa_tmp.close()
    SERVICE_ACCOUNT = _sa_tmp.name
elif _SA_FILE:
    SERVICE_ACCOUNT = _SA_FILE
else:
    raise EnvironmentError("Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")

IST        = timezone(timedelta(hours=5, minutes=30))
START_DATE = "2026-07-01"   # fixed start — fetch everything from 1 July onwards
FETCH_DAYS = (datetime.now(IST).date() - datetime.strptime(START_DATE, "%Y-%m-%d").date()).days + 1

# ── Status mappings ───────────────────────────────────────────────────────────
STATUS_LABELS = ["Cancelled", "Confirmed", "PFD", "In Transit",
                 "Out for delivery", "Delivered", "Undelivered", "RTO"]

STATUS_MAP = {
    # Confirmed and Cancelled now come from Shopify, not Clickpost
    "In Transit":       {4, 5, 18, 19, 20, 1004, 1005, 1006},
    "Out for delivery": {6, 44},
    "Delivered":        {8, 48},
    "Undelivered":      {9},
    "RTO":              {11, 12, 13, 14, 15, 21, 26, 27, 45, 47, 50, 52},
}

SUMMARY_COLS = (["Date", "Grand Total"] + STATUS_LABELS +
                [""] + [s + " %" for s in STATUS_LABELS])

ORDER_COLS = [
    "Shopify Order #", "AWB", "Channel",
    "Order Date (IST)", "Last Updated (IST)", "Last Scan Time",
    "Status Code", "Status", "Location", "City", "Courier Partner", "Remark",
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_category(code):
    for cat, codes in STATUS_MAP.items():
        if code in codes:
            return cat
    return None

def fmt_date(d):
    return f"{d.day} {d.strftime('%b')} {d.strftime('%y')}"

def to_ist_str(utc_iso):
    if not utc_iso:
        return ""
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_iso

def get_or_create_sheet(spreadsheet, name):
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=name, rows=100000, cols=25)

# ── Step 1: Fetch Shopify orders ──────────────────────────────────────────────
def fetch_shopify_orders():
    """Returns dict: order_number (str) -> {order_date, shopify_id, awb}"""
    import re
    print("Fetching Shopify orders...", flush=True)
    url     = f"https://{SHOPIFY_STORE}/admin/api/2024-01/orders.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    params  = {
        "created_at_min": f"{START_DATE}T00:00:00+05:30",
        "status": "any",
        "limit":  250,
        "fields": "id,order_number,created_at,fulfillments,cancelled_at",
    }

    order_map = {}  # order_number -> {order_date, shopify_id, awb}
    page = 1
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        orders = resp.json().get("orders", [])
        if not orders:
            break

        for order in orders:
            order_number = str(order.get("order_number", ""))
            created_at   = order.get("created_at", "")
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                order_date = str(dt.astimezone(IST).date())
            except Exception:
                order_date = ""

            # Get first AWB from fulfillments (if any)
            awb = ""
            for f in order.get("fulfillments", []):
                tn = (f.get("tracking_number") or "").strip()
                if tn:
                    awb = tn
                    break

            order_map[order_number] = {
                "order_date":   order_date,
                "awb":          awb,
                "cancelled_at": order.get("cancelled_at") or "",
            }

        print(f"  Page {page}: {len(orders)} orders (total: {len(order_map):,})", flush=True)
        page += 1

        link = resp.headers.get("Link", "")
        if 'rel="next"' not in link:
            break
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not match:
            break
        next_url = match.group(1)
        params = {"page_info": next_url.split("page_info=")[1].split("&")[0], "limit": 250}
        time.sleep(0.3)

    print(f"Shopify fetch complete — {len(order_map):,} orders from {page-1} pages.", flush=True)
    return order_map

# ── Step 2: Fetch Clickpost statuses ─────────────────────────────────────────
def fetch_clickpost_statuses():
    """Returns dict: order_id (str) -> latest Clickpost record"""
    now = datetime.now(IST)
    waybill_latest = {}

    for day_offset in range(FETCH_DAYS):
        day       = (now - timedelta(days=day_offset)).date()
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=IST)
        print(f"\nClickpost {day} ", end="", flush=True)

        for window in range(48):
            win_start = day_start + timedelta(minutes=30 * window)
            if win_start >= now:
                break
            win_end = win_start + timedelta(minutes=30)
            try:
                r = requests.get(
                    "https://api.clickpost.in/api/v1/updated-order",
                    params={"key": CLICKPOST_KEY, "username": CLICKPOST_USERNAME,
                            "start_date": int(win_start.timestamp()),
                            "end_date":   int(win_end.timestamp())},
                    headers={"accept": "application/json"},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                records = data.get("result", []) if data.get("meta", {}).get("success") else []
                print(".", end="", flush=True)

                for rec in records:
                    oid        = str(rec.get("order_id") or "").strip()
                    code       = rec.get("clickpost_status_code")
                    updated_at = rec.get("updated_at") or ""
                    if not oid or code is None:
                        continue
                    existing = waybill_latest.get(oid)
                    if existing is None or updated_at > existing.get("updated_at", ""):
                        waybill_latest[oid] = rec
            except Exception as e:
                print(f"[ERR:{e}]", end="", flush=True)
            time.sleep(0.05)

    print(f"\nClickpost fetch complete — {len(waybill_latest):,} unique AWBs.", flush=True)
    return waybill_latest

# ── Step 3: Join & build rows ─────────────────────────────────────────────────
def build_orders(order_map, cp_status):
    """One row per Shopify order, joined with Clickpost status by order_number."""
    rows = [ORDER_COLS]
    for order_number, shopify in sorted(
        order_map.items(),
        key=lambda x: x[1].get("order_date", ""),
        reverse=True,
    ):
        rec  = cp_status.get(order_number, {})
        code = rec.get("clickpost_status_code", "")
        cancelled = bool(shopify.get("cancelled_at"))
        rows.append([
            order_number,
            shopify.get("awb", "") or rec.get("waybill", ""),
            rec.get("channel_name", "Swiss Beauty"),
            shopify.get("order_date", ""),
            to_ist_str(rec.get("updated_at", "")),
            rec.get("timestamp", ""),
            code,
            "Cancelled" if cancelled else rec.get("clickpost_status_description", ""),
            rec.get("location", ""),
            rec.get("clickpost_city", ""),
            rec.get("courier_partner", ""),
            rec.get("remark", ""),
        ])
    return rows

def build_summary(order_map, cp_status):
    """Group by Shopify order_date.
    Cancelled/Confirmed → from Shopify cancelled_at.
    PFD = Confirmed − dispatched (orders with no Clickpost record yet).
    In Transit / OFD / Delivered / Undelivered / RTO → from Clickpost.
    """
    daily_counts = defaultdict(lambda: defaultdict(int))

    for order_number, shopify in order_map.items():
        order_date = shopify.get("order_date", "")
        if not order_date or order_date < START_DATE:
            continue

        if shopify.get("cancelled_at"):
            cat = "Cancelled"
        else:
            rec  = cp_status.get(order_number, {})
            code = rec.get("clickpost_status_code")
            cat  = get_category(code) if code is not None else None
            # No Clickpost record, or code not in our map → PFD (not yet dispatched)
            if cat is None:
                cat = "PFD"

        daily_counts[order_date][cat] += 1

    rows = [SUMMARY_COLS]
    grand = defaultdict(int)  # running totals across all dates

    for date_str in sorted(daily_counts.keys(), reverse=True):
        c     = daily_counts[date_str]
        total = sum(c.values()) or 1
        confirmed = total - c["Cancelled"]

        def pct(v):
            return round(v / total, 4)

        d = datetime.strptime(date_str, "%Y-%m-%d")
        rows.append([
            fmt_date(d), total,
            c["Cancelled"], confirmed, c["PFD"],
            c["In Transit"], c["Out for delivery"],
            c["Delivered"], c["Undelivered"], c["RTO"],
            "",
            pct(c["Cancelled"]), pct(confirmed), pct(c["PFD"]),
            pct(c["In Transit"]), pct(c["Out for delivery"]),
            pct(c["Delivered"]), pct(c["Undelivered"]), pct(c["RTO"]),
        ])

        # accumulate totals
        grand["total"]     += total
        grand["Cancelled"] += c["Cancelled"]
        grand["PFD"]       += c["PFD"]
        grand["In Transit"]       += c["In Transit"]
        grand["Out for delivery"] += c["Out for delivery"]
        grand["Delivered"]        += c["Delivered"]
        grand["Undelivered"]      += c["Undelivered"]
        grand["RTO"]              += c["RTO"]

    # Totals row at the bottom
    gt = grand["total"] or 1
    g_confirmed = gt - grand["Cancelled"]

    def gpct(v):
        return round(v / gt, 4)

    rows.append([
        "TOTAL", gt,
        grand["Cancelled"], g_confirmed, grand["PFD"],
        grand["In Transit"], grand["Out for delivery"],
        grand["Delivered"], grand["Undelivered"], grand["RTO"],
        "",
        gpct(grand["Cancelled"]), gpct(g_confirmed), gpct(grand["PFD"]),
        gpct(grand["In Transit"]), gpct(grand["Out for delivery"]),
        gpct(grand["Delivered"]), gpct(grand["Undelivered"]), gpct(grand["RTO"]),
    ])

    return rows

# ── Step 4: Write to Google Sheets ────────────────────────────────────────────
def rgb(hex_str):
    h = hex_str.lstrip("#")
    return {"red": int(h[0:2],16)/255, "green": int(h[2:4],16)/255, "blue": int(h[4:6],16)/255}

def beautify(sh, orders_ws, summary_ws, num_order_rows):
    oid, sid = orders_ws.id, summary_ws.id
    DARK, WHITE, ALT, BOLD_BG = "#1B2631", "#FFFFFF", "#F4F6F7", "#EAECEE"
    STAT_COLORS = ["#7F8C8D", "#2980B9", "#E67E22", "#8E44AD",
                   "#16A085", "#27AE60", "#D35400", "#C0392B"]

    reqs = []

    # Header rows
    for sheet_id, ncols in [(oid, len(ORDER_COLS)), (sid, len(SUMMARY_COLS))]:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": rgb(DARK),
                "textFormat": {"foregroundColor": rgb(WHITE), "bold": True, "fontSize": 10},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat",
        }})
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 38}, "fields": "pixelSize",
        }})

    # Summary: status + % column header colors
    for i, hex_color in enumerate(STAT_COLORS):
        for col in [i + 2, i + 11]:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": rgb(hex_color),
                    "textFormat": {"foregroundColor": rgb(WHITE), "bold": True, "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                }},
                "fields": "userEnteredFormat",
            }})

    # Summary: % columns (L–S = index 11–18) → PERCENT format
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 50,
                  "startColumnIndex": 11, "endColumnIndex": 19},
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "PERCENT", "pattern": "0.0%"},
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
    }})

    # Summary: center-align numbers, bold Grand Total
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 50,
                  "startColumnIndex": 1, "endColumnIndex": len(SUMMARY_COLS)},
        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat(horizontalAlignment)",
    }})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 50,
                  "startColumnIndex": 1, "endColumnIndex": 2},
        "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "fontSize": 10},
            "backgroundColor": rgb(BOLD_BG),
        }},
        "fields": "userEnteredFormat",
    }})

    # Orders: alternating row banding
    try:
        meta = sh.fetch_sheet_metadata()
        for sheet in meta.get("sheets", []):
            if sheet["properties"]["sheetId"] == oid:
                for band in sheet.get("bandedRanges", []):
                    reqs.append({"deleteBanding": {"bandedRangeId": band["bandedRangeId"]}})
    except Exception:
        pass

    reqs.append({"addBanding": {"bandedRange": {
        "range": {"sheetId": oid, "startRowIndex": 1,
                  "endRowIndex": num_order_rows + 1,
                  "startColumnIndex": 0, "endColumnIndex": len(ORDER_COLS)},
        "rowProperties": {"firstBandColor": rgb(WHITE), "secondBandColor": rgb(ALT)},
    }}})

    # Column widths
    for sheet_id, widths in [
        (oid, [120, 160, 100, 120, 155, 155, 80, 130, 150, 100, 110, 260]),
        (sid, [100, 90, 90, 90, 90, 90, 125, 90, 100, 80, 20,
               90, 90, 80, 90, 130, 90, 100, 80]),
    ]:
        for i, w in enumerate(widths):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize",
            }})

    # Summary: bold TOTAL row at the bottom (row index = len of summary data)
    # We don't know the exact row at beautify time, so freeze last 1 row via a named range isn't easy;
    # instead apply bold+dark bg to a wide range at the bottom using a known max row
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 49, "endRowIndex": 50,
                  "startColumnIndex": 0, "endColumnIndex": len(SUMMARY_COLS)},
        "cell": {"userEnteredFormat": {
            "backgroundColor": rgb(DARK),
            "textFormat": {"foregroundColor": rgb(WHITE), "bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat",
    }})

    # Freeze header
    for sheet_id in [oid, sid]:
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }})

    # Conditional formatting on Status col (Orders col I = index 8)
    STATUS_CF = [
        ("Delivered", "#D5F5E3"), ("InTransit", "#D6EAF8"),
        ("OutForDelivery", "#D1F2EB"), ("FailedDelivery", "#FDEBD0"),
        ("RTO", "#FADBD8"), ("Cancelled", "#EAECEE"),
        ("PickupPending", "#FEF9E7"), ("OrderPlaced", "#EBF5FB"),
    ]
    for status_text, hex_color in STATUS_CF:
        reqs.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": oid, "startRowIndex": 1,
                            "endRowIndex": num_order_rows + 1,
                            "startColumnIndex": 8, "endColumnIndex": 9}],
                "booleanRule": {
                    "condition": {"type": "TEXT_CONTAINS",
                                  "values": [{"userEnteredValue": status_text}]},
                    "format": {"backgroundColor": rgb(hex_color)},
                },
            },
            "index": 0,
        }})

    # Summary: flag stale non-zero cells with light red
    # PFD older than 3 days (skip rows 1-3, start at index 4), col E = index 4
    # In Transit older than 10 days (skip rows 1-10, start at index 11), col F = index 5
    # Undelivered older than 10 days, col I = index 8
    STALE_CF = [
        (4,  49, "#F4CCCC"),   # PFD col (index 4),        rows after first 3 days  — light red
        (5,  11, "#FFF2CC"),   # In Transit col (index 5),  rows after first 10 days — light yellow
        (8,  11, "#E6D0F0"),   # Undelivered col (index 8), rows after first 10 days — light purple
    ]
    for col_idx, start_row, hex_color in STALE_CF:
        reqs.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sid,
                            "startRowIndex": start_row, "endRowIndex": 49,
                            "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_GREATER",
                                  "values": [{"userEnteredValue": "0"}]},
                    "format": {"backgroundColor": rgb(hex_color)},
                },
            },
            "index": 0,
        }})

    sh.batch_update({"requests": reqs})
    print("Formatting applied.")

def write_to_sheets(order_rows, summary_rows):
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    print("Writing Orders tab...")
    orders_ws = get_or_create_sheet(sh, "Orders")
    orders_ws.clear()
    orders_ws.update(order_rows, "A1")
    print(f"  {len(order_rows)-1:,} rows written.")

    print("Writing Summary tab...")
    summary_ws = get_or_create_sheet(sh, "Summary")
    summary_ws.clear()
    summary_ws.update(summary_rows, "A1")
    print(f"  {len(summary_rows)-1} rows written.")

    print("Applying formatting...")
    beautify(sh, orders_ws, summary_ws, len(order_rows) - 1)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Syncing orders from {START_DATE} → Google Sheets...")
    order_map = fetch_shopify_orders()
    cp_status = fetch_clickpost_statuses()
    order_rows   = build_orders(order_map, cp_status)
    summary_rows = build_summary(order_map, cp_status)
    write_to_sheets(order_rows, summary_rows)
    print("Done!")
