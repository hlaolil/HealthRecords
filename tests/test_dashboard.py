from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'

import mongomock
import pymongo
import re as _re
from datetime import datetime, timedelta, timezone

_fake = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _fake

import app as A
# Bound by VALUE on purpose. As a closure over the name, any later
# assignment to that name anywhere in this file would silently replace
# the database for the rest of the run.
A.get_mongo_client = lambda _client=_fake: _client
A.init_db_collections()

db = _fake['pharmacy_db']
meds = db['medications']
txs = db['transactions']
client = A.app.test_client()

# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf
arm_csrf(client)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='Admin User'):
    with client.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'x', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for c in ['medications', 'transactions', 'stock_takes',
              'stock_take_counts', 'audit_log']:
        db[c].delete_many({})


NOW = datetime.utcnow()


def month_start(back):
    """Start of the month `back` months before the current one."""
    y, m = NOW.year, NOW.month
    for _ in range(back):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return datetime(y, m, 15)   # mid-month, safely inside


def row_cells(body, name, section=None):
    """Cells AFTER the medication-name cell. `section` scopes to one panel,
    since the same medication appears in several tables on this page."""
    if section:
        start = body.index(section)
        nxt = body.find('<h2>', start + len(section))
        body = body[start:nxt if nxt != -1 else len(body)]
    mm = _re.search(r'<td>' + _re.escape(name) + r'</td>(.*?)</tr>', body, _re.S)
    if not mm:
        return []
    return [_re.sub(r'<[^>]+>', '', c).strip() for c in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)]

# Column layout of the consumption table, after the name cell:
#   0..N-1 monthly, then AMC, Trend, Pattern, Balance, MOS, Suggested Order
AMC, UNMET, FILL, TREND, PATTERN, BALANCE, MOS, ORDER = 6, 7, 8, 9, 10, 11, 12, 13


def add_med(name, balance, price=1.0, expiry='2030-01-01'):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'B1', 'price': price,
                     'expiry_date': expiry, 'schedule': 'not controlled'})


def dispense(name, qty, months_back):
    txs.insert_one({'type': 'dispense', 'med_name': name, 'quantity': qty,
                    'user': 'u', 'timestamp': month_start(months_back)})


# =====================================================================
print('\n--- 1. Page renders and is reachable by both roles ---')
reset()
add_med('Paracetamol, 500 mg', 100)
login('admin')
r = client.get('/dashboard')
check('admin gets 200', r.status_code == 200)
check('nav has Dashboard link', b'/dashboard' in r.data)
login('user', 'Pharm')
r = client.get('/dashboard')
check('non-admin also gets 200 (dashboard is not admin-only)', r.status_code == 200)

# =====================================================================
print('\n--- 2. Months appear side by side, oldest to newest ---')
reset()
login('admin')
add_med('Paracetamol, 500 mg', 300)
# 100, 200, 300 dispensed in the three completed months before this one
dispense('Paracetamol, 500 mg', 100, 3)
dispense('Paracetamol, 500 mg', 200, 2)
dispense('Paracetamol, 500 mg', 300, 1)
r = client.get('/dashboard?months=6&limit=0')
body = r.data.decode()
cells = row_cells(body, 'Paracetamol, 500 mg')
print('   row:', cells)
check('six month columns plus eight summary cells', len(cells) == 14, str(len(cells)))
monthly = cells[:6]
# window = [5,4,3,2,1,0] months back; the last column is the current month
check('months read oldest-to-newest, dispenses land in the right columns',
      monthly == ['0', '0', '100', '200', '300', '0'], str(monthly))
check('current month column is labelled MTD', 'MTD' in body)
check('peak month is bolded', '<strong>300</strong>' in body)

# =====================================================================
print('\n--- 3. AMC excludes the part-elapsed current month ---')
amc = cells[AMC]
# completed months in a 6-month window = 5 -> (0+0+0+100+200+300... ) careful:
# window is 6 months, last is current (MTD, zero). completed = 5 months:
# months_back 5,4,3,2,1 -> 0, 0, 100, 200, 300  => mean 120
# completed months in the window = 5 -> (0+0+100+200+300)/5 = 120
check('AMC = 120, i.e. averaged over completed months only', amc == '120.0', amc)
dispense('Paracetamol, 500 mg', 999, 0)   # big spike this month
body2 = client.get('/dashboard?months=6&limit=0').data.decode()
cells2 = row_cells(body2, 'Paracetamol, 500 mg')
check('current-month activity is shown in its column', cells2[5] == '999', cells2[5])
check('but does NOT change AMC', cells2[AMC] == '120.0', cells2[AMC])

