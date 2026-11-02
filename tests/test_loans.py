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
items = db['loan_items']
loans = db['loans']
c = A.app.test_client()
# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='L. Hlaoli'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['loan_items', 'loans', 'audit_log']:
        db[col].delete_many({})


TODAY = datetime.utcnow().date()


def dstr(offset=0):
    return (TODAY + timedelta(days=offset)).strftime('%Y-%m-%d')


def add_item(item_type='Crutches', code='', size='Adult L', condition='Good',
             note=''):
    return c.post('/loan-items', data={
        'action': 'add_item', 'item_type': item_type, 'item_code': code,
        'size': size, 'condition': condition, 'note': note},
        follow_redirects=True)


def issue(code, patient='M. Thabane', issued=0, due=14, contact='5880 1122',
          condition_out='Good', note=''):
    """Issue one item, or a list of them in the same issue."""
    return c.post('/loans', data={
        'action': 'issue', 'item_code': code, 'patient_name': patient,
        'patient_contact': contact,
        'issued_date': dstr(issued), 'due_date': dstr(due),
        'condition_out': condition_out, 'note': note}, follow_redirects=True)


def loan_id(code):
    doc = loans.find_one({'item_code': code, 'status': 'Out'})
    return str(doc['_id']) if doc else ''


def give_back(code, condition_in='Good'):
    return c.post('/loans', data={
        'action': 'return', 'loan_id': loan_id(code),
        'condition_in': condition_in}, follow_redirects=True)


def view(**kw):
    qs = '&'.join(f'{k}={v}' for k, v in kw.items())
    return c.get('/loans' + ('?' + qs if qs else '')).data.decode()


def register():
    return c.get('/loan-items').data.decode()


def text(html):
    return _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', ' ', html))


def history_table(html):
    """Just the Loan History section.

    The filter is scoped to history on purpose — "Currently Out" is a standing
    list and a filtered outstanding list can look empty when it is not — so an
    assertion about filtering has to look at history alone.
    """
    i = html.index('Loan History')
    j = html.index('Activity by Month')
    return html[i:j]


def row_for(html, code):
    """The register/out-table row containing an item code."""
    for r in _re.findall(r'<tr[^>]*>.*?</tr>', html, _re.S):
        if f'>{code}<' in r:
            return r
    return ''


print('--- 1. Adding items to the register ---')
reset()
login()
add_item('Crutches', size='Adult L')
check('an item is created', items.count_documents({}) == 1)
doc = items.find_one({})
check('a code is generated when none is given', doc['item_code'] == 'CR-001',
      str(doc.get('item_code')))
check('the type is stored', doc['item_type'] == 'Crutches')
check('it starts available', doc['status'] == 'Available')
check('the size is kept', doc['size'] == 'Adult L')
check('it is audited', db['audit_log'].count_documents(
    {'target_type': 'loan_item'}) == 1)

add_item('Crutches', size='Adult M')
add_item('Moon Boot', size='42')
add_item('Arm Sling', size='Universal')
check('codes increment within a type',
      items.find_one({'size': 'Adult M'})['item_code'] == 'CR-002')
check('each type has its own prefix',
      items.find_one({'item_type': 'Moon Boot'})['item_code'] == 'MB-001'
      and items.find_one({'item_type': 'Arm Sling'})['item_code'] == 'AS-001',
      str([d['item_code'] for d in items.find()]))

b = add_item('Crutches', code='CR-001')
check('a duplicate code is refused', items.count_documents({}) == 4)
check('and says which code clashed', 'CR-001 is already' in text(b.data.decode()))

b = add_item('Hoverboard')
check('an unknown item type is refused', items.count_documents({}) == 4)

add_item('Walking Frame', code='wf-xyz')
check('a hand-typed code is upper-cased',
      items.find_one({'item_code': 'WF-XYZ'}) is not None)

print('\n--- 2. A generated code never collides with a typed one ---')
reset()
login()
add_item('Crutches', code='CR-002')
add_item('Crutches')
add_item('Crutches')
codes = sorted(d['item_code'] for d in items.find())
check('the generator skips a code already in use',
      codes == ['CR-001', 'CR-002', 'CR-003'], str(codes))

