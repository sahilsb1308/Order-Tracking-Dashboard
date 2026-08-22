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
EASYECOM_API_KEY      = (os.getenv("EASYECOM_API_KEY")      or _require("EASYECOM_API_KEY")).strip()
EASYECOM_EMAIL        = (os.getenv("EASYECOM_EMAIL")        or _require("EASYECOM_EMAIL")).strip()
EASYECOM_PASSWORD     = (os.getenv("EASYECOM_PASSWORD")     or _require("EASYECOM_PASSWORD")).strip()
EASYECOM_LOCATION_KEY = (os.getenv("EASYECOM_LOCATION_KEY") or _require("EASYECOM_LOCATION_KEY")).strip()

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
START_DATE    = "2026-07-01"   # fixed start — Shopify fetch (all orders stay visible)
CP_FETCH_DAYS = 30             # Clickpost status refresh window (rolling, keeps run time bounded)
CUTOFF_DATE   = (datetime.now(IST).date() - timedelta(days=CP_FETCH_DAYS)).isoformat()
FETCH_DAYS    = (datetime.now(IST).date() - datetime.strptime(START_DATE, "%Y-%m-%d").date()).days + 1

# ── Status mappings ───────────────────────────────────────────────────────────
STATUS_LABELS = ["Cancelled", "Confirmed", "PFD", "In Transit",
                 "Out for delivery", "Delivered", "Undelivered", "Lost", "RTO", "NA"]

# EasyEcom order_status field → our category
EE_ORDER_STATUS_MAP = {
    "confirmed":         "Confirmed",
    "cancelled":         "Cancelled",
    "assigned":          "PFD",
    "manifest scanned":  "PFD",
    "pending":           "PFD",
    "printed":           "PFD",
    "ready to dispatch": "PFD",
}

# EasyEcom shipping_status field → our category (separate field)
EE_SHIPPING_STATUS_MAP = {
    "shipment created": "PFD",
    "handover":         "PFD",
    "shipment error":   "PFD",
}

# Categories that Clickpost is authoritative for (post-dispatch)
POST_DISPATCH_CATS = {"In Transit", "Out for delivery", "Delivered", "Undelivered", "RTO", "Lost"}

STATUS_MAP = {
    # PFD = pre-dispatch states: OrderPlaced(1), AWBRegistered/PickupPending(2),
    #       PickupRescheduled(14), OutForPickup(16)
    "PFD":              {1, 2, 14, 16},
    "In Transit":       {3, 4, 5, 17, 18, 19, 20, 25, 28, 1004, 1005, 1006},
    "Out for delivery": {6, 44},
    "Delivered":        {8, 48},
    "Undelivered":      {9, 43},   # 9=FailedDelivery, 43=ShipmentHeld
    "Lost":             {16},
    "RTO":              {11, 12, 13, 14, 15, 21, 26, 27, 45, 46, 47, 50, 52},
}