# =====================================================================
print('\n--- 4. Months of stock and urgency colouring ---')
reset()
login('admin')
# consumption in every completed month so AMC is exactly 100
for name, bal in [('Low Item, 10 mg', 50), ('Amber Item, 10 mg', 150),
                  ('Mid Item, 10 mg', 250), ('Fine Item, 10 mg', 1500)]:
    add_med(name, bal)
    for mb in (1, 2, 3, 4, 5):
        dispense(name, 100, mb)
add_med('Zero Item, 10 mg', 0)
add_med('Dormant Item, 10 mg', 40)
body = client.get('/dashboard?months=6&limit=0&sort=name').data.decode()

for name, mos, cls in [('Low Item, 10 mg', '0.5', 'expired'),
                       ('Amber Item, 10 mg', '1.5', 'close-to-expire'),
                       ('Mid Item, 10 mg', '2.5', 'normal'),
                       ('Fine Item, 10 mg', '15.0', 'normal')]:
    c = row_cells(body, name)
    check(f'{name}: AMC is 100', c[AMC] == '100.0', c[AMC])
    check(f'{name}: MOS = {mos}', c[MOS] == mos, f'{c[MOS]} (row {c})')
    mm = _re.search(r'<tr class="([a-z-]+)">\s*<td>' + _re.escape(name), body)
    check(f'{name}: row colour is {cls}', mm and mm.group(1) == cls, mm.group(1) if mm else '?')

c = row_cells(body, 'Zero Item, 10 mg')
check('zero balance reads "Out of stock"', c[MOS] == 'Out of stock', c[MOS])
c = row_cells(body, 'Dormant Item, 10 mg')
check('stock with no consumption reads "No recent use"', c[MOS] == 'No recent use', c[MOS])
check('dormant item is not flagged urgent',
      _re.search(r'<tr class="normal">\s*<td>Dormant', body) is not None)
check('dormant item suggests no order', c[ORDER] == '0', c[ORDER])

print('\n--- 5. Trend and pattern ---')
reset()
login('admin')
add_med('Steady Item, 10 mg', 500)
for mb in (1, 2, 3, 4, 5):
    dispense('Steady Item, 10 mg', 100, mb)
add_med('Erratic Item, 10 mg', 500)
for mb, q in [(1, 300), (2, 5), (3, 250), (4, 0), (5, 10)]:
    if q:
        dispense('Erratic Item, 10 mg', q, mb)
add_med('Rising Item, 10 mg', 500)
for mb, q in [(1, 300), (2, 100), (3, 100), (4, 100), (5, 100)]:
    dispense('Rising Item, 10 mg', q, mb)
body = client.get('/dashboard?months=6&limit=0&sort=name').data.decode()
c = row_cells(body, 'Steady Item, 10 mg')
check('flat consumption -> trend "steady"', c[TREND] == 'steady', c[TREND])
check('flat consumption -> pattern "Steady"', c[PATTERN] == 'Steady', c[PATTERN])
c = row_cells(body, 'Erratic Item, 10 mg')
check('lumpy consumption -> pattern "Erratic"', c[PATTERN] == 'Erratic', c[PATTERN])
c = row_cells(body, 'Rising Item, 10 mg')
check('last month well above AMC -> trend shows "up"', c[TREND].startswith('up'), c[TREND])

# =====================================================================
print('\n--- 6. Suggested order = three months cover minus balance ---')
c = row_cells(body, 'Steady Item, 10 mg')
check('AMC 100 with 500 on hand needs no order', c[ORDER] == '0', c[ORDER])
add_med('Short Item, 10 mg', 50)
for mb in (1, 2, 3, 4, 5):
    dispense('Short Item, 10 mg', 100, mb)
body = client.get('/dashboard?months=6&limit=0&sort=name').data.decode()
c = row_cells(body, 'Short Item, 10 mg')
check('AMC 100 with 50 on hand suggests 250 (3x100 - 50)', c[ORDER] == '250', c[ORDER])

