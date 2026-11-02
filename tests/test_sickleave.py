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


def login(role='admin', name='Admin User'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions']:
        db[col].delete_many({})


NOW = datetime.utcnow()
M_START = NOW.strftime('%Y-%m-01')
M_END = NOW.strftime('%Y-%m-%d')


def add(name, bal=10000):
    meds.insert_one({'name': name, 'balance': bal, 'batch': 'B', 'price': 2.5,
                     'pack_size': 100, 'pack_price': 250.0, 'expiry_date': '2030-01-01',
                     'schedule': 'not controlled'})


def visit(patient, pairs, outcome='Sick leave', days='3', company='Acme Mine',
          dx='influenza', when=None):
    return c.post('/dispense', data={
        'patient': patient, 'company': company, 'position': 'Miner',
        'birth_decade': '1980', 'gender': 'M', 'outcome': outcome,
        'outcome_days': days, 'diagnoses': dx, 'prescriber': 'Dr Khoele',
        'dispenser': 'S. Mokoena',
        'date': (when or NOW).strftime('%Y-%m-%d'),
        'med_names': [p[0] for p in pairs],
        'quantities': [str(p[1]) for p in pairs]}, follow_redirects=True)


def rep(**kw):
    d = {'report_type': 'sick_leave', 'start_date': M_START, 'end_date': M_END}
    d.update(kw)
    return c.post('/reports', data=d).data.decode()


def rows(body):
    seg = body[body.index('Sick Leave Register for'):]
    seg = seg[:seg.index('</table>')]
    out = [[_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<td>(.*?)</td>', m, _re.S)]
           for m in _re.findall(r'<tr>(.*?)</tr>', seg, _re.S)]
    return [r for r in out if r]        # drop the header row


print('\n--- 1. The register ---')
reset()
login()
add('Paracetamol tabs, 500 mg')
add('Ibuprofen Tab, 400 mg')
visit('Thabo Mokoena', [('Paracetamol tabs, 500 mg', 20)], days='3')
body = rep()
check('the report renders', 'Sick Leave Register for' in body)
r = rows(body)[0]
print('   row:', r)
check('the date is shown', r[0] == NOW.strftime('%Y-%m-%d'), r[0])
check('the name is shown', r[1] == 'Thabo Mokoena', r[1])
check('the company is shown', r[2] == 'Acme Mine', r[2])
check('the days are shown', r[3] == '3', r[3])
check('the diagnosis is shown', r[4] == 'influenza', r[4])
check('the prescriber is shown', r[5] == 'Dr Khoele', r[5])
check('the dispenser is shown', r[6] == 'S. Mokoena', r[6])

print('\n--- 2. One line per visit, not per medication ---')
visit('Palesa Nthako', [('Paracetamol tabs, 500 mg', 20),
                        ('Ibuprofen Tab, 400 mg', 10)], days='5')
body = rep()
check('a three-item visit is not printed three times',
      len(rows(body)) == 2, str(len(rows(body))))
check('the multi-item visit shows its own days',
      any(x[1] == 'Palesa Nthako' and x[3] == '5' for x in rows(body)))

print('\n--- 3. Only sick leave appears ---')
visit('Referred Person', [('Paracetamol tabs, 500 mg', 5)], outcome='Referral', days='')
visit('Ordinary Person', [('Paracetamol tabs, 500 mg', 5)], outcome='Outpatient', days='')
visit('Admitted Person', [('Paracetamol tabs, 500 mg', 5)], outcome='Admission', days='4')
body = rep()
names = [x[1] for x in rows(body)]
check('referrals are excluded', 'Referred Person' not in names)
check('outpatients are excluded', 'Ordinary Person' not in names)
check('admissions are excluded', 'Admitted Person' not in names, str(names))
check('only the two sick-leave visits remain', len(names) == 2, str(names))

print('\n--- 4. Summary ---')
check('certificate count', '<td>2</td>' in body)
check('total days is 8', '<td>8</td>' in body)
check('average days is 4.0', '4.0' in body)
check('longest is 5', '<td>5</td>' in body)

print('\n--- 5. Repeat certificates ---')
visit('Thabo Mokoena', [('Paracetamol tabs, 500 mg', 20)], days='2')
body = rep()
check('a repeat visit is listed again', len(rows(body)) == 3)
check('repeat patients are flagged', 'More Than One Certificate' in body)
seg = body[body.index('More Than One Certificate'):]
check('the repeat table names the patient', 'Thabo Mokoena' in seg)
check('and totals their certificates and days',
      '<td>2</td>' in seg and '<td>5</td>' in seg, seg[:300])
check('a patient with one certificate is not flagged',
      'Palesa Nthako' not in seg)

print('\n--- 6. Dates, search and edges ---')
last = (NOW.replace(day=1) - timedelta(days=5))
txs.insert_one({'type': 'dispense', 'transaction_id': 'TX-OLD', 'patient': 'Last Month',
                'company': 'Old Co', 'outcome': 'Sick leave', 'outcome_days': 7,
                'diagnoses': ['back pain'], 'prescriber': 'Dr X', 'dispenser': 'D',
                'med_name': 'Paracetamol tabs, 500 mg', 'quantity': 5,
                'date': last.strftime('%Y-%m-%d'), 'user': 'u', 'timestamp': last})
check('this month excludes last month',
      'Last Month' not in rep())
body = rep(start_date=last.strftime('%Y-%m-01'), end_date=last.strftime('%Y-%m-%d'))
check('last month shows its own certificate', 'Last Month' in body)
check('a list-type diagnosis renders as text', 'back pain' in body)
check('search narrows by patient', 'Thabo Mokoena' in rep(search='Thabo')
      and 'Palesa Nthako' not in rep(search='Thabo'))
check('search narrows by company', 'Thabo Mokoena' in rep(search='Acme'))
check('missing dates do not crash',
      c.post('/reports', data={'report_type': 'sick_leave'}).status_code == 200)
reset()
login()
body = rep()
check('an empty period says so', 'No sick leave was recorded' in body)
check('and shows no summary', '<h3>Summary</h3>' not in body)
login('user', 'Pharm')
check('a non-admin can run the register', 'Sick Leave Register for' in rep())

print('\n--- Days belong to sick leave only ---')
reset()
login()
add('Paracetamol')

# The field is hidden for every other outcome, but hiding does not clear: pick
# Sick leave, type 3, change to Referral, and the browser still posts 3. The
# server must drop it, or a referral carries sick-leave days.
visit('Stale Value', [('Paracetamol', 10)], outcome='Referral', days='3')
t = txs.find_one({'patient': 'Stale Value'})
check('a referral posted with days stores none', t.get('outcome_days') is None,
      str(t.get('outcome_days')))
check('but the outcome itself is kept', t['outcome'] == 'Referral')

visit('Admitted', [('Paracetamol', 10)], outcome='Admission', days='5')
check('an admission posted with days stores none',
      txs.find_one({'patient': 'Admitted'}).get('outcome_days') is None)

visit('Walk In', [('Paracetamol', 10)], outcome='Outpatient', days='2')
check('an outpatient posted with days stores none',
      txs.find_one({'patient': 'Walk In'}).get('outcome_days') is None)

visit('Signed Off', [('Paracetamol', 10)], outcome='Sick leave', days='4')
check('sick leave still stores its days',
      txs.find_one({'patient': 'Signed Off'})['outcome_days'] == 4)

body = rep()
check('only the sick leave visit reaches the register',
      'Signed Off' in body and 'Stale Value' not in body
      and 'Admitted' not in body)

print('\n--- The form shows the field only for sick leave ---')
login()
form = c.get('/dispense').data.decode()
check('the days field is hidden on a fresh form',
      _re.search(r'id="outcome-days-field"[^>]*style="display:none;"', form) is not None,
      form[form.find('outcome-days-field') - 20:][:140]
      if 'outcome-days-field' in form else 'field not found')
check('its label names sick leave', 'Days of Sick Leave' in form)
check('the old combined label is gone', 'sick leave / admission' not in form)
check('the outcome select drives the toggle',
      'onchange="toggleOutcomeDays()"' in form)
check('the toggle clears the box as it hides it',
      'input.value = \'\'' in form)
check('the toggle runs on load, so a reload is consistent',
      "DOMContentLoaded', toggleOutcomeDays" in form)
_cf = form[form.index('function clearForm'):]
_cf = _cf[:_cf.index('\n}')]
check('clearing the form re-hides the field',
      'toggleOutcomeDays();' in _cf,
      'clearForm blanks the outcome select, so the field must hide with it')

# Editing a sick-leave record must show the days immediately, before any
# script runs, or the days look lost.
tid = txs.find_one({'patient': 'Signed Off'})['transaction_id']
edit = c.get(f'/dispense?edit={tid}').data.decode()
if 'outcome-days-field' in edit and 'value="4"' in edit:
    check('editing a sick-leave visit shows the days field open',
          _re.search(r'id="outcome-days-field"[^>]*style="display:none;"', edit) is None,
          'the field must not start hidden when it already holds days')
    check('and the recorded days are in the box', 'value="4"' in edit)
else:
    check('editing a sick-leave visit shows the days field open',
          False, 'edit view did not render the field')
    check('and the recorded days are in the box', False, 'not rendered')

tid2 = txs.find_one({'patient': 'Stale Value'})['transaction_id']
edit2 = c.get(f'/dispense?edit={tid2}').data.decode()
check('editing a referral starts with the field hidden',
      _re.search(r'id="outcome-days-field"[^>]*style="display:none;"', edit2) is not None)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
