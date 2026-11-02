"""The app on a phone, a tablet and a desktop.

Measured before any of this was written: with no viewport meta tag a
Pixel-class phone laid every page out at 980px and then shrank it to fit, so
the dashboard rendered at 0.40 scale and the dispensing page at 0.25 — 16px
text drawn at about 4px, and a pinch-zoom needed before any tap. These checks
hold the structure that fixed it, because the symptom is invisible from a
desktop browser and nothing else in the suite would notice it going.
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
db = _f['pharmacy_db']
meds, txs = db['medications'], db['transactions']
c = A.app.test_client()
# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf, template_text
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


NOW = A.local_now()
meds.insert_many([
    {'name': 'Paracetamol tabs, 500 mg', 'balance': 500, 'price': 0.18,
     'expiry_date': '2029-03-31', 'batch': '250306', 'category': 'general',
     'pack_size': 1000, 'pack_price': 180.0},
    {'name': 'Morphine tabs, 10mg', 'balance': 59, 'price': 126.55,
     'expiry_date': '2028-03-31', 'batch': '90AUDA340', 'category': 'controlled',
     'pack_size': 10, 'pack_price': 1265.5, 'schedule': 'Schedule 6'},
])
txs.insert_one({
    'type': 'dispense', 'transaction_id': 'V-1', 'med_name': 'Paracetamol tabs, 500 mg',
    'quantity': 10, 'price': 0.18, 'line_value': 1.8, 'patient': 'Thabo Mokoena',
    'company': 'Letseng Diamonds', 'position': 'Drivers', 'gender': 'Male',
    'birth_decade': 1980, 'outcome': 'Outpatient', 'diagnoses': ['URTI'],
    'prescriber': 'Dr. T. Khothatso', 'dispenser': 'Letlotlo Hlaoli',
    'user': 'Letlotlo Hlaoli', 'date': NOW.strftime('%Y-%m-%d'), 'timestamp': NOW})

login()
PAGES = ['/', '/dispense', '/receive', '/add-medication', '/internal-issue',
         '/stock-take', '/temperature', '/key-register', '/loans',
         '/loan-items', '/audit', '/reports']

print('\n--- 1. Every page is a real document that says how wide the screen is ---')
pages = {}
for path in PAGES:
    r = c.get(path, follow_redirects=True)
    check(f'{path} loads', r.status_code == 200, str(r.status_code))
    pages[path] = r.data.decode()

# The login page is served to someone with no session, so check it that way.
c2 = A.app.test_client()
pages['/login'] = c2.get('/login').data.decode()

for path, body in pages.items():
    head = body[:900]
    check(f'{path} declares a doctype', head.lstrip().lower().startswith('<!doctype html'),
          head.lstrip()[:60])
    check(f'{path} declares its character set', '<meta charset="utf-8">' in head)
    ok = '<meta name="viewport" content="width=device-width, initial-scale=1">' in head
    check(f'{path} tells the phone to use the phone\'s width', ok,
          'without this the page is laid out at 980px and shrunk to fit')
    check(f'{path} has a title for the tab and for bookmarks', '<title>' in head)
    # The stylesheet and the session guard sit in the head, so the body tag
    # is a long way in; look for it anywhere, and before the page's own
    # content rather than at a fixed offset.
    check(f'{path} opens a body', '</head>' in body and '<body>' in body
          and body.index('<body>') > body.index('</head>'),
          f"head at {body.find('</head>')}, body at {body.find('<body>')}")

print('\n--- 2. Registers scroll inside their own box, not the page ---')
# The Dispensing register is 19 columns and about 1900px wide; the Key
# Register is 16. Both are wider than any screen, so without a scroll box of
# their own they drag the whole page sideways — on a laptop too, not only on
# a phone. Dropping columns is not an option: they are on the official form.
for path, body in pages.items():
    n_tables = len(_re.findall(r'<table\b', body))
    n_wrapped = len(_re.findall(r'<div class="table-scroll"><table\b', body))
    if n_tables:
        check(f'{path}: all {n_tables} tables sit in a scroll box',
              n_wrapped == n_tables, f'{n_wrapped} of {n_tables}')
        check(f'{path}: every scroll box is closed',
              body.count('</table></div>') == n_tables,
              f"{body.count('</table></div>')} of {n_tables}")

src = template_text('base.html')
check('the wrapper exists in the stylesheet', '.table-scroll {' in src)
check('and it scrolls sideways', 'overflow-x: auto;' in src)
check('the register still fills the page when it fits',
      'table {\n        width: 100%;' in src,
      'a table set to display:block stops stretching: the 7-column Audit Log '
      'shrank to 556px of a 1200px page')

print('\n--- 3. What changes on a small screen ---')
css = template_text('base.html')
check('there is a rule set for tablets and narrow windows',
      '@media (max-width: 820px)' in css,
      'the old breakpoint was 600px, narrower than a tablet held upright')
check('and one for phones', '@media (max-width: 480px)' in css)
check('and one for anything touched rather than clicked',
      '@media (pointer: coarse)' in css)
check('forms stop sitting in columns', 'grid-template-columns: 1fr;' in css)

print('\n--- 4. Things are big enough to hit with a finger ---')
# The Remove button beside a medication row measured 21px high.
for rule in ['min-height: 44px']:
    check(f'controls get a {rule.split()[1]} minimum', rule in css)
check('but not checkboxes, which would look wrong stretched',
      'input[type="checkbox"], input[type="radio"] { min-height: 0; }' in css)
check('and the submit button spans the screen rather than hiding in a corner',
      '.form-buttons input[type="submit"],' in css and 'width: 100%;' in css)

print('\n--- 5. Twelve nav links do not take the whole first screen ---')
# Wrapped over five rows of 44px, the sticky nav filled a phone screen and
# stayed there. One swipeable row instead.
nav_block = css[css.index('@media (max-width: 820px)'):]
nav_block = nav_block[:nav_block.index('@media (max-width: 480px)')]
check('the nav becomes one row that scrolls sideways',
      'white-space: nowrap;' in nav_block and 'overflow-x: auto;' in nav_block)
check('its links are still a finger wide', 'min-height: 36px;' in nav_block)

print('\n--- 6. What scrolls on screen must not be cut off on paper ---')
# These registers are printed. A box that clips on paper would lose columns
# from the record, which is the reason the form is printed at all.
pr = css[css.index('@media print'):]
pr = pr[:pr.index('}\n    }') + 6] if '}\n    }' in pr else pr[:600]
check('the scroll box is let out of its box for printing',
      'overflow: visible !important;' in pr, pr[:200])
check('and its edge shadows are not printed',
      'background-image: none !important;' in pr, pr[:200])
check('the sticky nav does not repeat down the page',
      '.nav-links { position: static; }' in pr, pr[:200])

print('\n--- 7. The temperature round fits a phone, since it is walked ---')
body = pages['/temperature']
check('the reading boxes shrink rather than push the range off the edge',
      '.temp-round-input { width: 68px;' in body, 'phone width rule missing')
check('the long column headings may break when there is no room',
      '&shy;' in body,
      '"Thermometer" alone was 92px wide for a single digit')
check('the acceptable range is still a column, not dropped',
      'Accept&shy;able' in body)

print('\n--- 8. One column is not enough on its own ---')
# Measured in a browser at 390px: the Temperature page still laid itself out
# at 458px and slid sideways, on the one page that is actually walked around
# the pharmacy rather than read at a desk. The single-column rule above was
# already in force. It was not enough, because a grid item will not shrink
# below its own content unless it is told it may — so the pair of buttons
# "Download CSV | Print Chart & Register", which refuse to sit on top of each
# other, set the width of the column, and the column set the width of the
# page. The dropdown and the month box ran off the edge of their own card.
small = css[css.index('@media (max-width: 820px)'):]
small = small[:small.index('@media (max-width: 480px)')]
check('grid items are allowed to shrink below their content',
      'min-width: 0;' in small,
      'without it one wide child sets the width of the whole page')
for sel in ('.filter-section > div', '.common-section > div'):
    check(f'{sel} is one of them', sel in small)
check('and a row of buttons wraps rather than widening the page',
      '.button-div { flex-wrap: wrap; }' in small)
check('each button then takes the width it needs',
      '.button-div > * { flex: 1 1 auto; }' in small)
check('no control may be wider than what holds it',
      'select, input, button { max-width: 100%; }' in small)
# This rule belongs to small screens only: a desktop filter bar still lays
# its boxes out side by side at their natural width.
check('none of this applies on a desktop',
      'min-width: 0;' not in css[:css.index('@media (max-width: 820px)')])

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
