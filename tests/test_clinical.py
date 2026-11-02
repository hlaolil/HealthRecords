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
    for col in ['medications', 'transactions', 'stock_takes', 'stock_take_counts',
                'audit_log', 'app_warnings']:
        db[col].delete_many({})


TODAY = datetime.utcnow()


def add(name, balance=1000):
    meds.insert_one({'name': name, 'balance': balance, 'batch': 'B', 'price': 2.5,
                     'pack_size': 100, 'pack_price': 250.0, 'expiry_date': '2030-01-01',
                     'schedule': 'not controlled'})


def dispense(patient, pairs, birth_decade='1980', outcome='Outpatient', days='', dx='flu'):
    return c.post('/dispense', data={
        'patient': patient, 'company': 'C', 'position': 'P',
        'birth_decade': birth_decade, 'gender': 'M', 'outcome': outcome,
        'outcome_days': days, 'diagnoses': dx, 'prescriber': 'Dr X',
        'dispenser': 'D', 'date': TODAY.strftime('%Y-%m-%d'),
        'med_names': [p[0] for p in pairs],
        'quantities': [str(p[1]) for p in pairs]}, follow_redirects=True)


def mrow(body, label):
    mm = _re.search(r'<td><strong>' + _re.escape(label) + r'</strong></td>(.*?)</tr>', body, _re.S)
    return [_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)] if mm else []


