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
logs = db['temperature_logs']
c = A.app.test_client()
# The app refuses a POST without its CSRF token; this makes the
# test client send it, so these checks exercise the real path.
from test_support import arm_csrf
arm_csrf(c)
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label + (('  ->  ' + detail) if detail and not cond else ''))


def login(role='admin', name='L. Hlaoli'):
    with c.session_transaction() as s:
        s['user'] = {'name': name, 'login': 'a', 'role': role}
        s['last_activity'] = datetime.now(timezone.utc).isoformat()


def reset():
    for col in ['temperature_logs', 'audit_log']:
        db[col].delete_many({})


TODAY = datetime.utcnow().date()
# Tests work in the PREVIOUS calendar month, which is always fully elapsed, so
# no test date is ever in the future. Keying them to the current month made the
# suite depend on which day it was run.
_first = TODAY.replace(day=1)
BASE = (_first - timedelta(days=1)).replace(day=1)
EARLIER = (BASE - timedelta(days=1)).replace(day=1)
MONTH = f'{BASE.year:04d}-{BASE.month:02d}'
EARLIER_MONTH = f'{EARLIER.year:04d}-{EARLIER.month:02d}'


def dstr(day):
    return f'{BASE.year:04d}-{BASE.month:02d}-{day:02d}'


def record(day, location='Dispensary', session='Morning', temp='21.0',
           note='', date=None, initials='LH', check_time='08:15',
           thermometer='TH-9'):
    return c.post('/temperature', data={
        'action': 'record', 'date': date or dstr(day), 'location': location,
        'session': session, 'temp': temp,
        'note': note, 'initials': initials, 'check_time': check_time,
        'thermometer': thermometer}, follow_redirects=True)


def day_pair(day, location='Dispensary', am='18.0', pm='22.0'):
    """A full day: the morning check and the afternoon check."""
    record(day, location=location, session='Morning', temp=am, check_time='08:15')
    record(day, location=location, session='Afternoon', temp=pm, check_time='15:40')


def view(location='Dispensary', month=None):
    return c.get(f'/temperature?location={location}&month={month or MONTH}').data.decode()


