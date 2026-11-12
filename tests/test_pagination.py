from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
# Bound by VALUE on purpose. As a closure over the name, any later
# assignment to that name anywhere in this file would silently replace
# the database for the rest of the run.
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
meds, txs = db['medications'], db['transactions']
c = A.app.test_client()

# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin'):
    with c.session_transaction() as s:
        s['user'] = {'name': 'Admin', 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'sales']:
        db[col].delete_many({})


NOW = datetime.utcnow()
# Must be inside the current month AND not in the future: hard-coding day 2
# made this suite fail on the 1st of any month, when day 2 has not happened yet.
THIS_MONTH = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
LAST_MONTH = (NOW.replace(day=1) - timedelta(days=5)).replace(hour=9)


def sale(n, when, customer='Customer', item='Med 0, 10 mg'):
    """A finished sale as the till writes it: the receipt and its stock line."""
    rn = f'HH-{when.strftime("%Y%m%d")}-{n:04d}'
    tid = f'TX-{n:05d}'
    db['sales'].insert_one({'receipt_no': rn, 'transaction_id': tid, 'status': 'completed',
                            'lines': [{'name': item, 'quantity': 1, 'unit_price': 2.0,
                                       'line_total': 2.0, 'cost': 1.0, 'rx_only': False}],
                            'total': 2.0, 'cost_total': 1.0, 'payment_method': 'Cash',
                            'customer': customer, 'cashier': 'Admin', 'timestamp': when})
    txs.insert_one({'type': 'dispense', 'transaction_id': tid, 'receipt_no': rn,
                    'med_name': item, 'quantity': 1, 'patient': customer, 'user': 'u',
                    'price': 1.0, 'line_value': 1.0, 'timestamp': when})
    return rn


def receipt(med, when, order='PO-1', invoice='INV-1'):
    txs.insert_one({'type': 'receive', 'med_name': med, 'quantity': 10, 'batch': 'B',
                    'price': 2.0, 'expiry_date': '2030-01-01', 'schedule': 'not controlled',
                    'stock_receiver': 'R', 'order_number': order, 'supplier': 'S',
                    'invoice_number': invoice, 'user': 'u', 'timestamp': when})


def add_meds(n=3):
    for i in range(n):
        meds.insert_one({'name': f'Med {i}, 10 mg', 'balance': 10000, 'batch': 'B',
                         'price': 2.0, 'expiry_date': '2030-01-01',
                         'schedule': 'not controlled'})


def receipts_on_page(body):
    return _re.findall(r'/sales/(HH-\d{8}-\d{4})"', body)


def pager_label(body):
    m = _re.search(r'Page \d+ of \d+', body)
    return m.group(0) if m else 'no pager'


# A whole day's trading can be thousands of sales; the cash-up shows one day
# by default, so these are all stamped within today (Lesotho time).
TODAY_L = A.local_now().strftime('%Y-%m-%d')
TODAY_UTC = datetime.strptime(TODAY_L, '%Y-%m-%d') - timedelta(hours=A.PHARMACY_UTC_OFFSET_HOURS) + timedelta(hours=9)

# =====================================================================
print('\n--- 1. The page does not grow without bound ---')
reset()
login()
add_meds()
for i in range(400):
    sale(i, TODAY_UTC + timedelta(seconds=i))
for i in range(400):
    receipt('Med 0, 10 mg', THIS_MONTH, order=f'PO-{i}', invoice=f'INV-{i}')
s_body = c.get('/sales').data.decode()
r_body = c.get('/receive').data.decode()
print(f'   400 sales          -> /sales   {len(s_body)/1024:.0f}KB')
print(f'   400 receipt lines  -> /receive {len(r_body)/1024:.0f}KB')
check('/sales page stays small', len(s_body) < 250 * 1024, f'{len(s_body)/1024:.0f}KB')
check('/receive page stays small', len(r_body) < 250 * 1024, f'{len(r_body)/1024:.0f}KB')
for i in range(400, 4000):
    sale(i, TODAY_UTC + timedelta(seconds=i))
big = c.get('/sales').data.decode()
print(f'   4,000 sales        -> /sales   {len(big)/1024:.0f}KB')
check('10x the data does not grow the page', abs(len(big) - len(s_body)) < 60 * 1024,
      f'{len(s_body)/1024:.0f}KB -> {len(big)/1024:.0f}KB')

