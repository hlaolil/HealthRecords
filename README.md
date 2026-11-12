# The Healing Hub Pharmacy — Stock & Point of Sale

A Flask + MongoDB app that runs the pharmacy's stock and till: selling at the counter, receiving deliveries, stock takes, expiry, the controlled drug register, fridge temperatures, reports, and the product list shown on the Healing Hub website.

Adapted from the HealthRecords clinic pharmacy app. The clinic-only parts (department requests, equipment loans, store key register, diagnoses and sick leave) were removed. Point of sale, selling prices and the public product feed were added.

## What it does

| Page | For |
| --- | --- |
| **Point of Sale** (`/sell`) | Ring up a basket, take payment (Cash, M-Pesa, EcoCash, Card, Medical aid), print a receipt. Stock drops automatically. |
| **Sales** (`/sales`) | Today's cash-up by payment method, gross profit (admins), every receipt, CSV download. Admins can void a sale, which returns its stock. |
| **Receiving** | Deliveries against supplier orders and invoices; sets each item's cost. |
| **Write-offs** | Damaged, spoiled or lost stock (expired stock is cleared from Reports). |
| **Stock Take** | Count the shelves and correct the balances, with variances recorded. |
| **Temperature** | Morning and afternoon max/min readings for the dispensary, stores and fridge. |
| **Reports** | Stock on hand, expiry, losses, unmet demand, inventory with AMC, order request, receive list, controlled drug register, period report. |
| **Dashboard** | Monthly movement, stock value, fill rate, expiring stock. |
| **Add / Edit Medication** | Cost per pack, **customer price**, prescription-only flag, and whether and how it appears on the website. |

### How a sale works

- Each line is written as an ordinary stock movement, so every existing report and the controlled drug register keep working. The receipt (payment, totals, receipt number `HH-YYYYMMDD-0001`) is kept in the `sales` collection.
- Cost and selling price are stored separately on every line, so gross profit is exact and a later price change never rewrites a past sale.
- A sale is all-or-nothing. If an item is short, unknown or has no price, nothing is sold and the cashier is told why. A shortfall is still recorded as unmet demand, which feeds the order request.
- Prescription-only items need the patient's name and prescription number. Controlled items also need the prescriber, for the register.

### The website product feed

`GET /api/public/products` is the only page that needs no login. It lists items an admin has ticked **Show on website**, with:

- name, category, short description
- price for over-the-counter items, and **no price for prescription-only items**
- `in_stock: true/false`, never the quantity
- no costs

The Healing Hub website reads it (set `STOCK_API_URL` there) and caches it for a few minutes.

## Setup

Environment variables:

| Variable | Purpose |
| --- | --- |
| `MONGODB_URI` | MongoDB Atlas connection string |
| `SECRET_KEY` | Long random string; the app refuses to start without it |
| `ADMIN_PASSWORD` | Needed to open the Register page and create accounts |
| `WEBSITE_ORIGIN` | Optional: the website's address, e.g. `https://healinghubpharmacy.vercel.app`, to allow only it to read the feed (defaults to any site) |
| `PHARMACY_UTC_OFFSET` | Optional, default `2` (Lesotho) |

On Vercel, import the repository; `app.py` exposes the Flask `app`. Locally:

```bash
pip install -r requirements.txt
SECRET_KEY=dev MONGODB_URI=mongodb://localhost:27017/ python app.py
```

### First steps

1. Open `/register`, enter `ADMIN_PASSWORD`, and create an **Admin** account, then accounts for staff (**Employee**).
2. Under **Add Medication**, set up each product: cost per pack, customer price, prescription-only, and website listing. Use an opening balance for stock already on the shelf.
3. Sell from **Point of Sale**; cash up from **Sales** at the end of the day.

## Tests

```bash
pip install mongomock
python tests/run_all.py
```

24 suites, 1,764 checks, run against an in-memory database. `tests/test_pos.py` covers the till, receipts, voids, cash-up and the public feed.