# =====================================================================
print('\n--- 7. Sorting and limiting ---')
reset()
login('admin')
for i, (name, bal, qty) in enumerate([('AAA Big, 10 mg', 9000, 500),
                                      ('BBB Small, 10 mg', 9000, 10),
                                      ('CCC Urgent, 10 mg', 5, 100)]):
    add_med(name, bal)
    for mb in (1, 2, 3):
        dispense(name, qty, mb)


def first_row(body):
    mm = _re.search(r'<tbody>\s*<tr class="[a-z-]+">\s*<td>(.*?)</td>',
                    body[body.index('Consumption by Month'):], _re.S)
    return mm.group(1).strip() if mm else None


check('sort=amc puts the biggest mover first',
      first_row(client.get('/dashboard?sort=amc&limit=0').data.decode()) == 'AAA Big, 10 mg')
check('sort=urgency puts the emptiest shelf first',
      first_row(client.get('/dashboard?sort=urgency&limit=0').data.decode()) == 'CCC Urgent, 10 mg')
check('sort=name puts them alphabetically',
      first_row(client.get('/dashboard?sort=name&limit=0').data.decode()) == 'AAA Big, 10 mg')

body = client.get('/dashboard?limit=0&search=Urgent').data.decode()
CONS = 'Consumption by Month'
check('search narrows the consumption table',
      bool(row_cells(body, 'CCC Urgent, 10 mg', section=CONS))
      and not row_cells(body, 'AAA Big, 10 mg', section=CONS))
check('search is reflected in the table heading', '(1 of 1 items)' in body)
check('search does not narrow the tiles', '>3</div>' in body)

body = client.get('/dashboard?months=3&limit=0').data.decode()
check('months=3 renders three month columns',
      len(row_cells(body, 'AAA Big, 10 mg')) == 11, str(len(row_cells(body, 'AAA Big, 10 mg'))))
body = client.get('/dashboard?months=99&limit=0').data.decode()
check('invalid months falls back to 6', len(row_cells(body, 'AAA Big, 10 mg')) == 14)
check('invalid sort/limit do not error',
      client.get('/dashboard?sort=xyz&limit=abc').status_code == 200)

# =====================================================================
print('\n--- 8. Tiles ---')
reset()
login('admin')
add_med('Out Item, 10 mg', 0)
add_med('Expired Item, 10 mg', 20, price=5.0, expiry='2020-01-01')
soon = (NOW + timedelta(days=45)).strftime('%Y-%m-%d')
add_med('Soon Item, 10 mg', 10, price=3.0, expiry=soon)
add_med('Fine Item, 10 mg', 100, price=2.0, expiry='2030-01-01')
body = client.get('/dashboard').data.decode()
check('tile: 4 items tracked', '>4</div>' in body)
check('tile: expired on shelf present', 'Expired on shelf' in body)
check('tile: expiring in 90 days shows value at risk', 'R30.00 at risk' in body)
# 0*? + 20*5 + 10*3 + 100*2 = 330
check('tile: stock value totals correctly', 'R330' in body)

# =====================================================================
print('\n--- 9. Expiring panel ---')
EXP = 'Expiring Within 90 Days'
# after the name cell: expiry, days left, balance, value, AMC, likely unused
c = row_cells(body, 'Soon Item, 10 mg', section=EXP)
check('expiring item listed with its expiry date', c and c[0] == soon, str(c))
check('days left is computed', c and c[1] == '45', str(c))
check('item with no consumption puts its whole balance at risk',
      c and c[5] == '10', str(c))
check('an item expiring later than 90 days is not listed',
      not row_cells(body, 'Fine Item, 10 mg', section=EXP))

add_med('Moving Item, 10 mg', 100, price=1.0, expiry=soon)
for mb in (1, 2, 3, 4, 5):
    dispense('Moving Item, 10 mg', 60, mb)
body = client.get('/dashboard').data.decode()
c = row_cells(body, 'Moving Item, 10 mg', section=EXP)
# 45 days left at AMC 60 -> ~90 units will move, more than the 100 on hand? no:
# projected use = 60 * 45/30 = 90, so 10 left unused
check('fast-moving item near expiry projects only the true surplus at risk',
      c and c[5] == '10', str(c))

