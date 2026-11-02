"""Lets a test in this folder find the app it tests.

The tests live in tests/, the app one folder up. Importing this first puts
that folder on the import path, so `import app` works, and makes it the
working directory, so a check that reads 'app.py' or 'templates/...' reads
the real files. Run a suite from anywhere: python tests/test_loans.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)
