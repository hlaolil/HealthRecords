"""Cross-site request forgery, and the session cookie.

Every one of the POST routes in this app changed data on the word of a
session cookie alone. A page on another site could put up a form aimed at
this one — write off stock, delete a visit, add a user — and a pharmacist who
happened to be logged in would submit it by clicking anything on that page.
Nothing in the audit log would look unusual, because as far as this app was
concerned the pharmacist did it.

Note that this suite does NOT arm its client with the token: the whole point
is to post without one and be refused.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
os.environ['SECRET_KEY'] = 'k'
os.environ['MONGO_URI'] = 'mongodb://localhost:27017'
import mongomock, pymongo, re as _re
from datetime import datetime, timezone

_f = mongomock.MongoClient()
pymongo.MongoClient = lambda *a, **k: _f
import app as A
A.get_mongo_client = lambda _client=_f: _client
A.init_db_collections()
db = _f['pharmacy_db']
meds, txs = db['medications'], db['transactions']
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def fresh_client(role='admin', name='Letlotlo Hlaoli'):
    cl = A.app.test_client()
    with cl.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()
    return cl


def token_of(cl):
    with cl.session_transaction() as s:
        return s.get(A.CSRF_SESSION_KEY)


print('\n--- 1. A state-changing post without the token is refused ---')
meds.delete_many({})
txs.delete_many({})
meds.insert_one({'name': 'Morphine tabs, 10mg', 'balance': 100, 'price': 1.0,
                 'expiry_date': '2028-03-31', 'batch': 'B',
                 'category': 'controlled', 'schedule': 'controlled',
                 'pack_size': 1, 'pack_price': 1.0})
c = fresh_client()
c.get('/dispense')                       # the token is issued on a page load
check('the session now holds a token', bool(token_of(c)), str(token_of(c)))

attack = c.post('/dispense', data={
    'patient': 'Forged Visit', 'company': 'X', 'position': 'Y',
    'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'date': A.local_now().strftime('%Y-%m-%d'),
    'med_names': 'Morphine tabs, 10mg', 'quantities': '50',
    'diagnoses': 'Headache'})
check('the request is refused', attack.status_code == 400, str(attack.status_code))
check('and NOTHING was recorded',
      txs.count_documents({'patient': 'Forged Visit'}) == 0)
check('the morphine is still on the shelf',
      meds.find_one({'name': 'Morphine tabs, 10mg'})['balance'] == 100,
      str(meds.find_one({'name': 'Morphine tabs, 10mg'})['balance']))
body = attack.data.decode()
check('the person is told nothing was saved', 'Nothing was saved' in body)
check('and that their typing may still be recoverable',
      'not lost' in body.lower(), body[:200])
check('with a way back', '/login' in body)

print('\n--- 2. A wrong token is no better than none ---')
wrong = c.post('/dispense', data={
    'patient': 'Forged Again', 'company': 'X', 'position': 'Y',
    'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'date': A.local_now().strftime('%Y-%m-%d'),
    'med_names': 'Morphine tabs, 10mg', 'quantities': '50',
    'diagnoses': 'Headache', A.CSRF_FIELD_NAME: 'not-the-real-token'})
check('a guessed token is refused', wrong.status_code == 400,
      str(wrong.status_code))
check('and still nothing was recorded',
      txs.count_documents({'patient': 'Forged Again'}) == 0)

print('\n--- 3. The real token lets the work through ---')
good = c.post('/dispense', data={
    'patient': 'Real Visit', 'company': 'Letseng', 'position': 'Driver',
    'birth_decade': '1980', 'gender': 'Male', 'outcome': 'Outpatient',
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'date': A.local_now().strftime('%Y-%m-%d'),
    'med_names': 'Morphine tabs, 10mg', 'quantities': '5',
    'diagnoses': 'Headache', A.CSRF_FIELD_NAME: token_of(c)},
    follow_redirects=True)
check('the visit is recorded', txs.count_documents({'patient': 'Real Visit'}) == 1,
      str(good.data.decode()[:160]))
check('and the stock moved',
      meds.find_one({'name': 'Morphine tabs, 10mg'})['balance'] == 95,
      str(meds.find_one({'name': 'Morphine tabs, 10mg'})['balance']))

print('\n--- 4. The token may also travel in a header, for the keep-alive ---')
# /api/session-ping is a POST that is not a form.
ping = c.post('/api/session-ping')
check('the ping is refused without the token', ping.status_code == 400,
      str(ping.status_code))
ping = c.post('/api/session-ping', headers={A.CSRF_HEADER: token_of(c)})
check('and accepted with it', ping.status_code == 200, str(ping.status_code))
check('the page gives the ping somewhere to read the token from',
      'name="csrf-token"' in c.get('/dispense').data.decode())
check('and the ping actually sends it',
      "'X-CSRF-Token': tokenMeta.content" in c.get('/dispense').data.decode())

print('\n--- 5. Every POST form on every page carries the field ---')
# 43 forms across 17 templates. The field is added to the rendered page
# rather than written into each template, because a scheme that relies on
# remembering to add a line to 43 forms is a scheme that protects 42 of them.
pages = ['/', '/dispense', '/receive', '/add-medication', '/internal-issue',
         '/stock-take', '/temperature', '/key-register', '/loans',
         '/loan-items', '/audit', '/reports']
total_forms = 0
for path in pages:
    page = c.get(path, follow_redirects=True).data.decode()
    forms = _re.findall(r'<form\b[^>]*method="POST"[^>]*>(.{0,400})', page, _re.S)
    total_forms += len(forms)
    missing = [f[:80] for f in forms if A.CSRF_FIELD_NAME not in f]
    check(f'{path}: all {len(forms)} POST forms carry the token',
          not missing, str(missing))
check('and that was a real number of forms, not zero', total_forms >= 20,
      str(total_forms))

anon = A.app.test_client()
login_page = anon.get('/login').data.decode()
_forms = _re.findall(r'<form\b[^>]*method="POST"[^>]*>(.{0,400})', login_page, _re.S)
check('the login form carries one too',
      _forms and all(A.CSRF_FIELD_NAME in f for f in _forms), str(_forms[:1]))

print('\n--- 6. A GET is never refused, and never altered ---')
for path in ['/dispense', '/reports', '/audit']:
    r = c.get(path)
    check(f'{path} still loads', r.status_code == 200, str(r.status_code))
csv_page = c.get('/audit?export=csv')
check('a CSV download is not rewritten on the way out',
      A.CSRF_FIELD_NAME not in csv_page.data.decode(),
      'the field is only ever added to HTML')

print('\n--- 7. An expired session gets the login page, not a rejection ---')
# This matters: refusing the post outright would lose a half-typed visit for
# somebody whose session simply timed out. The login redirect is the kinder
# and equally safe answer, and the browser draft then puts their work back.
stale = A.app.test_client()
r = stale.post('/dispense', data={'patient': 'Timed Out'},
               follow_redirects=False)
check('a session with no token at all is not rejected outright',
      r.status_code != 400, str(r.status_code))
check('it is sent to the login page instead',
      r.status_code in (301, 302) and '/login' in (r.headers.get('Location') or ''),
      f"{r.status_code} {r.headers.get('Location')}")
check('and nothing was recorded', txs.count_documents({'patient': 'Timed Out'}) == 0)

print('\n--- 8. The session cookie itself ---')
check('it is kept away from scripts',
      A.app.config['SESSION_COOKIE_HTTPONLY'] is True)
check('a form on another site cannot send it',
      A.app.config['SESSION_COOKIE_SAMESITE'] == 'Lax',
      str(A.app.config['SESSION_COOKIE_SAMESITE']))
check('it is set over plain HTTP locally, so a local copy still works',
      A.app.config['SESSION_COOKIE_SECURE'] is False,
      'VERCEL is not set in this environment')
check('and the deployed app turns Secure on by itself',
      "os.environ.get('VERCEL')" in open('app.py').read(),
      'Vercel sets VERCEL for every deployment')

print('\n--- 9. The token is a token, not a guess ---')
check('it is long enough to be unguessable', len(token_of(c)) >= 32,
      str(len(token_of(c))))
other = fresh_client()
other.get('/dispense')
check('two sessions do not share one', token_of(c) != token_of(other))
check('it is compared in constant time',
      'hmac.compare_digest' in open('app.py').read(),
      'a plain == leaks the answer one character at a time')

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