print('\n--- 3. Issuing an item ---')
reset()
login()
add_item('Crutches')
b = issue('CR-001', patient='M. Thabane')
check('a loan is created', loans.count_documents({}) == 1)
ln = loans.find_one({})
check('it records the patient', ln['patient_name'] == 'M. Thabane')
check('it records the contact number', ln['patient_contact'] == '5880 1122')
check('there is no OPD / File No any more', 'patient_id' not in ln)
check('it records who issued it', ln['issued_by'] == 'L. Hlaoli')
check('it records the condition going out', ln['condition_out'] == 'Good')
check('it is open', ln['status'] == 'Out')
check('nothing is returned yet', ln['returned_date'] is None)
check('it has a reference', ln['loan_ref'].startswith('LN-'))
check('the reference is shown to the user', ln['loan_ref'] in text(b.data.decode()))
check('it is audited', db['audit_log'].count_documents(
    {'target_type': 'loan', 'action': 'CREATE'}) == 1)

print('\n--- 4. Availability is derived, never stored ---')
check('the item is NOT flagged on-loan on the item record',
      items.find_one({'item_code': 'CR-001'})['status'] == 'Available',
      'the item document should still read Available; the ledger decides')
page = view()
check('but the page shows it as on loan', 'On loan' in text(page))
check('it is not offered for issue again',
      'CR-001 &mdash;' not in page and '>CR-001 —' not in page)
_tiles = _re.findall(r'<div class="loan-tile[^"]*"><span class="n">(\d+)</span>(\w[^<]*)</div>',
                     page.replace('\n', ''))
check('the tile counts it as out', ('1', 'On loan') in _tiles, str(_tiles))
check('and does not count it as available', ('0', 'Available') in _tiles, str(_tiles))
reg = register()
check('the register shows who has it', 'M. Thabane' in row_for(reg, 'CR-001'))

print('\n--- 5. The same item cannot go to two patients ---')
b = issue('CR-001', patient='P. Mokete')
check('a second issue is refused', loans.count_documents({}) == 1)
check('and says it is already out', 'already out' in text(b.data.decode()))
check('the first patient still holds it',
      loans.find_one({'status': 'Out'})['patient_name'] == 'M. Thabane')

print('\n--- 6. Returning ---')
b = give_back('CR-001')
ln = loans.find_one({})
check('the loan is closed', ln['status'] == 'Returned')
check('the return date is set', ln['returned_date'] is not None)
check('who received it is recorded', ln['returned_to'] == 'L. Hlaoli')
check('the condition coming back is recorded', ln['condition_in'] == 'Good')
check('the item is available again',
      items.find_one({'item_code': 'CR-001'})['status'] == 'Available')
check('it is offered for issue again', 'CR-001' in view())
check('the return is audited', db['audit_log'].count_documents(
    {'target_type': 'loan', 'action': 'UPDATE'}) == 1)

b = give_back('CR-001')
check('returning an already-returned loan is refused',
      'not found' in text(b.data.decode()) or 'already closed' in text(b.data.decode()))
check('no second return was written',
      loans.count_documents({'status': 'Returned'}) == 1)

print('\n--- 7. An item returned damaged does not go back into the pool ---')
reset()
login()
add_item('Moon Boot')
issue('MB-001')
dmg = give_back('MB-001', condition_in='Damaged')
check('the item is marked damaged',
      items.find_one({'item_code': 'MB-001'})['status'] == 'Damaged')
check('its condition is updated',
      items.find_one({'item_code': 'MB-001'})['condition'] == 'Damaged')
page = view()
check('it is not offered for issue', 'MB-001 &mdash;' not in page)
b = issue('MB-001')
check('and issuing it directly is refused', loans.count_documents({}) == 1)
check('the refusal names the state', 'Damaged' in text(b.data.decode()))
_m = _re.search(r'class="message (\w+)"', dmg.data.decode())
check('the damaged-return banner is amber, not green',
      _m is not None and _m.group(1) == 'partial',
      _m.group(0) if _m else 'no banner')