print('\n--- 10. Stock take panels are admin-only ---')
reset()
login('admin')
add_med('Paracetamol, 500 mg', 100)
add_med('Aspirin, 100 mg', 50)
add_med('Uncounted Item, 10 mg', 77, price=4.0)
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({})['_id'])
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Paracetamol, 500 mg',
                                 'counted': '93'}, follow_redirects=True)
client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid,
                                 'med_name': 'Aspirin, 100 mg',
                                 'counted': '50'}, follow_redirects=True)
admin_body = client.get('/dashboard').data.decode()
check('admin sees the discrepancy-by-month panel',
      'Stock Take Discrepancies by Month' in admin_body)
check('admin sees stock take adjustments in the activity table',
      'Items adjusted at stock take' in admin_body)
check('admin sees largest discrepancies', 'Largest Discrepancies' in admin_body)
check('largest discrepancies names the item and net variance',
      _re.search(r'<td>Paracetamol, 500 mg</td>\s*<td>-7</td>', admin_body) is not None)
check('accuracy computed (1 of 2 agreed = 50%)', '50%' in admin_body)
check('never-counted panel lists the uncounted item',
      'Never Counted' in admin_body and 'Uncounted Item, 10 mg' in admin_body)
check('never-counted excludes items that were counted',
      admin_body.count('Aspirin, 100 mg') >= 1
      and not _re.search(r'Never Counted.*?Aspirin, 100 mg', admin_body, _re.S))

login('user', 'Pharm')
user_body = client.get('/dashboard').data.decode()
check('non-admin does NOT see the discrepancy panel',
      'Stock Take Discrepancies by Month' not in user_body)
check('non-admin does NOT see the adjustments rows',
      'Items adjusted at stock take' not in user_body
      and 'Value of adjustments' not in user_body)
check('non-admin does NOT see largest discrepancies',
      'Largest Discrepancies' not in user_body)
check('non-admin does NOT see the never-counted panel',
      'Never Counted' not in user_body)
check('non-admin dashboard mentions nothing about stock take at all',
      'Stock Take' not in user_body and 'stock take' not in user_body)
check('non-admin still sees consumption and AMC',
      'Consumption by Month' in user_body and '<th>AMC</th>' in user_body)

# =====================================================================
print('\n--- 11. Query efficiency ---')
_of, _oa = mongomock.collection.Collection.find, mongomock.collection.Collection.aggregate
calls = {}


def load_with(n):
    """Query count for a dashboard load with n medications."""
    reset()
    for i in range(n):
        name = f'Item {i:03d}, 10 mg'
        add_med(name, 100 + i)
        for mb in (1, 2, 3):
            dispense(name, 10 + i, mb)
    calls.clear()

    def _cf(self, *a, **k):
        if self.name in ('transactions', 'medications', 'stock_take_counts'):
            calls['find'] = calls.get('find', 0) + 1
        return _of(self, *a, **k)

    def _ca(self, *a, **k):
        calls['aggregate'] = calls.get('aggregate', 0) + 1
        return _oa(self, *a, **k)

    mongomock.collection.Collection.find, mongomock.collection.Collection.aggregate = _cf, _ca
    try:
        r = client.get('/dashboard?limit=0')
    finally:
        mongomock.collection.Collection.find, mongomock.collection.Collection.aggregate = _of, _oa
    return sum(calls.values()), r


login('admin')
small, _ = load_with(10)
large, r = load_with(60)
print(f'   10 medications -> {small} queries;  60 medications -> {large} queries')
check('query count does not grow with the formulary', small == large, f'{small} vs {large}')
# flatness is the property that matters; the absolute count grows only when a
# new panel is added, never with the size of the formulary
check('query count stays bounded', large <= 12, str(large))
check('page still renders all 60', r.data.decode().count(', 10 mg</td>') >= 60)

print('\n--- 12. Activity counts visits, items and patients ---')
reset()
login('admin')
for n in ('Paracetamol, 500 mg', 'Aspirin, 100 mg', 'Amoxycillin Cap, 250 mg'):
    add_med(n, 1000, price=2.0)