SUMMARY_COLS = (["Date", "Grand Total"] + STATUS_LABELS +
                [""] + [s + " %" for s in STATUS_LABELS if s != "Confirmed"])

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
        "created_at_min": f"{CUTOFF_DATE}T00:00:00+05:30",
        "status": "any",
        "limit":  250,
        "order":  "created_at asc",   # oldest first — July data fetched before August
        "fields": "id,order_number,created_at,fulfillments,cancelled_at",
    }

    order_map = {}  # order_number -> {order_date, shopify_id, awb}
    page = 1
    while True:
        # retry up to 3 times on transient errors
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=60)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 10))
                    print(f"  [rate limit] sleeping {retry_after}s", flush=True)
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                break
            except Exception as e:
                print(f"  [retry {attempt+1}] {e}", flush=True)
                time.sleep(5)
        else:
            print("  Shopify fetch failed after 3 retries — stopping pagination.", flush=True)
            break

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

            awb = ""
            for f in order.get("fulfillments", []):
                tn = (f.get("tracking_number") or "").strip()
                if tn:
                    awb = tn
                    break

            order_map[order_number] = {
                "order_date":      order_date,
                "awb":             awb,
                "cancelled_at":    order.get("cancelled_at") or "",
                "has_fulfillment": bool(order.get("fulfillments")),
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
        time.sleep(0.5)

    print(f"Shopify fetch complete — {len(order_map):,} orders from {page-1} pages.", flush=True)
    return order_map

# ── Step 2: Fetch Clickpost statuses ─────────────────────────────────────────
def fetch_clickpost_statuses():
    """Returns (by_order_id, by_awb): both dicts map to latest Clickpost record"""
    now = datetime.now(IST)
    waybill_latest = {}
    awb_latest = {}

    for day_offset in range(CP_FETCH_DAYS):
        day       = (now - timedelta(days=day_offset)).date()
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=IST)
        print(f"\nClickpost {day} ", end="", flush=True)

        for window in range(48):
            win_start = day_start + timedelta(minutes=30 * window)
            if win_start >= now:
                break
            win_end = win_start + timedelta(minutes=30)
            next_url = None
            page = 0

            while True:
                # retry up to 3 times with backoff
                for attempt in range(3):
                    try:
                        if next_url:
                            r = requests.get(next_url, headers={"accept": "application/json"}, timeout=30)
                        else:
                            r = requests.get(
                                "https://api.clickpost.in/api/v1/updated-order",
                                params={"key": CLICKPOST_KEY, "username": CLICKPOST_USERNAME,
                                        "start_date": int(win_start.timestamp()),
                                        "end_date":   int(win_end.timestamp())},
                                headers={"accept": "application/json"},
                                timeout=30,
                            )
                        if r.status_code == 429:
                            wait = int(r.headers.get("Retry-After", 10))
                            print(f"[RL:{wait}s]", end="", flush=True)
                            time.sleep(wait)
                            continue
                        r.raise_for_status()
                        break
                    except Exception as e:
                        print(f"[ERR:{e}]", end="", flush=True)
                        time.sleep(5 * (attempt + 1))
                else:
                    break  # skip this window after 3 failures

                data = r.json()
                meta = data.get("meta", {})
                if not meta.get("success"):
                    break
                records = data.get("result", [])
                page += 1
                if page == 1:
                    print(".", end="", flush=True)
                else:
                    print(f"p{page}", end="", flush=True)

                for rec in records:
                    oid        = str(rec.get("order_id") or "").strip()
                    awb        = str(rec.get("waybill") or "").strip()
                    code       = rec.get("clickpost_status_code")
                    updated_at = rec.get("updated_at") or ""
                    if not oid or code is None:
                        continue
                    existing = waybill_latest.get(oid)
                    if existing is None or updated_at > existing.get("updated_at", ""):
                        waybill_latest[oid] = rec
                    if awb:
                        existing_awb = awb_latest.get(awb)
                        if existing_awb is None or updated_at > existing_awb.get("updated_at", ""):
                            awb_latest[awb] = rec

                next_url = meta.get("next") or None
                if not next_url or not records:
                    break
                time.sleep(0.2)  # avoid rate limiting between pages

            time.sleep(0.1)  # avoid rate limiting between windows

    print(f"\nClickpost fetch complete — {len(waybill_latest):,} by order_id, {len(awb_latest):,} by AWB.", flush=True)
    return waybill_latest, awb_latest