_m2 = _re.search(r'class="message (\w+)"', b.data.decode())
check('a plain refusal banner is red, not unstyled',
      _m2 is not None and _m2.group(1) == 'error',
      _m2.group(0) if _m2 else 'no banner')

print('\n--- 8. Overdue ---')
reset()
login()
add_item('Crutches')
add_item('Crutches')
issue('CR-001', patient='Late Patient', issued=-30, due=-9)
issue('CR-002', patient='Fine Patient', issued=-2, due=12)
page = view()
check('the overdue loan is flagged',
      'loan-overdue' in row_for(page, 'CR-001'), row_for(page, 'CR-001')[:120])
check('the on-time loan is not flagged',
      'loan-overdue' not in row_for(page, 'CR-002'))
check('the days overdue are shown', '9 over' in row_for(page, 'CR-001'),
      row_for(page, 'CR-001'))
check('the days out are shown', '30' in row_for(page, 'CR-001'))
check('the overdue count is reported', 'past due' in text(page))
check('the register marks it overdue too',
      'Overdue' in row_for(register(), 'CR-001'))

print('\n--- 9. Dates that cannot be right ---')
reset()
login()
add_item('Crutches')
b = c.post('/loans', data={
    'action': 'issue', 'item_code': 'CR-001', 'patient_name': 'X',
    'issued_date': dstr(3), 'due_date': dstr(20)}, follow_redirects=True)
check('a future issue date is refused', loans.count_documents({}) == 0)
check('and says so', 'future' in text(b.data.decode()))

b = c.post('/loans', data={
    'action': 'issue', 'item_code': 'CR-001', 'patient_name': 'X',
    'issued_date': dstr(0), 'due_date': dstr(-5)}, follow_redirects=True)
check('a due date before the issue date is refused', loans.count_documents({}) == 0)
check('and says so', 'before the issue date' in text(b.data.decode()))

b = c.post('/loans', data={
    'action': 'issue', 'item_code': 'CR-001', 'patient_name': 'X',
    'issued_date': dstr(0), 'due_date': dstr(900)}, follow_redirects=True)
check('an absurdly long loan is refused', loans.count_documents({}) == 0)

b = c.post('/loans', data={
    'action': 'issue', 'item_code': 'CR-001', 'patient_name': '  ',
    'issued_date': dstr(0), 'due_date': dstr(14)}, follow_redirects=True)
check('a loan with no patient name is refused', loans.count_documents({}) == 0)
check('and explains why', 'traceable' in text(b.data.decode()))

b = issue('CR-999')
check('an unknown item code is refused', loans.count_documents({}) == 0)
check('and says it is not in the register',
      'not in the equipment register' in text(b.data.decode()),
      text(b.data.decode())[:160])

b = c.post('/loans', data={'action': 'issue', 'item_code': 'CR-001',
                           'patient_name': 'X', 'issued_date': 'rubbish',
                           'due_date': dstr(14)}, follow_redirects=True)
check('a malformed date is handled', loans.count_documents({}) == 0)

print('\n--- 10. Writing an item off as lost ---')
reset()
login()
add_item('Crutches')
issue('CR-001', patient='Gone Away', issued=-120, due=-100)
b = c.post('/loans', data={'action': 'mark_lost', 'loan_id': loan_id('CR-001')},
           follow_redirects=True)
ln = loans.find_one({})
check('the loan is closed as lost', ln['status'] == 'Lost')
check('it is not recorded as returned', ln['returned_date'] is None)
check('who wrote it off is recorded', ln['closed_by'] == 'L. Hlaoli')
check('the item is marked lost',
      items.find_one({'item_code': 'CR-001'})['status'] == 'Lost')
check('it leaves the available pool', 'CR-001 &mdash;' not in view())
check('the write-off is audited', db['audit_log'].count_documents(
    {'target_type': 'loan', 'action': 'UPDATE'}) == 1)
