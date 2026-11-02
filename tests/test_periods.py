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
        s['user'] = {'name': 'Admin User', 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'deliveries', 'audit_log']:
        db[col].delete_many({})


def add(name, balance=100000, price=2.5):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'B', 'price': price,
                     'pack_size': 100, 'pack_price': price * 100,
                     'expiry_date': '2030-01-01', 'schedule': 'not controlled'})


def dispense(patient, qty=10):
    return c.post('/dispense', data={
        'patient': patient, 'company': 'C', 'position': 'P', 'birth_year': '1985',
        'gender': 'M', 'outcome': '', 'outcome_days': '', 'diagnoses': 'flu',
        'prescriber': 'Dr X', 'dispenser': 'D',
        'date': datetime.utcnow().strftime('%Y-%m-%d'),
        'med_names': ['Paracetamol, 500 mg'], 'quantities': [str(qty)]},
        follow_redirects=True)


print('\n--- 1. A dispense appears immediately ---')
reset()
login()
add('Paracetamol, 500 mg')
r = dispense('Thabo M', 10)
body = r.data.decode()
check('the new entry is in the list straight away', 'Thabo M' in body, )
check('the confirmation is shown', 'successfully' in body)
r = dispense('Palesa N', 5)
body = r.data.decode()
check('the second entry also appears immediately', 'Palesa N' in body)
check('and the first is still there', 'Thabo M' in body)

print('\n--- 2. Refreshing after a dispense does not dispense again ---')
before = meds.find_one({})['balance']
# a redirect means the browser lands on a GET; re-issuing it changes nothing
c.get('/dispense')
c.get('/dispense')
check('re-loading the page does not deduct stock again',
      meds.find_one({})['balance'] == before, str(meds.find_one({})['balance']))
check('only two dispenses exist', txs.count_documents({'type': 'dispense'}) == 2)

print('\n--- 3. Edit Medication can no longer move stock ---')
reset()
login()
add('Paracetamol, 500 mg', balance=500)
body = c.get('/edit-medication?med_name=Paracetamol, 500 mg').data.decode()
check('the balance field is gone', 'name="balance"' not in body)
check('the page explains why', 'does <strong>not</strong> change the quantity' in body)
check('and says where to correct a balance', 'run a stock take' in body)
c.post('/edit-medication', data={'med_name': 'Paracetamol, 500 mg', 'balance': '99999',
                                 'batch': 'NEW', 'pack_size': '50', 'pack_price': '100',
                                 'expiry_date': '2031-01-01',
                                 'schedule': 'controlled'}, follow_redirects=True)
m = meds.find_one({})
check('a posted balance is ignored', m['balance'] == 500, str(m['balance']))
check('but the real attributes are updated',
      m['batch'] == 'NEW' and m['pack_size'] == 50 and m['schedule'] == 'controlled')
check('and the unit price is derived', m['price'] == 2.0, str(m['price']))
check('no stock moved without a transaction', txs.count_documents({}) == 0)

print('\n--- 4. Period report: day, week, month, quarter, year ---')
reset()
login()
add('Paracetamol, 500 mg')
D = datetime(2026, 5, 20, 10, 0)          # a Wednesday


def at(dt, qty):
    txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg',
                    'quantity': qty, 'transaction_id': f'TX{dt.isoformat()}',
                    'patient': f'P{dt.day}', 'price': 2.5,
                    'line_value': qty * 2.5, 'user': 'u', 'timestamp': dt})


at(D, 10)                                  # the day itself
at(D - timedelta(days=2), 20)              # same week (Monday)
at(D - timedelta(days=10), 40)             # earlier in May
at(datetime(2026, 2, 10), 80)              # same year, different quarter
at(datetime(2025, 5, 20), 160)             # previous year


def period(kind, start='2026-05-20'):
    b = c.post('/reports', data={'report_type': 'monthly_report', 'period': kind,
                                 'start_date': start, 'end_date': start}).data.decode()
    mm = _re.search(r'<td>Total cost dispensed</td>\s*<td>(.*?)</td>', b, _re.S)
    return b, (_re.sub(r'<[^>]+>', '', mm.group(1)).strip() if mm else None)