# ── Step 3: Fetch EasyEcom pre-shipment statuses ─────────────────────────────
def fetch_easyecom_statuses():
    """Returns dict: order_number (str) → category (Confirmed/Cancelled/PFD).
    Only covers pre-shipment statuses; post-dispatch comes from Clickpost.
    """
    print("Fetching EasyEcom token...", flush=True)
    try:
        resp = requests.post(
            "https://api.easyecom.io/access/token",
            headers={"x-api-key": EASYECOM_API_KEY, "Content-Type": "application/json"},
            json={"email": EASYECOM_EMAIL, "password": EASYECOM_PASSWORD,
                  "location_key": EASYECOM_LOCATION_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("data", {}).get("jwt_token") or resp.json().get("token") or ""
        if not token:
            print(f"  EasyEcom auth failed: {resp.text[:200]}", flush=True)
            return {}
    except Exception as e:
        print(f"  EasyEcom auth error: {e}", flush=True)
        return {}

    print("  Token obtained. Fetching EasyEcom orders...", flush=True)
    headers = {"Authorization": f"Bearer {token}", "x-api-key": EASYECOM_API_KEY}

    cutoff = datetime.now(IST).date() - timedelta(days=CP_FETCH_DAYS)
    from_date = cutoff.strftime("%Y-%m-%d")
    to_date   = datetime.now(IST).date().strftime("%Y-%m-%d")

    ee_map = {}  # order_number → category

    def _fetch_ee_pages(extra_params, label):
        page = 1
        seen = 0
        while True:
            for attempt in range(3):
                try:
                    r = requests.get(
                        "https://api.easyecom.io/orders",
                        headers=headers,
                        params={"from_date": from_date, "to_date": to_date,
                                "page_no": page, "per_page": 250, **extra_params},
                        timeout=60,
                    )
                    if r.status_code == 429:
                        time.sleep(int(r.headers.get("Retry-After", 10)))
                        continue
                    r.raise_for_status()
                    break
                except Exception as e:
                    print(f"  [EasyEcom {label} retry {attempt+1}] {e}", flush=True)
                    time.sleep(5)
            else:
                print(f"  EasyEcom {label} fetch failed after retries — stopping.", flush=True)
                break

            body   = r.json()
            orders = (body.get("data") or {}).get("orders") or body.get("orders") or []
            if not orders:
                break

            for o in orders:
                ref             = str(o.get("reference_code") or o.get("channel_order_id") or "").strip()
                order_status    = str(o.get("order_status") or o.get("status") or "").strip().lower()
                shipping_status = str(o.get("shipping_status") or o.get("shipment_status") or "").strip().lower()
                if not ref:
                    continue
                cat = EE_ORDER_STATUS_MAP.get(order_status) or EE_SHIPPING_STATUS_MAP.get(shipping_status)
                if cat:
                    ee_map[ref] = cat
                    seen += 1

            print(f"  EasyEcom {label} page {page}: {len(orders)} orders", flush=True)
            total = (body.get("data") or {}).get("total_records") or 0
            if not orders or (total and page * 250 >= int(total)):
                break
            page += 1
            time.sleep(0.3)
        return seen

    # Fetch all active orders (confirmed, PFD statuses)
    n1 = _fetch_ee_pages({}, "active")
    # Fetch cancelled orders separately — EasyEcom default endpoint excludes them
    # Try both capitalizations since EasyEcom may be case-sensitive
    n2 = _fetch_ee_pages({"order_status": "Cancelled"}, "cancelled")

    print(f"EasyEcom fetch complete — {n1} active + {n2} cancelled = {len(ee_map):,} total.", flush=True)
    return ee_map

# ── Step 4: Join & build rows ─────────────────────────────────────────────────
def _cp_rec(order_number, shopify, cp_status, awb_status):
    """Look up Clickpost record: try order_id first, fall back to AWB."""
    rec = cp_status.get(order_number)
    if rec is None:
        awb = shopify.get("awb") or ""
        rec = awb_status.get(awb) if awb else None
    return rec or {}

def _resolve_category(order_number, shopify, cp_status, awb_status, ee_statuses):
    """Priority: Clickpost post-dispatch > EasyEcom pre-dispatch > NA.
    Pre-dispatch (Confirmed/Cancelled/PFD) is always from EasyEcom.
    Post-dispatch (In Transit/OFD/Delivered/Undelivered/RTO/Lost) always from Clickpost.
    """
    rec     = _cp_rec(order_number, shopify, cp_status, awb_status)
    cp_code = rec.get("clickpost_status_code")
    cp_cat  = get_category(cp_code) if cp_code is not None else None
    ee_cat  = ee_statuses.get(order_number)

    if cp_cat in POST_DISPATCH_CATS:
        return cp_cat, rec          # Clickpost wins for post-dispatch
    if ee_cat is not None:
        return ee_cat, rec          # EasyEcom handles all pre-dispatch
    return "NA", rec                # nothing found in either source

def build_orders(order_map, cp_status, awb_status, ee_statuses):
    """One row per Shopify order, joined with Clickpost + EasyEcom status."""
    rows = [ORDER_COLS]
    for order_number, shopify in sorted(
        order_map.items(),
        key=lambda x: x[1].get("order_date", ""),
        reverse=True,
    ):
        cat, rec = _resolve_category(order_number, shopify, cp_status, awb_status, ee_statuses)
        code = rec.get("clickpost_status_code", "")
        rows.append([
            order_number,
            shopify.get("awb") or rec.get("waybill") or "",
            rec.get("channel_name") or "Swiss Beauty",
            shopify.get("order_date") or "",
            to_ist_str(rec.get("updated_at") or ""),
            rec.get("timestamp") or "",
            code,
            cat if cat in ("Cancelled", "PFD") else (rec.get("clickpost_status_description") or cat),
            rec.get("location") or "",
            rec.get("clickpost_city") or "",
            rec.get("courier_partner") or "",
            rec.get("remark") or "",
        ])
    return rows

def build_summary(order_map, cp_status, awb_status, ee_statuses):
    """Group by Shopify order_date.
    Confirmed/Cancelled/PFD → EasyEcom (with Clickpost post-dispatch taking precedence).
    In Transit / OFD / Delivered / Undelivered / RTO / Lost → Clickpost.
    """
    daily_counts = defaultdict(lambda: defaultdict(int))

    for order_number, shopify in order_map.items():
        order_date = shopify.get("order_date", "")
        if not order_date or order_date < CUTOFF_DATE:
            continue

        cat, _ = _resolve_category(order_number, shopify, cp_status, awb_status, ee_statuses)
        daily_counts[order_date][cat] += 1

    rows = [SUMMARY_COLS]
    grand = defaultdict(int)  # running totals across all dates

    for date_str in sorted(daily_counts.keys(), reverse=True):
        c          = daily_counts[date_str]
        total      = sum(c.values()) or 1
        conf_denom = (total - c["Cancelled"]) or 1  # non-cancelled denominator for %

        def pct_of_total(v):
            return round(v / total, 4)
        def pct_of_conf(v):
            return round(v / conf_denom, 4)

        d = datetime.strptime(date_str, "%Y-%m-%d")
        rows.append([
            fmt_date(d), total,
            c["Cancelled"], c["Confirmed"], c["PFD"],
            c["In Transit"], c["Out for delivery"],
            c["Delivered"], c["Undelivered"], c["Lost"], c["RTO"], c["NA"],
            "",
            pct_of_total(c["Cancelled"]), pct_of_conf(c["PFD"]),
            pct_of_conf(c["In Transit"]), pct_of_conf(c["Out for delivery"]),
            pct_of_conf(c["Delivered"]), pct_of_conf(c["Undelivered"]),
            pct_of_conf(c["Lost"]), pct_of_conf(c["RTO"]),
            pct_of_total(c["NA"]),
        ])

        # accumulate totals
        grand["total"]            += total
        grand["Cancelled"]        += c["Cancelled"]
        grand["Confirmed"]        += c["Confirmed"]
        grand["PFD"]              += c["PFD"]
        grand["In Transit"]       += c["In Transit"]
        grand["Out for delivery"] += c["Out for delivery"]
        grand["Delivered"]        += c["Delivered"]
        grand["Undelivered"]      += c["Undelivered"]
        grand["Lost"]             += c["Lost"]
        grand["RTO"]              += c["RTO"]
        grand["NA"]               += c["NA"]

    # Totals row at the bottom
    gt           = grand["total"] or 1
    g_conf_denom = (gt - grand["Cancelled"]) or 1

    rows.append([
        "TOTAL", gt,
        grand["Cancelled"], grand["Confirmed"], grand["PFD"],
        grand["In Transit"], grand["Out for delivery"],
        grand["Delivered"], grand["Undelivered"], grand["Lost"], grand["RTO"], grand["NA"],
        "",
        round(grand["Cancelled"]        / gt,           4),
        round(grand["PFD"]              / g_conf_denom, 4),
        round(grand["In Transit"]       / g_conf_denom, 4),
        round(grand["Out for delivery"] / g_conf_denom, 4),
        round(grand["Delivered"]        / g_conf_denom, 4),
        round(grand["Undelivered"]      / g_conf_denom, 4),
        round(grand["Lost"]             / g_conf_denom, 4),
        round(grand["RTO"]              / g_conf_denom, 4),
        round(grand["NA"]               / gt,           4),
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
                   "#16A085", "#27AE60", "#D35400", "#555555", "#C0392B"]

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
        for col in [i + 2, i + 12]:
            if col >= len(SUMMARY_COLS):
                continue
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
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 60,
                  "startColumnIndex": 12, "endColumnIndex": 20},
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
        (sid, [100, 90, 90, 90, 90, 90, 125, 90, 100, 70, 80, 20,
               90, 80, 90, 130, 90, 100, 70, 80]),
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

    # ── Delete all existing CF rules before re-adding ──────────────────────────
    # fetch_sheet_metadata() only returns sheets.properties (no conditionalFormats),
    # so we must request that field explicitly via a raw API call.
    _api_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sh.id}"
    _cf_resp = sh.client.request(
        "get", _api_url,
        params={"fields": "sheets(properties.sheetId,conditionalFormats)"},
    )
    try:
        cf_meta = _cf_resp.json()
    except Exception:
        cf_meta = {}
    for sheet in cf_meta.get("sheets", []):
        sheet_id = sheet["properties"]["sheetId"]
        existing_cf = sheet.get("conditionalFormats", [])
        for i in range(len(existing_cf) - 1, -1, -1):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}})

    # Conditional formatting on Status col (Orders col I = index 8)
    STATUS_CF = [
        ("Delivered", "#D5F5E3"), ("InTransit", "#D6EAF8"),
        ("OutForDelivery", "#D1F2EB"), ("FailedDelivery", "#FDEBD0"),
        ("RTO", "#FADBD8"), ("Cancelled", "#EAECEE"),
        ("PickupPending", "#FEF9E7"), ("OrderPlaced", "#EBF5FB"),
        ("Lost", "#D5D8DC"),
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

    # Summary: flag stale non-zero cells
    # PFD older than 3 days  → col E (index 4), skip header + first 3 data rows → startRowIndex 4
    # In Transit older than 10 days → col F (index 5), startRowIndex 11
    # Undelivered older than 10 days → col I (index 8), startRowIndex 11
    # Custom formula used so that 0-value cells are never highlighted
    STALE_CF = [
        (4,  4,  "#F4CCCC", "E5"),    # PFD,        > 3 days stale — light red
        (5,  11, "#FFF2CC", "F12"),   # In Transit, > 10 days stale — light yellow
        (8,  11, "#E6D0F0", "I12"),   # Undelivered,> 10 days stale — light purple
    ]
    for col_idx, start_row, hex_color, cell_ref in STALE_CF:
        reqs.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sid,
                            "startRowIndex": start_row, "endRowIndex": 60,
                            "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1}],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=AND({cell_ref}<>"",{cell_ref}>0)'}],
                    },
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
    for attempt in range(5):
        try:
            sh = gc.open_by_key(SHEET_ID)
            break
        except Exception as e:
            if attempt == 4:
                raise
            wait = 10 * (attempt + 1)
            print(f"  Sheets API error ({e}), retrying in {wait}s...")
            time.sleep(wait)

    print("Writing Orders tab...")
    orders_ws = get_or_create_sheet(sh, "Orders")
    orders_ws.clear()
    orders_ws.update(order_rows, "A1", value_input_option="RAW")
    print(f"  {len(order_rows)-1:,} rows written.")

    print("Writing Summary tab...")
    summary_ws = get_or_create_sheet(sh, "Summary")
    summary_ws.clear()
    summary_ws.update(summary_rows, "A1", value_input_option="RAW")
    print(f"  {len(summary_rows)-1} rows written.")

    print("Applying formatting...")
    try:
        beautify(sh, orders_ws, summary_ws, len(order_rows) - 1)
    except Exception as e:
        print(f"  Warning: formatting failed ({e}) — data was written successfully.")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Syncing orders from {START_DATE} → Google Sheets...")
    order_map = fetch_shopify_orders()
    ee_statuses  = fetch_easyecom_statuses()
    cp_status, awb_status = fetch_clickpost_statuses()
    order_rows   = build_orders(order_map, cp_status, awb_status, ee_statuses)
    summary_rows = build_summary(order_map, cp_status, awb_status, ee_statuses)
    write_to_sheets(order_rows, summary_rows)
    print("Done!")