check('a lost item no longer counts as out', 'Nothing is out' in text(view()))

print('\n--- 11. Changing an item status ---')
reset()
login()
add_item('Crutches')
iid = str(items.find_one({})['_id'])
c.post('/loan-items', data={'action': 'set_status', 'item_id': iid,
                            'status': 'Retired'}, follow_redirects=True)
check('an item can be retired',
      items.find_one({})['status'] == 'Retired')
check('a retired item is not offered', 'CR-001 &mdash;' not in view())
b = issue('CR-001')
check('and cannot be issued', loans.count_documents({}) == 0)

c.post('/loan-items', data={'action': 'set_status', 'item_id': iid,
                            'status': 'Available'}, follow_redirects=True)
issue('CR-001')
b = c.post('/loan-items', data={'action': 'set_status', 'item_id': iid,
                                'status': 'Retired'}, follow_redirects=True)
check('an item that is out cannot be retired underneath the loan',
      items.find_one({})['status'] == 'Available')
check('and the refusal says to take it back first',
      'back in first' in text(b.data.decode()))

b = c.post('/loan-items', data={'action': 'set_status', 'item_id': iid,
                                'status': 'Teleported'}, follow_redirects=True)
check('an invalid status is refused', items.find_one({})['status'] == 'Available')
b = c.post('/loan-items', data={'action': 'set_status', 'item_id': 'nonsense',
                                'status': 'Retired'}, follow_redirects=True)
check('a malformed item id is handled', b.status_code == 200)

print('\n--- 12. History and search ---')
reset()
login()
add_item('Crutches')
add_item('Arm Sling')
issue('CR-001', patient='Mpho Letsie')
give_back('CR-001')
issue('AS-001', patient='Thato Moloi')
page = view()
check('both loans appear in history',
      'Mpho Letsie' in page and 'Thato Moloi' in page)
check('a returned loan shows its days', '>Returned<' in page)
h = history_table(view(q='Mpho'))
check('search by patient narrows the history',
      'Mpho Letsie' in h and 'Thato Moloi' not in h)
_ref = loans.find_one({'patient_name': 'Thato Moloi'})['loan_ref']
h = history_table(view(q=_ref))
check('search by loan reference works',
      'Thato Moloi' in h and 'Mpho Letsie' not in h)
h = history_table(view(q='AS-001'))
check('search by item code works', 'Thato Moloi' in h)
h = history_table(view(status='Returned'))
check('filtering by status works',
      'Mpho Letsie' in h and 'Thato Moloi' not in h)
check('but the outstanding list is NOT filtered away',
      'Thato Moloi' in view(status='Returned'),
      'a filtered outstanding list can look empty when it is not')
check('and the page says the filter applies to history only',
      'narrows the history table' in text(view()))
page = view(q='O%27Brien+%28snr%29')
check('a search with regex characters does not crash', '<table' in page)

print('\n--- 13. Activity by month ---')
reset()
login()
add_item('Crutches')
issue('CR-001', issued=-3, due=11)
give_back('CR-001')
rows = A._loan_activity(loans)
check('six months are reported', len(rows) == 6, str(len(rows)))
check('this month counts the issue', rows[-1]['issued'] == 1, str(rows[-1]))
check('this month counts the return', rows[-1]['returned'] == 1, str(rows[-1]))
check('average days out is reported', rows[-1]['avg_days'] is not None)
check('the table renders', 'Activity by Month' in view())

print('\n--- 14. Access control ---')
reset()
login('admin')
add_item('Crutches')
login('pharmacist', 'A. Nurse')
b = c.get('/loan-items', follow_redirects=True)
check('a non-admin cannot open the equipment register',
      'Add an Item' not in b.data.decode())
check('and is told why', 'Only admins' in text(b.data.decode()))
b = add_item('Moon Boot')
check('a non-admin cannot add an item', items.count_documents({}) == 1)

check('a non-admin CAN open the loans page', '<h1>Equipment Loans' in view())
b = issue('CR-001', patient='Counter Work')
check('a non-admin can issue an item', loans.count_documents({}) == 1)
check('the loan records the pharmacist',
      loans.find_one({})['issued_by'] == 'A. Nurse')
