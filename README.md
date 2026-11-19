# NMC Pharmacy — Health Records

The pharmacy system for a workplace clinic (document LD-HSE/NMC/HRD/6.1.3.3). It replaces the paper registers and spreadsheets for dispensing, stock and the clinic's other daily records.

Built with Flask and MongoDB Atlas, deployed on Vercel.

**Live:** https://health-records-chi.vercel.app (requires a login)

> There is also a copy that works with the NMC Stock Take phone app: [HealthRecords-with-Android](https://github.com/hlaolil/HealthRecords-with-Android).

## What it does

| Page | For |
| --- | --- |
| **Dashboard** | Stock levels, items to reorder, short-dated and expired stock |
| **Dispense** | Record what was given to each patient, with diagnosis lookup and the patient's history |
| **Receive** | Book in deliveries by hand or from an uploaded invoice; edit or delete a receipt |
| **Stock take** | Count sheets, variances and posting the counts |
| **Expiry** | Correct expiry dates and remove expired stock (one item or in bulk) |
| **Internal issue / Requests** | Stock issued to departments, and department requests (including standard lists) |
| **Loans** | Equipment loans and returns |
| **Key register** | Store key sign-out |
| **Temperature** | Fridge temperature log |
| **Reports** | Usage, demand, costing and period reports, with CSV export |
| **Audit** | Who changed what, and when |
| **Accounts** | User logins with roles, password expiry and CSRF protection |

## Run it locally

Requires Python 3 and a MongoDB database.

```bash
pip install -r requirements.txt
export SECRET_KEY="a-long-random-string"
export MONGODB_URI="mongodb+srv://..."
export ADMIN_PASSWORD="password-for-the-register-page"
python app.py            # or: gunicorn app:app
```

## Environment variables

| Variable | What |
| --- | --- |
| `SECRET_KEY` | A long random string (required) |
| `MONGODB_URI` | The MongoDB Atlas connection string |
| `ADMIN_PASSWORD` | Opens the Register page, for adding users |
| `PHARMACY_UTC_OFFSET` | Optional. Hours from UTC for the pharmacy's clock (default 2, Lesotho) |
| `COOKIE_SECURE` | Optional. Set it to send cookies over HTTPS only |

## Tests

```bash
python3 tests/run_all.py            # every suite
python3 tests/run_all.py stocktake  # only suites whose name contains "stocktake"
```

The tests use a fake database (mongomock) and never touch the live one. `tests/` is not deployed (see `.vercelignore`).

## Related

- [healing-hub-stock](https://github.com/hlaolil/healing-hub-stock): adapted from this app for a retail pharmacy, with a point of sale
- [NMC_StockTake_App](https://github.com/hlaolil/NMC_StockTake_App): the Android stock-take companion
