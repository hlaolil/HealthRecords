"""Work the app should not be asking the user to do.

Every check here is about a field the app already knows the answer to, or a
chore it could do in one pass instead of N.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re, json

def local_today():
    return __import__('app').local_now().strftime('%Y-%m-%d')
from datetime import datetime, timedelta, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
# Bound by VALUE on purpose. As a closure over the name `_f`, any later
# assignment to `_f` anywhere in this file would silently replace the database
# for the rest of the run — which is exactly what one stray loop variable did.
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
meds, txs = db['medications'], db['transactions']
c = A.app.test_client()
# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf, page_source

# The pages are template files now; a page is assembled from base.html plus
# its own file, and page_source() puts them back together for these checks.
_dispense_src = page_source('dispense.html')
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='Letlotlo Hlaoli'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['medications', 'transactions', 'audit_log']:
        db[col].delete_many({})


def add(name, bal=1000, pack_size=100, schedule='not controlled'):
    meds.insert_one({'name': name, 'balance': bal, 'batch': 'B1', 'price': 2.5,
                     'pack_size': pack_size, 'pack_price': 250.0,
                     'expiry_date': '2030-01-01', 'schedule': schedule})


def field(body, name, tag='input'):
    m = _re.search(r'<' + tag + r'[^>]*name="' + _re.escape(name) + r'"[^>]*>', body)
    return m.group(0) if m else ''


def text(body):
    return _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', ' ', body))


TODAY = A.local_now().strftime('%Y-%m-%d')
MONTH_START = A.local_now().replace(day=1).strftime('%Y-%m-%d')


print('--- 1. Dispensing: the date is not typed ---')
reset()
login()
add('Paracetamol, 500 mg')
body = c.get('/dispense').data.decode()
check('the date box opens on today', f'value="{TODAY}"' in field(body, 'date'),
      field(body, 'date'))
check('and cannot be set in the future', 'max=' in field(body, 'date'),
      field(body, 'date'))
check('today comes from the pharmacy clock, not the server clock',
      TODAY == A.local_now().strftime('%Y-%m-%d'))

print('\n--- 2. Dispensing: the dispenser is whoever is logged in ---')
_sel = body[body.index('<select name="dispenser"'):]
_sel = _sel[:_sel.index('</select>')]
check('the logged-in user is pre-selected',
      _re.search(r'<option value="Letlotlo Hlaoli"[^>]*selected', _sel) is not None,
      _sel[:300])
check('only one option is selected', _sel.count('selected') == 1, str(_sel.count('selected')))

login(name='Mathuto Kutoane')
_sel = c.get('/dispense').data.decode()
_sel = _sel[_sel.index('<select name="dispenser"'):]
_sel = _sel[:_sel.index('</select>')]
check('a different user gets their own name',
      _re.search(r'<option value="Mathuto Kutoane"[^>]*selected', _sel) is not None)

login(name='Somebody Not Listed')
_sel = c.get('/dispense').data.decode()
_sel = _sel[_sel.index('<select name="dispenser"'):]
_sel = _sel[:_sel.index('</select>')]
check('a name that is not an issuer selects nothing, rather than guessing',
      'selected' not in _sel, _sel[:200])

print('\n--- 3. A returning patient is not re-interviewed ---')
reset()
login()
add('Paracetamol, 500 mg')
first = c.post('/dispense', data={
    'patient': 'Thabo Mokoena', 'company': 'Acme Mine', 'position': 'Miner',
    'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
    'diagnoses': 'influenza', 'prescriber': 'Dr Khoele',
    'dispenser': 'Letlotlo Hlaoli', 'date': TODAY,
    'med_names': ['Paracetamol, 500 mg'], 'quantities': ['10']},
    follow_redirects=True)
check('the first visit is recorded', txs.count_documents({'type': 'dispense'}) == 1)

r = c.get('/api/patient-lookup?name=Thabo Mokoena')
d = json.loads(r.data)
check('their company comes back', d.get('company') == 'Acme Mine', str(d))
check('their position comes back', d.get('position') == 'Miner')
check('their birth decade comes back', str(d.get('birth_decade')) == '1980', str(d.get('birth_decade')))
check('their gender comes back', d.get('gender') == 'Male')
check('and the visit it came from is named', d.get('last_visit') == TODAY, str(d.get('last_visit')))

d2 = json.loads(c.get('/api/patient-lookup?name=thabo mokoena').data)
check('the match ignores case', d2.get('company') == 'Acme Mine')
d3 = json.loads(c.get('/api/patient-lookup?name=Thabo').data)
check('a partial name does NOT match somebody else',
      d3 == {}, 'a loose match would attach one person\'s employer to another')
d4 = json.loads(c.get('/api/patient-lookup?name=Never Seen').data)
check('an unknown patient returns nothing', d4 == {})
d5 = json.loads(c.get('/api/patient-lookup?name=').data)
check('an empty name returns nothing', d5 == {})
d6 = json.loads(c.get('/api/patient-lookup?name=O%27Brien+%28snr%29').data)
check('a name with regex characters does not crash', d6 == {})

# the most recent visit wins, because people change employer
c.post('/dispense', data={
    'patient': 'Thabo Mokoena', 'company': 'Beta Mine', 'position': 'Foreman',
    'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
    'diagnoses': 'back pain', 'prescriber': 'Dr Khoele',
    'dispenser': 'Letlotlo Hlaoli', 'date': TODAY,
    'med_names': ['Paracetamol, 500 mg'], 'quantities': ['5']},
    follow_redirects=True)
d7 = json.loads(c.get('/api/patient-lookup?name=Thabo Mokoena').data)
check('the most recent visit is the one recalled',
      d7.get('company') == 'Beta Mine' and d7.get('position') == 'Foreman',
      str(d7))

body = c.get('/dispense').data.decode()
check('the form carries the recall script', 'recallPatient' in body)
# This check used to assert "it only fills empty fields" — the original rule,
# and the bug itself: once a patient had been recalled the boxes were no longer
# empty, so the next name filled nothing and the previous patient's employer
# stayed on screen. The rule is about WHO put the value there, not whether
# there is one.
check('the app may replace what the app itself filled in',
      "el.dataset.autofilled !== '1' && el.value" in body
      and "el.dataset.autofilled = '1'" in body,
      'the rule must be ownership, not emptiness')
check('and never touches what the user typed',
      'clearRecalled' in body and "dataset.autofilled === '1'" in body)
check('it says which visit the details came from',
      'Filled from the last visit on' in body)
check('and warns that two people can share a name',
      'same person' in body)
check('a lookup needs a real name before it fires',
      'name.length < 3' in body)

r = c.get('/api/patient-lookup?name=Thabo Mokoena')
check('the lookup is behind the login', r.status_code == 200)
with c.session_transaction() as s:
    s.clear()
r = c.get('/api/patient-lookup?name=Thabo Mokoena', follow_redirects=False)
check('and refuses an anonymous caller', r.status_code in (301, 302),
      str(r.status_code))
login()

print('\n--- 4. Receiving: the product describes itself ---')
reset()
login()
add('Amoxicillin, 250 mg', pack_size=500, schedule='not controlled')
add('Morphine, 10 mg', pack_size=10, schedule='controlled')
d = json.loads(c.get('/api/medication-defaults').data)
check('every stocked item is listed', len(d) == 2, str(len(d)))
check('its pack size comes back', d['Amoxicillin, 250 mg']['pack_size'] == 500,
      str(d.get('Amoxicillin, 250 mg')))
check('its schedule comes back',
      d['Morphine, 10 mg']['schedule'] == 'controlled')
check('the price is NOT offered',
      'price' not in d['Amoxicillin, 250 mg']
      and 'pack_price' not in d['Amoxicillin, 250 mg'],
      'price belongs to this invoice, not to the product')

body = c.get('/receive').data.decode()
check('the receive form carries the fill script', 'fillFromMedication' in body)
check('it fills pack size', '[name="pack_size"]' in body)
check('it does not overwrite a pack size already changed',
      "ps.value === '1'" in body)
check('it does not overwrite a schedule the user picked',
      'dataset.touched' in body)
check('it explains why price is left alone',
      'belongs to this invoice' in body or 'NOT filled' in body)
check('the stock receiver defaults to whoever is logged in',
      'value="Letlotlo Hlaoli"' in field(body, 'stock_receiver'),
      field(body, 'stock_receiver'))

# Every page that asks who did it should answer for itself. Checking only the
# receive form let the Add Medication one sit blank: an earlier fix to it was
# lost in a failed edit and no test noticed, because no test looked there.
for _path, _label in [('/receive', 'receiving'), ('/add-medication', 'add medication')]:
    _page = c.get(_path, follow_redirects=True).data.decode()
    _fld = field(_page, 'stock_receiver')
    check(f'{_label} names the logged-in user by default',
          'value="Letlotlo Hlaoli"' in _fld, f'{_path}: {_fld}')

print('\n--- 5. Reports open on the month to date ---')
reset()
login()
body = c.post('/reports', data={}).data.decode()
check('the start date defaults to the first of the month',
      f'value="{MONTH_START}"' in field(body, 'start_date'),
      field(body, 'start_date'))
check('the end date defaults to today',
      f'value="{TODAY}"' in field(body, 'end_date'), field(body, 'end_date'))
body = c.post('/reports', data={'report_type': 'inventory',
                                'start_date': '2026-01-01',
                                'end_date': '2026-01-31'}).data.decode()
check('a period the user chose is kept, not overwritten',
      'value="2026-01-01"' in field(body, 'start_date'),
      field(body, 'start_date'))

print('\n--- 6. Internal issues must NOT name the issuer as the receiver ---')
# "Requested / Received By" is whoever in the department asked for the stock
# and took it. The pharmacist issuing it is recorded separately from the
# session, so defaulting this to the logged-in user put the issuer's own name
# in the receiver's column on every transfer.
reset()
login()
body = c.get('/internal-issue').data.decode()
_fld = field(body, 'issued_to')
check('the receiver is NOT pre-filled with the logged-in user',
      'Letlotlo Hlaoli' not in _fld, _fld)
check('and is not pre-filled with anyone else either',
      'value=' not in _fld, _fld)
check('it still has to be answered', 'required' in _fld, _fld)
check('previous receivers are offered to save the typing',
      'list="receiver_suggestions"' in _fld and 'receiver_suggestions' in body)

# the suggestions come from who has actually received before
txs.insert_many([
    {'type': 'internal_issue', 'med_name': 'Paracetamol, 500 mg',
     'quantity': -5, 'department': 'Lab', 'issued_to': 'Sr. Mokoena',
     'user': 'Letlotlo Hlaoli', 'timestamp': datetime.utcnow()},
    {'type': 'internal_issue', 'med_name': 'Paracetamol, 500 mg',
     'quantity': -3, 'department': 'PHC', 'issued_to': 'Sr. Nkoe',
     'user': 'Letlotlo Hlaoli', 'timestamp': datetime.utcnow()},
])
check('past receivers are listed', sorted(A._past_receivers(db))
      == ['Sr. Mokoena', 'Sr. Nkoe'], str(A._past_receivers(db)))
body = c.get('/internal-issue').data.decode()
check('and appear on the form', 'Sr. Mokoena' in body and 'Sr. Nkoe' in body)
check('the issuer is still recorded from the session, not typed',
      'name="issued_by"' not in body,
      'the issuer comes from who is logged in')

print('\n--- 7. Clearing expired stock is one pass ---')
reset()
login()
yesterday = (A.local_now() - timedelta(days=1)).strftime('%Y-%m-%d')
for n, bal in (('Expired A', 100), ('Expired B', 40), ('Expired C', 7)):
    meds.insert_one({'name': n, 'balance': bal, 'batch': 'B', 'price': 2.0,
                     'pack_size': 1, 'pack_price': 2.0,
                     'expiry_date': yesterday, 'schedule': 'not controlled'})
body = c.post('/reports', data={'report_type': 'expired_list',
                                'start_date': MONTH_START,
                                'end_date': TODAY}).data.decode()
check('the list offers a single pass', 'Remove All Ticked' in body)
check('every row is ticked to begin with', body.count('name="sel"') == 3,
      str(body.count('name="sel"')))
check('each row carries its own quantity',
      'name="qty__Expired A"' in body and 'name="qty__Expired C"' in body)
check('pre-filled with the balance on hand',
      _re.search(r'name="qty__Expired B"[^>]*value="40"', body) is not None,
      field(body, 'qty__Expired B'))
check('the bulk form sits outside the table, so the row forms still work',
      body.index('id="bulk-expiry"') < body.index('<table'),
      'nested forms do not work; the row inputs join it by form=""')
check('and the row inputs join it', 'form="bulk-expiry"' in body)
check('the per-item buttons are still there for a single write-off',
      'remove-expired' in body and 'Remove Expired' in body)

r = c.post('/remove-expired-bulk', data={
    'report_type': 'expired_list', 'sel': ['Expired A', 'Expired C'],
    'qty__Expired A': '100', 'qty__Expired C': '7',
    'bulk_reason': 'quarterly clear-out'}, follow_redirects=True)
check('the ticked items are written off',
      meds.find_one({'name': 'Expired A'})['balance'] == 0
      and meds.find_one({'name': 'Expired C'})['balance'] == 0)
check('the unticked one is untouched',
      meds.find_one({'name': 'Expired B'})['balance'] == 40)
check('each write-off is its own ledger entry',
      txs.count_documents({'type': 'expiry_removal'}) == 2)
check('signed negative, like every other write-off',
      all(t['quantity'] < 0 for t in txs.find({'type': 'expiry_removal'})))
check('the reason is carried onto each line',
      all(t['reason'] == 'quarterly clear-out'
          for t in txs.find({'type': 'expiry_removal'})))
check('the balance before is recorded for the audit trail',
      sorted(t['balance_before'] for t in txs.find({'type': 'expiry_removal'}))
      == [7, 100])
check('each one is audited separately',
      db['audit_log'].count_documents({'target_type': 'expiry_removal'}) == 2)
body = r.data.decode()
check('the result says how much went', '2 items written off' in text(body),
      text(body)[:220])
check('and what it was worth', 'R214.00' in text(body), text(body)[:220])

print('\n--- 7b. One bad line does not cost the whole pass ---')
reset()
login()
for n, bal in (('Good One', 50), ('Too Many', 5)):
    meds.insert_one({'name': n, 'balance': bal, 'batch': 'B', 'price': 1.0,
                     'pack_size': 1, 'pack_price': 1.0,
                     'expiry_date': yesterday, 'schedule': 'not controlled'})
r = c.post('/remove-expired-bulk', data={
    'report_type': 'expired_list',
    'sel': ['Good One', 'Too Many', 'Not There'],
    'qty__Good One': '50', 'qty__Too Many': '999', 'qty__Not There': '1'},
    follow_redirects=True)
check('the good line still goes through',
      meds.find_one({'name': 'Good One'})['balance'] == 0)
check('the over-quantity line is refused',
      meds.find_one({'name': 'Too Many'})['balance'] == 5)
body = text(r.data.decode())
check('the result names what was skipped and why',
      'Too Many' in body and 'only 5 on hand' in body, body[:260])
check('and names the one that does not exist',
      'Not There' in body and 'not found' in body, body[:260])
check('the banner is amber when some went and some did not',
      'message partial' in r.data.decode(),
      _re.search(r'class="message \w+"', r.data.decode()).group(0)
      if _re.search(r'class="message \w+"', r.data.decode()) else 'none')

r = c.post('/remove-expired-bulk', data={'report_type': 'expired_list',
                                         'qty__Good One': '1'},
           follow_redirects=True)
check('ticking nothing removes nothing', 'Nothing was ticked' in text(r.data.decode()))
r = c.post('/remove-expired-bulk', data={
    'report_type': 'expired_list', 'sel': ['Too Many'],
    'qty__Too Many': 'abc'}, follow_redirects=True)
check('a non-numeric quantity is skipped, not crashed',
      meds.find_one({'name': 'Too Many'})['balance'] == 5)
check('and is named', 'not a number' in text(r.data.decode()))

print('\n--- 7c. Bulk write-off is admin only ---')
reset()
login('pharmacist', 'A. Nurse')
meds.insert_one({'name': 'Expired X', 'balance': 10, 'batch': 'B', 'price': 1.0,
                 'pack_size': 1, 'pack_price': 1.0, 'expiry_date': yesterday,
                 'schedule': 'not controlled'})
r = c.post('/remove-expired-bulk', data={'report_type': 'expired_list',
                                         'sel': ['Expired X'],
                                         'qty__Expired X': '10'},
           follow_redirects=True)
check('a non-admin cannot write stock off in bulk',
      meds.find_one({'name': 'Expired X'})['balance'] == 10)
check('and is told why', 'Only admins' in text(r.data.decode()))
body = c.post('/reports', data={'report_type': 'expired_list',
                                'start_date': MONTH_START,
                                'end_date': TODAY}).data.decode()
check('a non-admin is not shown the bulk control',
      'Remove All Ticked' not in body)

print('\n--- 2b. The defaults survive posting a visit ---')
# Clearing the form after a successful dispense blanked every field in the
# common section, including the two the app had just filled in. A default that
# lasts only until the first patient is not a default.
reset()
login()
add('Paracetamol, 500 mg')
body = c.get('/dispense').data.decode()
check('the form knows what its defaults are', 'FORM_DEFAULTS' in body)
check('they come from one dict the server builds',
      set(A._new_visit_defaults('X')) == {'date', 'dispenser', 'outcome'},
      str(sorted(A._new_visit_defaults('X'))))
check('including the outcome, which the first version left out',
      A._new_visit_defaults('X')['outcome'] == 'Outpatient')
# Target the DECLARATION, not the first mention — which is inside clearForm.
def defaults_decl(b):
    i = b.find('var FORM_DEFAULTS')
    return b[i:b.index('\n', i)] if i >= 0 else ''

_decl = defaults_decl(body)
check('the date default is today', f'"{TODAY}"' in _decl, _decl)
check('the issuer default is the logged-in user',
      '"Letlotlo Hlaoli"' in _decl, _decl)
check('the outcome default is there too', '"Outpatient"' in _decl, _decl)

# The template renders the form from the SAME dict the script restores from.
# They were separate before, which is how Outpatient came back on a fresh
# page but went blank after every saved visit — so the second patient of the
# day had no default at all.
_sel = body[body.index('<select name="outcome"'):]
_sel = _sel[:_sel.index('</select>')]
check('a fresh form really does start on Outpatient',
      _re.search(r'<option value="Outpatient"[^>]*selected', _sel) is not None, _sel[:220])
check('and only that one is selected', _sel.count('selected') == 1,
      str(_sel.count('selected')))
check('every rendered default is one the script will restore',
      all(f'"{v}"' in _decl for v in A._new_visit_defaults('Letlotlo Hlaoli').values()),
      _decl)
_cf = body[body.index('function clearForm'):]
_cf = _cf[:_cf.index('\n}')]
check('clearing the form puts the defaults back', 'FORM_DEFAULTS' in _cf,
      'otherwise the date goes blank after every visit')
check('and it does so AFTER blanking everything',
      _cf.index("el.value = ''") < _cf.index('FORM_DEFAULTS'),
      'restoring before blanking would be undone immediately')

# The defaults are interpolated into JavaScript, so a name with an apostrophe
# in it must not be able to break the script — that exact mistake took out the
# whole dispensing page once already.
login(name="Sean O'Brien")
body = c.get('/dispense').data.decode()
_decl = defaults_decl(body)
check('a name with an apostrophe is escaped, not pasted raw',
      "O'Brien" not in _decl, _decl)
check('it is JSON-escaped instead', '\\u0027' in _decl, _decl)
_scripts = _re.findall(r'<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>', body)
_defsrc = [x for x in _scripts if 'FORM_DEFAULTS' in x]
check('and the script still parses with that name in it', len(_defsrc) == 1)
login()

print('\n--- 7d. Changing the name refreshes what was recalled ---')
# The first version only filled EMPTY fields, so once a patient had been
# recalled the boxes were no longer empty and a second name filled nothing —
# leaving the previous patient's employer sitting on the next patient's visit.
# This runs the page's own code against a DOM stub, so it tests the behaviour
# and not the presence of the source text.
import subprocess, tempfile, shutil, json as _json

NODE = shutil.which('node') or '/opt/node22/bin/node'
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'recall_harness.js')

reset()
login()
add('Paracetamol, 500 mg')
body = c.get('/dispense').data.decode()
_scripts = _re.findall(r'<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>', body)
_recall = [x for x in _scripts if 'recallPatient' in x]
check('the recall code is on the page', len(_recall) == 1, str(len(_recall)))
check('the recall harness is available', os.path.exists(HARNESS), HARNESS)

if _recall and os.path.exists(HARNESS):
    records = {
        'Thabo Mokoena': {'patient': 'Thabo Mokoena', 'company': 'Acme Mine',
                          'position': 'Miner', 'birth_decade': '1980',
                          'gender': 'Male', 'last_visit': '2026-09-15'},
        'Palesa Nkoe': {'patient': 'Palesa Nkoe', 'company': 'Beta Works',
                        'position': 'Nurse', 'birth_decade': '1990',
                        'gender': 'Female', 'last_visit': '2026-09-20'},
    }
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
        fh.write(_recall[0]); js_path = fh.name
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
        _json.dump(records, fh); rec_path = fh.name
    try:
        r = subprocess.run([NODE, HARNESS, js_path, rec_path],
                           capture_output=True, text=True, timeout=60)
        out = _json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except Exception as e:
        out, r = {}, None
        check('the harness ran', False, str(e))
    finally:
        os.unlink(js_path); os.unlink(rec_path)

    check('the first patient fills',
          out.get('first', {}).get('company') == 'Acme Mine', str(out.get('first')))
    check('changing the name REFRESHES every recalled field',
          out.get('changed', {}).get('company') == 'Beta Works'
          and out.get('changed', {}).get('position') == 'Nurse'
          and out.get('changed', {}).get('birth_decade') == '1990'
          and out.get('changed', {}).get('gender') == 'Female',
          str(out.get('changed')))
    check('a name with no record CLEARS what was recalled',
          all(out.get('unknown', {}).get(f) == ''
              for f in ('company', 'position', 'birth_decade', 'gender')),
          str(out.get('unknown')) + ' — stale details on a new patient is the '
          'worst case, not the harmless one')
    check('and the note is cleared with them',
          out.get('unknown', {}).get('note') == '', str(out.get('unknown')))
    check('a value the user typed survives a name change',
          out.get('typed', {}).get('company') == 'Typed By Hand',
          str(out.get('typed')))
    check('while the recalled ones still refresh around it',
          out.get('typed', {}).get('position') == 'Nurse', str(out.get('typed')))

    # The fields are REQUIRED. Blanking them the moment the name changed meant
    # the click that caused it — on Dispense — failed its own validation, so
    # pressing the button did nothing and said nothing.
    check('the details are NOT blanked while the lookup is in flight',
          out.get('midLookup', {}).get('company') == 'Acme Mine',
          str(out.get('midLookup')) + ' — empty required fields here make the '
          'Dispense click fail validation silently')
    check('and they are replaced once the answer arrives',
          out.get('afterLookup', {}).get('company') == 'Beta Works'
          and out.get('afterLookup', {}).get('position') == 'Nurse',
          str(out.get('afterLookup')))
    check('a late answer does not overwrite a newer one',
          out.get('outOfOrder', {}).get('company') == 'Beta Works',
          str(out.get('outOfOrder')) + ' — the earlier lookup answered last')
    check('the lookup never writes into the box the cursor is in',
          out.get('cursorInField', {}).get('company') == '',
          str(out.get('cursorInField')) + ' — an answer landing mid-word read '
          '"Acme MineWhat The User Typed"')
    check('but the others are still filled around it',
          out.get('cursorInField', {}).get('position') == 'Miner',
          str(out.get('cursorInField')))
    check('and the skipped field is filled once the cursor leaves it empty',
          out.get('afterLeaving', {}).get('company') == 'Acme Mine',
          str(out.get('afterLeaving')))

print('\n--- 7e. The details refresh while the name is being typed ---')
# A text input fires `change` only when the value is committed — on blur or
# Enter. Bound to change alone, the details sat on the previous patient's
# answers for as long as the cursor stayed in the name box.
check('the lookup is bound to typing, not only to leaving the field',
      "addEventListener('input', recallSoon)" in body, 'input listener missing')
check('it still runs on change and on blur',
      "addEventListener('change', recallPatient)" in body
      and "addEventListener('blur', recallPatient)" in body)
check('typing is debounced rather than one request per keystroke',
      'RECALL_DEBOUNCE_MS' in body and 'clearTimeout(recallTimer)' in body)
check('a lookup in flight is distinguished from one that has finished',
      'recallInFlightFor' in body and 'recallDetailsFor' in body)
check('submitting waits for the details to catch up with the name',
      "form.addEventListener('submit'" in body and 'recallSubmitting' in body)
check('but a slow or failed lookup can never block a dispense',
      'Promise.race' in body)
check('the lookup leaves the field the cursor is in alone',
      'document.activeElement === el' in body)
check('a detail field left empty asks again when the cursor leaves it',
      "addEventListener('blur', function () {" in body and 'recallSoon();' in body)

# The draft stored the values but not who filled them in, so after a reload
# the app believed the user had typed the patient's details and would never
# refresh them again — for the rest of that tab.
check('the draft records which details the app filled in',
      "data.auto[el.name] = 1" in body, 'draft loses ownership')
check('and restoring puts that ownership back',
      "(d.auto || {})[n]" in body and "el.dataset.autofilled = '1'" in body)
# The draft saves itself when a field raises an event, and recalled details
# are set by code, which raises none — so the draft kept the patient's NAME
# but not their details, and a restored visit came back half empty.
check('a recalled detail is saved into the draft too',
      'if (typeof draftSave === \'function\') draftSave();' in body,
      'the draft would keep the name but lose the details')
check('clearing the form resets the lookup state with it',
      "recallDetailsFor = '';" in body and 'recallInFlightFor = null;' in body)

print('\n--- 7f. Posting still clears the form ---')
reset()
login()
add('Paracetamol, 500 mg', 100)
r = c.post('/dispense', data={
    'patient': 'Thabo Mokoena', 'company': 'Acme Mine', 'position': 'Miner',
    'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'date': local_today(), 'med_names': 'Paracetamol, 500 mg',
    'quantities': '7', 'diagnoses': 'Headache'}, follow_redirects=True)
_b = r.data.decode()
check('the visit was recorded',
      txs.count_documents({'patient': 'Thabo Mokoena', 'type': 'dispense'}) == 1)
check('the page says so', 'successfully' in _b.lower(), _b[:160])
# The clear is driven by the server's own success message, not by the page
# searching its own text — a sentinel phrase written in the script matches
# itself, which is exactly how the delivery draft wiped itself on every load.
check('the clear is triggered by the server message, not by page text',
      "{% if message and ('successfully' in message|lower" in _dispense_src
      or "'successfully' in message|lower" in _dispense_src)
check('and it clears the draft as well as the boxes',
      'draftClear();' in _b and 'clearForm();' in _b,
      'both must run or the next load restores the visit just saved')
check('an EDIT is not cleared out from under the user',
      '{% if not tx_data %}' in _dispense_src)
check('the clear is NOT decided by reading the message text',
      "'successfully' in message|lower or 'updated' in message|lower) and 'partial"
      not in _dispense_src,
      'a partial dispense says "Partial", so wording-based clearing left a '
      'recorded visit in the form')

print('\n--- 7g. A partial dispense is still a recorded dispense ---')
# Reported: "Partial - insufficient stock. Dispensed: A, B, C | Short: X" left
# the whole visit sitting in the form with the draft note under it. A, B and C
# had already been dispensed and their stock already deducted, so pressing
# Dispense again - the natural thing to do when a form looks unsubmitted -
# dispensed them a second time.

def visit(meds, qtys, patient='Thabo Mokoena'):
    return c.post('/dispense', data={
        'patient': patient, 'company': 'Acme Mine', 'position': 'Miner',
        'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
        'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
        'date': local_today(), 'med_names': meds, 'quantities': qtys,
        'diagnoses': 'Headache'}, follow_redirects=True).data.decode()

def clears(page):
    return 'draftClear();' in page and 'clearForm();' in page

reset()
login()
add('Paracetamol, 500 mg', 100)
meds.insert_one({'name': 'Short Item, 250 mg', 'balance': 0, 'price': 3.0,
                 'expiry_date': '2028-01-31', 'batch': 'SI1',
                 'category': 'general', 'pack_size': 10, 'pack_price': 30.0})

# A. some dispensed, one short
_p = visit(['Paracetamol, 500 mg', 'Short Item, 250 mg'], ['5', '15'])
check('a partial dispense reports what was short',
      'Partial' in _p and 'unmet demand' in _p, _p[:200])
check('the items that DID dispense were recorded',
      txs.count_documents({'type': 'dispense',
                           'med_name': 'Paracetamol, 500 mg'}) == 1)
check('so the form is cleared, not left one click from a second dispense',
      clears(_p), 'the form kept the visit after a partial')

# B. nothing could be recorded at all
reset()
login()
add('Paracetamol, 500 mg', 100)
_p = visit(['No Such Medicine, 1 mg'], ['5'])
check('an item not on file records nothing',
      txs.count_documents({'type': 'dispense'}) == 0)
check('and the form is KEPT so the user can fix it', not clears(_p))
check('with the message saying what to do',
      'Add the item under Add Medication' in _p)

# C. nothing dispensed, but the shortfall was recorded
reset()
login()
meds.insert_one({'name': 'Short Item, 250 mg', 'balance': 0, 'price': 3.0,
                 'expiry_date': '2028-01-31', 'batch': 'SI1',
                 'category': 'general', 'pack_size': 10, 'pack_price': 30.0})
_p = visit(['Short Item, 250 mg'], ['15'])
check('an out-of-stock item still records the unmet demand',
      txs.count_documents({'type': 'unmet_demand'}) == 1)
check('so that form is cleared too, or the demand is recorded twice',
      clears(_p))

# D. one dispensed, one not on file
reset()
login()
add('Paracetamol, 500 mg', 100)
_p = visit(['Paracetamol, 500 mg', 'No Such Medicine, 1 mg'], ['3', '2'])
check('a mixed result records the one that exists',
      txs.count_documents({'type': 'dispense'}) == 1)
check('and clears, because something was written', clears(_p))

# E. and the plain success case still clears
reset()
login()
add('Paracetamol, 500 mg', 100)
_p = visit(['Paracetamol, 500 mg'], ['2'])
check('a clean dispense clears as it always did', clears(_p))
check('the decision comes from the route, not the wording',
      'dispense_saved' in open('app.py').read())
# and the defaults survive the clear - this regressed twice before
for fld, val in [('date', local_today()), ('dispenser', 'Letlotlo Hlaoli'),
                 ('outcome', 'Outpatient')]:
    check(f'{fld} still has its default after posting',
          f'"{fld}": "{val}"' in _b or f'&#34;{fld}&#34;: &#34;{val}&#34;' in _b
          or val in field(_b, fld) or f"'{fld}': '{val}'" in _b,
          field(_b, fld)[:120])

# Editing a visit pre-fills the details from the record. Those belong to the
# patient named on it, so correcting the name has to refresh them.
txs.insert_one({
    'type': 'dispense', 'transaction_id': 'T-EDIT-1', 'med_name': 'Paracetamol, 500 mg',
    'quantity': 10, 'price': 1.0, 'line_value': 10.0, 'patient': 'Thabo Mokoena',
    'company': 'Acme Mine', 'position': 'Miner', 'birth_decade': 1980,
    'gender': 'Male', 'outcome': 'Outpatient', 'prescriber': 'Dr. Tau',
    'dispenser': 'Letlotlo Hlaoli', 'user': 'Letlotlo Hlaoli',
    'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    'timestamp': datetime.now(timezone.utc).replace(tzinfo=None)})
_tid = txs.find_one({'transaction_id': 'T-EDIT-1'})
if _tid and _tid.get('transaction_id'):
    edit = c.get(f"/dispense?edit={_tid['transaction_id']}").data.decode()
    _sec = edit[edit.index('class="common-section"'):]
    _sec = _sec[:_sec.index('</form>')] if '</form>' in _sec else _sec
    for f in ['company', 'position']:
        _i = _sec.find(f'name="{f}"')
        _tag = _sec[_sec.rindex('<input', 0, _i):_sec.index('>', _i) + 1]
        check(f'an edited visit marks {f} as the app\'s own, not hand-typed',
              'data-autofilled="1"' in _tag, _tag[:160])
else:
    check('there is a visit to edit', False, 'no dispense record seeded')

print('\n--- 8b. A search is not caged by the month the app chose ---')
# The dispensing and receiving lists show the current month when nothing else
# narrows them, and the route drops that window the moment there is a search —
# so a search really does look at everything. But the filter form rendered the
# app's own dates into its date boxes, and submitting a search sent them back
# with it. The promise under the heading was true on the first load and a lie
# from the second: looking up a patient last seen in March found nothing.
reset()
login()
add('Paracetamol, 500 mg', 500)
_old = (datetime.now(timezone.utc) - timedelta(days=120)).replace(tzinfo=None)
txs.insert_one({
    'type': 'dispense', 'transaction_id': 'V-OLD', 'patient': 'Teboho Ancient',
    'company': 'LMPS', 'position': 'Police', 'gender': 'Male',
    'birth_decade': 1970, 'outcome': 'Outpatient', 'diagnoses': ['Gout'],
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'user': 'Letlotlo Hlaoli', 'med_name': 'Paracetamol, 500 mg',
    'quantity': 20, 'price': 1.0, 'line_value': 20.0,
    'date': _old.strftime('%Y-%m-%d'), 'timestamp': _old})

def filter_form(page):
    """Just the filter form.

    The dispensing form carries its own hidden start_date and end_date so a
    save returns you to the list you were looking at, which is right and has
    nothing to do with searching. Only the visible boxes in the filter form
    decide what the next search asks for.
    """
    i = page.index('class="filter-form"')
    return page[i:page.index('</form>', i)]


body = c.get('/dispense').data.decode()
check('the month window is explained rather than silently applied',
      'Showing' in body and 'this month' in body)
check('an old visit is not in this month\'s list', 'Teboho Ancient' not in body)
_ff = filter_form(body)
_sd = field(_ff, 'start_date')
_ed = field(_ff, 'end_date')
check('but the date boxes are left EMPTY, so a search is not caged',
      'value=""' in _sd, _sd)
check('and the same for the end date', 'value=""' in _ed, _ed)
check('the dispensing form still remembers the view for after a save',
      'type="hidden" name="start_date"' in body,
      'saving a visit should return you to the list you were on')

body = c.get('/dispense?search=Teboho').data.decode()
check('searching finds a visit from four months ago', 'Teboho Ancient' in body)
_sd = field(filter_form(body), 'start_date')
check('and the result page still leaves the dates empty', 'value=""' in _sd, _sd)
check('so a second search from that page is not caged either',
      'Teboho Ancient' in c.get('/dispense?search=Ancient').data.decode())

# A range the USER typed is theirs and is kept.
_from = (datetime.now(timezone.utc) - timedelta(days=200)).strftime('%Y-%m-%d')
_to = datetime.now(timezone.utc).strftime('%Y-%m-%d')
body = c.get(f'/dispense?start_date={_from}&end_date={_to}').data.decode()
check('a range the user set is kept in the boxes',
      _from in field(filter_form(body), 'start_date'),
      field(filter_form(body), 'start_date'))
check('and is honoured', 'Teboho Ancient' in body)
check('a range that excludes the visit still excludes it',
      'Teboho Ancient' not in c.get(
          '/dispense?start_date=2020-01-01&end_date=2020-12-31').data.decode())

# Paging must not smuggle the app's dates back in.
check('paging links carry no dates while the window is the app\'s own',
      'start_date=&end_date=' in body.replace('&amp;', '&')
      or 'page=2' not in body, 'paging should not pin the default month')

print('\n--- 8c. The dispensing form is not a place for admin links ---')
body = c.get('/dispense').data.decode()
check('the roster link is gone from the form used all day',
      'Edit who appears' not in body)
check('the roster is still reachable where it is edited',
      'roster' in c.get('/key-register').data.decode().lower())

print('\n--- 9. A session that times out must not cost you the work ---')
reset()
login()
add('Paracetamol, 500 mg')

# The timeout is meant to measure inactivity, but the only activity it could
# see was a page request. Twenty minutes of typing a dispensing form makes no
# requests, so it looked exactly like twenty minutes of an empty room.
r = c.post('/api/session-ping')
check('there is a keep-alive the page can call', r.status_code == 200,
      str(r.status_code))
check('it reports the timeout it is defending', 'timeout_minutes' in r.data.decode())
check('it names the real figure',
      str(A.SESSION_TIMEOUT_MINUTES) in r.data.decode(), r.data.decode()[:80])

with c.session_transaction() as sess:
    sess.clear()
r = c.post('/api/session-ping', follow_redirects=False)
check('and it is behind the login like everything else',
      r.status_code in (301, 302), str(r.status_code))
login()

body = c.get('/dispense').data.decode()
check('every signed-in page carries the session guard',
      'session-ping' in body and 'session-warn' in body)
check('only real interaction counts as activity',
      "['keydown', 'click', 'input', 'change']" in body,
      'a bare timer would keep an unattended terminal signed in for ever')
check('a warning comes before the session goes', 'signed out in about' in body)
check('and the guard knows the configured timeout',
      f'= {A.SESSION_TIMEOUT_MINUTES} * 60 * 1000' in body,
      body[body.find('TIMEOUT_MS'):][:60])
check('the banner is hidden when printing', '.session-warn { display: none' in body)

print('\n--- 9b. The typed visit is kept in the browser ---')
check('the form keeps a draft as it is typed', 'draftSave' in body)
check('and puts it back on return', 'draftRestore' in body)
check('it lives only in the browser, per tab',
      'sessionStorage' in body and 'localStorage' not in body,
      'clinical data must not go into a cookie or outlive the tab')
check('a refused sessionStorage cannot break the form',
      body.count('catch (e)') >= 3, str(body.count('catch (e)')))
check('a saved visit clears the draft', 'draftClear();' in body)
_cf2 = body[body.index('function clearForm'):]
_cf2 = _cf2[:_cf2.index('\n}')]
check('so does clearing the form by hand', 'draftClear();' in _cf2, _cf2[-200:])
check('editing an existing record never overwrites the draft',
      body.count("tid && tid.value") >= 2,
      'an edit form belongs to a saved record, not to a draft')
check('the user is told the draft came back',
      'the visit you had started' in body)

print('\n--- 9c. The logout message tells the truth ---')
# It said 30 minutes while the timeout was 60.
import app as _A
_src = open(os.path.join(ROOT, 'app.py')).read() \
    if os.path.exists(os.path.join(ROOT, 'app.py')) else ''
check('the inactivity message is built from the real setting',
      'logged out after {SESSION_TIMEOUT_MINUTES} minutes' in _src
      or "f'You were logged out after {SESSION_TIMEOUT_MINUTES} minutes '" in _src,
      'a hard-coded figure drifts from the setting')
check('no stale 30-minute claim is left in the message',
      'logged out due to 30 minutes' not in _src)

print('\n--- 8. Every page\'s JavaScript actually parses ---')
# A Python-level \' escape inside a template became a bare apostrophe by the
# time the browser saw it, which broke the whole script block silently: the
# page rendered, the markup was all present, and nothing ran. Asserting that
# the text is on the page does not catch that. Parsing it does.
import subprocess, tempfile, shutil

NODE = shutil.which('node') or '/opt/node22/bin/node'
reset()
login()
add('Paracetamol, 500 mg')
pages = ['/dispense', '/receive', '/internal-issue', '/temperature',
         '/loans', '/key-register', '/dashboard', '/reports', '/add-medication',
         '/stock-take', '/loan-items']
checked = 0
for path in pages:
    body = c.get(path, follow_redirects=True).data.decode()
    scripts = _re.findall(r'<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>', body)
    for n, js in enumerate(scripts):
        if not js.strip():
            continue
        checked += 1
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
            # Parse only: wrapped so a bare return or await cannot upset it.
            fh.write('(async function(){\n' + js + '\n});')
            tmp = fh.name
        try:
            r = subprocess.run([NODE, '--check', tmp],
                               capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0
            detail = (r.stderr or '').strip().splitlines()
            detail = ' | '.join(detail[:3])
        except Exception as e:
            ok, detail = False, str(e)
        finally:
            os.unlink(tmp)
        check(f'{path} script #{n + 1} parses', ok, detail[:220])
check('several scripts were actually checked', checked >= 6, str(checked))

print(f"\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========")
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