b, v = period('day')
check('day covers only that date', v == 'R25.00', str(v))
check('the day is named', 'Wednesday, 20 May 2026' in b, )
b, v = period('week')
check('week runs Monday to Sunday and includes both', v == 'R75.00', str(v))
check('the week is labelled by its range', 'Week of 18 May' in b, )
b, v = period('month')
check('month covers all of May', v == 'R175.00', str(v))
check('the month is named', 'May 2026' in b)
b, v = period('quarter')
check('Q2 covers April to June only', v == 'R175.00', str(v))
check('the quarter is named', 'Q2 2026' in b)
b, v = period('year')
check('year covers all of 2026', v == 'R375.00', str(v))
check('the year is named', '>2026' in b or 'Report &mdash; 2026' in b)
check('the covered range is stated', '2026-01-01 to 2026-12-31' in b)

b, v = period('quarter', '2026-02-10')
check('a February date selects Q1', 'Q1 2026' in b and v == 'R200.00', str(v))
b, v = period('year', '2025-05-20')
check('the previous year is separate', v == 'R400.00', str(v))

print('\n--- 5. Period edge cases ---')
b, v = period('day', '2026-12-31')
check('the last day of a year works', b.count('Period Report') >= 0 and v == 'R0.00', str(v))
b, _ = period('quarter', '2026-11-15')
check('Q4 rolls into the next year correctly', 'Q4 2026' in b)
b, _ = period('week', '2026-01-01')
check('a week spanning a year boundary works', 'Week of' in b)
b, _ = c.post('/reports', data={'report_type': 'monthly_report', 'period': 'nonsense',
                                'start_date': '2026-05-20',
                                'end_date': '2026-05-20'}).data.decode(), None
check('an invalid period falls back to month', 'May 2026' in b)
check('a missing start date is reported',
      c.post('/reports', data={'report_type': 'monthly_report',
                               'period': 'day'}).status_code == 200)
check('the period selector is on the form',
      'name="period"' in c.get('/reports').data.decode())

# =====================================================================
print('\n--- 6. Average cost per patient ---')
reset()
login()
add('Paracetamol, 500 mg')
today = datetime.utcnow().strftime('%Y-%m-%d')


def visit(patient, qty):
    return c.post('/dispense', data={
        'patient': patient, 'company': 'C', 'position': 'P', 'birth_decade': '1980',
        'gender': 'M', 'outcome': 'Outpatient', 'outcome_days': '', 'diagnoses': 'flu',
        'prescriber': 'Dr X', 'dispenser': 'D', 'date': today,
        'med_names': ['Paracetamol, 500 mg'], 'quantities': [str(qty)]},
        follow_redirects=True)


# 2 patients, 3 visits, R750 total at R2.50/unit
visit('Thabo M', 100)      # R250
visit('Thabo M', 100)      # R250 — same patient, second visit
visit('Palesa N', 100)     # R250
b = c.post('/reports', data={'report_type': 'monthly_report', 'period': 'month',
                             'start_date': today, 'end_date': today}).data.decode()


def prow(body, label):
    mm = _re.search(r'<td>(?:<strong>)?' + _re.escape(label) + r'(?:</strong>)?</td>\s*<td>(.*?)</td>',
                    body, _re.S)
    return _re.sub(r'<[^>]+>', '', mm.group(1)).strip() if mm else None


check('cost per patient is reported', prow(b, 'Average cost per patient') is not None)
check('3 visits recorded', prow(b, 'Visits') == '3', prow(b, 'Visits'))
check('2 distinct patients', prow(b, 'Patients seen') == '2', prow(b, 'Patients seen'))
check('total cost R750.00', prow(b, 'Total cost dispensed') == 'R750.00',
      prow(b, 'Total cost dispensed'))
check('cost per visit is R250.00', prow(b, 'Average cost per visit') == 'R250.00',
      prow(b, 'Average cost per visit'))
check('cost per PATIENT is R375.00, not R250.00',
      prow(b, 'Average cost per patient') == 'R375.00',
      prow(b, 'Average cost per patient'))
check('the two measures are genuinely different',
      prow(b, 'Average cost per visit') != prow(b, 'Average cost per patient'))
check('visits per patient is 1.5', prow(b, 'Visits per patient') == '1.5',
      prow(b, 'Visits per patient'))