def banner(body):
    m = _re.search(r'<p class="message ([a-z]*)"[^>]*>(.*?)</p>', body, _re.S)
    return (m.group(1),
            _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', '', m.group(2))).strip()) if m else (None, None)


def txt(body):
    """Rendered text with entities and line wrapping normalised."""
    t = body.replace('&ndash;', '-').replace('&deg;', '\u00b0').replace('&mdash;', '-')
    return _re.sub(r'\s+', ' ', t)


def reg_row(body, day):
    seg = body[body.index('class="temp-register'):]
    seg = seg[:seg.index('</table>')]
    for m in _re.findall(r'<tr[^>]*>(.*?)</tr>', seg, _re.S):
        cells = [_re.sub(r'<[^>]+>', '', x).replace('&ndash;', '-').strip()
                 for x in _re.findall(r'<td>(.*?)</td>', m, _re.S)]
        if cells and cells[0] == str(day):
            return cells
    return []


def room(body, loc):
    """Just one room's block of the page: its heading, figures and chart.

    Every room is on the page at once now, so "nothing outside the range" has
    to be asked of a particular room — the page as a whole may well have a
    breach in another one.
    """
    blocks = body.split('<div class="temp-room">')[1:]
    for b in blocks:
        b = b.split('</div>\n</div>')[0]
        if _re.match(r'\s*<h3>' + _re.escape(loc) + r'\b', b):
            return txt(b)
    return ''


print('\n--- 1. The page and its four locations ---')
reset()
login()
body = c.get('/temperature').data.decode()
check('the page renders', 'Temperature Register' in body)
check('it is in the nav', '/temperature' in body)
for loc in ['Dispensary', 'Store 1', 'Store 2', 'Fridge']:
    check(f'location offered: {loc}', loc in body)
check('both sessions are offered', 'Morning' in body and 'Afternoon' in body)
check('one temperature is asked for per check', 'name="temp"' in body)
check('the three-figure fields are gone',
      'name="max"' not in body and 'name="min"' not in body
      and 'name="actual"' not in body)
check('the page explains how the pens are used',
      'morning reading is the blue line' in txt(body)
      and 'average of the two' in txt(body))
check('a print button is offered', 'window.print()' in body)
check('the month defaults to this month',
      f'value="{TODAY.year:04d}-{TODAY.month:02d}"' in body)

print('\n--- 2. Recording one temperature per check ---')
reset()
login()
record(3, temp='17.2', note='reset after reading')
d = logs.find_one({})
check('the temperature is stored', d['temp'] == 17.2)
check('no three-figure fields are written',
      'max' not in d and 'min' not in d and 'actual' not in d, str(sorted(d)))
check('the location is stored', d['location'] == 'Dispensary')
check('the session is stored', d['session'] == 'Morning')
check('the note is kept', d['note'] == 'reset after reading')
check('who recorded it is kept', d['recorded_by'] == 'L. Hlaoli')
check('it is audited',
      db['audit_log'].count_documents({'target_type': 'temperature_reading'}) == 1)
row = reg_row(view(), 3)
print('   day 3 row:', row)
check('the register shows the morning reading', row[1] == '17.2', str(row))
check('the afternoon is blank until recorded', row[2] == '-', str(row))
check('no average is shown with only one check',
      row[3] == '-', 'averaging one reading with nothing invents a figure')

record(3, session='Afternoon', temp='22.8')
row = reg_row(view(), 3)
check('both checks sit on one day row',
      row[1] == '17.2' and row[2] == '22.8', str(row))
check('the average of the two is shown', row[3] == '20', str(row))
check('two readings exist for the day', logs.count_documents({}) == 2)

print('\n--- 2b. A reading already stored the old way still reads ---')
reset()
login()
# Entered before this change: max/min/actual per session. 'actual' was the
# figure on the dial, which is what one reading now means.
logs.insert_one({'location': 'Dispensary', 'date': datetime(BASE.year, BASE.month, 5),
                 'session': 'Morning', 'max': 24.0, 'min': 17.0, 'actual': 19.5,
                 'initials': 'LH', 'check_time': '08:00', 'thermometer': 'TH-1',
                 'note': '', 'recorded_by': 'old', 'timestamp': datetime.utcnow()})
check('a legacy reading is read from its actual figure',
      A._reading_temp(logs.find_one({})) == 19.5)
check('and appears in the register', reg_row(view(), 5)[1] == '19.5',
      str(reg_row(view(), 5)))
check('a reading with nothing usable reads as None',
      A._reading_temp({'session': 'Morning'}) is None)
check('a non-numeric figure does not crash the reader',
      A._reading_temp({'temp': 'warm'}) is None)
r = record(5, temp='20.0')
check('correcting a legacy reading clears the old fields',
      'max' not in logs.find_one({}) and 'actual' not in logs.find_one({}),
      str(sorted(logs.find_one({}))))
check('and stores the new single figure', logs.find_one({})['temp'] == 20.0)

print('\n--- 3. Re-recording corrects instead of duplicating ---')
reset()
login()
record(3, temp='17.2')
record(3, session='Afternoon', temp='22.8')
r = record(3, session='Morning', temp='18.4')
check('no duplicate row is created', logs.count_documents({}) == 2,
      str(logs.count_documents({})))
check('the figure is corrected',
      logs.find_one({'session': 'Morning'})['temp'] == 18.4)
check('the correction says so', 'corrected' in r.data.decode())
check('the correction is audited with before and after',
      db['audit_log'].count_documents({'action': 'UPDATE',
                                       'target_type': 'temperature_reading'}) == 1)

print('\n--- 4. A cooler afternoon is allowed, not refused ---')
reset()
login()
# Under the max-min model a "high" below its own "low" was impossible and was
# refused. Here the two figures are just the morning and afternoon checks, and
# a cold front genuinely makes the afternoon cooler. Refusing it would block
# recording a real day.
day_pair(4, am='22.0', pm='16.0')
check('both checks are stored', logs.count_documents({}) == 2)
row = reg_row(view(), 4)
check('the morning stays the morning', row[1] == '22', str(row))
check('the afternoon stays the afternoon', row[2] == '16', str(row))
check('the average is still their mean', row[3] == '19', str(row))
svg = view()[view().index('<svg'):view().index('</svg>') + 6]
_grid = svg[:svg.index('Thermometer Number')]
_blue = _re.findall(r'<rect x="([\d.]+)" y="([\d.]+)"[^>]*fill="#0047b3"', _grid)
_red = _re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"[^>]*fill="#cc0000"', _grid)
check('the morning mark is still blue and the afternoon still red',
      len(_blue) == 1 and len(_red) == 1, f'blue={len(_blue)} red={len(_red)}')
_blue_cy = float(_blue[0][1]) + 2.9      # rect y is its top edge
check('the red mark sits BELOW the blue one, so the crossing is visible',
      float(_red[0][1]) > _blue_cy,
      f'red cy={_red[0][1]} blue cy={_blue_cy:.1f}')

print('\n--- 5. An implausible figure is refused ---')
reset()
login()
r = record(5, temp='300')
check('a figure no room thermometer can show is refused',
      logs.count_documents({}) == 0)
check('and says so', 'outside the range' in r.data.decode())
r = record(5, temp='not a number')
check('a non-numeric temperature is refused', logs.count_documents({}) == 0)
check('and says it must be a number', 'must be a number' in r.data.decode())

print('\n--- 6. Out-of-range readings are counted as breaches ---')
reset()
login()
day_pair(6, am='17.0', pm='24.0')
day_pair(7, am='17.0', pm='27.5')
body = view()
check('one breach is reported',
      'Outside the range: 1' in room(body, 'Dispensary'),
      room(body, 'Dispensary')[:200])
check('the breaching row is highlighted',
      _re.search(r'<tr class="temp-breach">\s*<td>7</td>', body) is not None)
check('the compliant row is not highlighted',
      _re.search(r'<tr class="temp-breach">\s*<td>6</td>', body) is None)
check('the acceptable range for a room is stated',
      '15\u00b0C to 25\u00b0C' in txt(body))
record(8, temp='14.2')
check('a reading below the range is a breach too',
      'Outside the range: 2' in room(view(), 'Dispensary'))

print('\n--- 7. The fridge has its own range ---')
reset()
login()
day_pair(8, location='Fridge', am='3.0', pm='7.0')
body = view('Fridge')
check('the fridge range is 2 to 8', '2\u00b0C to 8\u00b0C' in txt(body))
check('a fridge reading in range is not a breach',
      'Nothing outside the range' in room(body, 'Fridge'),
      room(body, 'Fridge')[:200])
record(9, location='Fridge', session='Afternoon', temp='11.0')
_both = view('Fridge')
check('a fridge reading above 8 is a breach',
      'Outside the range: 1' in room(_both, 'Fridge'))
check('the same figure would be fine in the dispensary',
      'Nothing outside the range' in room(_both, 'Dispensary'),
      'and now it is the same page, not a second one')

print('\n--- 8. Locations are kept apart ---')
reset()
login()
record(10, location='Store 1', temp='24.0')
record(10, location='Store 2', temp='23.0')
check('two readings exist', logs.count_documents({}) == 2)
check('store 1 shows only its own', reg_row(view('Store 1'), 10)[1] == '24')
check('store 2 shows only its own', reg_row(view('Store 2'), 10)[1] == '23')
check('the dispensary shows neither',
      reg_row(view('Dispensary'), 10)[1] == '-',
      str(reg_row(view('Dispensary'), 10)))

print('\n--- 9. The chart ---')
reset()
login()
for day in (11, 12, 13):
    day_pair(day, am=str(17 + day * 0.1), pm=str(22 + day * 0.1))
body = view()
svg = body[body.index('<svg'):body.index('</svg>') + 6]
check('an SVG chart is rendered', svg.startswith('<svg'))
check('it needs no JavaScript', '<script' not in svg)

# --- it reproduces the official form, Appendix 18 / SR-TC-001 ---
check('it carries the form\'s own title',
      'STORE ROOM TEMPERATURE CHART' in svg)
check('it carries the form\'s document number', 'SR-TC-001' in svg)
check('it carries the form\'s effective date', '12/2018' in svg)
check('the month, year and location are named in the header',
      all(t in svg for t in [f'Month: {BASE:%B}', f'Year: {BASE.year}',
                             'Location: Dispensary']))
check('the day strip is labelled as the form labels it',
      'Days of the' in svg and 'Time interval' in svg)
check('every day of the month has a column heading',
      all(f'>{d}</text>' in svg for d in range(1, 32)), 'missing a day number')
_DIM = A._month_bounds(BASE.year, BASE.month)[2]
check('each day of this month is split into morning and afternoon',
      svg.count('>m</text>') == _DIM and svg.count('>a</text>') == _DIM,
      f"m={svg.count('>m</text>')} a={svg.count('>a</text>')} days={_DIM}")
# Asked of a February, which is always short. Asking it of last month failed
# whenever last month had 31 days, because then nothing is greyed.
_fb = view(month=f'{TODAY.year - 1}-02')
_fsvg = _fb[_fb.index('<svg'):_fb.index('</svg>') + 6] if '<svg' in _fb else ''
check('a short month still prints all 31 day columns, surplus ones greyed',
      '>31</text>' in _fsvg and ('#999999' in _fsvg or '#bbbbbb' in _fsvg or '#f2f2f2' in _fsvg))
check('every month prints a column 31', '>31</text>' in svg)
check('the header explains the m and a columns',
      'M – Morning' in svg and 'A – Afternoon' in svg)

# --- the printed temperature scale ---
# One row is one degree. The scale used to copy the paper form's five labels
# (+30, +26, +22, +18, +15) three rows apart, so a row meant 1 1/3 degrees at
# the top and 1 degree at the bottom - reported as "the numbering doesn't
# match the spacing", which was exactly right.
check('every degree from +30 down to +14 is labelled',
      all(f'+{v}°C' in svg for v in range(14, 31)), 'missing a degree')
check('including all five the paper form printed',
      all(t in svg for t in ['+30°C', '+26°C', '+22°C', '+18°C', '+15°C']))
_anch = A._temp_scale('Dispensary')['anchors']
check('the scale is linear: every degree is one row',
      all(A._temp_row(float(v), _anch) == 30 - v for v in range(14, 31)),
      str([(v, A._temp_row(float(v), _anch)) for v in (30, 26, 18, 15, 14)]))
check('so 30 to 26 takes as much room as 18 to 14',
      A._temp_row(26.0, _anch) - A._temp_row(30.0, _anch)
      == A._temp_row(14.0, _anch) - A._temp_row(18.0, _anch))
_ys = sorted(float(y) for y in _re.findall(r'<text x="12.0" y="([\d.]+)"[^>]*>\+\d+°C</text>', svg))
_gaps = {round(b - a, 1) for a, b in zip(_ys, _ys[1:])}
check('and the labels are drawn evenly spaced', max(_gaps) - min(_gaps) <= 0.2, str(_gaps))
check('a reading between two degrees falls between their rows',
      A._temp_row(20.5, _anch) == 9.5, str(A._temp_row(20.5, _anch)))
check('a reading above the top of the scale is clamped, never dropped',
      A._temp_row(40.0, _anch) == 0, str(A._temp_row(40.0, _anch)))
check('a reading below the bottom of the scale is clamped, never dropped',
      A._temp_row(-5.0, _anch) == A.TEMP_ROWS - 1, str(A._temp_row(-5.0, _anch)))
_fr = A._temp_scale('Fridge')
check('the fridge is one degree a row too, +10 down to +1',
      _fr['rows'] == 10 and all(A._temp_row(float(v), _fr['anchors']) == 10 - v
                                for v in range(1, 11)))

# --- the three pen colours the form specifies ---
check('the maximum series uses the red pen', '#cc0000' in svg)
check('the minimum series uses the blue pen', '#0047b3' in svg)
check('the actual series uses the black pen', '#000000' in svg)
check('three data lines are drawn', svg.count('<polyline') == 3, str(svg.count('<polyline')))
check('each series carries its own marker shape',
      '<circle' in svg and '<polygon' in svg)

# --- the three pens plot where the pharmacy records them -------------------
# Morning in the day's 'm' column, afternoon in its 'a' column, the average
# between the two. Column centres are derived the same way the chart does it.
_W, _M, _LBL, _COLS = 1190, 8, 104, 62
_cw = (_W - 2 * _M - _LBL) / _COLS
_gx = _M + _LBL
_cx = lambda i: _gx + (i + 0.5) * _cw
# The legend in the footer draws one marker per series too, so look only at
# the grid: everything before the footer's signing block.
_grid = svg[:svg.index('Thermometer Number')]
# a <rect> records its top-left corner, so add half the 5.8px square back
_blue = sorted(float(x) + 2.9 for x, _y in
               _re.findall(r'<rect x="([\d.]+)" y="([\d.]+)"[^>]*fill="#0047b3"', _grid))
_red = sorted(float(x) for x, _y in
              _re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"[^>]*fill="#cc0000"', _grid))
_black = sorted(float(m) for m in
                _re.findall(r'<polygon points="([\d.]+),', _grid))
check('one blue mark per day recorded', len(_blue) == 3, str(len(_blue)))
check('one red mark per day recorded', len(_red) == 3, str(len(_red)))
check('one black average per day recorded', len(_black) == 3, str(len(_black)))
# markers are drawn centred on the column, so compare to the column centre
_m_cols = [_cx((d - 1) * 2) for d in (11, 12, 13)]
_a_cols = [_cx((d - 1) * 2 + 1) for d in (11, 12, 13)]
_mids = [_gx + ((d - 1) * 2 + 1) * _cw for d in (11, 12, 13)]
check('every blue mark sits in a morning column',
      all(abs(b - m) < 0.5 for b, m in zip(_blue, _m_cols)),
      f'{[round(x,1) for x in _blue]} vs {[round(x,1) for x in _m_cols]}')
check('every red mark sits in an afternoon column',
      all(abs(r - a) < 0.5 for r, a in zip(_red, _a_cols)),
      f'{[round(x,1) for x in _red]} vs {[round(x,1) for x in _a_cols]}')
check('every black mark sits between the two',
      all(abs(k - mid) < 0.5 for k, mid in zip(_black, _mids)),
      f'{[round(x,1) for x in _black]} vs {[round(x,1) for x in _mids]}')
check('blue and red never share a column',
      not set(round(x, 1) for x in _blue) & set(round(x, 1) for x in _red))
check('the legend says what each pen records here',
      all(t in svg for t in ['= morning check', '= afternoon check', '= average']))
check('the legend names the pen colour, as the form words it',
      all(t in svg for t in ['Actual....', 'Minimum....', 'Maximum....',
                             'Black pen....', 'Blue pen....', 'Red pen....']))
check('the legend lists them in the form\'s order, actual first',
      svg.index('Actual....') < svg.index('Minimum....') < svg.index('Maximum....'))
check('the acceptable range box reproduces the form',
      all(t in svg for t in ['Acceptable Range: 15 °C to 25 °C',
                             'Cold spot + 15°C to + 17.5°C',
                             'Hot spot + 23°C to + 25°C']))

# --- who checked it, and when ---
check('there is a row for the checker\'s initials', 'Initials' in svg)
check('there is a row for the time of the check', 'Write time' in svg)
check('the recorded initials appear on the chart', '>LH</text>' in svg)
check('the recorded times appear on the chart', '08:15' in svg)
check('the times are rotated so two per day never collide',
      'rotate(-90' in svg)
check('the footer carries the room\'s own thermometer number',
      'Thermometer Number: 1' in svg, svg[svg.find('Thermometer'):][:60])
check('there is a signing block for review',
      'Reviewed by:' in svg and 'Date:' in svg)

print('\n--- 10. A missed check leaves no mark on the line ---')
reset()
login()
# days 13-14 and 16-17 fully recorded, day 15 missed entirely. Two days are
# needed on each side: one isolated point draws a marker but no line, so a
# single day either side would leave nothing to break.
for day in (13, 14, 16, 17):
    day_pair(day, am='17.0', pm='21.0')
body = view()
svg = body[body.index('<svg'):body.index('</svg>') + 6]
check('each series is ONE line, not fragments',
      svg.count('<polyline') == 3, str(svg.count('<polyline')))
check('that line runs through every reading there is',
      all(len(p.split()) == 4 for p in _re.findall(r'<polyline points="([^"]+)"', svg)),
      str([len(p.split()) for p in _re.findall(r'<polyline points="([^"]+)"', svg)]))
check('nothing is dashed on the plotted data',
      'stroke-dasharray' not in svg)
_grid = svg[:svg.index('Thermometer Number')]
check('a mark is made only where a reading was taken',
      len(_re.findall(r'<circle cx="[\d.]+"[^>]*fill="#cc0000"', _grid)) == 4,
      'days 13, 14, 16, 17 were checked; 15 was not')
check('the missed day still shows as a blank Initials column',
      svg.count('>LH</text>') == 8,
      'four days checked twice each')
check('the register shows the missed day as blank',
      all(x == '-' for x in reg_row(body, 15)[1:4]), str(reg_row(body, 15)))
check('the page explains where a missed check does show',
      'blank column in the' in txt(body))

print('\n--- 10c. Scattered readings are still one line ---')
reset()
login()
# days 12 and 16 only, with nothing between them
for day in (12, 16):
    day_pair(day, am='17.0', pm='21.0')
body = view()
svg = body[body.index('<svg'):body.index('</svg>') + 6]
check('two far-apart days still join into one line per series',
      svg.count('<polyline') == 3, str(svg.count('<polyline')))
check('each with exactly its two readings',
      all(len(p.split()) == 2 for p in _re.findall(r'<polyline points="([^"]+)"', svg)))
check('and each day still shows its marks',
      len(_re.findall(r'<circle cx="[\d.]+"[^>]*fill="#cc0000"',
                      svg[:svg.index('Thermometer Number')])) == 2)

print('\n--- 10b. A half-recorded day has no average ---')
reset()
login()
day_pair(18, am='17.0', pm='21.0')
record(19, session='Morning', temp='17.5')      # afternoon missed
day_pair(20, am='18.0', pm='22.0')
body = view()
row = reg_row(body, 19)
check('the morning is recorded', row[1] == '17.5', str(row))
check('the afternoon is blank', row[2] == '-', str(row))
check('no average is shown for a half-recorded day', row[3] == '-', str(row))
svg = body[body.index('<svg'):body.index('</svg>') + 6]
_grid = svg[:svg.index('Thermometer Number')]
check('the blue line runs unbroken through the day',
      len(_re.findall(r'<rect x="[\d.]+" y="[\d.]+"[^>]*fill="#0047b3"', _grid)) == 3,
      'the morning check was done on all three days')
check('no red mark is made for the missed afternoon',
      len(_re.findall(r'<circle cx="[\d.]+" cy="[\d.]+"[^>]*fill="#cc0000"', _grid)) == 2)
check('and none for the average that needs it',
      len(_re.findall(r'<polygon points="[\d.]+,', _grid)) == 2,
      'an average needs both checks')
plain = _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', '', txt(body)))
check('completeness is reported',
      _re.search(r'Checks recorded: 5 of (60|62)\b', plain) is not None,
      plain[plain.find('Checks recorded'):][:60])
check('days with both checks are counted',
      'days with both checks' in plain, plain[plain.find('days with'):][:50])
check('the page explains how the three lines are derived',
      'morning reading is the blue line' in plain)

print('\n--- 11. Month selection ---')
reset()
login()
record(1, date=f'{EARLIER.year:04d}-{EARLIER.month:02d}-01', temp='21.0')
record(2, temp='20.0')
body = view()
check('the selected month shows only its own reading', reg_row(body, 2)[1] == '20',
      str(reg_row(body, 2)))
check('and not the earlier month\'s', reg_row(body, 1)[1] == '-', str(reg_row(body, 1)))
body = view(month=EARLIER_MONTH)
check('an earlier month can be shown', EARLIER.strftime('%B %Y') in body)
check('and shows its own reading', reg_row(body, 1)[1] == '21', str(reg_row(body, 1)))
check('the register has a row for every day of the month',
      len(_re.findall(r'<tr[^>]*>\s*<td>\d+</td>',
                      body[body.index('class="temp-register'):])) >= 28)
check('a malformed month falls back to this month',
      TODAY.strftime('%B %Y') in c.get('/temperature?month=nonsense').data.decode())

print('\n--- 12. Bad input ---')
reset()
login()
tomorrow = (TODAY + timedelta(days=1)).strftime('%Y-%m-%d')
r = record(1, date=tomorrow)
check('a future date is refused', logs.count_documents({}) == 0)
check('and says so', 'future' in r.data.decode())
for bad, label in [('abc', 'non-numeric'), ('', 'empty'), ('??', 'punctuation'),
                   ('250', 'implausibly high'), ('-99', 'implausibly low')]:
    record(2, temp=bad)
check('no invalid reading was stored', logs.count_documents({}) == 0,
      str(list(logs.find({}, {'_id': 0}))))
r = c.post('/temperature', data={'action': 'record', 'date': dstr(2),
                                 'location': 'Dispensary', 'session': 'Evening',
                                 'temp': '21'},
           follow_redirects=True)
check('an unknown session is refused', logs.count_documents({}) == 0)
r = c.post('/temperature', data={'action': 'record', 'date': dstr(2),
                                 'location': 'Nowhere', 'session': 'Morning',
                                 'temp': '21'},
           follow_redirects=True)
check('an unknown location falls back to the first, not an invented one',
      logs.count_documents({'location': 'Nowhere'}) == 0
      and logs.count_documents({'location': 'Dispensary'}) == 1)
r = c.post('/temperature', data={'action': 'nonsense'}, follow_redirects=True)
check('an unknown action is handled', 'Unknown action' in r.data.decode())
check('an empty month renders a chart frame',
      '<svg' in c.get('/temperature?location=Store 2').data.decode())

print('\n--- 13. Deleting a reading is admin only ---')
reset()
login()
record(20, temp='21.0')
rid = str(logs.find_one({})['_id'])
body = view()
check('an admin sees a delete control', 'name="reading_id"' in body)
login('user', 'Pharm')
ubody = view()
check('a non-admin sees no delete control', 'name="reading_id"' not in ubody)
check('a non-admin can still record a reading',
      (record(21, temp='20.0') or True)
      and logs.count_documents({}) == 2, str(logs.count_documents({})))
r = c.post('/temperature', data={'action': 'delete', 'reading_id': rid},
           follow_redirects=True)
check('and cannot delete by posting directly', logs.count_documents({}) == 2)
check('the refusal is explained', 'Access denied' in r.data.decode())
login()
r = c.post('/temperature', data={'action': 'delete', 'reading_id': rid,
                                 'location': 'Dispensary', 'month': MONTH},
           follow_redirects=True)
check('an admin can delete it', logs.count_documents({}) == 1)
check('the deletion is audited',
      db['audit_log'].count_documents({'action': 'DELETE',
                                       'target_type': 'temperature_reading'}) == 1)
r = c.post('/temperature', data={'action': 'delete', 'reading_id': 'rubbish'},
           follow_redirects=True)
check('a malformed id is handled', 'not found' in r.data.decode())

print('\n--- 13b. Banner severity ---')
reset()
login()
# Flask stores a plain flash() under the literal category 'message', which is
# truthy, so the template's error fallback never fired and refusals rendered
# unstyled instead of red.
b = c.post('/temperature', data={
    'action': 'record', 'date': dstr(1), 'location': 'Dispensary',
    'session': 'Nonsense', 'max': '24', 'min': '17', 'actual': '21'},
    follow_redirects=True)
_m = _re.search(r'class="message (\w+)"', b.data.decode())
check('a plain refusal renders as an error, not unstyled',
      _m is not None and _m.group(1) == 'error',
      _m.group(0) if _m else 'no banner')
b = record(4, temp='21.0')
_m = _re.search(r'class="message (\w+)"', b.data.decode())
check('a successful record still renders as success',
      _m is not None and _m.group(1) == 'success',
      _m.group(0) if _m else 'no banner')

print('\n--- 13c. Local time decides the round ---')
_real_local_now = A.local_now


def at(hh, mm=0, day=None):
    """Pin the pharmacy wall clock for the next call."""
    d = day or BASE.day
    A.local_now = lambda: datetime(BASE.year, BASE.month, d, hh, mm)


def unpin():
    A.local_now = _real_local_now


# Lesotho keeps UTC+2 all year, so the offset is exact, not an approximation.
check('the pharmacy clock runs two hours ahead of UTC',
      A.PHARMACY_UTC_OFFSET_HOURS == 2.0, str(A.PHARMACY_UTC_OFFSET_HOURS))
_utc = datetime.utcnow()
_loc = _real_local_now()
check('local time really is ahead of the server clock',
      abs(((_loc - _utc).total_seconds() / 3600) - 2.0) < 0.05,
      f'{(_loc - _utc).total_seconds() / 3600:.2f}h')

for hh, mm, expected in [(0, 1, 'Morning'), (8, 15, 'Morning'),
                         (11, 59, 'Morning'), (12, 0, 'Afternoon'),
                         (12, 1, 'Afternoon'), (15, 40, 'Afternoon'),
                         (23, 59, 'Afternoon')]:
    at(hh, mm)
    check(f'{hh:02d}:{mm:02d} is the {expected.lower()} round',
          A.local_session() == expected, A.local_session())
unpin()

# The bug this guards against: 13:30 local is 11:30 UTC, so deciding the round
# from the server clock would file an afternoon check as a morning one.
_afternoon_local = datetime(BASE.year, BASE.month, BASE.day, 13, 30)
A.local_now = lambda: _afternoon_local
check('an early-afternoon check is not filed as morning',
      A.local_session() == 'Afternoon',
      'UTC would have read 11:30 and said Morning')
unpin()

print('\n--- 13d. Each room has its own thermometer ---')
check('the four rooms map to thermometers 1 to 4',
      [A.TEMP_THERMOMETERS[l] for l in A.TEMP_LOCATIONS] == ['1', '2', '3', '4'],
      str(A.TEMP_THERMOMETERS))
reset()
login()
body = c.get('/temperature').data.decode()
check('the thermometer is no longer typed', 'name="thermometer"' not in body)
check('each room shows the instrument it keeps',
      all(t in body for t in ['Dispensary', 'Store 1', 'Store 2', 'Fridge']))

print('\n--- 13e. The whole round in one submission ---')
reset()
login()
at(9, 5)
r = c.post('/temperature', data={
    'action': 'record_round', 'location': 'Store 1', 'month': MONTH,
    'temp_0': '19.1', 'temp_1': '20.2', 'temp_2': '21.3', 'temp_3': '4.4'},
    follow_redirects=True)
check('all four rooms are recorded from one submission',
      logs.count_documents({}) == 4, str(logs.count_documents({})))
for i, loc in enumerate(A.TEMP_LOCATIONS):
    d = logs.find_one({'location': loc})
    check(f'{loc} stores its reading',
          d['temp'] == [19.1, 20.2, 21.3, 4.4][i], str(d.get('temp')))
    check(f'{loc} is stamped with thermometer {i + 1}',
          d['thermometer'] == str(i + 1), str(d.get('thermometer')))
    check(f'{loc} is filed as the morning round', d['session'] == 'Morning')
    check(f'{loc} carries the time of the round', d['check_time'] == '09:05')
    check(f'{loc} carries the initials of whoever is logged in',
          d['initials'] == 'LH', str(d.get('initials')))
    check(f'{loc} is dated today', d['date'].day == BASE.day)
check('the whole round is audited',
      db['audit_log'].count_documents({'target_type': 'temperature_reading'}) == 4)
cls, msg = banner(r.data.decode())
check('the banner confirms the round', cls == 'success', f'{cls}: {msg}')
check('and names what was saved', 'Dispensary 19.1' in msg, msg[:140])

print('\n--- 13f. An unchecked room records nothing ---')
reset()
login()
at(9, 5)
c.post('/temperature', data={
    'action': 'record_round', 'location': 'Store 1', 'month': MONTH,
    'temp_0': '19.1', 'temp_1': '', 'temp_2': '  ', 'temp_3': '4.4'},
    follow_redirects=True)
check('only the rooms with a figure are recorded',
      logs.count_documents({}) == 2, str(logs.count_documents({})))
check('the blank rooms have no record',
      logs.find_one({'location': 'Store 1'}) is None
      and logs.find_one({'location': 'Store 2'}) is None)
r = c.post('/temperature', data={
    'action': 'record_round', 'location': 'Store 1', 'month': MONTH,
    'temp_0': '', 'temp_1': '', 'temp_2': '', 'temp_3': ''},
    follow_redirects=True)
check('an entirely empty round records nothing', logs.count_documents({}) == 2)
cls, msg = banner(r.data.decode())
check('and says so plainly', 'Nothing was entered' in msg, msg[:80])

print('\n--- 13g. Re-running a round corrects it ---')
reset()
login()
at(9, 5)
c.post('/temperature', data={'action': 'record_round', 'location': 'Store 1',
                             'month': MONTH, 'temp_0': '19.1'},
       follow_redirects=True)
at(10, 30)
r = c.post('/temperature', data={'action': 'record_round', 'location': 'Store 1',
                                 'month': MONTH, 'temp_0': '18.6'},
           follow_redirects=True)
check('no duplicate is created', logs.count_documents({}) == 1,
      str(logs.count_documents({})))
check('the figure is corrected', logs.find_one({})['temp'] == 18.6)
check('the time of the correcting check is kept',
      logs.find_one({})['check_time'] == '10:30')
cls, msg = banner(r.data.decode())
check('the banner says it was a correction', 'corrected' in msg, msg[:120])

# The afternoon round is a different record, not a correction of the morning.
at(14, 0)
c.post('/temperature', data={'action': 'record_round', 'location': 'Store 1',
                             'month': MONTH, 'temp_0': '22.4'},
       follow_redirects=True)
check('the afternoon round is a separate record', logs.count_documents({}) == 2)
check('and the morning one is untouched',
      logs.find_one({'session': 'Morning'})['temp'] == 18.6)
check('the afternoon reading is its own',
      logs.find_one({'session': 'Afternoon'})['temp'] == 22.4)

print('\n--- 13h. Bad figures in a round ---')
reset()
login()
at(9, 5)
r = c.post('/temperature', data={
    'action': 'record_round', 'location': 'Store 1', 'month': MONTH,
    'temp_0': '19.1', 'temp_1': 'warm', 'temp_2': '400', 'temp_3': '4.4'},
    follow_redirects=True)
check('the good readings are still saved', logs.count_documents({}) == 2,
      'one bad box must not lose the rest of the round')
check('the non-numeric one is not stored',
      logs.find_one({'location': 'Store 1'}) is None)
check('the implausible one is not stored',
      logs.find_one({'location': 'Store 2'}) is None)
cls, msg = banner(r.data.decode())
check('the banner is red when something was rejected', cls == 'error', str(cls))
check('and names each room that was rejected',
      'Store 1' in msg and 'Store 2' in msg, msg[:200])

print('\n--- 13i. A breach in the round is called out ---')
reset()
login()
at(9, 5)
r = c.post('/temperature', data={
    'action': 'record_round', 'location': 'Store 1', 'month': MONTH,
    'temp_0': '19.1', 'temp_3': '12.0'},
    follow_redirects=True)
check('both readings are recorded', logs.count_documents({}) == 2)
cls, msg = banner(r.data.decode())
check('the banner is amber, not green', cls == 'partial', str(cls))
check('it names the room out of range', 'Fridge 12' in msg, msg[:200])
check('it does not flag the compliant room',
      'OUTSIDE RANGE' in msg and 'Dispensary 19.1°C' not in msg.split('OUTSIDE RANGE')[1],
      msg[:200])

print('\n--- 13j. The round panel on the page ---')
reset()
login()
at(14, 30)
body = c.get('/temperature').data.decode()
check('the panel is titled for the current round',
      'Afternoon Round' in body, body[body.find('Round') - 40:][:80])
check('it shows the time it derived that from', '14:30' in body)
check('there is one box per room',
      all(f'name="temp_{i}"' in body for i in range(4)))
check('the first box takes focus, so no click is needed to start',
      'autofocus' in body)
check('each room shows its acceptable range', '2 to 8' in body)
at(9, 0)
c.post('/temperature', data={'action': 'record_round', 'location': 'Dispensary',
                             'month': MONTH, 'temp_0': '19.1'},
       follow_redirects=True)
body = c.get('/temperature').data.decode()
check('a room already done this round says so',
      'typing here corrects it' in body)
check('and shows what is already recorded', '19.1' in body)
check('the single-reading form is still available for corrections',
      'action" value="record"' in body or 'value="record"' in body)
check('it is tucked away rather than in the way', '<details' in body)
unpin()

print('\n--- 14. Printing ---')
body = view()
check('a print stylesheet is present', '@media print' in body)
check('it prints landscape A4', 'A4 landscape' in body)
check('the controls are hidden when printing', 'display: none' in body)
check('a signature line is provided for the register',
      'Signature:' in txt(body) and 'Checked by:' in txt(body))

print('\n--- 20. Every room on one page ---')
# Asked for: "show all the charts on the same page instead of the table".
# Picking one room at a time meant four page loads to answer the only
# question anybody asks of this page - is everything all right?
reset()
login()
day_pair(8, location='Dispensary', am='18.0', pm='22.0')
day_pair(8, location='Fridge', am='3.0', pm='11.0')
body = view()
_rooms = _re.findall(r'<div class="temp-room">\s*<h3>([^<]+?)\s*(?:<small>|</h3>)', body)
check('there is a chart for every room', _rooms == list(A.TEMP_LOCATIONS),
      str(_rooms))
check('each one is a real chart',
      body.count('<div class="temp-chart"><svg') == len(A.TEMP_LOCATIONS),
      str(body.count('<div class="temp-chart"><svg')))
check('each room states its own range',
      '2\u00b0C to 8\u00b0C' in room(body, 'Fridge')
      and '15\u00b0C to 25\u00b0C' in room(body, 'Dispensary'))
check('a breach in one room is shown against that room',
      'Outside the range: 1' in room(body, 'Fridge'))
check('and not against the others',
      'Nothing outside the range' in room(body, 'Dispensary')
      and 'Nothing outside the range' in room(body, 'Store 1'))
check('the same page whichever room the table is set to',
      _re.findall(r'<div class="temp-room">\s*<h3>([^<]+?)\s*(?:<small>|</h3>)',
                  view('Store 2')) == list(A.TEMP_LOCATIONS))
check('the readings table is folded away rather than leading the page',
      '<details class="temp-register' in body
      and body.index('class="temp-room"') < body.index('class="temp-register'))
check('but still there, for initials, times, notes and corrections',
      reg_row(body, 8)[1:3] == ['18', '22'], str(reg_row(body, 8)))
check('and it does not print with the charts',
      'class="temp-register no-print"' in body)
check('each room prints on a page of its own',
      'page-break-after: always' in body)

print('\n--- 21. Show / Hide on password boxes ---')
_anon = A.app.test_client()
_login = _anon.get('/login').data.decode()
check('the login page carries the show/hide script',
      "querySelectorAll('input[type=\"password\"]')" in _login)
check('the box goes back to hidden when the form is sent',
      "box.type = 'password';" in _login,
      'so the browser still offers to save it, and it is not left on screen')
check('the button is not a submit button',
      "btn.type = 'button';" in _login,
      'a plain <button> inside a form submits it')
check('and it tells a screen reader what it does',
      'aria-pressed' in _login and 'Show password' in _login)
check('the password field itself is still a password field',
      'type="password" name="password"' in _login)

print('\n--- 22. A reading can be corrected, not only deleted ---')
# Asked: "in actions why is there only delete and no edit". The Actions column
# held a lone red "M" and "A" - both deletes. Correcting meant knowing that
# re-entering the same room, date and round in the form below overwrote it.
reset()
login('employee', 'Mathuto Kutoane')
record(14, session='Morning', temp='22.5', initials='MK', check_time='08:05',
       note='door open')
login()
body = view()
_doc = db['temperature_logs'].find_one({})
_rid = str(_doc['_id'])
check('each recorded round has an Edit button',
      f'edit={_rid}' in body and 'class="edit-btn">Edit</button>' in body)
check('and a Delete that says so, not just a letter',
      '>Delete</button>' in body and '>M</button>' not in body)
check('each pair is labelled with its round', 'temp-act-round">Morning<' in body)

body = c.get(f'/temperature?location=Dispensary&month={MONTH}&edit={_rid}').data.decode()
_form = body[body.index('id="correct"'):body.index('</details>', body.index('id="correct"'))]
check('Edit opens the form, already open', 'id="correct" open' in body)
check('saying which reading it is correcting',
      f'Correcting the morning reading for Dispensary, {dstr(14)}' in _form)
check('and what it was, and who took it',
      '22.5' in _form and 'Mathuto Kutoane' in _form)
check('with the date filled in', f'value="{dstr(14)}"' in _form)
check('the round chosen', '<option value="Morning" selected' in _form)
check('the figure, initials, time and note filled in',
      'value="22.5"' in _form and 'value="MK"' in _form
      and 'value="08:05"' in _form and 'value="door open"' in _form)
check('and the button says what it does', 'Save Correction' in _form)

record(14, session='Morning', temp='20.5', initials='MK', check_time='08:05',
       note='door open')
_doc = db['temperature_logs'].find_one({})
check('saving corrects the same reading, not a second one',
      db['temperature_logs'].count_documents({}) == 1 and _doc['temp'] == 20.5)
check('the person who took the reading is kept',
      _doc.get('recorded_by') == 'Mathuto Kutoane', str(_doc.get('recorded_by')))
check('who corrected it is recorded', _doc.get('corrected_by') == 'L. Hlaoli')
check('and what it was changed from',
      _doc.get('corrections') and _doc['corrections'][-1]['from'] == 22.5
      and _doc['corrections'][-1]['to'] == 20.5, str(_doc.get('corrections')))
check('the register says so in the notes',
      'morning corrected from 22.5°C by L. Hlaoli' in txt(view()), '')
check('the audit log has it too', db['audit_log'].count_documents(
    {'target_type': 'temperature_reading', 'action': 'UPDATE',
     'changes': {'$regex': '22.5'}}) == 1)

print('\n--- 23. The round form keeps the history too ---')
reset()
login('employee', 'Mathuto Kutoane')
record(15, session='Morning', temp='19.0')
_before = db['temperature_logs'].find_one({})
login()
A.local_now = lambda: datetime(BASE.year, BASE.month, 15, 8, 30)
c.post('/temperature', data={'action': 'record_round', 'temp_0': '18.0'},
       follow_redirects=True)
A.local_now = _real_local_now
_doc = db['temperature_logs'].find_one({'location': 'Dispensary'})
check('a round entry over an existing reading keeps who took it',
      _doc.get('recorded_by') == 'Mathuto Kutoane', str(_doc))
check('and records the change',
      _doc.get('corrections') and _doc['corrections'][-1]['from'] == 19.0, str(_doc))

print('\n--- 24. Deleting a reading records what was deleted ---')
reset()
record(16, session='Afternoon', temp='26.5')
_rid = str(db['temperature_logs'].find_one({})['_id'])
c.post('/temperature', data={'action': 'delete', 'reading_id': _rid,
                             'location': 'Dispensary', 'month': MONTH},
       follow_redirects=True)
_a = db['audit_log'].find_one({'action': 'DELETE', 'target_type': 'temperature_reading'})
check('the audit entry names the temperature that was deleted',
      _a and '26.5' in str(_a.get('changes')), str(_a and _a.get('changes')))
check('rather than the old empty high/low/actual fields',
      _a and 'None' not in str(_a.get('changes')), str(_a and _a.get('changes')))

print('\n--- 25. Only an admin can open a reading for correction ---')
reset()
record(17)
_rid = str(db['temperature_logs'].find_one({})['_id'])
login('employee', 'Mathuto Kutoane')
body = c.get(f'/temperature?location=Dispensary&month={MONTH}&edit={_rid}').data.decode()
check('a non-admin gets the ordinary form', 'Correcting the' not in body)
check('and no Edit or Delete buttons', 'class="edit-btn">Edit<' not in body
      and '>Delete</button>' not in body)
login()

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
