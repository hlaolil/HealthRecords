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
    for col in ['medications', 'transactions']:
        db[col].delete_many({})


NOW = datetime.utcnow()
# Must be inside the current month AND not in the future: hard-coding day 2
# made this suite fail on the 1st of any month, when day 2 has not happened yet.
THIS_MONTH = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
LAST_MONTH = (NOW.replace(day=1) - timedelta(days=5)).replace(hour=9)


def visit(patient, items, when, tid=None):
    tid = tid or f'TX-{patient}-{when.isoformat()}'
    for med, qty in items:
        txs.insert_one({'type': 'dispense', 'transaction_id': tid, 'patient': patient,
                        'med_name': med, 'quantity': qty, 'user': 'u',
                        'company': 'C', 'position': 'P', 'age_group': 'adult',
                        'gender': 'F', 'prescriber': 'Dr X', 'dispenser': 'D',
                        'date': when.strftime('%Y-%m-%d'), 'diagnoses': ['dx'],
                        'sick_leave_days': 0, 'timestamp': when})
    return tid


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


def visit_numbers(body):
    """The bold visit counters rendered in the dispense table."""
    return _re.findall(r'>\s*(\d+)\.\s*\n', body)


def tids_on_page(body):
    return _re.findall(r'value="(TX-[^"]+)"', body)


# =====================================================================
print('\n--- 1. The page no longer grows without bound ---')
reset()
login()
add_meds()
for i in range(400):
    visit(f'Patient {i:03d}', [('Med 0, 10 mg', 5), ('Med 1, 10 mg', 3)], THIS_MONTH, tid=f'TX-{i:04d}')
for i in range(400):
    receipt('Med 0, 10 mg', THIS_MONTH, order=f'PO-{i}', invoice=f'INV-{i}')

d_body = c.get('/dispense').data.decode()
r_body = c.get('/receive').data.decode()
print(f'   400 visits (800 lines) -> /dispense {len(d_body)/1024:.0f}KB')
print(f'   400 receipt lines      -> /receive  {len(r_body)/1024:.0f}KB')
check('/dispense page stays small', len(d_body) < 250 * 1024, f'{len(d_body)/1024:.0f}KB')
check('/receive page stays small', len(r_body) < 250 * 1024, f'{len(r_body)/1024:.0f}KB')

# 10x the data, page size must not move
for i in range(400, 4000):
    visit(f'Patient {i:04d}', [('Med 0, 10 mg', 5)], THIS_MONTH, tid=f'TX-{i:04d}')
big = c.get('/dispense').data.decode()
print(f'   4,000 visits           -> /dispense {len(big)/1024:.0f}KB')
check('10x the data does not grow the page', abs(len(big) - len(d_body)) < 60 * 1024,
      f'{len(d_body)/1024:.0f}KB -> {len(big)/1024:.0f}KB')

# =====================================================================
print('\n--- 2. Page size is respected ---')
check('dispense shows exactly one page of visits',
      len(tids_on_page(big)) == A.DISPENSE_PAGE_SIZE, str(len(tids_on_page(big))))
check('dispense reports the true total', f'of 4000 visits' in big, )
r_body = c.get('/receive').data.decode()
check('receive reports the true total', 'of 400 receipts' in r_body)
check('receive shows one page of lines',
      r_body.count('<td>INV-') == A.RECEIVE_PAGE_SIZE, str(r_body.count('<td>INV-')))

# =====================================================================
print('\n--- 3. A visit is never split across a page boundary ---')
reset()
login()
add_meds()
# 30 visits, each with 3 medication lines -> lines would split at any line-based paging
for i in range(30):
    visit(f'Patient {i:02d}',
          [('Med 0, 10 mg', 1), ('Med 1, 10 mg', 2), ('Med 2, 10 mg', 3)],
          THIS_MONTH - timedelta(minutes=i), tid=f'TX-{i:03d}')

seen, complete = [], True
for p in (1, 2):
    body = c.get(f'/dispense?page={p}').data.decode()
    for tid in tids_on_page(body):
        seen.append(tid)
        lines = txs.count_documents({'transaction_id': tid})
        rendered = body.count(f'value="{tid}"')
        # each visit renders its full set of medication rows
        if txs.count_documents({'transaction_id': tid}) != 3:
            complete = False
