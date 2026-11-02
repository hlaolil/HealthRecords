"""Run every suite in this folder and give one total.

    python tests/run_all.py            all of them
    python tests/run_all.py loans      only suites whose name contains 'loans'

Each suite runs in its own process, because each one swaps in its own fake
database when it imports the app; run together in one process they would
share it and trip over each other's data.
"""
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
pick = sys.argv[1] if len(sys.argv) > 1 else ''
suites = [p for p in sorted(glob.glob(os.path.join(HERE, 'test_*.py')))
          if not p.endswith('test_support.py') and pick in os.path.basename(p)]

total_pass = total_fail = 0
broken = []
for path in suites:
    name = os.path.basename(path)
    out = subprocess.run([sys.executable, path], capture_output=True, text=True)
    text = out.stdout + out.stderr
    found = re.findall(r'(\d+) passed, (\d+) failed', text)
    if not found:
        broken.append(name)
        print(f'  CRASHED  {name}')
        print('    ' + '\n    '.join(text.strip().splitlines()[-8:]))
        continue
    p, f = map(int, found[-1])
    total_pass += p
    total_fail += f
    print(f"  {'ok    ' if f == 0 else 'FAILED'}  {name:32} {p:5} passed  {f:3} failed")
    if f:
        for line in text.splitlines():
            if line.strip().startswith('FAIL '):
                print('            ' + line.strip())

print(f'\n{len(suites)} suites: {total_pass} passed, {total_fail} failed'
      + (f', {len(broken)} crashed' if broken else ''))
sys.exit(1 if total_fail or broken else 0)
