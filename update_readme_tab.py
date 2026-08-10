import os
import tempfile
import gspread
from google.oauth2.service_account import Credentials

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

SHEET_ID = os.getenv("SHEET_ID") or input("Enter SHEET_ID: ")

CONTENT = [
    ["ORDER TRACKING DASHBOARD — HOW THIS WORKS"],
    ["Swiss Beauty | Updated: Aug 2026"],
    [],
    ["WHAT IS THIS SHEET?"],
    ["A live scoreboard for every order placed on Shopify since 1 July 2026."],
    ["Main sheet: https://docs.google.com/spreadsheets/d/1bXoh1KPzgpPw8ppMSYMP8rBOYqoT39ENI3qfpjJGuHc"],
    ["Every morning at 8:00 AM IST it pulls fresh data from Shopify and Clickpost, joins them, and writes two tabs — one with every individual order and one with a daily summary. A morning KPI snapshot is saved, and an evening email at 10:00 PM IST shows how numbers shifted during the day vs that morning snapshot."],
    [],
    ["THE 3 QUESTIONS THIS SHEET ANSWERS"],
    ["Question", "Where to look"],
    ["How many orders are stuck and not dispatched?", "Summary tab → PFD column"],
    ["What is today's RTO rate?", "Summary tab → RTO % column"],
    ["Where is a specific order right now?", "Orders tab → filter by Shopify Order #"],
    [],
    ["THE TABS IN THIS FILE"],
    ["Tab Name", "What it is"],
    ["README", "This guide."],
    ["Orders", "One row = one Shopify order. Shows AWB, status, courier, location, last scan."],
    ["Summary", "One row = one date. Shows daily counts and percentages for every status bucket."],
    [],
    ["THE STATUS BUCKETS"],
    ["Status", "Source", "Meaning"],
    ["Cancelled", "Shopify", "Order was cancelled before dispatch (no fulfillment created)"],
    ["Confirmed", "Calculated", "Total minus Cancelled — orders that were dispatched or are in process"],
    ["PFD (Pending for Dispatch)", "Calculated", "Confirmed orders with no matching Clickpost status — AWB registered, pickup pending, or not yet handed to courier"],
    ["In Transit", "Clickpost", "Shipment picked up and moving between hubs (codes 3, 4, 5, 17–20, 25, 28)"],
    ["Out for Delivery", "Clickpost", "Shipment is with the delivery agent today (codes 6, 44)"],
    ["Delivered", "Clickpost", "Successfully delivered to customer (codes 8, 48)"],
    ["Undelivered", "Clickpost", "Delivery attempted but failed, or shipment held (codes 9, 43)"],
    ["RTO", "Clickpost", "Return to origin initiated, in transit, or delivered back (codes 11–15, 21, 26, 27, 45–47, 50, 52)"],
    [],
    ["THE MATH"],
    ["Grand Total  =  Cancelled  +  Confirmed"],
    ["Confirmed    =  PFD  +  In Transit  +  Out for Delivery  +  Delivered  +  Undelivered  +  RTO"],
    ["Every order falls into exactly one bucket. The TOTAL row at the bottom of the Summary tab sums all columns."],
    [],
    ["CLICKPOST STATUS → DASHBOARD MAPPING"],
    ["Clickpost Status", "Dashboard Bucket"],
    ["PickedUp", "In Transit"],
    ["InTransit, OriginCityIn, OriginCityOut, DestinationHubIn, ShipmentDelayed", "In Transit"],
    ["Out for delivery", "Out for Delivery"],
    ["Delivered", "Delivered"],
    ["Failed delivery, ShipmentHeld", "Undelivered"],
    ["RTO-Marked, RTO-InTransit, RTO-OutForDelivery, RTO-Delivered, RTO-ContactCustomer, RTO-ShipmentDelayed, Lost", "RTO"],
    ["Order placed, Pickup pending, Out for pickup, AWB registered", "PFD (fallthrough — no explicit code mapping)"],
    [],
    ["STALE ORDER HIGHLIGHTS (Summary Tab)"],
    ["Column", "Highlight", "Rule"],
    ["PFD", "Light red", "Non-zero cells older than 3 days"],
    ["In Transit", "Light yellow", "Non-zero cells older than 10 days"],
    ["Undelivered", "Light purple", "Non-zero cells older than 10 days"],
    [],
    ["HOW THE AUTOMATION WORKS"],
    ["Step", "Time", "What happens"],
    ["1", "8:00 AM IST", "GitHub Actions triggers sync_clickpost.py"],
    ["2", "~8:05 AM IST", "Script fetches all Shopify orders from 1 Jul 2026 onwards"],
    ["3", "~8:10 AM IST", "Script scans Clickpost in 30-min windows for every day since 1 Jul 2026, with retry/backoff"],
    ["4", "~8:20 AM IST", "Both tabs are fully rewritten in Google Sheets"],
    ["5", "8:30 AM IST", "n8n sends the morning email report with 30-day KPI tiles and day-wise table"],
    ["6", "8:30 AM IST", "n8n saves a 30-day KPI snapshot to the snapshot sheet (used for evening shift comparison)"],
    ["7", "9:30 PM IST", "n8n triggers a second GitHub Actions sync to refresh data before the evening email"],
    ["8", "10:00 PM IST", "n8n sends the evening email with KPI shift vs morning snapshot and day-wise table"],
    [],
    ["N8N WORKFLOWS"],
    ["File", "Purpose"],
    ["n8n_order_summary_email.json", "Morning workflow — reads Summary sheet, sends KPI email, saves 30-day snapshot"],
    ["n8n_evening_delta_email.json", "Evening workflow — reads Summary + Snapshot sheets, sends shift vs morning email"],
    ["n8n_night_sync.json", "Night sync workflow — triggers GitHub Actions at 9:30 PM IST to refresh data before evening email"],
    [],
    ["Snapshot sheet: https://docs.google.com/spreadsheets/d/1PPYtpNZiNyr99HNL707Mk1_O6okNAphigyv7nswbHXw"],
    ["Stores one row per morning run with 30-day KPI totals and percentages. Evening workflow picks the most recent morning row for today to compute shift."],
    ["All % use Confirmed as denominator for dispatched statuses (PFD, In Transit, OFD, Delivered, Undelivered, RTO) and Grand Total for Cancelled and Confirmed tiles."],
    [],
    ["CREDENTIALS REQUIRED (GitHub Secrets)"],
    ["Secret Name", "What it is"],
    ["SHOPIFY_STORE", "Shopify store domain"],
    ["SHOPIFY_TOKEN", "Shopify Admin API access token"],
    ["CLICKPOST_KEY", "Clickpost API key"],
    ["CLICKPOST_USERNAME", "Clickpost username"],
    ["SHEET_ID", "Google Sheets spreadsheet ID"],
    ["GOOGLE_SERVICE_ACCOUNT_JSON", "Service account JSON for Sheets write access"],
    [],
    ["No credentials are stored in the code. All values come from environment variables at runtime."],
    [],
    ["TO TRIGGER MANUALLY"],
    ["Go to GitHub → Actions → Daily Order Sync → Run workflow → Run workflow"],
    ["The sheet will be fully refreshed within ~20 minutes."],
]

