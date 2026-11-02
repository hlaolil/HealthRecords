"""Differential test for the reports() rewrite.

The per-medication query loop was replaced with a single aggregation. The
numbers must not have moved. This computes every expected figure from the raw
data with a deliberately naive, independent implementation and compares it
against what the route actually renders, over randomised data.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'

import random
import re as _re
import mongomock, pymongo
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
meds, txs = db['medications'], db['transactions']
client = A.app.test_client()

# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf
arm_csrf(client)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin'):
    with client.session_transaction() as s:
        s['user'] = {'name': 'Admin', 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for c in ['medications', 'transactions']:
        db[c].delete_many({})


def cells(body, name):
    # A row the records cannot explain carries a warning mark after the name,
    # so the name cell may hold more than the name.
    mm = _re.search(r'<td>' + _re.escape(name) + r'(?: <strong>[^<]*</strong>)?</td>(.*?)</tr>',
                    body, _re.S)
    if not mm:
        return []
    return [_re.sub(r'<[^>]+>', '', c).strip() for c in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)]


# =====================================================================
print('\n--- 1. Randomised inventory report vs independent arithmetic ---')
random.seed(20260824)
reset()
login()

START, END = datetime(2026, 5, 1), datetime(2026, 5, 31, 23, 59, 59)
names = [f'Med {i:03d}, 10 mg' for i in range(40)]
for name in names:
    meds.insert_one({'name': name, 'balance': random.randint(0, 900), 'batch': 'B',
                     'price': 1.0, 'expiry_date': '2030-01-01',
                     'schedule': 'not controlled'})
    for _ in range(random.randint(0, 6)):
        # spread transactions inside, before and after the report period
        day = random.randint(-40, 70)
        kind = random.choice(['dispense', 'receive', 'adjustment'])
        qty = random.randint(1, 60)
        txs.insert_one({'type': kind, 'med_name': name,
                        'quantity': -qty if kind == 'adjustment' and random.random() < 0.5 else qty,
                        'user': 'u', 'timestamp': datetime(2026, 5, 1) + timedelta(days=day)})


def expected_inventory(name):
    """Naive independent recomputation straight from the raw documents."""
    d = r = a = 0
    for tx in txs.find({'med_name': name}):
        ts = tx['timestamp']
        if not (START <= ts <= END):
            continue
        if tx['type'] == 'dispense':
            d += tx['quantity']
        elif tx['type'] == 'receive':
            r += tx['quantity']
        else:
            a += tx['quantity']
    cur = meds.find_one({'name': name})['balance']
    beg = max(0, cur - r + d - a)
    days = (END.date() - START.date()).days + 1
    # Issued is everything that left: dispensed, less the net of stock-take
    # corrections (a count that found more takes it off).
    issued = d - a
    # Adjusted AMC: consumption scaled to 30 days, a whole number rounded
    # half up. No unmet demand in this data, so it is the plain AMC rounded.
    adj = int(d / days * 30 + 0.5)
    order = max(0, 3 * adj - cur)
    # beginning, received, issued, current, adjusted AMC, amount to order
    return [str(beg), str(r), str(issued), str(cur), str(adj), str(order)]


body = client.post('/reports', data={'report_type': 'inventory',
                                     'start_date': '2026-05-01',
                                     'end_date': '2026-05-31'}).data.decode()
mismatches = []
for name in names:
    got, want = cells(body, name), expected_inventory(name)
    if got != want:
        mismatches.append((name, got, want))
check(f'all {len(names)} inventory rows match an independent recomputation',
      not mismatches, str(mismatches[:3]))

def _net_adjustment(name):
    return sum(t['quantity'] for t in txs.find({'med_name': name, 'type': 'adjustment',
                                                'timestamp': {'$gte': START, '$lte': END}}))
nonzero_adj = sum(1 for n in names if _net_adjustment(n) != 0)
check(f'the sample actually exercised adjustments ({nonzero_adj} rows)', nonzero_adj >= 5,
      str(nonzero_adj))

print('\n--- 2. Every row still reconciles ---')
bad = []
for name in names:
    c = cells(body, name)
    if c and int(c[0]) + int(c[1]) - int(c[2]) != int(c[3]):
        # beginning is floored at 0, so a row can legitimately not close when
        # the back-calculation would have gone negative
        if int(c[0]) != 0:
            bad.append((name, c))
check('every row reconciles: Beginning + Received - Issued = Current', not bad,
      str(bad[:3]))
_floored = [n for n in names if cells(body, n) and int(cells(body, n)[0]) == 0
            and int(cells(body, n)[0]) + int(cells(body, n)[1])
            - int(cells(body, n)[2]) != int(cells(body, n)[3])]
check('a row the records cannot explain is flagged, not left to look right',
      all(_re.search(r'<tr class="expired"[^>]*>\s*<td>' + _re.escape(n), body)
          for n in _floored), str(_floored))
check('and the page says what it means when there are any',
      (not _floored) or 'do not\nadd up' in body, str(len(_floored)))
check('every Adjusted AMC is a whole number',
      all(cells(body, n) and cells(body, n)[4].isdigit() for n in names))

print('\n--- 3. A non-admin sees exactly the same rows ---')
login('user')
ubody = client.post('/reports', data={'report_type': 'inventory',
                                      'start_date': '2026-05-01',
                                      'end_date': '2026-05-31'}).data.decode()
bad = [(n, cells(body, n), cells(ubody, n)) for n in names
       if cells(body, n) != cells(ubody, n)]
check('admin and non-admin rows are identical', not bad, str(bad[:3]))
login()

# =====================================================================
print('\n--- 4. Randomised stock-on-hand vs independent arithmetic ---')
NOW = datetime.now(timezone.utc)
ASOF = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)


def expected_balance_at(name):
    cur = meds.find_one({'name': name})['balance']
    d = r = a = 0
    for tx in txs.find({'med_name': name}):
        ts = tx['timestamp']
        ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        if not (ASOF < ts <= NOW):
            continue
        if tx['type'] == 'dispense':
            d += tx['quantity']
        elif tx['type'] == 'receive':
            r += tx['quantity']
        else:
            a += tx['quantity']
    return max(0, cur - r + d - a)


body = client.post('/reports', data={'report_type': 'stock_on_hand',
                                     'end_date': '2026-05-31'}).data.decode()
bad = []
for name in names:
    c = cells(body, name)
    if not c:
        bad.append((name, 'row missing'))
        continue
    if c[0] != str(expected_balance_at(name)):
        bad.append((name, c[0], expected_balance_at(name)))
check(f'all {len(names)} stock-on-hand balances match an independent recomputation',
      not bad, str(bad[:3]))

# =====================================================================
print('\n--- 5. The 300-medication cap is gone ---')
reset()
login()
for i in range(320):
    meds.insert_one({'name': f'Item {i:04d}, 10 mg', 'balance': 10, 'batch': 'B',
                     'price': 1.0, 'expiry_date': '2030-01-01',
                     'schedule': 'not controlled'})
body = client.post('/reports', data={'report_type': 'inventory',
                                     'start_date': '2026-05-01',
                                     'end_date': '2026-05-31'}).data.decode()
check('the 300th medication is present', 'Item 0299, 10 mg' in body)
check('the 301st medication is present (was silently dropped before)',
      'Item 0300, 10 mg' in body)
check('the 320th medication is present', 'Item 0319, 10 mg' in body)
check('all 320 rows render', body.count(', 10 mg</td>') == 320,
      str(body.count(', 10 mg</td>')))

# =====================================================================
print('\n--- 6. Query counts no longer scale with the formulary ---')
_of, _oa, _ofo = (mongomock.collection.Collection.find,
                  mongomock.collection.Collection.aggregate,
                  mongomock.collection.Collection.find_one)
calls = {}


def count_queries(fn):
    calls.clear()

    def f(self, *a, **k):
        calls['find'] = calls.get('find', 0) + 1
        return _of(self, *a, **k)

    def g(self, *a, **k):
        calls['aggregate'] = calls.get('aggregate', 0) + 1
        return _oa(self, *a, **k)

    def h(self, *a, **k):
        calls['find_one'] = calls.get('find_one', 0) + 1
        return _ofo(self, *a, **k)
    (mongomock.collection.Collection.find, mongomock.collection.Collection.aggregate,
     mongomock.collection.Collection.find_one) = f, g, h
    try:
        fn()
    finally:
        (mongomock.collection.Collection.find, mongomock.collection.Collection.aggregate,
         mongomock.collection.Collection.find_one) = _of, _oa, _ofo
    return sum(calls.values())


for n in (10, 100, 320):
    reset()
    for i in range(n):
        meds.insert_one({'name': f'Item {i:04d}, 10 mg', 'balance': 10, 'batch': 'B',
                         'price': 1.0, 'expiry_date': '2030-01-01',
                         'schedule': 'not controlled'})
        txs.insert_one({'type': 'dispense', 'med_name': f'Item {i:04d}, 10 mg',
                        'quantity': 5, 'user': 'u', 'timestamp': datetime(2026, 5, 10)})
    inv = count_queries(lambda: client.post('/reports', data={
        'report_type': 'inventory', 'start_date': '2026-05-01', 'end_date': '2026-05-31'}))
    soh = count_queries(lambda: client.post('/reports', data={
        'report_type': 'stock_on_hand', 'end_date': '2026-05-31'}))
    print(f'   {n:>3} medications -> inventory: {inv} queries, stock on hand: {soh} queries')
    check(f'inventory stays flat at {n} medications (was {2 * n + 1})', inv <= 5, str(inv))
    check(f'stock on hand stays flat at {n} medications (was {2 * n + 1})', soh <= 5, str(soh))

# =====================================================================
print('\n--- 7. Search still narrows correctly after the rewrite ---')
reset()
login()
for name, bal in [('Paracetamol, 500 mg', 100), ('Aspirin, 100 mg', 50)]:
    meds.insert_one({'name': name, 'balance': bal, 'batch': 'B', 'price': 1.0,
                     'expiry_date': '2030-01-01', 'schedule': 'not controlled'})
    txs.insert_one({'type': 'dispense', 'med_name': name, 'quantity': 20,
                    'user': 'u', 'timestamp': datetime(2026, 5, 10)})
body = client.post('/reports', data={'report_type': 'inventory', 'search': 'Aspirin',
                                     'start_date': '2026-05-01',
                                     'end_date': '2026-05-31'}).data.decode()
check('search returns only the matching medication',
      'Aspirin, 100 mg' in body and 'Paracetamol, 500 mg' not in body)
c = cells(body, 'Aspirin, 100 mg')
# 20 dispensed over 31 days: AMC 19.35, so 19; order 3 x 19 - 50 = 7.
check('a searched row still carries the right figures',
      c == ['70', '0', '20', '50', '19', '7'], str(c))
body = client.post('/reports', data={'report_type': 'stock_on_hand', 'search': 'Aspirin',
                                     'end_date': '2026-05-31'}).data.decode()
check('stock-on-hand search still narrows',
      'Aspirin, 100 mg' in body and 'Paracetamol, 500 mg' not in body)

# =====================================================================
print('\n--- 8. Controlled drug register untouched and still correct ---')
reset()
login()
meds.insert_one({'name': 'Morphine, 010 mg', 'balance': 43, 'batch': 'B', 'price': 1.0,
                 'expiry_date': '2030-01-01', 'schedule': 'controlled'})
for kind, qty, day in [('receive', 50, 5), ('dispense', 10, 10), ('adjustment', -7, 20)]:
    txs.insert_one({'type': kind, 'med_name': 'Morphine, 010 mg', 'quantity': qty,
                    'user': 'u', 'timestamp': datetime(2026, 5, day)})
body = client.post('/reports', data={'report_type': 'controlled_drug_register',
                                     'start_date': '2026-05-01',
                                     'end_date': '2026-05-31'}).data.decode()
check('register renders', 'Morphine, 010 mg' in body)
check('beginning balance is 10 (43 - 50 + 10 + 7)',
      'Opening balance <strong>10</strong>' in body)
check('ending balance is the current balance',
      'closing balance <strong>43</strong>' in body)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