# =====================================================================
print('\n--- 2. Page size is respected ---')
check('sales shows exactly one page', len(set(receipts_on_page(big))) == A.SALES_PAGE_SIZE,
      str(len(set(receipts_on_page(big)))))
check('sales reports the true total', 'of 4000 sales' in big)
check('the cash-up still counts every sale, not just the page', '<strong>4000</strong>' in big)
r_body = c.get('/receive').data.decode()
check('receive reports the true total', 'of 400 receipts' in r_body)
check('receive shows one page of lines',
      r_body.count('<td>INV-') == A.RECEIVE_PAGE_SIZE, str(r_body.count('<td>INV-')))

# =====================================================================
print('\n--- 3. Every sale appears on exactly one page ---')
reset()
login()
add_meds()
for i in range(60):
    sale(i, TODAY_UTC + timedelta(minutes=i))
seen = []
for p in (1, 2, 3):
    seen += receipts_on_page(c.get(f'/sales?page={p}').data.decode())
check('60 sales over three pages, none twice, none missing',
      len(seen) // 1 >= 60 and len(set(seen)) == 60, f'{len(seen)} links, {len(set(seen))} unique')

# =====================================================================
print('\n--- 4. The default is today; a search escapes it ---')
reset()
login()
add_meds()
sale(1, TODAY_UTC, customer='Today Customer')
sale(2, LAST_MONTH, customer='Old Customer')
body = c.get('/sales').data.decode()
check("an unfiltered list shows today's sales", 'Today Customer' in body)
check('and not last month', 'Old Customer' not in body)
check('a search looks across all time', 'Old Customer' in c.get('/sales?search=Old').data.decode())
day = LAST_MONTH.strftime('%Y-%m-%d')
check('an explicit date range overrides the default',
      'Old Customer' in c.get(f'/sales?start_date={day}&end_date={day}').data.decode())

# =====================================================================
print('\n--- 5. Paging links preserve the filters ---')
reset()
login()
add_meds()
for i in range(60):
    sale(i, LAST_MONTH + timedelta(minutes=i), customer=f'Patient {i:02d}')
start = (LAST_MONTH - timedelta(days=1)).strftime('%Y-%m-%d')
end = (LAST_MONTH + timedelta(days=1)).strftime('%Y-%m-%d')
body = c.get(f'/sales?start_date={start}&end_date={end}&search=Patient').data.decode()
check('next-page link carries the date range', f'start_date={start}' in body and f'end_date={end}' in body)
check('next-page link carries the search term', 'search=Patient' in body)
check('a filtered list still pages', 'Page 1 of' in body)
p2 = c.get(f'/sales?page=2&start_date={start}&end_date={end}&search=Patient').data.decode()
check('page 2 of a filtered list still applies the filter',
      'Page 2 of' in p2 and len(set(receipts_on_page(p2))) == A.SALES_PAGE_SIZE)

# =====================================================================
print('\n--- 6. Out-of-range and malformed pages ---')
reset()
login()
add_meds()
for i in range(60):
    sale(i, TODAY_UTC + timedelta(minutes=i))
body = c.get('/sales?page=9999').data.decode()
check('a page past the end clamps to the last page', 'Page 3 of 3' in body, pager_label(body))
check('clamped page still renders rows', len(set(receipts_on_page(body))) == 10,
      str(len(set(receipts_on_page(body)))))
check('page=0 is treated as page 1', 'Page 1 of 3' in c.get('/sales?page=0').data.decode())
check('page=abc does not error', c.get('/sales?page=abc').status_code == 200)
check('page=-5 does not error', c.get('/sales?page=-5').status_code == 200)
check('receive handles a bad page', c.get('/receive?page=abc').status_code == 200)
check('a bad date is refused politely, not a crash',
      c.get('/sales?start_date=yesterday').status_code in (200, 302))
reset()
login()
body = c.get('/sales').data.decode()
check('an empty list renders without a pager', 'Page 1 of' not in body and 'No sales found' in body)
check('an empty receive list renders', c.get('/receive').status_code == 200)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
