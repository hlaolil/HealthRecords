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
check('and what it was worth', 'M214.00' in text(body), text(body)[:220])

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

body = c.get('/sell').data.decode()
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
pages = ['/sell', '/sales', '/write-off', '/receive', '/temperature',
         '/dashboard', '/reports', '/add-medication', '/stock-take']
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
