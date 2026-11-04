"""Department requests: a department posts, the pharmacy serves.

Asked for: "the request page where departments can post their request and
then what I do is just to serve them". Department accounts were chosen over
open access, and a shortfall for want of stock is to count as unmet demand.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
from werkzeug.security import generate_password_hash
from test_support import arm_csrf, inventory_rows
db = _f['pharmacy_db']
reqs, meds, txs, users = (db['dept_requests'], db['medications'],
                          db['transactions'], db['users'])
c = A.app.test_client()
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def as_user(name='Letlotlo Hlaoli', role='admin', department=None):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': name.lower().replace(' ', ''), 'role': role}
        if department:
            s['user']['department'] = department
        s['last_active'] = datetime.now(timezone.utc).isoformat()


def as_ward(dept='Emergency Department', name='Sr. Mokoena'):
    as_user(name, 'department', dept)


def reset():
    for col in ['dept_requests', 'medications', 'transactions', 'audit_log',
                'users', 'key_register']:
        db[col].delete_many({})
    for name, bal, price in [('Paracetamol tabs, 500 mg', 500, 0.18),
                             ('Gauze swabs', 40, 1.5),
                             ('Ringers lactate 1L', 0, 22.0)]:
        meds.insert_one({'name': name, 'balance': bal, 'price': price,
                         'category': 'pharmaceutical', 'batch': 'B1',
                         'expiry_date': '2029-01-01'})


def post(lines, note='', dept=None):
    data = {'action': 'post', 'note': note,
            'med_name': [n for n, _ in lines], 'quantity': [str(q) for _, q in lines]}
    if dept:
        data['department'] = dept
    return c.post('/requests', data=data, follow_redirects=True)


def last_req():
    return reqs.find_one(sort=[('requested_at', -1)])


def serve(doc, gives, received_by='Sr. Mokoena', note=''):
    data = {'action': 'serve', 'request_id': str(doc['_id']),
            'received_by': received_by, 'serve_note': note}
    for i, g in enumerate(gives):
        data[f'give__{i}'] = str(g)
    return c.post('/requests', data=data, follow_redirects=True)


def bal(name):
    return meds.find_one({'name': name})['balance']


def banner(html):
    m = _re.search(r'<p class="message[^"]*"[^>]*>(.*?)</p>', html, _re.S)
    return m.group(1) if m else ''


# =====================================================================
print('\n--- 1. A department account ---')
reset()
with c.session_transaction() as s:
    s['admin_access'] = True
r = c.post('/register', data={'username': 'ed', 'password': 'pw123456', 'name': 'Emergency',
                              'role': 'department', 'department': ''}, follow_redirects=True)
check('a department account must name its department',
      users.find_one({'username': 'ed'}) is None and 'Choose which department' in r.data.decode())
with c.session_transaction() as s:
    s['admin_access'] = True
c.post('/register', data={'username': 'ed', 'password': 'pw123456', 'name': 'Emergency Dept',
                          'role': 'department', 'department': 'Emergency Department'})
u = users.find_one({'username': 'ed'})
check('it is stored with its department', u and u['department'] == 'Emergency Department'
      and u['role'] == 'department', str(u))
with c.session_transaction() as s:
    s['admin_access'] = True
c.post('/register', data={'username': 'nurse', 'password': 'pw123456', 'name': 'N. Nurse',
                          'role': 'employee', 'department': 'Lab'})
check('an ordinary account is not tied to a department',
      users.find_one({'username': 'nurse'})['department'] == '')
check('the registration page offers the department role',
      'Department (requests only)' in A.app.jinja_env.get_template('register.html')
      .render(departments=A.REQUEST_DEPARTMENTS, csrf_token=lambda: 'x'))

with c.session_transaction() as s:
    s.clear()
r = c.post('/login', data={'username': 'ed', 'password': 'pw123456'})
check('a department logs in straight to its requests',
      r.status_code == 302 and r.headers['Location'].endswith('/requests'), r.headers.get('Location'))
check('and its session knows its department',
      c.get('/requests').data.decode().count('Emergency Department') >= 1)
check('a department login does not touch the store key register',
      db['key_register'].count_documents({}) == 0)

print('\n--- 2. A department sees its requests and nothing else ---')
for path in ['/dispense', '/reports', '/receive', '/internal-issue', '/dashboard',
             '/key-register', '/loans', '/temperature', '/audit', '/stock-take',
             '/api/patient-lookup?q=a', '/backup', '/']:
    r = c.get(path)
    check(f'{path} sends a department back to its requests',
          r.status_code == 302 and r.headers['Location'].endswith('/requests'),
          f"{r.status_code} {r.headers.get('Location')}")
r = c.post('/dispense', data={'patient': 'x'})
check('a POST to dispensing is turned away too',
      r.status_code == 302 and r.headers['Location'].endswith('/requests'))
page = c.get('/requests').data.decode()
check('its menu has only Requests and Logout',
      'href="/requests"' in page and 'href="/dispense"' not in page
      and 'href="/reports"' not in page and 'href="/logout"' in page)
check('it can still use the item suggestions', c.get('/api/medications').status_code == 200)
check('and it sees no stock balances', 'On hand' not in page and '>500<' not in page)

print('\n--- 3. Posting a request ---')
r = post([('Paracetamol tabs, 500 mg', 100), ('Gauze swabs', 20)], note='weekend')
d = last_req()
check('the request is saved for the department', d and d['department'] == 'Emergency Department'
      and d['status'] == 'Pending' and len(d['lines']) == 2, str(d))
check('with who asked, and the note', d['requested_by'] == 'Emergency Dept' and d['note'] == 'weekend')
check('the department is told it was sent', 'sent to the pharmacy with 2 items' in banner(r.data.decode()))
check('posting moves no stock', bal('Paracetamol tabs, 500 mg') == 500 and txs.count_documents({}) == 0)
check('and it is audited', db['audit_log'].find_one({'target_type': 'dept_request',
                                                     'action': 'CREATE'}) is not None)
n = reqs.count_documents({})
r = post([('Paracetamol tabs, 500 mg', 10), ('Panado syrup', 5)])
check('an item not on the pharmacy list stops the whole request',
      reqs.count_documents({}) == n and 'not on the pharmacy list' in banner(r.data.decode()))
r = post([('Gauze swabs', 5), ('Gauze swabs', 5)])
check('the same item twice is refused', reqs.count_documents({}) == n
      and 'twice' in banner(r.data.decode()))
r = post([('Gauze swabs', 0)])
check('a zero quantity is refused', reqs.count_documents({}) == n)
r = post([('Gauze swabs', 'two')])
check('a quantity in words is refused', reqs.count_documents({}) == n
      and 'whole number' in banner(r.data.decode()))
r = post([('', '')])
check('an empty request is refused', reqs.count_documents({}) == n
      and 'at least one item' in banner(r.data.decode()))
r = post([('Gauze swabs', 3)], dept='Lab')
check('a department cannot post as another department',
      last_req()['department'] == 'Emergency Department')
page = c.get('/requests').data.decode()
check('its requests are listed with their status', 'Pending' in page and d['ref'] in page)

as_ward('Lab', 'Lab Tech')
post([('Gauze swabs', 4)])
lab_ref = last_req()['ref']
page = c.get('/requests').data.decode()
check("a department sees only its own requests", lab_ref in page and d['ref'] not in page)
r = c.post('/requests', data={'action': 'cancel', 'request_id': str(d['_id'])}, follow_redirects=True)
check("and cannot cancel another department's",
      reqs.find_one({'_id': d['_id']})['status'] == 'Pending')
r = serve(reqs.find_one({'ref': lab_ref}), [4])
check('a department cannot serve, even its own',
      reqs.find_one({'ref': lab_ref})['status'] == 'Pending' and bal('Gauze swabs') == 40)
c.post('/requests', data={'action': 'cancel', 'request_id': str(reqs.find_one({'ref': lab_ref})['_id'])})
check('it can cancel its own while it is waiting',
      reqs.find_one({'ref': lab_ref})['status'] == 'Cancelled')

print('\n--- 4. The pharmacy queue ---')
as_user()
page = c.get('/requests').data.decode()
check('the menu shows how many are waiting', 'Requests <strong>(' in page)
n_wait = reqs.count_documents({'status': 'Pending'})
check('and the count is right', f'Requests <strong>({n_wait})</strong>' in page, str(n_wait))
check('a waiting request shows what is on hand', 'On hand' in page and '>500<' in page)
check('Give is filled in with what can be supplied',
      'name="give__0" value="100"' in page and 'name="give__1" value="20"' in page)
check('cancelled requests are not in the queue', f'id="{lab_ref}"' not in page)
check('the nav count is on other pages too',
      f'Requests <strong>({n_wait})</strong>' in c.get('/internal-issue').data.decode())

print('\n--- 5. Serving in full ---')
d = reqs.find_one({'_id': d['_id']})
r = serve(d, [100, 20])
d2 = reqs.find_one({'_id': d['_id']})
check('the request is marked served', d2['status'] == 'Served', d2['status'])
check('stock comes off the shelf', bal('Paracetamol tabs, 500 mg') == 400 and bal('Gauze swabs') == 20)
iss = list(txs.find({'type': 'internal_issue', 'request_ref': d['ref']}))
check('each line is an internal issue to the department',
      len(iss) == 2 and all(t['department'] == 'Emergency Department' for t in iss))
check('naming who collected and who served',
      all(t['issued_to'] == 'Sr. Mokoena' and t['user'] == 'Letlotlo Hlaoli' for t in iss))
check('carrying the request reference', all(t['issue_id'] == d['ref'] for t in iss))
check('nothing is unmet when all was given', txs.count_documents({'type': 'unmet_demand'}) == 0)
check('the message says it was served', 'served (120 of 120 units)' in banner(r.data.decode()))
check('the department sees what it was given',
      d2['lines'][0]['given'] == 100 and d2['received_by'] == 'Sr. Mokoena')
r = serve(d2, [100, 20])
check('serving it again is refused and moves nothing',
      bal('Paracetamol tabs, 500 mg') == 400 and 'no longer waiting' in banner(r.data.decode()))
inv = inventory_rows(c.post('/reports', data={'report_type': 'inventory', 'category': 'both'}).data.decode())
check('the inventory report counts it as issued',
      inv.get('Paracetamol tabs, 500 mg', {}).get('Issued') == '100', str(inv.get('Paracetamol tabs, 500 mg')))

print('\n--- 6. Short of stock ---')
as_ward()
post([('Gauze swabs', 50), ('Ringers lactate 1L', 6), ('Paracetamol tabs, 500 mg', 30)])
d = last_req()
as_user()
page = c.get('/requests').data.decode()
check('Give is capped at what is on hand',
      'name="give__0" value="20"' in page and 'name="give__1" value="0"' in page)
r = serve(d, [21, 0, 30])
check('giving more than is on hand is refused, and nothing moves',
      bal('Gauze swabs') == 20 and bal('Paracetamol tabs, 500 mg') == 400
      and reqs.find_one({'_id': d['_id']})['status'] == 'Pending'
      and 'only 20 on hand' in banner(r.data.decode()), banner(r.data.decode()))
r = serve(d, [20, 0, 31])
check('giving more than was asked for is refused',
      'more than the 30 asked for' in banner(r.data.decode()) and bal('Gauze swabs') == 20)
r = serve(d, [20, 0, 10])
d2 = reqs.find_one({'_id': d['_id']})
check('served in part', d2['status'] == 'Part served', d2['status'])
um = {t['med_name']: t for t in txs.find({'type': 'unmet_demand', 'request_ref': d['ref']})}
check('what the shelf could not cover is unmet demand',
      um.get('Gauze swabs', {}).get('shortfall') == 30
      and um.get('Ringers lactate 1L', {}).get('shortfall') == 6, str(um))
check('holding back when stock is there is not unmet demand',
      'Paracetamol tabs, 500 mg' not in um)
check('unmet demand never moves stock', all(t['quantity'] == 0 for t in um.values()))
check('it records the department', um['Gauze swabs']['department'] == 'Emergency Department')
check('the message names the shortfalls',
      'unmet demand' in banner(r.data.decode()) and 'Gauze swabs 30 short' in banner(r.data.decode()))
check('the line records given and short',
      d2['lines'][0]['given'] == 20 and d2['lines'][0]['unmet'] == 30)
today = A.local_now().strftime('%Y-%m-%d')
rep = c.post('/reports', data={'report_type': 'unmet_demand', 'start_date': today,
                               'end_date': today, 'category': 'both'}).data.decode()
check('the unmet demand report shows it against the department',
      'Emergency Department (request)' in rep)
inv = inventory_rows(c.post('/reports', data={'report_type': 'inventory', 'category': 'both'}).data.decode())
g = inv.get('Gauze swabs', {})
# 20 + 20 issued, 30 unmet, over the 3-month default
check('and the shortfall raises the Adjusted AMC the order is worked from',
      int(g.get('Adjusted AMC', 0)) > 0 and int(g.get('Amount to Order', 0)) > 0, str(g))

as_ward()
post([('Ringers lactate 1L', 4)])
d = last_req()
as_user()
serve(d, [0])
check('nothing on the shelf at all is "Not supplied"',
      reqs.find_one({'_id': d['_id']})['status'] == 'Not supplied')

print('\n--- 7. Declining, and a missing collector ---')
as_ward()
post([('Gauze swabs', 2)])
d = last_req()
as_user()
r = serve(d, [2], received_by=' ')
check('a request cannot be served without naming who collects it',
      reqs.find_one({'_id': d['_id']})['status'] == 'Pending' and bal('Gauze swabs') == 0 + 0
      or reqs.find_one({'_id': d['_id']})['status'] == 'Pending')
r = c.post('/requests', data={'action': 'decline', 'request_id': str(d['_id']), 'reason': ''},
           follow_redirects=True)
check('declining needs a reason', reqs.find_one({'_id': d['_id']})['status'] == 'Pending')
c.post('/requests', data={'action': 'decline', 'request_id': str(d['_id']),
                          'reason': 'Use the ward stock first'})
d2 = reqs.find_one({'_id': d['_id']})
check('declined, with the reason kept', d2['status'] == 'Declined'
      and d2['decline_reason'] == 'Use the ward stock first')
as_ward()
page = c.get('/requests').data.decode()
check('the department sees the reason', 'Use the ward stock first' in page)
c.post('/requests', data={'action': 'cancel', 'request_id': str(d['_id'])})
check('a request dealt with cannot then be cancelled',
      reqs.find_one({'_id': d['_id']})['status'] == 'Declined')

print('\n--- 8. Posting for a department that phoned ---')
as_user()
post([('Paracetamol tabs, 500 mg', 12)], dept='PHC')
d = last_req()
check('staff can post for a department', d['department'] == 'PHC' and d['requested_by'] == 'Letlotlo Hlaoli')
n = reqs.count_documents({})
post([('Paracetamol tabs, 500 mg', 12)])
check('but must say which', reqs.count_documents({}) == n)
page = c.get('/requests?department=PHC').data.decode()
check('the history filters by department', 'Dealt With' in page)

print('\n--- 9. An ordinary staff login is unaffected ---')
as_user('N. Nurse', 'employee')
check('a staff login still reaches dispensing', c.get('/dispense').status_code == 200)
check('and can serve requests', 'name="action" value="serve"' in c.get('/requests').data.decode())

print('\n--- 10. Times are the pharmacy clock, not UTC ---')
as_user()
reqs.insert_one({'ref': 'REQ-T', 'department': 'Lab', 'requested_by': 'x',
                 'requested_at': datetime(2026, 11, 4, 4, 24), 'status': 'Pending',
                 'lines': [{'med_name': 'Gauze swabs', 'requested': 1, 'given': None, 'unmet': None}]})
page = c.get('/requests').data.decode()
check('04:24 UTC shows as 06:24', '2026-11-04 06:24' in page and '04:24' not in page)
check('and no UTC label is left on the page', 'UTC' not in page)

print('\n--- 11. The emergency trolley tops up to its standard list ---')
# Asked for: "just the top up requests. for the required quantities, use your
# best estimates".
reset()
std = db['dept_standards']; std.delete_many({})
for name in ['Adrenaline 1mg/ml inj', 'Atropine 1mg/ml inj', 'Hydrocortisone 100mg inj',
             'Lignocaine 2% 20ml inj', 'Lignocaine 10% spray', 'Water for injection 10ml']:
    meds.insert_one({'name': name, 'balance': 50, 'price': 2.0, 'category': 'pharmaceutical'})
as_ward('Emergency Trolley', 'Sr. Kutoane')
page = c.get('/requests').data.decode()
rows = list(std.find({'department': 'Emergency Trolley'}).sort('order', 1))
check('the trolley list is seeded on first use', len(rows) == len(A.TROLLEY_STANDARD_SEED), str(len(rows)))
check('the trolley page shows Top Up to Standard', 'Top Up to Standard' in page and 'Should hold' in page)
by = {r['item']: r for r in rows}
check('an item with exactly one match is linked to it',
      by['Adrenalin 1 mg']['med_name'] == 'Adrenaline 1mg/ml inj'
      and by['Water for injection']['med_name'] == 'Water for injection 10ml')
check('an item with no match is left unlinked', by['Amiodarone']['med_name'] is None)
check('a search finds its words in any order',
      by['Xylocaine spray']['med_name'] == 'Lignocaine 10% spray'
      and by['Lignocaine 400 mg']['med_name'] == 'Lignocaine 2% 20ml inj')
check('the estimate quantities are as seeded',
      by['Adrenalin 1 mg']['required'] == 10 and by['Ambubag']['required'] == 1
      and by['Warfarin']['required'] == 0)
check('the trolley is told which lines are not on the pharmacy list',
      'not on the pharmacy list' in page)


def topup(have, note=''):
    data = {'action': 'topup', 'note': note}
    for r in std.find({'department': 'Emergency Trolley'}):
        v = have.get(r['item'], have.get('*'))
        if v is not None:
            data[f"have__{r['_id']}"] = str(v)
    return c.post('/requests', data=data, follow_redirects=True)


n = reqs.count_documents({})
r = topup({'Adrenalin 1 mg': 1})
check('a top-up with lines not counted is not sent', reqs.count_documents({}) == n
      and 'not counted' in banner(r.data.decode()), banner(r.data.decode())[:200])
r = topup({'*': 'x'})
check('a count in words is refused', reqs.count_documents({}) == n)
# November's sheet: adrenalin 1, atropine 3, hydrocortisone 1, lignocaine 2,
# water for injection 0; everything else at its standard
counts = {r2['item']: r2['required'] for r2 in rows}
counts.update({'Adrenalin 1 mg': 1, 'Atropine': 3, 'Hydrocortisone 100 mg': 1,
               'Lignocaine 400 mg': 2, 'Water for injection': 0, 'Amiodarone': 0})
r = topup(counts, note='after a resus')
d = last_req()
got = {l['med_name']: l['requested'] for l in d['lines']}
check('the request is the difference to standard',
      got == {'Adrenaline 1mg/ml inj': 9, 'Atropine 1mg/ml inj': 2,
              'Hydrocortisone 100mg inj': 4, 'Lignocaine 2% 20ml inj': 1,
              'Water for injection 10ml': 10}, str(got))
check('an item at its standard is not requested', 'Lignocaine 10% spray' not in got)
check('a short item not on the pharmacy list goes in the note',
      'Amiodarone x6' in d['note'] and 'after a resus' in d['note'], d['note'])
check('it is marked as a top-up, with the counts kept',
      d.get('kind') == 'topup' and d['topup_counts']['Adrenalin 1 mg'] == 1)
check('the trolley is told what was sent', 'to bring Emergency Trolley up to standard' in banner(r.data.decode()))
check('and the pharmacy serves it like any request',
      d['status'] == 'Pending' and d['department'] == 'Emergency Trolley')
as_user()
page = c.get('/requests').data.decode()
check('it is in the pharmacy queue with Give filled in',
      d['ref'] in page and 'Top-up to standard list' in page)
serve(d, [l['requested'] for l in d['lines']], received_by='Sr. Kutoane')
check('serving it issues the stock to the trolley',
      reqs.find_one({'_id': d['_id']})['status'] == 'Served'
      and txs.count_documents({'type': 'internal_issue', 'department': 'Emergency Trolley'}) == 5)

as_ward('Emergency Trolley', 'Sr. Kutoane')
full = {r2['item']: r2['required'] for r2 in rows}
n = reqs.count_documents({})
r = topup(full)
check('a trolley already at standard sends nothing',
      reqs.count_documents({}) == n and 'nothing to top up' in banner(r.data.decode()))
over = dict(full); over['Adrenalin 1 mg'] = 15
n = reqs.count_documents({})
topup(over)
check('holding more than standard requests nothing for that item', reqs.count_documents({}) == n)

as_ward('Lab', 'Lab Tech')
check('a department with no standard list has no top-up section',
      'Top Up to Standard' not in c.get('/requests').data.decode())

print('\n--- 12. The pharmacist edits the standard list ---')
as_user('N. Nurse', 'employee')
r = c.get('/requests/standard')
check('only an admin can open the standard lists', r.status_code == 302)
as_user()
amio = std.find_one({'item': 'Amiodarone'})
adr = std.find_one({'item': 'Adrenalin 1 mg'})
# Two amiodarones arrive after the list was made: the line stays unlinked
# and the page offers both rather than guessing.
meds.insert_one({'name': 'Amiodarone 150mg/3ml inj', 'balance': 10, 'price': 5.0,
                 'category': 'pharmaceutical'})
meds.insert_one({'name': 'Amiodarone 200mg tabs', 'balance': 10, 'price': 1.0,
                 'category': 'pharmaceutical'})
page = c.get('/requests/standard').data.decode()
check('the admin page lists the trolley items', 'Adrenalin 1 mg' in page and 'Should hold' in page)
check('it offers the possible matches for an unlinked line',
      'possible:' in page and 'Amiodarone 150mg/3ml inj' in page and 'Amiodarone 200mg tabs' in page)
check('and does not pick one itself', std.find_one({'_id': amio['_id']})['med_name'] is None)
data = {'action': 'save', 'department': 'Emergency Trolley'}
for r2 in std.find({'department': 'Emergency Trolley'}):
    data[f"required__{r2['_id']}"] = str(r2['required'])
    data[f"med__{r2['_id']}"] = r2.get('med_name') or ''
data[f"required__{adr['_id']}"] = '6'
data[f"med__{amio['_id']}"] = 'Amiodarone 150mg/3ml inj'
r = c.post('/requests/standard', data=data, follow_redirects=True)
check('a quantity can be changed', std.find_one({'_id': adr['_id']})['required'] == 6)
check('a line can be linked to a pharmacy item',
      std.find_one({'_id': amio['_id']})['med_name'] == 'Amiodarone 150mg/3ml inj')
check('the change is audited', db['audit_log'].find_one({'target_type': 'dept_standard'}) is not None)
data[f"med__{amio['_id']}"] = 'Amiodarone tabs'
r = c.post('/requests/standard', data=data, follow_redirects=True)
check('a link to an item not on the list is refused',
      std.find_one({'_id': amio['_id']})['med_name'] == 'Amiodarone 150mg/3ml inj'
      and 'not on the pharmacy list' in banner(r.data.decode()))
c.post('/requests/standard', data={'action': 'add', 'department': 'Emergency Trolley',
       'item': 'Glucagon 1 mg', 'required': '1', 'med_name': ''})
check('an item can be added', std.find_one({'item': 'Glucagon 1 mg'}) is not None)
c.post('/requests/standard', data={'action': 'remove', 'department': 'Emergency Trolley',
       'row_id': str(std.find_one({'item': 'Glucagon 1 mg'})['_id'])})
check('and removed', std.find_one({'item': 'Glucagon 1 mg'}) is None)
c.post('/requests/standard', data={'action': 'add', 'department': 'PHC',
       'item': 'Paracetamol', 'required': '100', 'med_name': 'Gauze swabs'})
as_ward('PHC', 'PHC Nurse')
check('any department can be given a list', 'Top Up to Standard' in c.get('/requests').data.decode())
as_ward('Emergency Trolley', 'Sr. Kutoane')
c.get('/requests')
check('opening the page again never re-seeds over an edit',
      std.find_one({'_id': adr['_id']})['required'] == 6
      and std.count_documents({'department': 'Emergency Trolley'}) == len(A.TROLLEY_STANDARD_SEED))
check('a department login cannot open the standard lists',
      c.get('/requests/standard').status_code == 302)

print('\n--- 13. A nurse does the trolley check under her own login ---')
# Asked for: "add the count form to the staff Requests page".
as_user('Sr. Kutoane', 'employee')
page = c.get('/requests').data.decode()
check('the staff Requests page has the count form',
      'Top Up to Standard' in page and 'id="topup-form"' in page and 'There now' in page)
check('it opens on the Emergency Trolley',
      'Top Up to Standard &mdash; Emergency Trolley' in page)
check('with a link to it from the top of the page', 'href="#topup"' in page)
check('the department choice lists every department with a list',
      '<option value="Emergency Trolley" selected>' in page and '<option value="PHC"' in page)
check('and it sends the department with the counts',
      'name="department" value="Emergency Trolley"' in page)
page = c.get('/requests?topup=PHC').data.decode()
check('choosing another department opens its list', 'Top Up to Standard &mdash; PHC' in page
      and 'name="department" value="PHC"' in page)
check('a department with no list is not offered',
      '<option value="Lab"' not in page.split('id="topup"')[1].split('</form>')[0])


def staff_topup(dept, have):
    data = {'action': 'topup', 'department': dept, 'note': ''}
    for r2 in std.find({'department': dept}):
        v = have.get(r2['item'], have.get('*'))
        if v is not None:
            data[f"have__{r2['_id']}"] = str(v)
    return c.post('/requests', data=data)


r = staff_topup('Emergency Trolley', {'Adrenalin 1 mg': 0})
check('a check with lines not counted is not sent, and comes back to the same list',
      r.status_code == 302 and 'topup=Emergency' in r.headers['Location']
      and r.headers['Location'].endswith('#topup'), r.headers.get('Location'))
full = {r2['item']: r2['required'] for r2 in std.find({'department': 'Emergency Trolley'})}
full['Adrenalin 1 mg'] = 2
n = reqs.count_documents({})
staff_topup('Emergency Trolley', full)
d = last_req()
check('a counted check sends the top-up for the trolley',
      reqs.count_documents({}) == n + 1 and d['department'] == 'Emergency Trolley'
      and d.get('kind') == 'topup')
check('under the nurse\'s own name', d['requested_by'] == 'Sr. Kutoane')
check('asking for the difference', {l['med_name']: l['requested'] for l in d['lines']}
      == {'Adrenaline 1mg/ml inj': 4}, str(d['lines']))
page = c.get('/requests').data.decode()
check('and it waits in the queue to be served', f'id="{d["ref"]}"' in page)
r = staff_topup('Doctor Consultation', {'*': 0})
check('a department with no list cannot be topped up',
      reqs.count_documents({}) == n + 1)
as_ward('Emergency Trolley', 'Sr. Kutoane')
check('the trolley account still has its own form',
      'Top Up to Standard &mdash; Emergency Trolley' in c.get('/requests').data.decode())

print(f"\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========")
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
