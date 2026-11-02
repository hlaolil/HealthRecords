"""The type checker.

A type checker nobody runs is a type checker that is already broken, so this
suite runs it and fails on anything it reports. That is the only thing here
that really matters; the rest of the checks hold the settings that decide
whether running it is worth anything.

The setting that does the work is check_untyped_defs. Without it mypy walks
straight past any function that has no annotations and finds nothing at all
— which, before this, was every function in the file. With it on, 111
problems came out of code that all 2,489 behavioural checks were passing.
Most were places where one name was doing two jobs:

  * `q` was a Mongo filter in five report branches and the quarter number in
    a sixth.
  * `e` was the caught exception and then the running total for one patient,
    four lines later. Python deletes the name at the end of an except block,
    so reading `e` there reads a variable that no longer exists unless
    something has rebound it first.
  * `inits` was a string of somebody's initials when recording a round and a
    list of them when drawing the month.
  * `d` was the anchor date of the monthly report in a function where `d` is
    a document everywhere else.

None of those was breaking today. All of them were a wrong edit away from it.

The other setting that earns its place is no_implicit_optional, which is how
a None reaches a .strip() in production: a parameter that defaults to None
has to say it can be None.
"""
from _root import ROOT  # the app folder, one up from tests/
import configparser
import os
import re
import subprocess
import sys

HERE = ROOT
PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(('  PASS  ' if cond else '  FAIL  ') + label
          + (('  ->  ' + detail) if detail and not cond else ''))


print('\n--- 1. There is a configuration, and it checks the bodies ---')
ini = os.path.join(HERE, 'mypy.ini')
check('mypy.ini exists', os.path.exists(ini))
cfg = configparser.ConfigParser()
cfg.read(ini)
check('there is a [mypy] section', cfg.has_section('mypy'))
m = cfg['mypy'] if cfg.has_section('mypy') else {}
check('the function bodies are checked, not skipped',
      m.get('check_untyped_defs') == 'True',
      'without this mypy ignores every unannotated function, which is the '
      'only kind that has bugs in it')
check('a None has to be declared',
      m.get('no_implicit_optional') == 'True')
check('and None is taken seriously at all', m.get('strict_optional') == 'True')
check('an ignore that stopped being needed is reported',
      m.get('warn_unused_ignores') == 'True',
      'otherwise the file fills up with silencers for problems long fixed')
check('unreachable code is reported', m.get('warn_unreachable') == 'True')
check('the app itself is in the files it checks',
      'app.py' in (m.get('files') or ''), m.get('files', ''))
check('and so are the two scripts that touch the same data',
      'cleanup_dormant.py' in (m.get('files') or '')
      and 'error_logger.py' in (m.get('files') or ''))
check('the version it checks against is the one Vercel runs',
      m.get('python_version') == '3.12', m.get('python_version', 'unset'))

print('\n--- 2. Nothing is silenced ---')
src = open(os.path.join(HERE, 'app.py')).read()
ignores = re.findall(r'#\s*type:\s*ignore[^\n]*', src)
check('app.py contains no type: ignore comments', not ignores, str(ignores))
check('and no blanket exclusion of a module in the config',
      not any('ignore_errors' in k for k in m),
      'ignore_missing_imports for a third-party package is fine; '
      'ignore_errors would switch off the checking')
# The third-party sections are only about packages that ship no type
# information. They say nothing about this code.
third_party = [s for s in cfg.sections() if s != 'mypy']
check('the only per-module settings are for third-party packages',
      all(cfg[s].get('ignore_missing_imports') == 'True'
          and set(cfg[s]) <= {'ignore_missing_imports'} for s in third_party),
      str({s: dict(cfg[s]) for s in third_party}))

print('\n--- 3. The annotations exist ---')
# Every function defined at module level, which is all of them that matter:
# the routes and the helpers that compute quantities, money and dates.
import ast
tree = ast.parse(src)
funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
check('there are over a hundred functions to annotate', len(funcs) > 100,
      str(len(funcs)))
unreturned = [f.name for f in funcs if f.returns is None]
check('every one of them declares what it returns', not unreturned,
      str(unreturned[:8]))
unargued = [(f.name, a.arg) for f in funcs for a in f.args.args
            if a.annotation is None]
check('and every parameter of every one says what it takes', not unargued,
      str(unargued[:8]))

print('\n--- 4. A None default says it can be None ---')
optional_lies = []
for f in funcs:
    args = f.args.args
    defaults = [None] * (len(args) - len(f.args.defaults)) + list(f.args.defaults)
    for a, d in zip(args, defaults):
        if isinstance(d, ast.Constant) and d.value is None and a.annotation:
            if 'None' not in ast.unparse(a.annotation):
                optional_lies.append(f'{f.name}({a.arg})')
check('no parameter defaults to None while claiming it cannot be',
      not optional_lies, str(optional_lies))

print('\n--- 5. Nothing is evaluated at import time ---')
# The names imported only for the checker do not exist when the app runs, so
# a deployment must never evaluate an annotation. This is what guarantees it.
check('annotations are postponed',
      'from __future__ import annotations' in src.split('import os')[0],
      'a type-checking-only name in a signature would be a NameError at '
      'startup without this')
check('the checker-only imports are behind TYPE_CHECKING',
      'if TYPE_CHECKING:' in src)
for name in ('Collection', 'Database', 'ResponseReturnValue', 'Response'):
    block = src.split('if TYPE_CHECKING:')[1].split('\n\n')[0]
    check(f'{name} is imported for the checker only', name in block)
# Proof rather than inspection: importing the module is what a deploy does.
r = subprocess.run(
    [sys.executable, '-c',
     'import os; os.environ.setdefault("SECRET_KEY", "k"); '
     'import mongomock, pymongo; '
     'pymongo.MongoClient = lambda *a, **k: mongomock.MongoClient(); '
     'import app; print("imported", len([f for f in dir(app)]))'],
    capture_output=True, text=True, cwd=HERE)
check('and the app imports cleanly with none of them present',
      r.returncode == 0 and 'imported' in r.stdout,
      (r.stderr or r.stdout)[-400:])

print('\n--- 6. The checker reports nothing ---')
r = subprocess.run(['mypy', '--no-error-summary'],
                   capture_output=True, text=True, cwd=HERE)
out = (r.stdout + r.stderr).strip()
if r.returncode == 127 or 'No module named' in out:
    check('mypy is installed', False, 'pip install mypy')
else:
    lines = [l for l in out.split('\n') if l.strip()]
    check('mypy finds nothing to report', not lines,
          '\n        ' + '\n        '.join(lines[:25]))

print(f'\n=========  {len(PASS)} passed, {len(FAIL)} failed  =========')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    raise SystemExit(1)