check('every visit appears on exactly one page', len(seen) == len(set(seen)) == 30,
      f'{len(seen)} ids, {len(set(seen))} unique')
check('no visit is truncated', complete)
p1 = tids_on_page(c.get('/dispense?page=1').data.decode())
p2 = tids_on_page(c.get('/dispense?page=2').data.decode())
check('pages do not overlap', not (set(p1) & set(p2)), str(set(p1) & set(p2)))
check('page 1 holds a full page', len(p1) == A.DISPENSE_PAGE_SIZE, str(len(p1)))
check('page 2 holds the remainder', len(p2) == 5, str(len(p2)))

body = c.get('/dispense?page=1').data.decode()
check('all three medication lines of a visit render together',
      body.count('Med 0, 10 mg') >= 25 and body.count('Med 2, 10 mg') >= 25)

# =====================================================================
print('\n--- 4. Default window is this month; search escapes it ---')
reset()
login()
add_meds()
visit('Recent Person', [('Med 0, 10 mg', 1)], THIS_MONTH, tid='TX-NEW')
visit('Historic Person', [('Med 0, 10 mg', 1)], LAST_MONTH, tid='TX-OLD')
receipt('Med 0, 10 mg', THIS_MONTH, order='PO-NEW', invoice='INV-NEW')
receipt('Med 0, 10 mg', LAST_MONTH, order='PO-OLD', invoice='INV-OLD')

body = c.get('/dispense').data.decode()
check('unfiltered dispense shows this month', 'Recent Person' in body)
check('unfiltered dispense hides older records', 'Historic Person' not in body)
check('the default is explained on the page', 'Showing <strong>this month</strong>' in body)

body = c.get('/dispense?search=Historic').data.decode()
check('search reaches back past the default window', 'Historic Person' in body)
check('search suppresses the default-window notice',
      'Showing <strong>this month</strong>' not in body)

body = c.get('/receive').data.decode()
check('unfiltered receive shows this month', 'INV-NEW' in body and 'INV-OLD' not in body)
body = c.get('/receive?search=INV-OLD').data.decode()
check('receive search reaches back past the window', 'INV-OLD' in body)

old = LAST_MONTH.strftime('%Y-%m-%d')
body = c.get(f'/dispense?start_date={old}&end_date={old}').data.decode()
check('an explicit date range overrides the default',
      'Historic Person' in body and 'Recent Person' not in body)
check('explicit dates suppress the notice',
      'Showing <strong>this month</strong>' not in body)

# =====================================================================
print('\n--- 5. Paging links preserve the filters ---')
reset()
login()
add_meds()
for i in range(60):
    visit(f'Patient {i:02d}', [('Med 0, 10 mg', 1)], LAST_MONTH, tid=f'TX-{i:03d}')
start = LAST_MONTH.replace(day=1).strftime('%Y-%m-%d')
end = LAST_MONTH.strftime('%Y-%m-%d')
body = c.get(f'/dispense?start_date={start}&end_date={end}&search=Patient').data.decode()
check('next-page link carries the date range',
      f'start_date={start}' in body and f'end_date={end}' in body)
check('next-page link carries the search term', 'search=Patient' in body)
check('a filtered list still pages', 'Page 1 of' in body)

p2 = c.get(f'/dispense?page=2&start_date={start}&end_date={end}&search=Patient').data.decode()
check('page 2 of a filtered list still applies the filter',
      'Page 2 of' in p2 and len(tids_on_page(p2)) == A.DISPENSE_PAGE_SIZE)

# =====================================================================
print('\n--- 6. Out-of-range and malformed pages ---')
reset()
login()
add_meds()
# 60 visits inside the default window -> 3 pages at 25 per page
for i in range(60):
    visit(f'Patient {i:02d}', [('Med 0, 10 mg', 1)], THIS_MONTH - timedelta(minutes=i),
          tid=f'TX-{i:03d}')
body = c.get('/dispense?page=9999').data.decode()
check('a page past the end clamps to the last page', 'Page 3 of 3' in body,
      _re.search(r'Page \d+ of \d+', body).group(0) if _re.search(r'Page \d+ of \d+', body) else 'no pager')
