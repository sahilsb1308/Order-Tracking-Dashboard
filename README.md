# ORDER TRACKING DASHBOARD — HOW THIS WORKS

**Swiss Beauty | Updated: Aug 2026**

---

## WHAT IS THIS SHEET?

A live scoreboard for every order placed on Shopify since 1 July 2026.

**Main sheet:** https://docs.google.com/spreadsheets/d/1bXoh1KPzgpPw8ppMSYMP8rBOYqoT39ENI3qfpjJGuHc

Every morning at 8:00 AM IST it pulls fresh data from Shopify and Clickpost, joins them, and writes two tabs — one with every individual order and one with a daily summary of where all orders stand. A morning KPI snapshot is saved, and an evening email at 10:00 PM IST shows how numbers shifted during the day vs that morning snapshot.

---

## THE 3 QUESTIONS THIS SHEET ANSWERS

| Question | Where to look |
|---|---|
| How many orders are stuck and not dispatched? | Summary tab → PFD column |
| What is today's RTO rate? | Summary tab → RTO % column |
| Where is a specific order right now? | Orders tab → filter by Shopify Order # |

---

## THE TABS IN THIS FILE

| Tab Name | What it is |
|---|---|
| Orders | The main table. One row = one Shopify order. Shows AWB, status, courier, location, last scan. |
| Summary | One row = one date. Shows daily counts and percentages for every status bucket. |

---

## THE STATUS BUCKETS

| Status | Source | Meaning |
|---|---|---|
| Cancelled | Shopify | Order was cancelled before dispatch (no fulfillment created) |
| Confirmed | Calculated | Total minus Cancelled — orders that were dispatched or are in process |
| PFD (Pending for Dispatch) | Calculated | Confirmed orders with no matching Clickpost status — AWB registered, pickup pending, or not yet handed to courier |
| In Transit | Clickpost | Shipment picked up and moving between hubs (codes 3, 4, 5, 17–20, 25, 28) |
| Out for Delivery | Clickpost | Shipment is with the delivery agent today (codes 6, 44) |
| Delivered | Clickpost | Successfully delivered to customer (codes 8, 48) |
| Undelivered | Clickpost | Delivery attempted but failed, or shipment held (codes 9, 43) |
| RTO | Clickpost | Return to origin initiated, in transit, or delivered back (codes 11–15, 21, 26, 27, 45–47, 50, 52) |

---

## THE MATH

```
Grand Total  =  Cancelled  +  Confirmed
Confirmed    =  PFD  +  In Transit  +  Out for Delivery  +  Delivered  +  Undelivered  +  RTO
```

Every order falls into exactly one bucket. The TOTAL row at the bottom of the Summary tab sums all columns.

---

## CLICKPOST STATUS → DASHBOARD MAPPING

| Clickpost Status | Dashboard Bucket |
|---|---|
| PickedUp | In Transit |
| InTransit, OriginCityIn, OriginCityOut, DestinationHubIn, ShipmentDelayed | In Transit |
| Out for delivery | Out for Delivery |
| Delivered | Delivered |
| Failed delivery, ShipmentHeld | Undelivered |
| RTO-Marked, RTO-InTransit, RTO-OutForDelivery, RTO-Delivered, RTO-ContactCustomer, RTO-ShipmentDelayed, Lost | RTO |
| Order placed, Pickup pending, Out for pickup, AWB registered | PFD (fallthrough — no explicit code mapping) |

---

## STALE ORDER HIGHLIGHTS (Summary Tab)

| Column | Highlight | Rule |
|---|---|---|
| PFD | Light red | Non-zero cells older than 3 days |
| In Transit | Light yellow | Non-zero cells older than 10 days |
| Undelivered | Light purple | Non-zero cells older than 10 days |

---

## HOW THE AUTOMATION WORKS

| Step | Time | What happens |
|---|---|---|
| 1 | 8:00 AM IST | GitHub Actions triggers `sync_clickpost.py` |
| 2 | ~8:05 AM IST | Script fetches all Shopify orders from 1 Jul 2026 onwards |
| 3 | ~8:10 AM IST | Script scans Clickpost in 30-min windows for every day since 1 Jul 2026, with retry/backoff |
| 4 | ~8:20 AM IST | Both tabs are fully rewritten in Google Sheets |
| 5 | 8:30 AM IST | n8n sends the morning email report with 30-day KPI tiles and day-wise table |
| 6 | 8:30 AM IST | n8n saves a 30-day KPI snapshot to the snapshot sheet (used for evening shift comparison) |
| 7 | 9:30 PM IST | n8n triggers a second GitHub Actions sync to refresh data before the evening email |
| 8 | 10:00 PM IST | n8n sends the evening email with KPI shift vs morning snapshot and day-wise table |

---

## THE COLUMNS (Orders Tab)

| Column | Source | Meaning |
|---|---|---|
| Shopify Order # | Shopify | Order number (join key between Shopify and Clickpost) |
| AWB | Shopify / Clickpost | Airway bill / tracking number |
| Channel | Clickpost | Sales channel |
| Order Date (IST) | Shopify | When the order was placed |
| Last Updated (IST) | Clickpost | When Clickpost last received a status update |
| Last Scan Time | Clickpost | Timestamp of the last physical scan |
| Status Code | Clickpost | Raw numeric status code |
| Status | Shopify + Clickpost | Human-readable status (Cancelled from Shopify; rest from Clickpost) |
| Location | Clickpost | Last known hub/location |
| City | Clickpost | City of last scan |
| Courier Partner | Clickpost | Logistics partner handling the shipment |
| Remark | Clickpost | Latest scan remark |

---

## N8N WORKFLOWS

| File | Purpose |
|---|---|
| `n8n_order_summary_email.json` | Morning workflow — reads Summary sheet, sends KPI email, saves 30-day snapshot |
| `n8n_evening_delta_email.json` | Evening workflow — reads Summary + Snapshot sheets, sends shift vs morning email |
| `n8n_night_sync.json` | Night sync workflow — triggers GitHub Actions at 9:30 PM IST to refresh data before evening email |

The snapshot sheet (`1PPYtpNZiNyr99HNL707Mk1_O6okNAphigyv7nswbHXw`) stores one row per morning run with 30-day KPI totals and percentages. The evening workflow picks the most recent morning row for the current day to compute shift.

All % in emails and snapshots use **Confirmed as denominator** for dispatched statuses (PFD, In Transit, OFD, Delivered, Undelivered, RTO) and **Grand Total as denominator** for Cancelled and Confirmed tiles.

---

## CREDENTIALS REQUIRED (GitHub Secrets)

| Secret Name | What it is |
|---|---|
| `SHOPIFY_STORE` | Shopify store domain |
| `SHOPIFY_TOKEN` | Shopify Admin API access token |
| `CLICKPOST_KEY` | Clickpost API key |
| `CLICKPOST_USERNAME` | Clickpost username |
| `SHEET_ID` | Google Sheets spreadsheet ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account JSON for Sheets write access |

No credentials are stored in the code. All values come from environment variables at runtime.

---

## TO TRIGGER MANUALLY

Go to **GitHub → Actions → Daily Order Sync → Run workflow → Run workflow**

The sheet will be fully refreshed within ~20 minutes.