b = c.post('/loans', data={'action': 'mark_lost', 'loan_id': loan_id('CR-001')},
           follow_redirects=True)
check('a non-admin cannot write an item off as lost',
      loans.find_one({})['status'] == 'Out')
check('and is told why', 'Only admins' in text(b.data.decode()))
check('a non-admin sees no Lost button',
      'mark_lost' not in view())
b = give_back('CR-001')
check('a non-admin can take a return', loans.find_one({})['status'] == 'Returned')

login('admin')
check('an admin sees the register link in the nav',
      '/loan-items' in view())
login('pharmacist', 'A. Nurse')
check('a non-admin does not', '/loan-items' not in view())

print('\n--- 15. Odds and ends ---')
reset()
login()
check('an empty register renders', '<h1>Equipment Loans' in view())
check('it says there is nothing to lend', 'Nothing is available' in text(view()))
check('the empty register page renders', 'No items yet' in text(register()))
b = c.post('/loans', data={'action': 'nonsense'}, follow_redirects=True)
check('an unknown action is handled', b.status_code == 200)
b = c.post('/loan-items', data={'action': 'nonsense'}, follow_redirects=True)
check('an unknown register action is handled', b.status_code == 200)
add_item('Crutches')
check('the page offers a print control', 'window.print()' in view())
check('it prints landscape A4', 'size: A4 landscape' in view())
check('a signature line is provided', 'Checked by' in text(view()))

print('\n--- 20. A crutch pair goes out in one issue ---')
# Asked for: "sometimes I have to issue crutch 1 and 2 in the same issue".
reset()
login()
add_item('Crutches')
add_item('Crutches')
add_item('Wheelchair')
page = view()
check('the issue form offers tick boxes, not a single choice',
      'type="checkbox" name="item_code" value="CR-001"' in page
      and 'type="checkbox" name="item_code" value="CR-002"' in page
      and '<select name="item_code"' not in page)
check('and there is no OPD / File No box', 'patient_id' not in page
      and 'OPD' not in page)

r = issue(['CR-001', 'CR-002'], patient='Lerato Mokhele')
_rows = list(loans.find({'patient_name': 'Lerato Mokhele'}).sort('loan_ref', 1))
check('both items are lent', len(_rows) == 2, str(len(_rows)))
check('each is its own loan, so each can come back on its own',
      {x['item_code'] for x in _rows} == {'CR-001', 'CR-002'}
      and all(x['status'] == 'Out' for x in _rows))
check('they share one issue reference',
      len({x['issue_ref'] for x in _rows}) == 1, str([x['issue_ref'] for x in _rows]))
_base = _rows[0]['issue_ref']
check('and are numbered under it',
      [x['loan_ref'] for x in _rows] == [f'{_base}-1', f'{_base}-2'],
      str([x['loan_ref'] for x in _rows]))
check('same patient, dates and condition on both',
      len({(x['patient_name'], x['issued_date'], x['due_date'], x['condition_out'])
           for x in _rows}) == 1)
check('the message names both', 'CR-001 and CR-002 issued to Lerato Mokhele'
      in r.data.decode(), r.data.decode()[:300])
check('both are on the Currently Out list',
      'CR-001' in view() and 'CR-002' in view())
check('both are audited', db['audit_log'].count_documents(
    {'target_type': 'loan', 'action': 'CREATE'}) == 2)
check('searching the issue reference finds both',
      history_table(view(q=_base)).count('<td>Lerato Mokhele</td>') == 2)

give_back('CR-001')
check('one can come back without the other',
      loans.find_one({'item_code': 'CR-001', 'patient_name': 'Lerato Mokhele'})['status'] == 'Returned'
      and loans.find_one({'item_code': 'CR-002'})['status'] == 'Out')