dash = c.get('/dashboard').data.decode()
check('the dashboard carries a cost-per-patient tile', 'Cost per patient' in dash)
check('it counts patients over the whole window', 'patient(s) over the period' in dash)

reset()
login()
add('Paracetamol, 500 mg')
b = c.post('/reports', data={'report_type': 'monthly_report', 'period': 'month',
                             'start_date': today, 'end_date': today}).data.decode()
check('no divide-by-zero with no patients',
      prow(b, 'Average cost per patient') == 'R0.00',
      prow(b, 'Average cost per patient'))
check('visits per patient shows a dash', prow(b, 'Visits per patient') == '-')
check('the dashboard handles an empty period',
      c.get('/dashboard').status_code == 200)

print('\n--- The inventory report looks back three months ---')
# Asked for: "inventory must default at 3 months".
login()
_today = A.local_now().date()
_three = A._months_back(_today, 3).strftime('%Y-%m-%d')
_rp = c.get('/reports').data.decode()
check('the form knows the three-month start for the inventory report',
      f'inventory = "{_three}"' in _rp, str([l for l in _rp.splitlines() if 'inventory =' in l]))
check('other reports still start at the first of the month',
      f'value="{_today.replace(day=1):%Y-%m-%d}"' in _rp)
_ir = c.post('/reports', data={'report_type': 'inventory', 'category': 'both'}).data.decode()
check('an inventory report asked for without dates covers three months',
      f'Inventory Report for {_three} to {_today:%Y-%m-%d}' in _ir,
      str(_re.findall(r'Inventory Report for [^<]*', _ir)[:1]) if '_re' in dir() else '')
_ir2 = c.post('/reports', data={'report_type': 'inventory', 'category': 'both',
                                'start_date': '2026-01-01', 'end_date': '2026-01-31'}).data.decode()
check('dates given are used as given', 'Inventory Report for 2026-01-01 to 2026-01-31' in _ir2)

print('\n--- An inventory report for a past period ---')
# The stored balance is today's. A report for an earlier period has to take
# everything since off it, or its balances are today's and its warning blames
# records that are not missing.
reset(); login()
from datetime import datetime as _dt
def _tx(t, q, when):
    db['transactions'].insert_one({'type': t, 'med_name': 'Testamol', 'quantity': q,
        'timestamp': when, 'date': when.strftime('%Y-%m-%d'), 'user': 'x'})
_tx('opening_balance', 200, _dt(2025, 12, 31, 8))
_tx('receive', 300, _dt(2026, 1, 10, 9))
_tx('dispense', 100, _dt(2026, 1, 20, 9))
_tx('dispense', 250, _dt(2026, 5, 5, 9))
_tx('internal_issue', 50, _dt(2026, 9, 5, 9))
db['medications'].insert_one({'name': 'Testamol', 'balance': 100,
                              'category': 'pharmaceutical', 'price': 1})
from test_support import inventory_rows as _rows
_jan = c.post('/reports', data={'report_type': 'inventory', 'category': 'both',
              'start_date': '2026-01-01', 'end_date': '2026-01-31'}).data.decode()
_r = _rows(_jan).get('Testamol') or {}
check('January begins with the 200 on the shelf then', _r.get('Beginning Balance') == '200', str(_r))
check('and ends with the 400 on the shelf then, not today\'s 100',
      _r.get('Current Balance') == '400', str(_r))
check('the movements in January are unchanged',
      _r.get('Received') == '300' and _r.get('Issued') == '100', str(_r))
check('the AMC is January\'s: 100 / 31 x 30 = 97', _r.get('Adjusted AMC') == '97', str(_r))
check('the order is against January\'s shelf: 3 x 97 < 400, so 0',
      _r.get('Amount to Order') == '0', str(_r))
check('the row is not flagged as unexplained', '&#9888;' not in _jan.split('Testamol')[1][:200])
check('the page says the balances are as they stood then',
      'Balances are as they stood at the end of 2026-01-31' in _jan)
_now = c.post('/reports', data={'report_type': 'inventory', 'category': 'both'}).data.decode()
check('a report to today still uses today\'s shelf',
      (_rows(_now).get('Testamol') or {}).get('Current Balance') == '100')
check('and carries no such note', 'Balances are as they stood' not in _now)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