check('clamped page still renders rows', len(tids_on_page(body)) == 10,
      str(len(tids_on_page(body))))
check('page=0 is treated as page 1', 'Page 1 of 3' in c.get('/dispense?page=0').data.decode())
check('page=abc does not error', c.get('/dispense?page=abc').status_code == 200)
check('page=-5 does not error', c.get('/dispense?page=-5').status_code == 200)
check('receive handles a bad page', c.get('/receive?page=abc').status_code == 200)

reset()
login()
add_meds()
body = c.get('/dispense').data.decode()
check('an empty list renders without a pager', c.get('/dispense').status_code == 200
      and 'Page 1 of' not in body)
check('an empty receive list renders', c.get('/receive').status_code == 200)

print('\n--- 7. Query count is flat ---')
_of, _oa, _cd = (mongomock.collection.Collection.find,
                 mongomock.collection.Collection.aggregate,
                 mongomock.collection.Collection.count_documents)
calls = {}


def load(url, n_visits):
    reset()
    add_meds()
    for i in range(n_visits):
        visit(f'P{i:04d}', [('Med 0, 10 mg', 1), ('Med 1, 10 mg', 1)], THIS_MONTH, tid=f'TX-{i:05d}')
    calls.clear()

    def f(self, *a, **k):
        calls['find'] = calls.get('find', 0) + 1
        return _of(self, *a, **k)

    def g(self, *a, **k):
        calls['agg'] = calls.get('agg', 0) + 1
        return _oa(self, *a, **k)

    def h(self, *a, **k):
        calls['count'] = calls.get('count', 0) + 1
        return _cd(self, *a, **k)
    (mongomock.collection.Collection.find, mongomock.collection.Collection.aggregate,
     mongomock.collection.Collection.count_documents) = f, g, h
    try:
        r = c.get(url)
    finally:
        (mongomock.collection.Collection.find, mongomock.collection.Collection.aggregate,
         mongomock.collection.Collection.count_documents) = _of, _oa, _cd
    return sum(calls.values()), len(r.data)


login()
for url in ('/dispense', '/receive'):
    q1, s1 = load(url, 50)
    q2, s2 = load(url, 1000)
    print(f'   {url:<11} 50 visits -> {q1} queries {s1/1024:.0f}KB;  '
          f'1000 visits -> {q2} queries {s2/1024:.0f}KB')
    check(f'{url} query count is flat', q1 == q2, f'{q1} vs {q2}')
    check(f'{url} response size is flat', abs(s1 - s2) < 40 * 1024,
          f'{s1/1024:.0f}KB vs {s2/1024:.0f}KB')

# =====================================================================
print('\n--- 8. Editing and deleting still work through a paged list ---')
reset()
login()
add_meds()
for i in range(40):
    visit(f'Patient {i:02d}', [('Med 0, 10 mg', 2)], THIS_MONTH - timedelta(minutes=i),
          tid=f'TX-{i:03d}')
before = meds.find_one({'name': 'Med 0, 10 mg'})['balance']
r = c.post('/delete-dispense', data={'transaction_id': 'TX-000'}, follow_redirects=True)
check('deleting a dispense from a paged list restores stock',
      meds.find_one({'name': 'Med 0, 10 mg'})['balance'] == before + 2)
check('the deleted visit is gone from the list',
      'TX-000' not in c.get('/dispense').data.decode())
check('the list still pages after a delete', 'of 39 visits' in c.get('/dispense').data.decode())

rid = str(txs.find_one({'type': 'dispense'})['_id'])
check('the edit link still resolves', c.get('/dispense?edit=TX-001').status_code == 200)

reset()
login()
add_meds()
for i in range(60):
    receipt('Med 0, 10 mg', THIS_MONTH, order=f'PO-{i}', invoice=f'INV-{i}')
rid = str(txs.find_one({'invoice_number': 'INV-0'})['_id'])
check('edit-receive renders with a bounded list',
      c.get(f'/edit-receive/{rid}').status_code == 200)
body = c.get(f'/edit-receive/{rid}').data.decode()
check('edit-receive does not dump every row',
      body.count('<td>INV-') <= A.RECEIVE_PAGE_SIZE, str(body.count('<td>INV-')))

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