print('\n--- 21. All or nothing ---')
# If one of the ticked items cannot go, none goes: half a pair on the register
# would show a patient holding one crutch.
r = issue(['CR-002', 'WC-001'], patient='Someone Else')
check('an item already out stops the whole issue',
      loans.count_documents({'patient_name': 'Someone Else'}) == 0)
check('and the wheelchair that was free is not lent either',
      loans.find_one({'item_code': 'WC-001', 'status': 'Out'}) is None)
check('the message says nothing was issued and why',
      'Nothing was issued' in r.data.decode()
      and 'CR-002 is already out on loan' in r.data.decode(), r.data.decode()[:300])
r = issue([], patient='Nobody')
check('ticking nothing is refused', 'Choose at least one item' in r.data.decode())
r = issue(['WC-001', 'WC-001'], patient='Twice Ticked')
check('the same item ticked twice is lent once',
      loans.count_documents({'patient_name': 'Twice Ticked'}) == 1)
check('a single item keeps a plain reference, no number',
      not loans.find_one({'patient_name': 'Twice Ticked'})['loan_ref'].endswith('-1'))

print('\n--- 22. Correcting a loan ---')
# Asked for: "is there a way to edit and delete?" A correction keeps what it
# replaced and who made it; the original issuer is untouched.
reset(); login()
add_item(code='CR-101'); add_item(code='CR-102'); add_item(code='CR-103')
issue('CR-101', patient='Mpho Mokoen', contact='5000 0000', due=14)
lid = loan_id('CR-101')
before = loans.find_one({'item_code': 'CR-101'})


def correct(lid, **kw):
    data = {'action': 'edit_loan', 'loan_id': lid, 'patient_name': 'Mpho Mokoena',
            'patient_contact': '5000 0000', 'issued_date': dstr(0),
            'due_date': dstr(14), 'condition_out': 'Good', 'note': ''}
    data.update(kw)
    return c.post('/loans', data=data, follow_redirects=True)


page = c.get(f'/loans?edit={lid}').data.decode()
check('the edit form opens filled in with the loan',
      'Correct Loan' in page and 'value="Mpho Mokoen"' in page
      and f'value="{dstr(14)}"' in page)
check('the history has Edit and Delete for an admin',
      f'edit={lid}' in page and 'delete_loan' in page)
r = correct(lid, due_date=dstr(21))
after = loans.find_one({'_id': before['_id']})
check('the name and due date are corrected',
      after['patient_name'] == 'Mpho Mokoena'
      and after['due_date'].date() == TODAY + timedelta(days=21))
fix = (after.get('corrections') or [{}])[-1]
check('the old values are kept with who changed them',
      fix.get('by') == 'L. Hlaoli'
      and fix['changes']['patient_name'] == {'from': 'Mpho Mokoen', 'to': 'Mpho Mokoena'}
      and set(fix['changes']) == {'patient_name', 'due_date'}, str(fix))
check('the issue date, untouched, is not recorded as a change',
      after['issued_date'] == before['issued_date'])
check('the original issuer is kept', after['issued_by'] == before['issued_by'])
check('the change is in the audit log',
      db['audit_log'].find_one({'action': 'UPDATE', 'target_id': before['loan_ref'],
                                'changes': {'$regex': 'patient "Mpho Mokoen" -> "Mpho Mokoena"'}})
      is not None, str(list(db['audit_log'].find({}, {'_id': 0}))[-1:]))
check('the history notes the correction',
      'corrected by L. Hlaoli' in c.get('/loans').data.decode())
r = correct(lid, due_date=dstr(21))
check('saving with nothing changed says so and stores nothing',
      'Nothing was changed' in r.data.decode()
      and len(loans.find_one({'_id': before['_id']})['corrections']) == 1)
r = correct(lid, issued_date=dstr(1))
check('a correction cannot date a loan in the future',
      'cannot be dated in the future' in r.data.decode()
      and loans.find_one({'_id': before['_id']})['issued_date'] == before['issued_date'])
r = correct(lid, due_date=dstr(-5))
check('nor put the due date before the issue date',
      'cannot be before the issue date' in r.data.decode())