def visit(patient, items, months_back):
    """One encounter: several medication lines sharing a transaction_id."""
    tid = f'TX-{patient}-{months_back}-{len(items)}-{abs(hash(str(items))) % 9999}'
    for med, qty in items:
        txs.insert_one({'type': 'dispense', 'transaction_id': tid, 'patient': patient,
                        'med_name': med, 'quantity': qty, 'user': 'u',
                        'timestamp': month_start(months_back)})


# 3 visits, 6 items, 2 distinct patients — all in the same month
visit('Thabo M', [('Paracetamol, 500 mg', 20), ('Aspirin, 100 mg', 10)], 1)
visit('Thabo M', [('Amoxycillin Cap, 250 mg', 15)], 1)
visit('Palesa N', [('Paracetamol, 500 mg', 30), ('Aspirin, 100 mg', 5),
                   ('Amoxycillin Cap, 250 mg', 12)], 1)
# two deliveries, three receipt lines
for med, qty, order in [('Paracetamol, 500 mg', 500, 'PO-1'),
                        ('Aspirin, 100 mg', 200, 'PO-1'),
                        ('Amoxycillin Cap, 250 mg', 300, 'PO-2')]:
    txs.insert_one({'type': 'receive', 'med_name': med, 'quantity': qty,
                    'order_number': order, 'supplier': 'S', 'price': 2.0,
                    'user': 'u', 'timestamp': month_start(1)})

body = client.get('/dashboard?months=6').data.decode()


def measure_row(body, label):
    mm = _re.search(r'<td><strong>' + _re.escape(label) + r'</strong></td>(.*?)</tr>', body, _re.S)
    return [_re.sub(r'<[^>]+>', '', c).strip() for c in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)] if mm else []


check('unit quantities are no longer totalled across medications',
      'Total Movement by Month' not in body and 'Activity by Month' in body)
# month index 4 in a 6-month window is one month back
check('3 dispensing visits counted', measure_row(body, 'Dispensing visits')[4] == '3',
      str(measure_row(body, 'Dispensing visits')))
check('6 items dispensed counted', measure_row(body, 'Items dispensed')[4] == '6',
      str(measure_row(body, 'Items dispensed')))
check('items per visit = 2.0', measure_row(body, 'Items per visit')[4] == '2.0',
      str(measure_row(body, 'Items per visit')))
check('2 distinct patients (repeat visitor counted once)',
      measure_row(body, 'Patients seen')[4] == '2', str(measure_row(body, 'Patients seen')))
check('3 receipt lines', measure_row(body, 'Stock receipts')[4] == '3',
      str(measure_row(body, 'Stock receipts')))
check('2 deliveries (two lines shared one order number)',
      measure_row(body, 'Deliveries')[4] == '2', str(measure_row(body, 'Deliveries')))
# 92 units dispensed x $2.00
check('value dispensed = $184', measure_row(body, 'Value dispensed')[4] == 'R184',
      str(measure_row(body, 'Value dispensed')))
check('value received = $2,000', measure_row(body, 'Value received')[4] == 'R2,000',
      str(measure_row(body, 'Value received')))
check('a month with no activity shows a dash for items per visit',
      measure_row(body, 'Items per visit')[0] == '-')

print('\n--- 13. Adjustment activity is admin-only and counted, not summed ---')
client.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid2 = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
for med, counted in [('Paracetamol, 500 mg', 400), ('Aspirin, 100 mg', 900)]:
    client.post('/stock-take', data={'action': 'count', 'stock_take_id': sid2,
                                     'med_name': med, 'counted': str(counted)},
                follow_redirects=True)
body = client.get('/dashboard?months=6').data.decode()
check('2 items adjusted this month', measure_row(body, 'Items adjusted at stock take')[5] == '2',
      str(measure_row(body, 'Items adjusted at stock take')))
check('adjustment value row present for admin', bool(measure_row(body, 'Value of adjustments')))
login('user', 'Pharm')
ubody = client.get('/dashboard?months=6').data.decode()
check('non-admin sees the activity table', 'Activity by Month' in ubody)
check('non-admin sees visits and patients',
      bool(measure_row(ubody, 'Dispensing visits')) and bool(measure_row(ubody, 'Patients seen')))
check('non-admin sees no adjustment rows',
      not measure_row(ubody, 'Items adjusted at stock take')
      and not measure_row(ubody, 'Value of adjustments'))

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