print('\n--- 1. Year of birth is chosen as a decade ---')
reset()
login()
add('Paracetamol, 500 mg')
body = c.get('/dispense').data.decode()
check('the form offers a birth decade', 'name="birth_decade"' in body)
check('the free-text year input is gone', 'name="birth_year"' not in body)
check('the old age band dropdown is gone', 'name="age_group"' not in body)
check('ranges are shown, not bare years', '1980' in body and '1989' in body)
check('the current decade is offered', str((TODAY.year // 10) * 10) in body)
check('the most recent decade comes first',
      body.index(str((TODAY.year // 10) * 10)) < body.index('1930'))
check('the list column is relabelled', '<th>Born</th>' in body)

dispense('Thabo M', [('Paracetamol, 500 mg', 10)], birth_decade='1980')
t = txs.find_one({'type': 'dispense'})
check('the decade start is stored as a number', t['birth_decade'] == 1980, str(t.get('birth_decade')))
check('no age_group is written', 'age_group' not in t)
check('the range is rendered back out', '1980–1989' in c.get('/dispense').data.decode()
      or '1980&ndash;1989' in c.get('/dispense').data.decode())
dispense('No Decade', [('Paracetamol, 500 mg', 5)], birth_decade='')
check('a blank decade does not crash the dispense',
      txs.count_documents({'type': 'dispense'}) == 2)
check('a blank decade stores None',
      txs.find_one({'patient': 'No Decade'})['birth_decade'] is None)
dispense('Bad Decade', [('Paracetamol, 500 mg', 5)], birth_decade='abc')
check('a non-numeric decade does not crash',
      txs.count_documents({'type': 'dispense'}) == 3)

print('\n--- 2. Outcome replaces the required sick-leave field ---')
reset()
login()
add('Paracetamol, 500 mg')
body = c.get('/dispense').data.decode()
check('the outcome field exists', 'name="outcome"' in body)
check('it offers all four outcomes',
      all(o in body for o in ['Outpatient', 'Sick leave', 'Referral', 'Admission']))
check('Outpatient replaces the empty option', '-- None --' not in body
      and 'Outpatient' in body)
check('the required days field is gone', 'name="sick_leave_days"' not in body)
check('the list column is relabelled', '<th>Outcome</th>' in body)

dispense('A', [('Paracetamol, 500 mg', 5)])                                  # outpatient
dispense('B', [('Paracetamol, 500 mg', 5)], outcome='Sick leave', days='3')
dispense('C', [('Paracetamol, 500 mg', 5)], outcome='Referral')
dispense('D', [('Paracetamol, 500 mg', 5)], outcome='Admission', days='2')
check('an ordinary visit is recorded as Outpatient',
      txs.find_one({'patient': 'A'})['outcome'] == 'Outpatient')
check('an empty submission still defaults to Outpatient',
      (dispense('Z', [('Paracetamol, 500 mg', 1)], outcome='') or True)
      and txs.find_one({'patient': 'Z'})['outcome'] == 'Outpatient')
check('a referral needs no days',
      txs.find_one({'patient': 'C'})['outcome'] == 'Referral'
      and txs.find_one({'patient': 'C'})['outcome_days'] is None)
check('sick leave keeps its days', txs.find_one({'patient': 'B'})['outcome_days'] == 3)
# Days are now sick leave only: the dispensing form shows the field for that
# outcome alone, and the server drops days sent with any other. An admission
# therefore no longer carries a length of stay.
check('an admission no longer keeps days',
      txs.find_one({'patient': 'D'})['outcome_days'] is None,
      str(txs.find_one({'patient': 'D'})['outcome_days']))
check('but the admission itself is still recorded',
      txs.find_one({'patient': 'D'})['outcome'] == 'Admission')
body = c.get('/dispense').data.decode()
check('the list shows the outcome and days', 'Sick leave (3d)' in body)
check('an outpatient visit is named in the list', 'Outpatient' in body)

print('\n--- 3. Outcomes on the dashboard, counted per visit ---')
body = c.get('/dashboard').data.decode()
check('the outcomes panel exists', 'Visit Outcomes by Month' in body)
check('Outpatient gives the others a denominator',
      bool(mrow(body, 'Outpatient')), str(mrow(body, 'Outpatient')))
check('sick leave counted once', mrow(body, 'Sick leave')[-2] == '1',
      str(mrow(body, 'Sick leave')))
check('its days are totalled', mrow(body, 'Sick leave')[-1] == '3')
check('referral counted', mrow(body, 'Referral')[-2] == '1')
check('a referral shows a dash for days', mrow(body, 'Referral')[-1] == '–'
      or mrow(body, 'Referral')[-1] == '&ndash;', str(mrow(body, 'Referral')))
check('admission counted', mrow(body, 'Admission')[-2] == '1')

# a multi-medication visit must count ONCE
add('Aspirin, 100 mg')
dispense('E', [('Paracetamol, 500 mg', 5), ('Aspirin, 100 mg', 5)],
         outcome='Referral', dx='malaria')
check('two dispensed lines are still one referral',
      mrow(c.get('/dashboard').data.decode(), 'Referral')[-2] == '2',
      str(mrow(c.get('/dashboard').data.decode(), 'Referral')))
check('the per-visit basis is explained', 'one referral, not three' in
      c.get('/dashboard').data.decode())

print('\n--- 4. Disease trends ---')
body = c.get('/dashboard').data.decode()
check('the disease panel exists', 'Disease Trends' in body)


def dxrow(body, name):
    seg = body[body.index('Disease Trends'):]
    seg = seg[:seg.index('</table>')]
    mm = _re.search(r'<td>' + _re.escape(name) + r'</td>(.*?)</tr>', seg, _re.S)
    return [_re.sub(r'<[^>]+>', '', x).strip()
            for x in _re.findall(r'<td>(.*?)</td>', mm.group(1), _re.S)] if mm else []


check('flu is listed', bool(dxrow(body, 'flu')))
# A, B, C, D and Z were all dispensed with a flu diagnosis
check('flu counted once per visit, 5 visits', dxrow(body, 'flu')[-2] == '5',
      str(dxrow(body, 'flu')))
check('malaria counted once', dxrow(body, 'malaria')[-2] == '1', str(dxrow(body, 'malaria')))
check('the episode basis is explained', 'counts once under each' in body)

reset()
login()
add('Paracetamol, 500 mg', 100000)
for i in range(20):
    dispense(f'P{i}', [('Paracetamol, 500 mg', 1)], dx='common cold')
for i in range(3):
    dispense(f'Q{i}', [('Paracetamol, 500 mg', 1)], dx='rare thing')
body = c.get('/dashboard').data.decode()
seg = body[body.index('Disease Trends'):]
order = _re.findall(r'<tr><td>([a-z ]+)</td>', seg)
check('diagnoses are ranked by frequency', order[0] == 'common cold', str(order[:3]))
check('multiple diagnoses on one visit each count',
      dxrow(c.get('/dashboard').data.decode(), 'common cold')[-2] == '20')

print('\n--- 5. Stock take shows every item up front ---')
reset()
login()
for n in ['Aspirin, 100 mg', 'Paracetamol, 500 mg', 'Zinc, 20 mg']:
    add(n, balance=100)
c.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
body = c.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('a count sheet is shown', 'Count Sheet' in body)
check('all three items are pre-listed',
      all(n in body for n in ['Aspirin, 100 mg', 'Paracetamol, 500 mg', 'Zinc, 20 mg']))
check('each has its own count box', body.count('name="counted__') == 3)
check('the sheet is alphabetical',
      body.index('Aspirin, 100 mg') < body.index('Paracetamol, 500 mg')
      < body.index('Zinc, 20 mg'))
# No longer blind, at the pharmacist's request: the sheet shows the system
# figure so a stock take short of time can act only where the shelf differs.
# (The old check passed by luck - the figure is in a cell with a class.)
check('the sheet shows the system figure',
      '<td class="st-system">' in body)
check('it says rows left alone stay on the sheet',
      "rows you haven't touched stay on the sheet" in body)

print('\n--- 6. Post part of the sheet, come back later ---')
r = c.post('/stock-take', data={
    'action': 'count_sheet', 'stock_take_id': sid,
    'med_name': ['Aspirin, 100 mg', 'Paracetamol, 500 mg', 'Zinc, 20 mg'],
    'counted__0': '95', 'counted__1': '', 'counted__2': '',
    'note__0': 'shelf A', 'note__1': '', 'note__2': ''}, follow_redirects=True)
check('only the filled row was posted',
      db['stock_take_counts'].count_documents({}) == 1)
check('the counted item was corrected',
      meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 95)
check('the blank rows changed nothing',
      meds.find_one({'name': 'Zinc, 20 mg'})['balance'] == 100)
check('the note was kept',
      db['stock_take_counts'].find_one({})['note'] == 'shelf A')
check('the summary reports what happened', '1 count(s) posted' in r.data.decode())
check('and classifies them', '0 agreed, 1 short, 0 over' in r.data.decode())
body = c.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('the counted item drops off the sheet',
      body.count('name="counted__') == 2)
check('and appears in the counted list below', 'shelf A' in body)
check('the remaining count is shown', '2 still to count' in body)

r = c.post('/stock-take', data={
    'action': 'count_sheet', 'stock_take_id': sid,
    'med_name': ['Paracetamol, 500 mg', 'Zinc, 20 mg'],
    'counted__0': '100', 'counted__1': '105',
    'note__0': '', 'note__1': ''}, follow_redirects=True)
check('the rest can be posted later',
      db['stock_take_counts'].count_documents({}) == 3)
check('agreements and overages are classified',
      '1 agreed, 0 short, 1 over' in r.data.decode())
body = c.get(f'/stock-take?stock_take_id={sid}').data.decode()
check('the sheet is then empty', 'Every medication has been counted' in body)

print('\n--- 7. The sheet applies the same rules as a single count ---')
reset()
login()
add('Paracetamol, 500 mg', 100)
c.post('/stock-take', data={'action': 'open'}, follow_redirects=True)
sid = str(db['stock_takes'].find_one({'status': 'open'})['_id'])
c.post('/stock-take', data={'action': 'count_sheet', 'stock_take_id': sid,
                            'med_name': ['Paracetamol, 500 mg'],
                            'counted__0': '93', 'note__0': ''}, follow_redirects=True)
adj = txs.find_one({'type': 'adjustment'})
check('a signed adjustment is posted to the ledger', adj and adj['quantity'] == -7)
check('it is classified from the sign',
      db['stock_take_counts'].find_one({})['discrepancy_type'] == 'issued_not_recorded')
check('it is audited',
      db['audit_log'].count_documents({'target_type': 'stock_take_count'}) == 1)
check('it shows in the audit view',
      'Paracetamol, 500 mg' in c.get('/audit?view=stocktake').data.decode())

print('\n--- 8. Bad rows on the sheet ---')
add('Aspirin, 100 mg', 50)
r = c.post('/stock-take', data={
    'action': 'count_sheet', 'stock_take_id': sid,
    'med_name': ['Aspirin, 100 mg', 'Ghost Item, 1 mg'],
    'counted__0': '-5', 'counted__1': '10',
    'note__0': '', 'note__1': ''}, follow_redirects=True)
check('a negative count is skipped', meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 50)
check('an unknown medication is skipped', meds.count_documents({}) == 2)
check('skipped rows are reported', 'skipped as invalid or unknown' in r.data.decode())
r = c.post('/stock-take', data={'action': 'count_sheet', 'stock_take_id': sid,
                                'med_name': ['Aspirin, 100 mg'],
                                'counted__0': 'abc', 'note__0': ''}, follow_redirects=True)
check('a non-numeric count is skipped', meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 50)
r = c.post('/stock-take', data={'action': 'count_sheet', 'stock_take_id': sid,
                                'med_name': ['Aspirin, 100 mg'],
                                'counted__0': '', 'note__0': ''}, follow_redirects=True)
check('an entirely blank post says so', 'No counts were entered' in r.data.decode())
c.post('/stock-take', data={'action': 'close', 'stock_take_id': sid}, follow_redirects=True)
r = c.post('/stock-take', data={'action': 'count_sheet', 'stock_take_id': sid,
                                'med_name': ['Aspirin, 100 mg'],
                                'counted__0': '40', 'note__0': ''}, follow_redirects=True)
check('a closed stock take rejects a posted sheet',
      meds.find_one({'name': 'Aspirin, 100 mg'})['balance'] == 50)

print('\n--- One malformed old record must not take the page down ---')
# transaction_id was read with t['transaction_id']. A dispense record without
# that field groups under None, which the $in query then matches, and the
# KeyError returned a 500 for the WHOLE page: no form, no history, no way to
# dispense anything at all until the record was found and fixed by hand.
reset()
login()
add('Paracetamol, 500 mg', 100)
txs.insert_one({'type': 'dispense', 'med_name': 'Paracetamol, 500 mg',
                'quantity': 5, 'price': 1.0, 'line_value': 5.0,
                'patient': 'Legacy Record', 'outcome': 'Outpatient',
                'user': 'Letlotlo Hlaoli',
                'timestamp': datetime.utcnow()})          # no transaction_id
r = c.get('/dispense')
check('the dispensing page still loads', r.status_code == 200, str(r.status_code))
_b = r.data.decode()
check('and the form is still there to use', 'name="patient"' in _b)
# and it is still possible to dispense
r = c.post('/dispense', data={
    'patient': 'After The Bad Record', 'company': 'X', 'position': 'Y',
    'birth_decade': '1990', 'gender': 'Male', 'outcome': 'Outpatient',
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'date': datetime.utcnow().strftime('%Y-%m-%d'),
    'med_names': 'Paracetamol, 500 mg', 'quantities': '1',
    'diagnoses': 'Headache'}, follow_redirects=True)
check('dispensing still works with that record in the database',
      txs.count_documents({'patient': 'After The Bad Record'}) == 1,
      str(r.data.decode()[:200]))

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