r = correct(lid, patient_name='  ')
check('nor blank the patient', 'patient name is required' in r.data.decode()
      and loans.find_one({'_id': before['_id']})['patient_name'] == 'Mpho Mokoena')
give_back('CR-101')
r = correct(lid, issued_date=dstr(-3), due_date=dstr(21))
check('a returned loan can still be corrected',
      loans.find_one({'_id': before['_id']})['issued_date'].date() == TODAY - timedelta(days=3))

print('\n--- 23. Deleting a loan ---')
issue('CR-102', patient='Wrong Entry')
lid2 = loan_id('CR-102')
ref2 = loans.find_one({'item_code': 'CR-102'})['loan_ref']
r = c.post('/loans', data={'action': 'delete_loan', 'loan_id': lid2}, follow_redirects=True)
check('the loan is gone', loans.find_one({'loan_ref': ref2}) is None)
check('and the item is free to lend again',
      'value="CR-102"' in c.get('/loans').data.decode())
log = db['audit_log'].find_one({'action': 'DELETE', 'target_id': ref2})
check('the audit log keeps what the row held',
      log is not None and 'Wrong Entry' in log['changes'] and 'CR-102' in log['changes'],
      str(log))
issue('CR-103', patient='Not Lost')
lid3 = loan_id('CR-103')
c.post('/loans', data={'action': 'mark_lost', 'loan_id': lid3})
check('(setup) written off', items.find_one({'item_code': 'CR-103'})['status'] == 'Lost')
c.post('/loans', data={'action': 'delete_loan', 'loan_id': lid3})
check('deleting a mistaken write-off puts the item back to Available',
      items.find_one({'item_code': 'CR-103'})['status'] == 'Available')
r = c.post('/loans', data={'action': 'delete_loan', 'loan_id': 'nonsense'}, follow_redirects=True)
check('a bad id is refused politely', 'not found' in r.data.decode())

print('\n--- 24. Only admins correct or delete loans ---')
issue('CR-102', patient='Staff Test')
lid4 = loan_id('CR-102')
login(role='user', name='N. Officer')
page = c.get(f'/loans?edit={lid4}').data.decode()
check('a non-admin sees no edit form or delete button',
      'Correct Loan' not in page and 'delete_loan' not in page and f'edit={lid4}' not in page)
r = correct(lid4, patient_name='Changed')
check('a non-admin correction is refused',
      'Only admins' in r.data.decode()
      and loans.find_one({'item_code': 'CR-102', 'status': 'Out'})['patient_name'] == 'Staff Test')
c.post('/loans', data={'action': 'delete_loan', 'loan_id': lid4})
check('a non-admin delete is refused', loans.find_one({'item_code': 'CR-102', 'status': 'Out'}) is not None)
login()

print('\n--- 25. Correcting and deleting equipment items ---')
add_item(code='MB-900', item_type='Moon Boot', size='Adlt M')
mb = items.find_one({'item_code': 'MB-900'})


def correct_item(doc, **kw):
    data = {'action': 'edit_item', 'item_id': str(doc['_id']), 'item_code': doc['item_code'],
            'item_type': doc['item_type'], 'size': doc.get('size', ''),
            'condition': doc.get('condition', 'Good'), 'note': doc.get('note', '')}
    data.update(kw)
    return c.post('/loan-items', data=data, follow_redirects=True)


page = c.get(f"/loan-items?edit={mb['_id']}").data.decode()
check('the item edit form opens filled in', 'Correct MB-900' in page and 'value="Adlt M"' in page)
correct_item(mb, size='Adult M', item_code='MB-001')
mb2 = items.find_one({'_id': mb['_id']})
check('a never-lent item can have its size and code corrected',
      mb2['size'] == 'Adult M' and mb2['item_code'] == 'MB-001')
check('with the old values kept',
      mb2['corrections'][-1]['changes']['size'] == {'from': 'Adlt M', 'to': 'Adult M'})
cr = items.find_one({'item_code': 'CR-101'})
r = correct_item(cr, item_code='CR-999')
check('an item that has been lent keeps its code',
      'cannot change' in r.data.decode() and items.find_one({'_id': cr['_id']})['item_code'] == 'CR-101')
