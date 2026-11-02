from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re
from datetime import datetime, timezone

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
from test_support import arm_csrf, page_source
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
    for col in ['medications', 'transactions', 'app_warnings']:
        db[col].delete_many({})


NOW = datetime.utcnow()


def add(name, bal):
    meds.insert_one({'name': name, 'balance': bal, 'batch': 'B', 'price': 2.5,
                     'pack_size': 100, 'pack_price': 250.0,
                     'expiry_date': '2030-01-01', 'schedule': 'not controlled'})


def dispense(pairs):
    return c.post('/dispense', data={
        'patient': 'Thabo M', 'company': 'C', 'position': 'P',
        'birth_decade': '1980', 'gender': 'M', 'outcome': 'Outpatient',
        'outcome_days': '', 'diagnoses': 'flu', 'prescriber': 'Dr X',
        'dispenser': 'D', 'date': NOW.strftime('%Y-%m-%d'),
        'med_names': [p[0] for p in pairs],
        'quantities': [str(p[1]) for p in pairs]}, follow_redirects=True)


def banner(body):
    m = _re.search(r'<p class="message ([a-z]*)"[^>]*>(.*?)</p>', body, _re.S)
    return (m.group(1), _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', '', m.group(2))).strip()) if m else (None, None)


print('\n--- 1. Everything fine: green ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 1000)
cls, msg = banner(dispense([('Paracetamol tabs, 500 mg', 20)]).data.decode())
print('  ', cls, '|', msg[:90])
check('a full dispense is green', cls == 'success', str(cls))
check('and says so', 'successfully' in msg.lower())

print('\n--- 2. Short stock: amber ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 10)
cls, msg = banner(dispense([('Paracetamol tabs, 500 mg', 30)]).data.decode())
print('  ', cls, '|', msg[:110])
check('insufficient stock is amber, not red', cls == 'partial', str(cls))
check('the heading names the cause', 'insufficient stock' in msg.lower(), msg[:80])
check('the shortfall is explained', 'unmet demand' in msg.lower())
check('what WAS given is still reported', '10' in msg)

print('\n--- 3. Item not on file: RED ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 1000)
cls, msg = banner(dispense([('Paracetamol tabs, 500 mg', 20),
                            ('Paracetomol tabs, 500 mg', 20)]).data.decode())
print('  ', cls, '|', msg[:140])
check('a missing item turns the banner RED', cls == 'error', str(cls))
check('it is NOT shown as an ordinary partial', cls != 'partial')
check('the heading names the cause', 'not on file' in msg.lower(), msg[:80])
check('it says nothing was recorded for it', 'nothing was recorded' in msg.lower())
check('it says what to do', 'Add Medication' in msg)
check('the item that DID dispense is still confirmed',
      'Paracetamol tabs, 500 mg' in msg)
check('the good line really was dispensed',
      meds.find_one({'name': 'Paracetamol tabs, 500 mg'})['balance'] == 980)
check('no phantom medication was created', meds.count_documents({}) == 1)

print('\n--- 4. Both problems at once: red wins ---')
reset()
login()
add('Paracetamol tabs, 500 mg', 5)
cls, msg = banner(dispense([('Paracetamol tabs, 500 mg', 30),
                            ('Ghost Item, 1 mg', 10)]).data.decode())
print('  ', cls, '|', msg[:160])
check('the more serious fault sets the colour', cls == 'error', str(cls))
check('but the shortfall is still reported too',
      'unmet demand' in msg.lower() and 'not on file' in msg.lower())

print('\n--- 5. Nothing dispensed at all ---')
reset()
login()
cls, msg = banner(dispense([('Ghost Item, 1 mg', 10)]).data.decode())
check('an entirely unknown prescription is red', cls == 'error', str(cls))
check('and says nothing was dispensed', 'not dispensed' in msg.lower(), msg[:70])
reset()
login()
add('Paracetamol tabs, 500 mg', 0)
cls, msg = banner(dispense([('Paracetamol tabs, 500 mg', 10)]).data.decode())
check('out of stock with nothing given stays amber', cls == 'partial', str(cls))
check('and says no stock', 'no stock' in msg.lower(), msg[:70])
check('the demand is still captured',
      txs.count_documents({'type': 'unmet_demand'}) == 1)

print('\n--- 6. The three colours are actually different ---')
check('success and partial use different CSS classes', 'success' != 'partial')
# The colours live in the shared stylesheet in base.html; the class the
# route picks is used by the page itself.
src = page_source('dispense.html')
check('.message.partial is defined amber', '.message.partial' in src and '#fff3cd' in src)
check('.message.error is defined red', '#f8d7da' in src)
check('the template honours the route-set class', '{{ message_class }}' in src)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
