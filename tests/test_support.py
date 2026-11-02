"""Shared scaffolding for the test suites.

The app refuses a POST that does not carry its CSRF token. That is the point
of it, and it means every suite in this directory would otherwise have to be
rewritten to fetch a token before each of several hundred POSTs.

The alternative usually taken — switching the protection off while testing —
would leave 2,400-odd checks exercising a path that does not exist in
production. So instead the test client is taught to send the token, the same
way a real browser does, and every suite keeps testing the real thing.

The second half of this file is about where the HTML now lives. The pages
used to be Python strings, so a suite could check one by reading
A.DISPENSE_TEMPLATE. They are template files now, and a page is assembled
from several of them, so page_source() does the assembling and the suites
keep asking the same question of the same thing.
"""
from _root import ROOT  # the app folder, one up from tests/
import os
import re

TEMPLATE_DIR = os.path.join(ROOT, 'templates')


def arm_csrf(client, session_key='_csrf_token', header='X-CSRF-Token'):
    """Make this test client send the session's CSRF token on every POST.

    The token goes in the header rather than the form body, so nothing a test
    posts is altered: a suite checking exactly which fields reach a route
    still sees exactly what it sent.
    """
    original_post = client.post

    def post(*args, **kwargs):
        with client.session_transaction() as sess:
            token = sess.get(session_key)
            if not token:
                token = 'test-csrf-token'
                sess[session_key] = token
        headers = dict(kwargs.get('headers') or {})
        headers.setdefault(header, token)
        kwargs['headers'] = headers
        return original_post(*args, **kwargs)

    client.post = post
    return client


def template_text(name):
    """The source of one template file, exactly as it is on disk."""
    return open(os.path.join(TEMPLATE_DIR, name)).read()


def page_source(name):
    """Everything a page is built from: the base, the partials, and the page.

    A check for a line of CSS does not need to know whether it sits in
    base.html or in the page, and a check for the medication loader does not
    need to know it is a partial. One string, searched the way the old
    single-string templates were.
    """
    seen, parts = set(), []

    def walk(fname):
        if fname in seen:
            return
        seen.add(fname)
        text = template_text(fname)
        parts.append(text)
        for ref in re.findall(r'{%-?\s*(?:extends|include|import|from)\s+'
                              r'[\'"]([^\'"]+)[\'"]', text):
            walk(ref)

    walk(name)
    return '\n'.join(parts)


def inventory_rows(html):
    """The inventory table as {item: {heading: cell}}, read by heading.

    The suites used to read it by position - cells[7] was Current Balance -
    which broke every one of them the day columns were removed, and would
    have silently read the wrong column if one had been added instead.
    """
    import re as _re
    start = html.find('<th>Beginning Balance</th>')
    if start < 0:
        return {}
    table = html[html.rfind('<table', 0, start):html.find('</table>', start)]
    heads = [_re.sub(r'<[^>]+>', '', h).strip()
             for h in _re.findall(r'<th[^>]*>(.*?)</th>', table, _re.S)]
    rows = {}
    for tr in _re.findall(r'<tr>(.*?)</tr>', table, _re.S):
        cells = [_re.sub(r'<[^>]+>', '', c).strip()
                 for c in _re.findall(r'<td[^>]*>(.*?)</td>', tr, _re.S)]
        if len(cells) == len(heads):
            # the warning mark on a row the records cannot explain
            cells[0] = cells[0].replace('&#9888;', '').replace('\u26a0', '').strip()
            rows[cells[0]] = dict(zip(heads, cells))
    return rows


def reconciles(row):
    """Beginning + Received - Issued = Current, on the figures as printed."""
    n = lambda k: float(row[k])
    return n('Beginning Balance') + n('Received') - n('Issued') == n('Current Balance')