r = correct_item(cr, note='left grip worn')
check('but its other details can be corrected',
      items.find_one({'_id': cr['_id']})['note'] == 'left grip worn')
r = correct_item(mb2, item_code='CR-101')
check('a correction cannot give an item a code already used',
      'already in the register' in r.data.decode())
page = c.get('/loan-items').data.decode()
# MB-001, and CR-103: its only loan was the mistaken write-off, now deleted.
check('Delete is offered only for an item never lent',
      page.count('name="action" value="delete_item"') == 2,
      str(page.count('name="action" value="delete_item"')))
r = c.post('/loan-items', data={'action': 'delete_item', 'item_id': str(cr['_id'])}, follow_redirects=True)
check('a lent item cannot be deleted', 'Set it to Retired' in r.data.decode()
      and items.find_one({'_id': cr['_id']}) is not None)
c.post('/loan-items', data={'action': 'delete_item', 'item_id': str(mb['_id'])})
check('a never-lent item can be deleted', items.find_one({'_id': mb['_id']}) is None)
check('and the deletion is audited',
      db['audit_log'].find_one({'action': 'DELETE', 'target_id': 'MB-001'}) is not None)

print('\n--- 26. A name with an apostrophe does not break the delete button ---')
issue('CR-103', patient="Tsepo O'Neil")
page = c.get('/loans').data.decode()
m = next((x for x in _re.finditer(r"onsubmit='return confirm\((.*?)\);'", page)
          if 'Neil' in x.group(1)), None)
check("the confirm text is valid in a single-quoted attribute",
      m is not None and "'" not in m.group(1) and 'O\\u0027Neil' in m.group(1),
      m.group(1) if m else 'no match')

print('\n--- 27. Correcting a pair corrects both ---')
reset(); login()
add_item(code='CR-201'); add_item(code='CR-202')
issue(['CR-201', 'CR-202'], patient='Lineo Mohapi', note='')
a = loans.find_one({'item_code': 'CR-201'}); b = loans.find_one({'item_code': 'CR-202'})
page = c.get(f"/loans?edit={a['_id']}").data.decode()
check('the edit form offers to correct the other item in the issue',
      'name="whole_issue"' in page and 'CR-202, issued with it' in page)
r = c.post('/loans', data={'action': 'edit_loan', 'loan_id': str(a['_id']),
           'patient_name': 'Lineo Mohapi-Sello', 'patient_contact': '5880 1122',
           'issued_date': dstr(0), 'due_date': dstr(20), 'condition_out': 'Fair',
           'note': 'left one', 'whole_issue': '1'}, follow_redirects=True)
b2 = loans.find_one({'_id': b['_id']})
check('the other crutch gets the corrected name and due date',
      b2['patient_name'] == 'Lineo Mohapi-Sello'
      and b2['due_date'].date() == TODAY + timedelta(days=20))
check('but keeps its own condition and note',
      b2['condition_out'] == 'Good' and b2.get('note', '') == '')
check('with its own correction history',
      set(b2['corrections'][-1]['changes']) == {'patient_name', 'due_date'})
check('the message says both were corrected', 'Also corrected on CR-202' in r.data.decode())
c.post('/loans', data={'action': 'edit_loan', 'loan_id': str(a['_id']),
       'patient_name': 'Only This One', 'patient_contact': '5880 1122',
       'issued_date': dstr(0), 'due_date': dstr(20), 'condition_out': 'Fair',
       'note': 'left one'})
check('unticked, only the one loan changes',
      loans.find_one({'_id': b['_id']})['patient_name'] == 'Lineo Mohapi-Sello'
      and loans.find_one({'_id': a['_id']})['patient_name'] == 'Only This One')
add_item(code='CR-203'); issue('CR-203', patient='Single')
page = c.get(f"/loans?edit={loan_id('CR-203')}").data.decode()
check('a single-item loan offers no such box', 'name="whole_issue"' not in page)

print(f"\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========")
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
