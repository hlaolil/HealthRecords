"""Staff meeting indicators on the dashboard, and the medication errors log."""
from _root import ROOT  # noqa: F401
import os
os.environ['SECRET_KEY'] = 'test-secret-key'
import mongomock
import pymongo
from datetime import datetime, timedelta, timezone

_fake = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _fake
import app as A
A.get_mongo_client = lambda _client=_fake: _client
A.init_db_collections()
from test_support import arm_csrf

db = _fake['pharmacy_db']
client = A.app.test_client()
arm_csrf(client)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin'):
    with client.session_transaction() as s:
        s['user'] = {'name': 'Admin User', 'login': 'x', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


login()
now = A.local_now()
py, pm = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
start, end = A._local_month_bounds(py, pm)
mid = start + timedelta(days=10)

# A: balance 10 now; last month: received 5 (day 5), dispensed 15 (day 10) -> hit 0 then refilled
db['medications'].insert_many([
    {'name': 'A', 'balance': 10, 'price': 2.0},
    {'name': 'B', 'balance': 50, 'price': 1.0},
    {'name': 'C', 'balance': 0, 'price': 1.0},
    {'name': 'D', 'balance': 0, 'price': 1.0},
])
db['transactions'].insert_many([
    {'med_name': 'A', 'type': 'dispense', 'quantity': 10, 'timestamp': mid},
    {'med_name': 'A', 'type': 'receive', 'quantity': 10, 'timestamp': mid + timedelta(days=5)},
    {'med_name': 'A', 'type': 'dispense', 'quantity': 0, 'timestamp': mid - timedelta(days=3)},
    {'med_name': 'B', 'type': 'dispense', 'quantity': 5, 'timestamp': mid},
    {'med_name': 'B', 'type': 'expiry_removal', 'quantity': -4, 'price': 1.5,
     'timestamp': mid},
    # C ran out 3 months before the month: out under six months, still counted
    {'med_name': 'C', 'type': 'dispense', 'quantity': 4, 'timestamp': start - timedelta(days=80)},
    # D has had nothing for a year: dormant, left out
    {'med_name': 'D', 'type': 'dispense', 'quantity': 4, 'timestamp': start - timedelta(days=365)},
    # After the month: A received 0 net; B had a receipt after month end
    {'med_name': 'B', 'type': 'receive', 'quantity': 20, 'timestamp': end + timedelta(hours=30)},
])
full = db['stock_takes'].insert_one({'reference': 'ST-1', 'status': 'closed', 'closed_at': mid}).inserted_id
part = db['stock_takes'].insert_one({'reference': 'ST-2', 'status': 'closed', 'closed_at': mid}).inserted_id
for n in ('A', 'B', 'C', 'D'):
    db['stock_take_counts'].insert_one({'stock_take_id': str(full), 'med_name': n, 'counted': 1})
db['stock_take_counts'].insert_one({'stock_take_id': str(part), 'med_name': 'A', 'counted': 1})

k = A._meeting_kpis(db, py, pm, 'pharmaceutical')
names = sorted(s['med_name'] for s in k['stocked_out'])
# A: closing 10, before receive 0 -> hit zero. B: closing 30, never zero. C: always 0.
check('stock-out finds items that touched zero', names == ['A', 'C'], str(names))
check('stock-out rate', k['stockout_pct'] == '66.7%', k['stockout_pct'])
check('item at zero over six months is left out', k['dormant'] == ['D'] and k['tracked'] == 3,
      str(k['dormant']) + str(k['tracked']))
check('stock-out objective is 10%', k['stockout_objective'] == '10%' and not k['stockout_met'])
check('expired cost uses price on the write-off', k['expired_value'] == 'R6.00', k['expired_value'])
check('only the full count counts', k['st_done'] == 1 and len(k['st_partial']) == 1,
      str(k['st_full']) + str(k['st_partial']))
check('two full counts a year', k['st_expected'] == 2 and k['st_due'] == pm * 2 // 12)

# PTO and errors via the page
day = (now - timedelta(days=0)).strftime('%Y-%m-%d')
client.post('/meeting-log', data={'action': 'add_error', 'date': day, 'error_type': 'Near-miss',
                                  'description': 'Wrong strength picked, caught at check'})
check('error saves', db['medication_errors'].count_documents({}) == 1)
k2 = A._meeting_kpis(db, now.year, now.month, 'pharmaceutical')
check('this month counts the error', k2['errors'] == 1)
check('no PTO left', 'pto_done' not in k2)

r = client.get('/dashboard')
body = r.get_data(as_text=True)
check('dashboard shows the panel', r.status_code == 200 and 'Staff Meeting Indicators' in body)
check('no PTO on the dashboard', 'PTO' not in body)
r = client.get('/meeting-log')
check('log page renders', r.status_code == 200 and 'Wrong strength' in r.get_data(as_text=True))
login('department')
check('department kept out', client.get('/meeting-log').status_code == 302)

print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
raise SystemExit(1 if FAIL else 0)
