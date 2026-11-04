"""Stock on hand while dispensing.

Asked for: "what if we were able to see the stock on hand for the item even
as we were dispensing it". The form asks /api/stock each time an item is
picked and shows the balance under it, warning when the quantity is more than
the shelf holds.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
from test_support import arm_csrf, page_source
db = _f['pharmacy_db']
c = A.app.test_client()
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='employee', name='N. Nurse', department=None):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'n', 'role': role}
        if department:
            s['user']['department'] = department
        s['last_active'] = datetime.now(timezone.utc).isoformat()


today = A.local_now().date()
db['medications'].insert_many([
    {'name': 'Paracetamol tabs, 500 mg', 'balance': 500, 'expiry_date': '2029-03-31'},
    {'name': 'Morphine tabs, 10mg', 'balance': 59,
     'expiry_date': (today + timedelta(days=40)).isoformat()},
    {'name': 'Ringers lactate 1L', 'balance': 0, 'expiry_date': '2028-01-01'},
    {'name': 'Old syrup', 'balance': 12, 'expiry_date': (today - timedelta(days=5)).isoformat()},
    {'name': 'Half tabs', 'balance': 7.0},
])

print('\n--- 1. The lookup ---')
login()
j = c.get('/api/stock?name=Paracetamol tabs, 500 mg').get_json()
check('an item on file gives its balance', j == {'found': True, 'balance': 500,
      'expiry': '2029-03-31', 'expiry_days': (datetime(2029, 3, 31).date() - today).days}, str(j))
j = c.get('/api/stock?name=Morphine tabs, 10mg').get_json()
check('and how many days to its expiry', j['balance'] == 59 and j['expiry_days'] == 40, str(j))
check('an expired item says so by a negative count',
      c.get('/api/stock?name=Old syrup').get_json()['expiry_days'] == -5)
check('an empty shelf is 0', c.get('/api/stock?name=Ringers lactate 1L').get_json()['balance'] == 0)
check('a whole number stored as 7.0 reads 7', c.get('/api/stock?name=Half tabs').get_json()['balance'] == 7)
check('an item with no expiry gives none', c.get('/api/stock?name=Half tabs').get_json()['expiry'] is None)
check('a name not on file is not found', c.get('/api/stock?name=Panado').get_json() == {'found': False})
check('no name is not found', c.get('/api/stock').get_json() == {'found': False})
db['medications'].update_one({'name': 'Morphine tabs, 10mg'}, {'$inc': {'balance': -9}})
check('it is the balance now, not when the page loaded',
      c.get('/api/stock?name=Morphine tabs, 10mg').get_json()['balance'] == 50)

print('\n--- 2. Who may ask ---')
with c.session_transaction() as s:
    s.clear()
r = c.get('/api/stock?name=Paracetamol tabs, 500 mg')
check('signed out, it is refused', r.status_code == 302)
login('department', 'Sr. Mokoena', 'Lab')
r = c.get('/api/stock?name=Paracetamol tabs, 500 mg')
check('a department login cannot read the pharmacy stock',
      r.status_code == 302 and r.headers['Location'].endswith('/requests'))

print('\n--- 3. The dispensing form uses it ---')
src = page_source('dispense.html')
check('the form asks for the stock of the item picked', "fetch('/api/stock?name='" in src)
check('and shows it under the medication box', "'On hand: '" in src and 'med-stock' in src)
check('it warns when more is asked for than the shelf holds',
      'will be recorded as unmet' in src and 'Out of stock' in src)
check('it adds up the same item on two lines', 'wanted +=' in src)
check('it warns of an item expiring or expired', 'EXPIRED' in src and 'expires ' in src)
check('it is told when it is an edit, where the visit is already off the shelf',
      'this visit is already off it' in src)
check('clearing the form clears the notes', ".med-stock').forEach" in src)
check('a restored draft looks its stock up too', 'showAllStock();' in src)
login()
page = c.get('/dispense').data.decode()
check('the dispensing page renders with it', page.count('const EDITING = false') == 1)

print(f"\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========")
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