def rgb(hex_str):
    h = hex_str.lstrip("#")
    return {"red": int(h[0:2],16)/255, "green": int(h[2:4],16)/255, "blue": int(h[4:6],16)/255}

SECTION_HEADERS = {
    "ORDER TRACKING DASHBOARD — HOW THIS WORKS",
    "WHAT IS THIS SHEET?", "THE 3 QUESTIONS THIS SHEET ANSWERS",
    "THE TABS IN THIS FILE", "THE STATUS BUCKETS", "THE MATH",
    "CLICKPOST STATUS → DASHBOARD MAPPING",
    "STALE ORDER HIGHLIGHTS (Summary Tab)", "HOW THE AUTOMATION WORKS",
    "N8N WORKFLOWS", "CREDENTIALS REQUIRED (GitHub Secrets)", "TO TRIGGER MANUALLY",
}

def main():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    try:
        ws = sh.worksheet("README")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="README", rows=200, cols=4)

    ws.clear()
    ws.update(CONTENT, "A1")
    print(f"Written {len(CONTENT)} rows.")

    # ── Formatting ────────────────────────────────────────────────────────────
    DARK, WHITE, SUBHDR = "#1B2631", "#FFFFFF", "#2C3E50"
    reqs = []
    sid = ws.id

    for i, row in enumerate(CONTENT):
        if not row:
            continue
        text = row[0]
        row_idx = i  # 0-based

        if text == "ORDER TRACKING DASHBOARD — HOW THIS WORKS":
            # Big title
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row_idx, "endRowIndex": row_idx+1,
                          "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": rgb(DARK),
                    "textFormat": {"foregroundColor": rgb(WHITE), "bold": True, "fontSize": 14},
                    "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat",
            }})
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": row_idx, "endIndex": row_idx+1},
                "properties": {"pixelSize": 42}, "fields": "pixelSize",
            }})
        elif text in SECTION_HEADERS:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row_idx, "endRowIndex": row_idx+1,
                          "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": rgb(SUBHDR),
                    "textFormat": {"foregroundColor": rgb(WHITE), "bold": True, "fontSize": 10},
                    "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat",
            }})
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": row_idx, "endIndex": row_idx+1},
                "properties": {"pixelSize": 30}, "fields": "pixelSize",
            }})
        elif len(row) > 1 and i > 0 and CONTENT[i-1] and CONTENT[i-1][0] in SECTION_HEADERS:
            # Sub-header row (column headers of tables)
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row_idx, "endRowIndex": row_idx+1,
                          "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": rgb("#EAECEE"),
                    "textFormat": {"bold": True, "fontSize": 9},
                }},
                "fields": "userEnteredFormat",
            }})

    # Column widths
    for col, width in [(0, 380), (1, 280), (2, 340)]:
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": col, "endIndex": col+1},
            "properties": {"pixelSize": width}, "fields": "pixelSize",
        }})

    # Wrap text for all cells
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": len(CONTENT),
                  "startColumnIndex": 0, "endColumnIndex": 4},
        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
        "fields": "userEnteredFormat(wrapStrategy)",
    }})

    sh.batch_update({"requests": reqs})
    print("Formatting applied. README tab updated.")

if __name__ == "__main__":
    main()
