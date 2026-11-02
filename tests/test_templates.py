"""The template layer.

The pages used to be Python strings in app.py: a page was CSS_STYLE + the
body, so the 18kB document head was pasted into every one of the seventeen of
them. 4,883 lines of HTML sat in the middle of the application code, nothing
could be checked, and anything shared had to be changed in one string and
hoped about everywhere else.

They are files now, and every page extends base.html. That buys a few things
this suite holds in place, because nothing else in the regression would
notice any of them going:

  * A template named in app.py that does not exist on disk is a 500 on that
    page, and only on the branch that renders it. The suites here exercise
    every route but not every branch, so the names are checked directly.

  * base.html is the one copy of the head, the stylesheet, the viewport line
    and the inactivity guard. A page that grows its own copy has quietly
    undone the whole exercise.

  * The document reference is on the printed form, so it is written once.
    The Store Key register is a different controlled form with its own
    reference and revision, and that is the one exception.

  * The pages close. None of them emitted </body> or </html> before.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
import re
import jinja2

TDIR = os.path.join(ROOT, 'templates')
APP = open(os.path.join(ROOT,
                        'app.py')).read()
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


def text(name):
    return open(os.path.join(TDIR, name)).read()


FILES = sorted(f for f in os.listdir(TDIR) if f.endswith('.html'))
PARTIALS = {'base.html', 'csrf_failed.html'}
PAGES = [f for f in FILES if not f.startswith('_') and f not in PARTIALS]

print('\n--- 1. The HTML is out of app.py ---')
check('no page is a Python string any more',
      not re.search(r'^[A-Z_]+_TEMPLATE\s*=', APP, re.M),
      str(re.findall(r'^([A-Z_]+_TEMPLATE)\s*=', APP, re.M)))
check('and nothing renders a string instead of a file',
      'render_template_string' not in APP)
check('the stylesheet is not in app.py either',
      not re.search(r'^CSS_STYLE\s*=', APP, re.M),
      'it is only mentioned now, in the comment explaining where it went')
check('app.py is the application again, not a page of HTML',
      len(APP.split('\n')) < 9000, f'{len(APP.split(chr(10))):,} lines')
check('there are seventeen pages', len(PAGES) == 17, str(len(PAGES)))

print('\n--- 2. Every template Jinja can actually compile ---')
# A template that does not parse is a 500 on whichever page uses it, and
# render_template only finds out when somebody loads that page.
env = jinja2.Environment(loader=jinja2.FileSystemLoader(TDIR), autoescape=True)
for f in FILES:
    try:
        env.parse(text(f), filename=f)
        ok, why = True, ''
    except jinja2.TemplateSyntaxError as e:
        ok, why = False, f'line {e.lineno}: {e.message}'
    check(f'{f} parses', ok, why)

print('\n--- 3. Every name app.py renders is a file that exists ---')
named = sorted(set(re.findall(r"render_template\(\s*'([^']+)'", APP)))
check('app.py renders by name in at least fifteen places',
      len(named) >= 15, str(len(named)))
for name in named:
    check(f"render_template('{name}') has a file",
          os.path.exists(os.path.join(TDIR, name)),
          'a typo here is a 500 on that page and nowhere else')
# And the other way: a file nothing renders is dead weight.
referenced = set(named)
for f in FILES:
    referenced.update(re.findall(
        r'{%-?\s*(?:extends|include|import|from)\s+[\'"]([^\'"]+)[\'"]',
        text(f)))
orphans = [f for f in FILES if f not in referenced]
check('no template is left behind unrendered', not orphans, str(orphans))

print('\n--- 4. base.html is the only copy of the shared document ---')
base = text('base.html')
for fragment, what in [
        ('<!DOCTYPE html>', 'the doctype'),
        ('<meta charset="utf-8">', 'the character set'),
        ('name="viewport"', 'the viewport line'),
        ('name="csrf-token"', 'the CSRF token meta'),
        ('<title>', 'the title tag'),
        ('TIMEOUT_MINUTES', 'the inactivity guard'),
        ('.table-scroll {', 'the register scroll box'),
        ('@media (max-width: 820px)', 'the small-screen rules')]:
    check(f'{what} is in base.html', fragment in base)
    strays = [f for f in PAGES if fragment in text(f)]
    check(f'and in no page of its own', not strays, str(strays))

print('\n--- 5. Every page inherits rather than repeats ---')
for f in PAGES:
    s = text(f)
    check(f'{f} extends base.html',
          s.lstrip().startswith('{% extends "base.html" %}'),
          s.lstrip()[:60])
    check(f'{f} puts its content in the content block',
          '{% block content %}' in s and '{% endblock %}' in s)

print('\n--- 6. The document reference is written once ---')
# It is printed on the form. Seventeen copies is seventeen places to miss
# when a revision changes.
check('base.html carries the reference',
      'LD-HSE/NMC/HRD/6.1.3.3' in base)
carriers = [f for f in PAGES if 'LD-HSE/NMC/HRD/6.1.3.3' in text(f)]
check('and no page repeats it', not carriers, str(carriers))
check('the Store Key register overrides it, being a different form',
      '{% block doc_ref %}' in text('key_register.html')
      and 'LD-HSE/NMC/PSKR/6.1.3.3' in text('key_register.html'))
check('with its revision intact',
      'Revision 2 of 2018' in text('key_register.html'))
overriders = [f for f in PAGES if '{% block doc_ref %}' in text(f)]
check('and it is the only page that does',
      overriders == ['key_register.html'], str(overriders))

print('\n--- 7. The pagination bar is one file, not two copies ---')
# It was 1,214 characters of macro, byte for byte the same in the dispensing
# page and the receiving page.
check('there is a partial for it', os.path.exists(os.path.join(TDIR, '_pagebar.html')))
check('it defines the macro once', text('_pagebar.html').count('{% macro pagebar') == 1)
definers = [f for f in FILES if '{% macro pagebar' in text(f)]
check('and nothing else defines it', definers == ['_pagebar.html'], str(definers))
for f in ('dispense.html', 'receive.html'):
    check(f'{f} imports it',
          '{% from "_pagebar.html" import pagebar' in text(f))
check('it is imported with context, because it builds links with url_for',
      'with context %}' in text('dispense.html')
      and 'url_for' in text('_pagebar.html'))

print('\n--- 8. The medication loader is one file too ---')
check('there is a partial', os.path.exists(os.path.join(TDIR, '_medication_options.html')))
includers = [f for f in PAGES
             if '{% include "_medication_options.html" %}' in text(f)]
check('included by the four pages with a medication picker',
      sorted(includers) == ['add_medication.html', 'dispense.html',
                            'internal_issue.html', 'receive.html'],
      str(sorted(includers)))
check('and the list still comes from the endpoint, not the page',
      '/api/medications' in text('_medication_options.html'))

print('\n--- 9. The pages close, and name themselves ---')
check('base.html closes the body', '</body>' in base)
check('base.html closes the document', '</html>' in base)
titles = {}
for f in PAGES:
    m = re.search(r'{% block title %}(.*?){% endblock %}', text(f))
    check(f'{f} sets a title for the browser tab', bool(m),
          'every tab said "NMC Pharmacy" before')
    if m:
        titles[f] = m.group(1)
check('the titles are distinct, so two open tabs can be told apart',
      len(set(titles.values())) == len(titles),
      str([t for t in titles.values()
           if list(titles.values()).count(t) > 1]))
check('and each one still says which app it is',
      all('NMC Pharmacy' in t for t in titles.values()),
      str([t for t in titles.values() if 'NMC Pharmacy' not in t]))

print('\n--- 10. Page styling sits in the head ---')
# A <style> in the body is not where a stylesheet goes. It still has to come
# after base.html's own, or it would not win.
check('base.html offers a block for it', '{% block page_styles %}' in base)
check('the block is inside the head',
      base.index('{% block page_styles %}') < base.index('</head>'))
check('and after the shared stylesheet, so a page override still wins',
      base.index('.table-scroll {') < base.index('{% block page_styles %}'))
for f in PAGES:
    s = text(f)
    if '<style>' not in s:
        continue
    check(f'{f} puts its styles in the block',
          '{% block page_styles %}' in s
          and s.index('{% block page_styles %}') < s.index('<style>'))

print('\n--- 11. The folder is found wherever the code is run from ---')
# Flask's default template folder is resolved against the working directory.
# A serverless handler does not necessarily run from the directory its code
# is in, which would be every page 500ing on the deployed app only.
check('the folder is pinned to app.py\'s own directory',
      "os.path.dirname(os.path.abspath(__file__))" in APP
      and "template_folder=_TEMPLATES" in APP)
check('and a missing folder says so at startup instead of per request',
      'The templates folder is missing' in APP)

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
