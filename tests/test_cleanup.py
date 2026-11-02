from _root import ROOT  # the app folder, one up from tests/
import os, sys, io, contextlib
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
import cleanup_dormant as CD
CD.MongoClient = lambda *a, **k: _f

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


def login():
    with c.session_transaction() as s:
        s['user'] = {'name': 'Admin User', 'login': 'a', 'role': 'admin'}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'audit_log', 'deliveries']:
        db[col].delete_many({})


NOW = datetime.utcnow()
OLD = NOW - timedelta(days=500)


def add(name, balance=0, schedule='not controlled'):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'B', 'price': 2.5,
                     'pack_size': 100, 'pack_price': 250.0, 'expiry_date': '2030-01-01',
                     'schedule': schedule, 'supplier': 'S'})


def tx(name, when, qty=10, kind='dispense'):
    txs.insert_one({'type': kind, 'med_name': name, 'quantity': qty,
                    'transaction_id': f'T{name}{when.isoformat()}', 'patient': 'P',
                    'price': 2.5, 'line_value': qty * 2.5, 'user': 'u',
                    'timestamp': when})


def run(*argv):
    out = io.StringIO()
    sys.argv = ['cleanup_dormant.py'] + list(argv)
    with contextlib.redirect_stdout(out):
        try:
            CD.main()
        except SystemExit as e:
            out.write(f'\nEXIT: {e}\n')
    return out.getvalue()


print('\n--- 1. Dry run is the default and changes nothing ---')
reset()
add('Dormant Item, 10 mg'); tx('Dormant Item, 10 mg', OLD)
add('Active Item, 10 mg'); tx('Active Item, 10 mg', NOW - timedelta(days=10))
add('Never Used, 10 mg')
before = list(meds.find({}, {'_id': 0}))
out = run()
check('the dry run is announced', 'DRY RUN' in out)
check('nothing was changed', list(meds.find({}, {'_id': 0})) == before)
check('the dormant item is proposed for archiving',
      'WOULD ARCHIVE' in out and 'Dormant Item, 10 mg' in out)
check('the never-used item is proposed for deletion',
      'WOULD DELETE' in out and 'Never Used, 10 mg' in out)
check('the active item is left alone', 'ACTIVE — left alone' in out)
check('it tells you how to proceed', '--apply --archive' in out)

print('\n--- 2. What it refuses to touch ---')
reset()
add('Has Stock, 10 mg', balance=250); tx('Has Stock, 10 mg', OLD)
add('Morphine, 10 mg', balance=0, schedule='controlled'); tx('Morphine, 10 mg', OLD)
out = run()
check('an item with stock on hand is skipped', 'SKIPPED' in out and 'Has Stock, 10 mg' in out)
check('and the reason names the stock', '250 unit(s) still on hand' in out)
check('and points at the write-off route', 'write the stock off' in out)
check('a controlled drug is skipped', 'Morphine, 10 mg' in out.split('SKIPPED')[1])
check('and the reason is the statutory register', 'statutory record' in out)
run('--apply', '--archive', '--yes')
check('neither was archived despite --apply',
      meds.count_documents({'archived': True}) == 0)

print('\n--- 3. Archiving keeps history intact ---')
reset()
login()
add('Dormant Item, 10 mg')
tx('Dormant Item, 10 mg', OLD, qty=100)
add('Active Item, 10 mg', balance=500)
tx('Active Item, 10 mg', NOW - timedelta(days=5), qty=20)
run('--apply', '--archive', '--yes')
check('the dormant item is archived', meds.find_one({'name': 'Dormant Item, 10 mg'})['archived'] is True)
check('the record still exists', meds.count_documents({'name': 'Dormant Item, 10 mg'}) == 1)
check('every transaction it had is untouched',
      txs.count_documents({'med_name': 'Dormant Item, 10 mg'}) == 1)
check('the active item is not archived',
      'archived' not in meds.find_one({'name': 'Active Item, 10 mg'}))
check('the action is written to the audit log',
      db['audit_log'].count_documents({'target_type': 'medication'}) >= 1)

print('\n--- 4. Archived items leave the working lists ---')
today = NOW.strftime('%Y-%m-%d')
body = c.post('/reports', data={'report_type': 'stock_on_hand', 'end_date': today}).data.decode()
check('gone from stock on hand', 'Dormant Item, 10 mg' not in body)
check('the active item remains', 'Active Item, 10 mg' in body)
body = c.post('/reports', data={'report_type': 'inventory',
                                'start_date': NOW.strftime('%Y-%m-01'),
                                'end_date': today}).data.decode()
check('gone from the inventory report', 'Dormant Item, 10 mg' not in body)
body = c.get('/dashboard?limit=0').data.decode()
check('gone from the dashboard', 'Dormant Item, 10 mg' not in body)
body = c.post('/reports', data={'report_type': 'order_request', 'cover_months': '3'}).data.decode()
check('never proposed for ordering', 'Dormant Item, 10 mg' not in body)
c.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
body = c.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('not on the stock take count sheet', 'Dormant Item, 10 mg' not in body)

print('\n--- 5. History still reports correctly ---')
old_month = OLD.strftime('%Y-%m-%d')
body = c.post('/reports', data={'report_type': 'monthly_report', 'period': 'month',
                                'start_date': old_month, 'end_date': old_month}).data.decode()
mm = _re.search(r'<td>Total cost dispensed</td>\s*<td>(.*?)</td>', body, _re.S)
val = _re.sub(r'<[^>]+>', '', mm.group(1)).strip() if mm else None
check('the archived item still counts in the period it was used',
      val == 'R250.00', str(val))
check('and is named in the highest-cost table',
      'Dormant Item, 10 mg' in body[body.index('Highest Cost Items'):])

print('\n--- 6. Deletion only for records with no history at all ---')
reset()
add('Never Used, 10 mg')
add('Used Once, 10 mg'); tx('Used Once, 10 mg', OLD)
run('--apply', '--delete', '--yes')
check('the never-used record is deleted', meds.count_documents({'name': 'Never Used, 10 mg'}) == 0)
check('the one with history is NOT deleted',
      meds.count_documents({'name': 'Used Once, 10 mg'}) == 1)
check('nor archived by --delete alone',
      'archived' not in meds.find_one({'name': 'Used Once, 10 mg'}))
check('the deletion is audited',
      db['audit_log'].count_documents({'action': 'DELETE'}) == 1)

print('\n--- 7. Window, restore and listing ---')
reset()
add('Eight Months, 10 mg'); tx('Eight Months, 10 mg', NOW - timedelta(days=240))
out = run('--days', '365')
check('8 months is still active on a 365-day window', 'ACTIVE — left alone' in out
      and 'WOULD ARCHIVE' in out and 'Eight Months, 10 mg' not in out.split('WOULD ARCHIVE')[1].split('WOULD DELETE')[0])
out = run('--days', '180')
check('a 180-day window catches it',
      'Eight Months, 10 mg' in out.split('WOULD ARCHIVE')[1].split('WOULD DELETE')[0])
run('--days', '180', '--apply', '--archive', '--yes')
check('it archives on the shorter window',
      meds.find_one({})['archived'] is True)
out = run('--list-archived')
check('archived items can be listed', 'Eight Months, 10 mg' in out)
out = run('--apply', '--restore', 'Eight Months, 10 mg')
check('restore brings it back', 'archived' not in meds.find_one({}))
check('restore is confirmed', 'Restored' in out)
out = run('--apply', '--restore', 'No Such Item')
check('restoring an unknown name says so', 'No medication named' in out)
out = run('--apply')
check('--apply without a mode is refused', 'needs either --archive or --delete' in out)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
