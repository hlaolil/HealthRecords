# Every annotation in this file is a string at runtime because of this line,
# so none of them is ever evaluated when the app starts. That matters for a
# deployment: a name that only exists for the type checker cannot bring the
# pharmacy down, and the TYPE_CHECKING imports below cost nothing at import.
from __future__ import annotations

import os
import re
import csv
import hmac
import io
import json
import secrets
import zipfile
import requests
from flask import Flask, request, render_template, jsonify, redirect, url_for, session, flash, get_flashed_messages, make_response
from functools import wraps
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ServerSelectionTimeoutError, DuplicateKeyError
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence, cast
from uuid import uuid4

if TYPE_CHECKING:                       # imported for the checker, never at run time
    from flask import Response
    from flask.typing import ResponseReturnValue
    from pymongo.collection import Collection
    from pymongo.database import Database

# What comes out of Mongo, and what goes into it as a filter. Naming them
# says the useful part — that these are documents and queries, not any old
# dictionary — without pretending the shapes are fixed, which they are not:
# a transaction written in 2024 has fewer fields than one written today, and
# the code has to keep reading both.
Doc = dict[str, Any]
Docs = list[Doc]
Query = dict[str, Any]
from collections import defaultdict
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import escape  # FIX: used to prevent XSS in nav links

load_dotenv()

from error_logger import init_error_logging, write_app_warning
from bson import ObjectId
from bson.errors import InvalidId

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES = os.path.join(_HERE, 'templates')
app = Flask(__name__, template_folder=_TEMPLATES)

# The pages are files in templates/ now, so that folder has to be deployed
# alongside this one. Said plainly at startup, because the alternative is
# every page answering 500 with a Jinja TemplateNotFound in the logs, which
# is a much longer way round to the same sentence.
#
# The path is pinned to this file's own directory rather than left to Flask's
# default, which is relative to the working directory: a serverless handler
# does not necessarily run from the directory its code is in.
if not os.path.isdir(_TEMPLATES):
    raise RuntimeError(
        f"The templates folder is missing: {_TEMPLATES}\n"
        "Every page of this app is a file in it. Deploy the templates/ "
        "directory next to app.py — on Vercel, make sure it is committed "
        "and, if the project uses includeFiles, that templates/** is listed."
    )

init_error_logging(app)
app.secret_key = os.getenv('SECRET_KEY')

# FIX: fail loudly at startup if SECRET_KEY is not set, rather than silently
# using a weak dev key in production.
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. "
        "Set it to a long random string before starting the server."
    )

# ---------------------------------------------------------------------
# The session cookie itself.
#
# HttpOnly keeps it out of reach of any script on the page. SameSite=Lax
# means a form on somebody else's site cannot post to this one carrying the
# pharmacist's session — which is the whole shape of a cross-site request
# forgery. Secure means it is never sent over plain HTTP.
#
# Secure is switched on by the VERCEL variable, which Vercel sets for you,
# so the deployed app is protected and a local copy served over http still
# works. COOKIE_SECURE=1 forces it on anywhere.
# ---------------------------------------------------------------------
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('VERCEL')
                               or os.environ.get('COOKIE_SECURE') == '1'),
)

# ---------------------------------------------------------------------
# Cross-site request forgery.
#
# Every one of the 23 POST routes in this file changed data on the word of
# a session cookie alone. A page on another site could therefore put up a
# form aimed at this one — write off stock, delete a visit, add a user —
# and a pharmacist who happened to be logged in would submit it by clicking
# anything on that page. Nothing in the audit log would look unusual,
# because as far as this app was concerned the pharmacist did it.
#
# The fix is the standard one: a random token, kept in the session, which
# every state-changing request must carry back. An attacker's page can make
# the browser send the cookie, but cannot read the token to send with it.
#
# The token is put into every POST form automatically, after the page is
# rendered (see _add_csrf_field). That is deliberate: there are 43 forms
# across 17 templates, and a scheme that depends on remembering to add a
# line to each one is a scheme that protects 42 forms.
# ---------------------------------------------------------------------
CSRF_FIELD_NAME = '_csrf_token'
CSRF_SESSION_KEY = '_csrf_token'
CSRF_HEADER = 'X-CSRF-Token'
CSRF_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
_POST_FORM_TAG = re.compile(
    rb'(<form\b[^>]*\bmethod\s*=\s*["\']post["\'][^>]*>)', re.I)


def csrf_token() -> str:
    """The token for this session, made on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


@app.before_request
def _require_csrf_token() -> tuple[str, int] | None:
    """Refuse a state-changing request that does not carry the token.

    Checked only when the session actually holds a token. A session that has
    expired holds nothing, and those requests are better answered by the
    login redirect than by a bare rejection: the pharmacist gets the login
    page and the browser draft puts their half-finished visit back. A fresh
    visitor posting to /login is in the same position.
    """
    if request.method not in CSRF_METHODS:
        return None
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return None
    sent = (request.form.get(CSRF_FIELD_NAME)
            or request.headers.get(CSRF_HEADER) or '')
    if hmac.compare_digest(str(sent), str(expected)):
        return None
    app.logger.warning(
        f'CSRF token missing or wrong on {request.method} {request.path} '
        f'from {request.remote_addr}')
    return render_template('csrf_failed.html'), 400


app.jinja_env.globals['csrf_token'] = csrf_token


@app.after_request
def _add_csrf_field(response: Response) -> Response:
    """Put the token into every POST form on the way out.

    Rendered HTML rather than the templates, so no form can be forgotten and
    a form added later is covered the day it is written.
    """
    try:
        if response.direct_passthrough:
            return response
        if not (response.content_type or '').startswith('text/html'):
            return response
        body = response.get_data()
        if b'<form' not in body:
            return response
        field = ('<input type="hidden" name="%s" value="%s">'
                 % (CSRF_FIELD_NAME, csrf_token())).encode()
        patched = _POST_FORM_TAG.sub(lambda m: m.group(1) + field, body)
        if patched != body:
            response.set_data(patched)
    except Exception as e:
        # A page that renders without the field is caught loudly by the
        # check above; a page that does not render at all helps nobody.
        app.logger.warning(f'CSRF field not added: {e}')
    return response


ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')

# -----------------------------------------------------------------------
# NEW: 30-minute inactivity session timeout.
#
# PERMANENT_SESSION_LIFETIME sets the ceiling for how long a "permanent"
# session cookie is valid. We mark sessions permanent at login (see
# login() below) and refresh the expiry on every request via the
# before_request hook further down, so the 30 minutes counts from the
# user's LAST activity, not from login time — true inactivity timeout,
# not a fixed session length.
# -----------------------------------------------------------------------
SESSION_TIMEOUT_MINUTES = 60
app.permanent_session_lifetime = timedelta(minutes=SESSION_TIMEOUT_MINUTES)

# -----------------------------------------------------------------------
# Local time at the pharmacy.
#
# The server runs in UTC. Lesotho keeps South African Standard Time, UTC+2
# all year with no daylight saving, so a fixed offset is exact rather than an
# approximation and needs no timezone database.
#
# This matters for anything that reads a wall clock. A check done at 13:30
# local is 11:30 UTC, so deciding "morning or afternoon" from UTC would file
# every early-afternoon reading as a morning one — silently, and on a
# compliance record.
# -----------------------------------------------------------------------
PHARMACY_UTC_OFFSET_HOURS = float(os.environ.get('PHARMACY_UTC_OFFSET', '2'))
PHARMACY_TZ = timezone(timedelta(hours=PHARMACY_UTC_OFFSET_HOURS))


def to_local(dt: datetime | None, fmt: str = '%Y-%m-%d %H:%M') -> str:
    """A stored (UTC) datetime as the pharmacy clock shows it, for display."""
    if not isinstance(dt, datetime):
        return ''
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (dt + timedelta(hours=PHARMACY_UTC_OFFSET_HOURS)).strftime(fmt)


app.jinja_env.filters['local'] = to_local


def local_now() -> datetime:
    """Wall-clock time at the pharmacy, naive, to match stored datetimes."""
    return datetime.now(timezone.utc).astimezone(PHARMACY_TZ).replace(tzinfo=None)


def local_session() -> str:
    """Which round a check done right now belongs to."""
    return 'Morning' if local_now().hour < TEMP_NOON_HOUR else 'Afternoon'

# NEW: passwords expire after this many days; checked at login time.
PASSWORD_MAX_AGE_DAYS = 365

# NEW: accounts expire after this many days (~2 years) and must be
# re-registered by an admin; checked at login time, before the password
# expiry check.
ACCOUNT_MAX_AGE_DAYS = 730

# -----------------------------------------------------------------------
# FIX (Performance): MongoDB module-level client with connection pooling.
# Previously a new MongoClient was created and closed on every request,
# which is slow and defeats PyMongo's built-in connection pool.
# -----------------------------------------------------------------------
_mongo_client: MongoClient | None = None

def get_mongo_client() -> MongoClient:
    global _mongo_client
    if _mongo_client is None:
        mongouri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
        _mongo_client = MongoClient(mongouri, serverSelectionTimeoutMS=120000)
    return _mongo_client


# -----------------------------------------------------------------------
# NEW: Explicit collection + index initialization.
#
# Previously audit_log (written by audit_logger.py) and error_logs (written
# by error_logger.py) were only created implicitly on first insert. That
# meant: (a) the /audit page couldn't distinguish "nothing happened yet"
# from "collection doesn't exist", and (b) there were no indexes, so
# sort('timestamp', -1) would do a full collection scan as these grow.
#
# This runs once at process startup (module import time + again defensively
# in __main__) and is safe to call repeatedly — create_collection raises
# CollectionInvalid if it already exists, which we just swallow, and
# create_index is idempotent by definition.
# -----------------------------------------------------------------------
def init_db_collections() -> None:
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        existing = db.list_collection_names()

        if 'audit_log' not in existing:
            db.create_collection('audit_log')
        if 'error_logs' not in existing:
            db.create_collection('error_logs')
        if 'app_warnings' not in existing:
            db.create_collection('app_warnings')
        # NEW (Stock Take): a stock take is a long-running session that stays
        # open across days while the admin counts items whenever convenient.
        # stock_takes holds the session header; stock_take_counts holds one
        # document per counted line.
        if 'stock_takes' not in existing:
            db.create_collection('stock_takes')
        if 'stock_take_counts' not in existing:
            db.create_collection('stock_take_counts')
        # NEW (Deliveries): header document carrying supplier, order number +
        # order total, invoice number + invoice total for a consignment. Its
        # line items are ordinary 'receive' transactions tagged with delivery_id.
        if 'deliveries' not in existing:
            db.create_collection('deliveries')
        # NEW (Temperature log): one document per location per date per session
        # (morning/afternoon), holding the max-min thermometer's three figures.
        if 'temperature_logs' not in existing:
            db.create_collection('temperature_logs')
        # NEW (Point of sale): one document per sale (the receipt); its lines
        # are ordinary 'dispense' transactions tagged with the same
        # transaction_id, so every stock report keeps working unchanged.
        if 'sales' not in existing:
            db.create_collection('sales')

        # audit_log: /audit page sorts by timestamp desc and filters by
        # action/target_type/target_id/user via $or regex, plus an exact
        # lookup pattern (target_type + target_id) for "history of this record".
        db['audit_log'].create_index([('timestamp', -1)])
        db['audit_log'].create_index([('action', 1)])
        db['audit_log'].create_index([('target_type', 1), ('target_id', 1)])
        db['audit_log'].create_index([('user', 1)])

        # error_logs: /audit page sorts by timestamp desc and filters by
        # path/endpoint/method.
        db['error_logs'].create_index([('timestamp', -1)])
        db['error_logs'].create_index([('endpoint', 1)])

        # app_warnings: handled business-logic issues (not found, insufficient
        # stock, etc.) written by error_logger.write_app_warning(). /audit
        # page sorts by timestamp desc and filters by warning_type/user.
        db['app_warnings'].create_index([('timestamp', -1)])
        db['app_warnings'].create_index([('warning_type', 1)])
        db['app_warnings'].create_index([('user', 1)])

        # Also index the collections the rest of the app queries heavily,
        # since dispense/receive/reports all filter or sort on these.
        db['transactions'].create_index([('timestamp', -1)])
        db['transactions'].create_index([('transaction_id', 1)])
        db['transactions'].create_index([('type', 1), ('timestamp', -1)])
        db['transactions'].create_index([('med_name', 1)])
        db['medications'].create_index([('name', 1)], unique=True)

        # NEW (Stock Take): the stock-take page lists open sessions first and
        # sorts by start time; the audit view sorts counts by timestamp desc
        # and filters by med_name/user/discrepancy_type.
        db['stock_takes'].create_index([('status', 1), ('started_at', -1)])
        db['stock_takes'].create_index([('reference', 1)], unique=True)
        db['stock_take_counts'].create_index([('timestamp', -1)])
        db['stock_take_counts'].create_index([('stock_take_id', 1), ('timestamp', -1)])
        db['stock_take_counts'].create_index([('med_name', 1)])
        db['stock_take_counts'].create_index([('discrepancy_type', 1)])
        # The register is read a month at a time for one location, and a
        # reading is uniquely identified by location + date + session — which
        # the unique index enforces, so a second entry corrects the first
        # instead of silently creating a duplicate row on the chart.
        db['temperature_logs'].create_index([('location', 1), ('date', 1)])
        db['temperature_logs'].create_index(
            [('location', 1), ('date', 1), ('session', 1)], unique=True)

        # sales: the sales list reads newest first; the receipt number is how a
        # customer refers to a sale, so it is unique.
        db['sales'].create_index([('timestamp', -1)])
        db['sales'].create_index([('receipt_no', 1)], unique=True)
        db['sales'].create_index([('status', 1), ('timestamp', -1)])
        db['deliveries'].create_index([('status', 1), ('opened_at', -1)])
        db['deliveries'].create_index([('reference', 1)], unique=True)
        db['deliveries'].create_index([('invoice_number', 1)])
        db['deliveries'].create_index([('order_number', 1)])
        db['transactions'].create_index([('delivery_id', 1)])

        app.logger.info("DB collections and indexes initialized (audit_log, error_logs, app_warnings, transactions, medications)")
        print("✅ DB collections and indexes initialized (audit_log, error_logs, app_warnings, transactions, medications)")
    except Exception as e:
        # Don't crash app startup over index creation — log and continue.
        # Collections will still be created implicitly on first write if this
        # fails (e.g. Mongo briefly unreachable at boot).
        app.logger.warning(f"init_db_collections failed (will retry implicitly on first write): {e}")
        print(f"⚠️  init_db_collections failed (will retry implicitly on first write): {e}")


# Run at import time so this also applies under `flask run` / gunicorn,
# not just `python app.py`.
init_db_collections()

# Login required decorator
def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


# NEW: enforce the 30-minute inactivity timeout on every request, before
# any route handler runs. This has to be a before_request hook rather than
# logic inside login_required, because it needs to actively clear an expired
# session (not just check membership) — otherwise a stale 'user' key would
# still satisfy login_required's `if 'user' not in session` check even
# though the user has been inactive well past the timeout.
@app.before_request
def enforce_session_timeout() -> ResponseReturnValue | None:
    if 'user' not in session:
        return None            # not logged in, nothing to enforce

    now = datetime.now(timezone.utc)
    last_active_str = session.get('last_active')

    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str)
        except ValueError:
            last_active = None
        if last_active and (now - last_active) > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            session.clear()
            flash(f'You were logged out after {SESSION_TIMEOUT_MINUTES} minutes '
                  f'of inactivity. Please log in again.', 'error')
            return redirect('/login')

    # Still within the window (or this is the first request after login) —
    # refresh the activity timestamp and make sure the cookie is marked
    # permanent so permanent_session_lifetime actually applies to it.
    session['last_active'] = now.isoformat()
    session.permanent = True

    # NEW: if this user's password has expired, block every route except
    # the change-password page itself and logout — otherwise a user could
    # just type /sell into the address bar and skip the requirement
    # entirely, since they're still a normally-authenticated session.
    if session.get('must_change_password') and request.endpoint not in (
        'change_expired_password', 'logout', 'static'
    ):
        return redirect('/change-expired-password')

    return None               # nothing to enforce: carry on to the route


# FIX (Security/XSS): escape user-controlled values before inserting into nav HTML.
# Previously the user's display name came straight from the DB and was rendered
# with |safe, allowing a name like <script>alert(1)</script> to execute.

# ---------------------------------------------------------------------------
# Session guard, rendered on every signed-in page via get_nav_links().
#
# Built as a plain string and concatenated rather than interpolated into an
# f-string: JavaScript is full of braces, and an f-string would need every one
# of them doubled. __TIMEOUT__ is substituted at render time.
# ---------------------------------------------------------------------------
SESSION_GUARD_JS = """
<div id="session-warn" class="session-warn"></div>
<script>
(function () {
    var TIMEOUT_MS = __TIMEOUT__ * 60 * 1000;
    var WARN_MS = 3 * 60 * 1000;      // warn with three minutes to go
    var MIN_PING_MS = 60 * 1000;      // at most one ping a minute
    var last = Date.now();            // last real interaction
    var lastPing = Date.now();
    var expired = false;
    var box = document.getElementById('session-warn');

    function show(msg, cls) {
        if (!box) return;
        box.textContent = msg;
        box.className = 'session-warn shown ' + (cls || '');
    }
    function hide() { if (box) box.className = 'session-warn'; }

    function ping() {
        lastPing = Date.now();
        var tokenMeta = document.querySelector('meta[name="csrf-token"]');
        fetch('/api/session-ping', {
            method: 'POST',
            headers: tokenMeta ? {'X-CSRF-Token': tokenMeta.content} : {}
        })
            .then(function (r) { if (!r.ok) markExpired(); })
            .catch(function () { /* offline: leave the countdown alone */ });
    }

    function markExpired() {
        expired = true;
        show('Your session has expired. Open the login page in another tab, '
             + 'sign in, then come back and save — what you have typed here '
             + 'is kept.', 'session-expired');
    }

    // Only genuine interaction counts. A page left open on an empty counter
    // makes none of these, so an unattended terminal still times out.
    ['keydown', 'click', 'input', 'change'].forEach(function (evt) {
        document.addEventListener(evt, function () {
            last = Date.now();
            if (expired) return;
            if (Date.now() - lastPing > MIN_PING_MS) ping();
        }, true);
    });

    setInterval(function () {
        if (expired) return;
        var idle = Date.now() - last;
        if (idle >= TIMEOUT_MS) {
            markExpired();
        } else if (idle >= TIMEOUT_MS - WARN_MS) {
            var mins = Math.max(1, Math.ceil((TIMEOUT_MS - idle) / 60000));
            show('You will be signed out in about ' + mins + ' minute'
                 + (mins === 1 ? '' : 's') + '. Type or click anywhere to stay '
                 + 'signed in.', 'session-soon');
        } else {
            hide();
        }
    }, 15000);
})();
</script>
"""

def get_nav_links() -> str:
    if 'user' in session:
        user = session['user']
        is_admin = user.get('role') == 'admin'
        # escape() returns a Markup object that Jinja will not double-escape,
        # but any HTML characters in the name are neutralised.
        name = escape(user.get('name', user.get('login', 'User')))
        add_med_link = '<a href="/add-medication">Add Medication</a> | ' if is_admin else ''
        audit_link = '<a href="/audit">Audit Log</a> | ' if is_admin else ''
        # NEW (Stock Take): admin-only physical count page.
        stock_take_link = '<a href="/stock-take">Stock Take</a> | ' if is_admin else ''
        return f"""
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/sell">Point of Sale</a> |
            <a href="/sales">Sales</a> |
            <a href="/dashboard">Dashboard</a> |
            <a href="/receive">Receiving</a> |
            <a href="/write-off">Write-offs</a> |
            <a href="/temperature">Temperature</a> |
            {add_med_link}
            <a href="/reports">Reports</a> |
            {stock_take_link}
            {audit_link}
            <span>Welcome, {name}! <a href="/logout">Logout</a></span>
        </p>
        """ + SESSION_GUARD_JS.replace('__TIMEOUT__', str(SESSION_TIMEOUT_MINUTES))
    else:
        return """
        <p class="nav-links"><strong>Navigate:</strong>
            <a href="/login">Login</a> | <a href="/register">Register</a>
        </p>
        """

# ============================================================
# Templates
# ============================================================
#
# The pages live in templates/ as real .html files, and every one of them
# extends base.html — which holds the document head, the stylesheet, the
# viewport line and the inactivity guard that all of them share.
#
# They used to be Python strings in this file: a page was CSS_STYLE + the
# body, so the 18kB head was pasted into all seventeen of them and anything
# that had to change in the shared part had to be found in one string and
# could not be checked anywhere. That was 4,883 lines of HTML in the middle
# of the application code, and about 289,000 characters of it were the same
# 18kB repeated.
#
# render_template() looks them up by name; the folder is pinned to this
# file's own directory below, because a serverless handler does not
# necessarily run with its working directory anywhere near the code.

# ============================================================
# Routes
# ============================================================

@app.route('/', methods=['GET'])
@login_required
def home() -> ResponseReturnValue:
    return redirect('/reports')


@app.route('/login', methods=['GET', 'POST'])
def login() -> ResponseReturnValue:
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if not username or not password:
            session['error'] = 'Username and password are required.'
            return redirect('/login')
        try:
            client = get_mongo_client()
            db = client['pharmacy_db']
            users = db['users']
            user_doc = users.find_one({'username': username})
            if user_doc and check_password_hash(user_doc['password_hash'], password):
                # NEW: account expiry check (2 years). This runs before the
                # password expiry check and before any session is created —
                # an expired account is blocked from logging in entirely,
                # not given a session that then gets redirected somewhere.
                # Same grandfathering approach as password_set_at: existing
                # users without a recorded account_created_at get it backfilled
                # to now rather than being retroactively expired on day one.
                account_created_at = user_doc.get('account_created_at')
                if not account_created_at:
                    account_created_at = datetime.now(timezone.utc)
                    users.update_one(
                        {'username': username},
                        {'$set': {'account_created_at': account_created_at}}
                    )
                    account_expired = False
                else:
                    if account_created_at.tzinfo is None:
                        account_created_at = account_created_at.replace(tzinfo=timezone.utc)
                    account_age = datetime.now(timezone.utc) - account_created_at
                    account_expired = account_age > timedelta(days=ACCOUNT_MAX_AGE_DAYS)

                if account_expired:
                    session['error'] = (
                        'Your account has expired and must be re-registered by an admin. '
                        'Please contact your administrator.'
                    )
                    return redirect('/login')

                # NEW: password expiry check. Existing users created before
                # this feature won't have a password_set_at field. Rather
                # than mass-expiring every existing account the moment this
                # ships, grandfather them in: backfill password_set_at to
                # now on this first login, so their year starts counting
                # from today. Only accounts with a recorded date older than
                # PASSWORD_MAX_AGE_DAYS are treated as expired going forward.
                password_set_at = user_doc.get('password_set_at')
                if not password_set_at:
                    password_set_at = datetime.now(timezone.utc)
                    users.update_one(
                        {'username': username},
                        {'$set': {'password_set_at': password_set_at}}
                    )
                    password_expired = False
                else:
                    if password_set_at.tzinfo is None:
                        password_set_at = password_set_at.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - password_set_at
                    password_expired = age > timedelta(days=PASSWORD_MAX_AGE_DAYS)

                session['user'] = {
                    'login': username,
                    'name': user_doc.get('name', username),
                    'role': user_doc.get('role', 'employee')
                }
                session.permanent = True
                session['last_active'] = datetime.now(timezone.utc).isoformat()

                if password_expired:
                    session['must_change_password'] = True
                    flash('Your password has expired and must be changed before continuing.', 'error')
                    return redirect('/change-expired-password')

                return redirect('/sell')
            else:
                session['error'] = 'Invalid username or password.'
        except ServerSelectionTimeoutError:
            session['error'] = 'Database connection failed. Please try again later.'
        return redirect('/login')

    error = session.pop('error', None)
    return render_template('login.html', error=error)


@app.route('/change-expired-password', methods=['GET', 'POST'])
@login_required
def change_expired_password() -> ResponseReturnValue:
    # If a user reaches this page without actually having an expired
    # password (e.g. they bookmarked it, or navigated back after already
    # changing it), just send them on to the app rather than show a
    # confusing forced form with nothing to enforce.
    if not session.get('must_change_password'):
        return redirect('/sell')

    error = None
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not current_password or not new_password or not confirm_password:
            error = 'All fields are required.'
        elif new_password != confirm_password:
            error = 'New password and confirmation do not match.'
        elif len(new_password) < 8:
            error = 'New password must be at least 8 characters.'
        elif new_password == current_password:
            error = 'New password must be different from your current password.'
        else:
            try:
                client = get_mongo_client()
                db = client['pharmacy_db']
                users = db['users']
                username = session['user']['login']
                user_doc = users.find_one({'username': username})

                if not user_doc or not check_password_hash(user_doc['password_hash'], current_password):
                    error = 'Current password is incorrect.'
                else:
                    users.update_one(
                        {'username': username},
                        {'$set': {
                            'password_hash': generate_password_hash(new_password),
                            'password_set_at': datetime.now(timezone.utc),
                        }}
                    )
                    session.pop('must_change_password', None)
                    flash('Password changed successfully. Please log in again with your new password.', 'success')
                    session.clear()
                    return redirect('/login')
            except ServerSelectionTimeoutError:
                error = 'Database connection failed. Please try again later.'

    return render_template('change_expired_password.html', error=error)


@app.route('/register', methods=['GET', 'POST'])
def register() -> ResponseReturnValue:
    if request.method == 'POST':
        if 'admin_pass' in request.form:
            if not ADMIN_PASSWORD:
                session['error'] = 'Admin password not configured. Contact system administrator.'
                return redirect('/register')
            if request.form['admin_pass'] == ADMIN_PASSWORD:
                session['admin_access'] = True
                return redirect('/register')
            else:
                session['error'] = 'Incorrect admin password.'
                return redirect('/register')
        else:
            if 'admin_access' not in session:
                session['error'] = 'Admin access required.'
                return redirect('/register')
            username = request.form.get('username')
            password = request.form.get('password')
            name = request.form.get('name')
            role = request.form.get('role')
            if not username or not password or not name or not role:
                session['error'] = 'All fields are required.'
                return redirect('/register')
            if role not in ('employee', 'viewer', 'admin'):
                session['error'] = 'Choose a role.'
                return redirect('/register')
            try:
                client = get_mongo_client()
                db = client['pharmacy_db']
                users = db['users']
                existing = users.find_one({'username': username})

                if existing:
                    # NEW: allow re-registration ONLY if this existing
                    # account has actually expired (2+ years old). This is
                    # the admin-driven re-registration flow for expired
                    # accounts — re-registering replaces the password and
                    # resets both the account and password clocks. Active
                    # accounts are still protected from being silently
                    # overwritten by reusing their username.
                    created_at = existing.get('account_created_at')
                    is_expired = False
                    if created_at:
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        is_expired = (datetime.now(timezone.utc) - created_at) > timedelta(days=ACCOUNT_MAX_AGE_DAYS)

                    if not is_expired:
                        session['error'] = 'Username already exists.'
                        return redirect('/register')

                    now = datetime.now(timezone.utc)
                    users.update_one(
                        {'username': username},
                        {'$set': {
                            'password_hash': generate_password_hash(password),
                            'name': name,
                            'role': role,
                            'account_created_at': now,
                            'password_set_at': now,
                        }}
                    )
                    # NEW: log re-registration of an expired account. register()
                    # has no @login_required and no session['user'] to attribute
                    # this to (it's gated by the shared ADMIN_PASSWORD, not a
                    # per-admin login), so this is written directly here rather
                    # than via audit_logger.py's usual decorator pattern.
                    db['audit_log'].insert_one({
                        'audit_id': str(uuid4()),
                        'timestamp': now,
                        'action': 'UPDATE',
                        'target_type': 'user_account',
                        'target_id': username,
                        'changes': {
                            'reason': 'expired account re-registered',
                            'old_account_created_at': str(created_at),
                            'new_name': name,
                            'new_role': role,
                        },
                        'user': 'admin (via /register admin password)',
                        'ip': request.remote_addr,
                        'user_agent': request.headers.get('User-Agent'),
                    })
                    session['message'] = f'Account "{username}" re-registered successfully! Please login.'
                    session.pop('admin_access', None)
                    return redirect('/login')

                now = datetime.now(timezone.utc)
                password_hash = generate_password_hash(password)
                users.insert_one({
                    'username': username,
                    'password_hash': password_hash,
                    'name': name,
                    'role': role,
                    'account_created_at': now,
                    'password_set_at': now,
                })
                session['message'] = 'Registration successful! Please login.'
                session.pop('admin_access', None)
                return redirect('/login')
            except ServerSelectionTimeoutError:
                session['error'] = 'Database connection failed. Please try again later.'
            return redirect('/register')

    error = session.pop('error', None)
    message = session.pop('message', None)
    if 'admin_access' not in session:
        return render_template('register_password.html', error=error)
    else:
        return render_template('register.html', error=error, message=message)


@app.route('/logout', methods=['GET'])
def logout() -> ResponseReturnValue:
    session.pop('user', None)
    session.pop('admin_access', None)
    return redirect('/login')


def _num(v: Any) -> Any:
    """A whole-number float shown as an int (3.0 -> 3), anything else as is."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


# ============================================================================
# POINT OF SALE
# ============================================================================
# A sale is one receipt. Its lines are written as ordinary 'dispense'
# transactions sharing the sale's transaction_id, which is what every stock
# report, the controlled drug register, AMC and the order request already
# read — so they all keep working without a single change. The receipt itself
# (payment, totals, receipt number) lives in the `sales` collection.
#
# Two prices travel on every line, deliberately apart:
#   price / line_value          -> COST, frozen at the moment of sale
#   unit_sell_price / line_total -> what the customer paid
# so margins can be reported honestly, and a later price change never
# rewrites a past sale.
#
# Selling is all-or-nothing. At a till you cannot hand over what is not on the
# shelf, so a basket with a short or unknown line is refused as a whole and
# the cashier fixes it; the shortfall is still recorded as unmet demand.
# ============================================================================

PAYMENT_METHODS = ['Cash', 'M-Pesa', 'EcoCash', 'Card', 'Medical aid']
SALES_PAGE_SIZE = 25
SALE_MAX_LINES = 40


def _next_receipt_no(db: Database) -> str:
    """HH-YYYYMMDD-0001: sequential per day, atomic across tills."""
    today = local_now().strftime('%Y%m%d')
    doc = db['counters'].find_one_and_update(
        {'_id': f'receipt-{today}'}, {'$inc': {'seq': 1}},
        upsert=True, return_document=ReturnDocument.AFTER)
    return f"HH-{today}-{int(doc['seq']):04d}"


def _money(v: Any) -> float:
    return round(float(v or 0) + 1e-9, 2)


def _sale_lines_from_form(form: Any) -> tuple[list[tuple[str, int]], str | None]:
    """(name, quantity) pairs from the basket; same name twice is merged."""
    names = [n.strip() for n in form.getlist('med_names')]
    qtys = form.getlist('quantities')
    merged: dict[str, int] = {}
    order: list[str] = []
    for name, q in zip(names, qtys):
        if not name:
            continue
        try:
            qty = int(q)
        except (TypeError, ValueError):
            return [], f'"{q}" is not a whole number for {name}.'
        if qty <= 0:
            return [], f'Quantity for {name} must be at least 1.'
        if name not in merged:
            order.append(name)
            merged[name] = 0
        merged[name] += qty
    if not order:
        return [], 'Add at least one item to the sale.'
    if len(order) > SALE_MAX_LINES:
        return [], f'A sale can have at most {SALE_MAX_LINES} different items.'
    return [(n, merged[n]) for n in order], None


@app.route('/api/pos/products', methods=['GET'])
@login_required
def pos_products() -> ResponseReturnValue:
    """Everything sellable, for the till's item picker: price, stock, Rx flag."""
    try:
        meds = get_mongo_client()['pharmacy_db']['medications'].find(
            _active(), {'_id': 0, 'name': 1, 'selling_price': 1, 'balance': 1,
                        'rx_only': 1, 'schedule': 1})
        return jsonify([{
            'name': m['name'],
            'price': _money(m.get('selling_price')),
            'balance': _num(m.get('balance', 0) or 0),
            'rx_only': bool(m.get('rx_only')),
            'controlled': m.get('schedule') == 'controlled',
        } for m in _by_name(meds)])
    except Exception as e:
        app.logger.warning(f'pos product list failed: {e}')
        return jsonify([])


@app.route('/sell', methods=['GET', 'POST'])
@login_required
def sell() -> ResponseReturnValue:
    message_class, message = _flashed_banner()
    if request.method == 'GET':
        return render_template('sell.html', nav_links=get_nav_links(),
                               message=message, message_class=message_class,
                               payment_methods=PAYMENT_METHODS, form={})
    form = request.form
    try:
        db = get_mongo_client()['pharmacy_db']
        medications, transactions = db['medications'], db['transactions']
        current_user = session['user']['name']

        def refuse(msg: str, cls: str = 'error') -> ResponseReturnValue:
            # The basket is kept on screen, so the cashier fixes one line
            # rather than ringing the whole sale up again.
            return render_template('sell.html', nav_links=get_nav_links(),
                                   message=msg, message_class=cls,
                                   payment_methods=PAYMENT_METHODS, form=form), 422

        lines, problem = _sale_lines_from_form(form)
        if problem:
            return refuse(problem)
        payment = form.get('payment_method') or ''
        if payment not in PAYMENT_METHODS:
            return refuse('Choose how the customer is paying.')
        customer = (form.get('customer') or '').strip()[:80]
        rx_no = (form.get('prescription_no') or '').strip()[:40]
        prescriber = (form.get('prescriber') or '').strip()[:80]
        aid_scheme = (form.get('aid_scheme') or '').strip()[:80]
        aid_member = (form.get('aid_member') or '').strip()[:40]
        if payment == 'Medical aid' and not (aid_scheme and aid_member):
            return refuse('Enter the medical aid scheme and member number.')

        meds: dict[str, Doc] = {}
        missing, short, unpriced = [], [], []
        sale_id = str(uuid4())
        for name, qty in lines:
            med = medications.find_one(_active({'name': name}))
            if not med:
                missing.append(name)
                continue
            meds[name] = med
            have = med.get('balance', 0) or 0
            if qty > have:
                short.append(f'{name}: {_num(have)} in stock, {qty} asked for')
                transactions.insert_one({
                    'type': 'unmet_demand', 'transaction_id': sale_id,
                    'med_name': name, 'quantity': 0,
                    # 'given' is what was on the shelf: the cashier normally
                    # sells that much once the basket is corrected, so only
                    # the part that could never be supplied counts as unmet.
                    'requested': qty, 'given': max(have, 0), 'shortfall': qty - max(have, 0),
                    'price': med.get('price', 0) or 0,
                    'shortfall_value': (qty - max(have, 0)) * (med.get('price', 0) or 0),
                    'patient': customer, 'prescriber': prescriber,
                    'dispenser': current_user,
                    'date': local_now().strftime('%Y-%m-%d'),
                    'user': current_user, 'timestamp': datetime.utcnow(),
                })
            if not _money(med.get('selling_price')):
                unpriced.append(name)
        if missing:
            return refuse('Not on file: ' + '; '.join(missing)
                          + '. Check the spelling, or add the item under Add Medication.')
        if short:
            return refuse('Not enough stock — nothing was sold. ' + '; '.join(short)
                          + '. Reduce the quantity; the shortfall has been recorded as unmet demand.',
                          'partial')
        if unpriced:
            return refuse('No selling price set for: ' + '; '.join(unpriced)
                          + '. An admin can set it under Edit Medication.')
        needs_rx = [n for n, _ in lines if meds[n].get('rx_only')
                    or meds[n].get('schedule') == 'controlled']
        if needs_rx and not (customer and rx_no):
            return refuse('Prescription items (' + ', '.join(needs_rx) + ') need the '
                          'patient\'s name and the prescription number.')
        controlled = [n for n in needs_rx if meds[n].get('schedule') == 'controlled']
        if controlled and not prescriber:
            return refuse('Controlled items (' + ', '.join(controlled) + ') need the '
                          'prescriber\'s name for the register.')

        total = _money(sum(_money(meds[n].get('selling_price')) * q for n, q in lines))
        tendered = None
        change = None
        if payment == 'Cash':
            try:
                tendered = _money(form.get('amount_tendered') or total)
            except ValueError:
                return refuse('Amount tendered must be a number.')
            if tendered + 1e-9 < total:
                return refuse(f'Amount tendered M{tendered:.2f} is less than the total M{total:.2f}.')
            change = _money(tendered - total)

        receipt_no = _next_receipt_no(db)
        now = datetime.utcnow()
        today = local_now().strftime('%Y-%m-%d')
        taken: list[tuple[str, int]] = []
        sale_lines: Docs = []
        for name, qty in lines:
            med = meds[name]
            # Guarded decrement: only succeeds if the stock is still there, so
            # two tills selling the last box at the same moment cannot both win.
            res = medications.update_one({'name': name, 'balance': {'$gte': qty}},
                                         {'$inc': {'balance': -qty}})
            if res.modified_count != 1:
                for n2, q2 in taken:
                    medications.update_one({'name': n2}, {'$inc': {'balance': q2}})
                transactions.delete_many({'transaction_id': sale_id, 'type': 'dispense'})
                return refuse(f'{name} was sold at another till a moment ago and there '
                              f'is no longer enough. Nothing was sold — check and try again.')
            taken.append((name, qty))
            cost = med.get('price', 0) or 0
            sell_price = _money(med.get('selling_price'))
            line_total = _money(sell_price * qty)
            transactions.insert_one({
                'type': 'dispense', 'transaction_id': sale_id, 'receipt_no': receipt_no,
                'patient': customer, 'prescriber': prescriber, 'prescription_no': rx_no,
                'dispenser': current_user, 'user': current_user, 'date': today,
                'med_name': name, 'quantity': qty,
                'price': cost, 'pack_size': med.get('pack_size'),
                'line_value': qty * cost,
                'unit_sell_price': sell_price, 'line_total': line_total,
                'payment_method': payment, 'schedule': med.get('schedule'),
                'timestamp': now,
            })
            sale_lines.append({'name': name, 'quantity': qty, 'unit_price': sell_price,
                               'line_total': line_total, 'cost': _money(qty * cost),
                               'rx_only': bool(med.get('rx_only'))})

        db['sales'].insert_one({
            'receipt_no': receipt_no, 'transaction_id': sale_id, 'status': 'completed',
            'lines': sale_lines, 'total': total,
            'cost_total': _money(sum(l['cost'] for l in sale_lines)),
            'payment_method': payment, 'amount_tendered': tendered, 'change': change,
            'medical_aid': ({'scheme': aid_scheme, 'member_no': aid_member}
                            if payment == 'Medical aid' else None),
            'customer': customer, 'prescription_no': rx_no, 'prescriber': prescriber,
            'cashier': current_user, 'timestamp': now,
        })
        write_audit_entry('CREATE', 'sale', receipt_no,
                          f'{len(sale_lines)} item(s), M{total:.2f} by {payment}, '
                          f'cashier {current_user}')
        flash(f'Sale {receipt_no} recorded: M{total:.2f}'
              + (f', change M{change:.2f}' if change else '') + '.', 'success')
        return redirect(url_for('receipt', receipt_no=receipt_no))
    except ServerSelectionTimeoutError:
        return render_template('sell.html', nav_links=get_nav_links(),
                               message='Database connection failed. Nothing was sold.',
                               message_class='error', payment_methods=PAYMENT_METHODS,
                               form=form), 500


# ---------------------------------------------------------------------------
# Damage write-off: stock LOST other than by expiry (broken, spilled, cold
# chain failure). Excluded from AMC exactly like expiry, so a breakage never
# makes the order request buy more. Expired stock has its own path under
# Reports > Expired Stock on Shelf.
# ---------------------------------------------------------------------------
DAMAGE_REASONS = ['Broken / spilled', 'Contaminated', 'Cold chain failure',
                  'Recalled by supplier', 'Lost / unaccounted', 'Other']


@app.route('/write-off', methods=['GET', 'POST'])
@login_required
def write_off() -> ResponseReturnValue:
    current_user = session['user']['name']
    flashed = get_flashed_messages()
    message = flashed[0] if flashed else None
    try:
        db = get_mongo_client()['pharmacy_db']
        medications, transactions = db['medications'], db['transactions']
        if request.method == 'POST':
            name = (request.form.get('med_name') or '').strip()
            try:
                qty = int(request.form.get('quantity', ''))
            except (TypeError, ValueError):
                flash('Quantity must be a whole number.')
                return redirect(url_for('write_off'))
            reason = (request.form.get('reason') or '').strip()
            if qty < 1 or not reason:
                flash('A quantity of at least 1 and a reason are both required.')
                return redirect(url_for('write_off'))
            med = medications.find_one({'name': name})
            if not med:
                flash(f'Medication "{name}" is not on file.')
                return redirect(url_for('write_off'))
            balance = med.get('balance', 0) or 0
            if qty > balance:
                flash(f'Cannot write off {qty} of "{name}" — only {balance} on hand.')
                return redirect(url_for('write_off'))
            price = med.get('price', 0) or 0
            medications.update_one({'name': name}, {'$inc': {'balance': -qty}})
            transactions.insert_one({
                'type': 'damage', 'med_name': name,
                'quantity': -qty,                    # signed, like expiry
                'price': price, 'line_value': -qty * price,
                'reason': reason, 'note': (request.form.get('note') or '').strip(),
                'batch': med.get('batch'),
                'expiry_date': med.get('expiry_date'),
                'balance_before': balance,
                'user': current_user, 'timestamp': datetime.utcnow(),
            })
            write_audit_entry('UPDATE', 'damage', name,
                              f'{qty} unit(s) written off as {reason} '
                              f'(balance {balance} -> {balance - qty}, '
                              f'value M{qty * price:.2f})')
            flash(f'Loss recorded: {name} x{qty} ({reason}). '
                  f'Balance is now {balance - qty}. Value M{qty * price:.2f}.')
            return redirect(url_for('write_off'))

        recent = list(transactions.find({'type': 'damage'}).sort('timestamp', -1).limit(100))
        for r in recent:
            r['when'] = to_local(r.get('timestamp'))
        return render_template('write_off.html', nav_links=get_nav_links(), message=message,
                               damage_reasons=DAMAGE_REASONS, recent=recent)
    except ServerSelectionTimeoutError:
        return render_template('write_off.html', nav_links=get_nav_links(),
                               message='Database connection failed. Please try again later.',
                               damage_reasons=DAMAGE_REASONS, recent=[]), 500


@app.route('/sales/<receipt_no>', methods=['GET'])
@login_required
def receipt(receipt_no: str) -> ResponseReturnValue:
    sale = get_mongo_client()['pharmacy_db']['sales'].find_one({'receipt_no': receipt_no})
    if not sale:
        flash(f'No sale with receipt number {receipt_no}.', 'error')
        return redirect(url_for('sales'))
    message_class, message = _flashed_banner()
    return render_template('receipt.html', nav_links=get_nav_links(), sale=sale,
                           when=to_local(sale.get('timestamp'), '%d %b %Y %H:%M'),
                           message=message, message_class=message_class,
                           is_admin=session['user'].get('role') == 'admin')


@app.route('/sales', methods=['GET'])
@login_required
def sales() -> ResponseReturnValue:
    """Sales list and cash-up: totals by payment method for the chosen days."""
    db = get_mongo_client()['pharmacy_db']
    start_date = request.args.get('start_date') or ''
    end_date = request.args.get('end_date') or ''
    search = (request.args.get('search') or '').strip()
    today = local_now().strftime('%Y-%m-%d')
    if not start_date and not end_date and not search:
        start_date = end_date = today         # the default view is today's cash-up
    q: Query = {}
    try:
        rng: Query = {}
        if start_date:
            rng['$gte'] = datetime.strptime(start_date, '%Y-%m-%d') - timedelta(hours=PHARMACY_UTC_OFFSET_HOURS)
        if end_date:
            rng['$lt'] = (datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
                          - timedelta(hours=PHARMACY_UTC_OFFSET_HOURS))
        if rng:
            q['timestamp'] = rng
    except ValueError:
        flash('Dates must be in the form YYYY-MM-DD.', 'error')
        return redirect(url_for('sales'))
    if search:
        rx = re.escape(search)
        q['$or'] = [{'receipt_no': {'$regex': rx, '$options': 'i'}},
                    {'customer': {'$regex': rx, '$options': 'i'}},
                    {'lines.name': {'$regex': rx, '$options': 'i'}}]

    by_method: dict[str, Doc] = {m: {'count': 0, 'total': 0.0} for m in PAYMENT_METHODS}
    grand = {'count': 0, 'total': 0.0, 'cost': 0.0, 'voided': 0}
    for s in db['sales'].find(q, {'status': 1, 'payment_method': 1, 'total': 1, 'cost_total': 1}):
        if s.get('status') == 'voided':
            grand['voided'] += 1
            continue
        m = by_method.setdefault(s.get('payment_method') or 'Other', {'count': 0, 'total': 0.0})
        m['count'] += 1
        m['total'] += s.get('total') or 0
        grand['count'] += 1
        grand['total'] += s.get('total') or 0
        grand['cost'] += s.get('cost_total') or 0
    grand['margin'] = grand['total'] - grand['cost']

    if request.args.get('export') == 'csv':
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['Receipt', 'Date', 'Status', 'Customer', 'Item', 'Quantity',
                     'Unit price', 'Line total', 'Payment', 'Cashier'])
        for s in db['sales'].find(q).sort('timestamp', 1):
            for l in s.get('lines', []):
                w.writerow([_csv_value(v) for v in (
                    s.get('receipt_no'), to_local(s.get('timestamp')), s.get('status'),
                    s.get('customer'), l.get('name'), l.get('quantity'),
                    f"{l.get('unit_price', 0):.2f}", f"{l.get('line_total', 0):.2f}",
                    s.get('payment_method'), s.get('cashier'))])
        resp = make_response(out.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = (
            f'attachment; filename="sales_{start_date or "all"}_{end_date or "all"}.csv"')
        return resp

    total = db['sales'].count_documents(q)
    pager = _pager(_requested_page(), SALES_PAGE_SIZE, total)
    rows = list(db['sales'].find(q).sort('timestamp', -1)
                .skip((pager['page'] - 1) * SALES_PAGE_SIZE).limit(SALES_PAGE_SIZE))
    for r in rows:
        r['when'] = to_local(r.get('timestamp'), '%d %b %H:%M')
    message_class, message = _flashed_banner()
    return render_template('sales.html', nav_links=get_nav_links(), rows=rows,
                           by_method=[(m, v) for m, v in by_method.items() if v['count']],
                           grand=grand, start_date=start_date, end_date=end_date,
                           search=search, pager=pager, window_defaulted=False, unit='sales',
                           is_admin=session['user'].get('role') == 'admin',
                           message=message, message_class=message_class)


def _load_delivery(deliveries: Collection, raw_id: str | None) -> Doc | None:
    if not raw_id:
        return None
    try:
        return deliveries.find_one({'_id': ObjectId(raw_id)})
    except (InvalidId, TypeError):
        return None


def _line_value(line: Doc) -> float:
    """Value of one receive line.

    NEW (Pack pricing): suppliers price by the pack but stock is counted in
    single units, so a line stores the pack figures as entered and the unit
    price is DERIVED from them. Reconciliation uses the stored line_value,
    computed from the pack price directly, because multiplying a rounded unit
    price back out by the quantity drifts: a pack of 3 at R250 gives a unit
    price of R83.3333..., and 3 x R83.33 is R249.99, which would fail an
    otherwise perfect invoice match by a cent.

    Falls back to quantity x price for rows written before pack pricing
    existed, and for lines edited through the single-line edit form.
    """
    if line.get('line_value') is not None:
        return line['line_value']
    return (line.get('quantity', 0) or 0) * (line.get('price', 0) or 0)


def _unit_price(pack_price: float, pack_size: float | None) -> float:
    """Price of a single countable unit, from the pack price and pack size."""
    pack_size = pack_size or 1
    return pack_price / pack_size if pack_size else pack_price


def _delivery_goods_value(transactions: Collection, delivery_id: Any) -> float:
    return sum(_line_value(l) for l in
               transactions.find({'type': 'receive', 'delivery_id': delivery_id},
                                 {'_id': 0, 'quantity': 1, 'price': 1, 'line_value': 1}))


def _order_rollup(deliveries: Collection, transactions: Collection, order_number: str | None) -> Doc | None:
    """NEW (Multiple invoices per order): one purchase order is normally
    fulfilled across several part-deliveries, each with its own invoice. The
    order-level position is therefore the SUM of the invoices raised against
    that order number, not any single one.

    There is no separate order record — deliveries are grouped by the order
    number typed on each invoice — so the order total is taken from the most
    recently opened delivery for that order, and a disagreement between
    deliveries is surfaced rather than silently resolved.
    """
    sibs = list(deliveries.find({'order_number': order_number}).sort('opened_at', -1))
    if not sibs:
        return None
    totals = {round(d.get('order_total', 0) or 0, 2) for d in sibs}
    order_total = sibs[0].get('order_total', 0) or 0
    invoiced = sum(d.get('invoice_total', 0) or 0 for d in sibs)
    goods = sum(_line_value(l) for l in transactions.find(
        {'type': 'receive', 'delivery_id': {'$in': [str(d['_id']) for d in sibs]}},
        {'_id': 0, 'quantity': 1, 'price': 1, 'line_value': 1}))
    outstanding = order_total - invoiced
    return {
        'order_number': order_number, 'order_total': order_total,
        'invoice_count': len(sibs), 'invoiced': invoiced, 'goods': goods,
        'outstanding': outstanding,
        'over_invoiced': invoiced - order_total > 0.005,
        'fully_invoiced': abs(outstanding) < 0.01,
        'conflicting_totals': len(totals) > 1,
        'siblings': [{'reference': d['reference'], 'invoice_number': d.get('invoice_number'),
                      'invoice_total': d.get('invoice_total', 0) or 0,
                      'status': d.get('status'), 'id': str(d['_id'])} for d in sibs],
    }


def _delivery_values(transactions: Collection, delivery_ids: Sequence[Any]) -> dict[Any, tuple[float, int]]:
    """delivery_id -> (goods value, line count), in one aggregation."""
    if not delivery_ids:
        return {}
    pipeline = [
        {'$match': {'type': 'receive', 'delivery_id': {'$in': delivery_ids}}},
        {'$group': {'_id': '$delivery_id',
                    'value': {'$sum': {'$ifNull': ['$line_value',
                                                   {'$multiply': ['$quantity', '$price']}]}},
                    'lines': {'$sum': 1}}},
    ]
    try:
        return {r['_id']: (r.get('value', 0) or 0, r.get('lines', 0)) for r in
                transactions.aggregate(pipeline)}
    except Exception as e:
        app.logger.warning(f"Delivery value aggregation unavailable: {e}")
        out: dict[Any, tuple[float, int]] = {}
        for l in transactions.find({'type': 'receive', 'delivery_id': {'$in': delivery_ids}},
                                   {'_id': 0, 'delivery_id': 1, 'quantity': 1,
                                    'price': 1, 'line_value': 1}):
            v, n = out.get(l['delivery_id'], (0.0, 0))
            out[l['delivery_id']] = (v + _line_value(l), n + 1)
        return out


def _reconcile_delivery(delivery: Doc, goods_value: float) -> Doc:
    """Three-way match: order vs invoice vs goods actually received.

    Differences are reported, never enforced. Stock physically standing on the
    shelf has to be recordable whatever the paperwork says; the job of this
    panel is to make sure nobody can record it without seeing the discrepancy.
    """
    order_total   = delivery.get('order_total', 0) or 0
    invoice_total = delivery.get('invoice_total', 0) or 0
    order_diff    = invoice_total - order_total
    goods_diff    = goods_value - invoice_total

    def band(diff, over, under):
        if abs(diff) < 0.01:
            return 'normal', 'matched', 'Matched'
        return ('expired' if diff > 0 else 'close-to-expire',
                f'M{diff:+.2f}', over if diff > 0 else under)

    o_class, o_label, o_note = band(
        order_diff, 'Invoiced above the order', 'Invoiced below the order')
    g_class, g_label, g_note = band(
        goods_diff, 'More goods than invoiced', 'Fewer goods than invoiced')
    return {'goods_value': goods_value,
            'order_class': o_class, 'order_diff_label': o_label, 'order_note': o_note,
            'goods_class': g_class, 'goods_diff_label': g_label, 'goods_note': g_note,
            'matched': abs(goods_diff) < 0.01 and abs(order_diff) < 0.01}


@app.route('/receive', methods=['GET', 'POST'])
@login_required
def receive() -> ResponseReturnValue:
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        deliveries = db['deliveries']
        # Read any flashed message from a preceding redirect (e.g. from
        # delete_receive success/error, or access-denied from other routes).
        flashed = get_flashed_messages()
        message = flashed[0] if flashed else None
        start_date = request.values.get('start_date')
        end_date = request.values.get('end_date')
        search = request.values.get('search')
        current_user = session['user']['name']
        # FIX (Performance): default to the current month so an unfiltered
        # visit never asks for the whole history. A search bypasses this and
        # looks across all time.
        start_date, end_date, window_defaulted = _default_window(start_date, end_date, search)
        page = _requested_page()

        base_query: Query = {'type': 'receive'}
        date_query: Query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt
        if date_query:
            base_query['timestamp'] = date_query
        if search:
            base_query['$or'] = [
                {'med_name':       {'$regex': search, '$options': 'i'}},
                {'batch':          {'$regex': search, '$options': 'i'}},
                {'supplier':       {'$regex': search, '$options': 'i'}},
                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                {'order_number':   {'$regex': search, '$options': 'i'}},
                {'invoice_number': {'$regex': search, '$options': 'i'}},
                {'expiry_date':    {'$regex': search, '$options': 'i'}},
            ]

        # FIX (Performance): a flat list, so ordinary skip/limit paging.
        total_lines = transactions.count_documents(base_query)
        pager = _pager(page, RECEIVE_PAGE_SIZE, total_lines)
        tx_list = list(transactions.find(base_query)
                       .sort('timestamp', -1)
                       .skip((pager['page'] - 1) * RECEIVE_PAGE_SIZE)
                       .limit(RECEIVE_PAGE_SIZE))

        # FIX (Bug): the original GET-based edit path used {'_id': edit_id} (a raw
        # string) which never matched a MongoDB ObjectId and always returned None.
        # The edit-receive route handles this properly, so the receive route now
        # simply never tries to load rx_data inline — editing always goes via
        # /edit-receive/<id>.
        rx_data = None

        # ---------------- Delivery actions ----------------------------
        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'open':
                try:
                    reference = f"DEL-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}"
                    new_id = deliveries.insert_one({
                        'reference': reference,
                        'supplier': request.form['supplier'].strip(),
                        'order_number': request.form['order_number'].strip(),
                        'order_total': float(request.form['order_total']),
                        'invoice_number': request.form['invoice_number'].strip(),
                        'invoice_total': float(request.form['invoice_total']),
                        'stock_receiver': request.form['stock_receiver'].strip(),
                        'note': (request.form.get('note') or '').strip(),
                        'status': 'open',
                        'received_by': current_user,
                        'opened_at': datetime.utcnow(),
                        'closed_by': None, 'closed_at': None,
                    }).inserted_id
                    write_audit_entry('CREATE', 'delivery', reference,
                                      f'Delivery {reference} opened by {current_user}')
                    flash(f'Delivery {reference} opened.')
                    return redirect(url_for('receive', delivery_id=str(new_id)))
                except (ValueError, KeyError) as e:
                    flash(f'Invalid delivery details: {e}')
                    return redirect(url_for('receive'))

            if action == 'line':
                d = _load_delivery(deliveries, request.form.get('delivery_id'))
                if not d:
                    flash('Delivery not found.')
                    return redirect(url_for('receive'))
                if d['status'] != 'open':
                    flash(f"Delivery {d['reference']} is closed. No further lines can be added.")
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))
                try:
                    med_name    = request.form['med_name'].strip()
                    quantity    = int(request.form['quantity'])       # single units
                    pack_size   = int(request.form.get('pack_size') or 1)
                    pack_price  = float(request.form['pack_price'])
                    batch       = request.form['batch'].strip()
                    expiry_date = request.form['expiry_date']
                    if quantity < 1:
                        raise ValueError('quantity must be at least 1')
                    if pack_size < 1:
                        raise ValueError('pack size must be at least 1')
                    if pack_price < 0:
                        raise ValueError('price cannot be negative')
                except (ValueError, KeyError) as e:
                    flash(f'Invalid line: {e}')
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))

                # NEW (Pack pricing): stock is counted in single units but
                # suppliers price by the pack, so the unit price is derived
                # rather than typed. Line value is computed from the pack price
                # directly so that a unit price which does not divide cleanly
                # (a pack of 3, say) cannot introduce a rounding mismatch
                # against the invoice.
                price      = _unit_price(pack_price, pack_size)
                line_value = quantity * pack_price / pack_size

                # FIX (Data integrity): this used to upsert, so a typo in the
                # medication name silently CREATED a new medication and put the
                # stock there, leaving the real item's balance untouched and
                # nobody any the wiser. Receiving now refuses an item that has
                # not been defined, exactly as the stock take does.
                existing_med = medications.find_one({'name': med_name})
                if not existing_med:
                    write_app_warning(
                        'medication_not_found',
                        f'Medication "{med_name}" not found while receiving on '
                        f'{d["reference"]}.',
                        {'med_name': med_name, 'delivery': d['reference']})
                    flash(f'Medication "{med_name}" is not on file. Add it under '
                          f'Add Medication first, then receive it here.')
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))

                # Classification comes from the medication record, never from
                # the form — see the note on the add-line template.
                schedule = existing_med.get('schedule', 'not controlled')

                # Supplier / order / invoice are INHERITED from the delivery
                # header — that is the whole point: they are typed once per
                # delivery, not once per medication, so they cannot drift
                # between lines of the same consignment.
                medications.update_one(
                    {'name': med_name},
                    {'$inc': {'balance': quantity},
                     '$set': {'batch': batch, 'price': price, 'expiry_date': expiry_date,
                              'pack_size': pack_size, 'pack_price': pack_price,
                              'stock_receiver': d['stock_receiver'],
                              'order_number': d['order_number'], 'supplier': d['supplier'],
                              'invoice_number': d['invoice_number']}}
                )
                transactions.insert_one({
                    'type': 'receive',
                    'med_name': med_name, 'quantity': quantity, 'batch': batch,
                    'price': price, 'pack_size': pack_size, 'pack_price': pack_price,
                    'line_value': line_value,
                    'expiry_date': expiry_date, 'schedule': schedule,
                    'stock_receiver': d['stock_receiver'],
                    'order_number': d['order_number'], 'supplier': d['supplier'],
                    'invoice_number': d['invoice_number'],
                    'delivery_id': str(d['_id']), 'delivery_reference': d['reference'],
                    'user': current_user, 'timestamp': datetime.utcnow(),
                })
                packs = quantity / pack_size
                flash(f'{med_name}: {quantity} units received onto {d["reference"]} '
                      f'({packs:g} x pack of {pack_size} at M{pack_price:.2f} = '
                      f'M{line_value:.2f}; unit price M{price:.4f}).')
                # line_added tells the page the line really posted, so the
                # half-typed draft can be dropped. The page must not try to
                # work this out by reading its own text.
                return redirect(url_for('receive', delivery_id=str(d['_id']),
                                        line_added=1))

            if action == 'close':
                d = _load_delivery(deliveries, request.form.get('delivery_id'))
                if not d:
                    flash('Delivery not found.')
                    return redirect(url_for('receive'))
                if d['status'] != 'open':
                    flash(f"Delivery {d['reference']} is already closed.")
                    return redirect(url_for('receive', delivery_id=str(d['_id'])))
                goods = _delivery_goods_value(transactions, str(d['_id']))
                deliveries.update_one(
                    {'_id': d['_id'], 'status': 'open'},
                    {'$set': {'status': 'closed', 'closed_by': current_user,
                              'closed_at': datetime.utcnow(),
                              'goods_value_at_close': goods}})
                write_audit_entry('UPDATE', 'delivery', d['reference'],
                                  f"closed by {current_user}; goods M{goods:.2f} "
                                  f"vs invoice M{d['invoice_total']:.2f}")
                # Warn but allow: a mismatch is surfaced, never blocking.
                if abs(goods - d['invoice_total']) >= 0.01:
                    flash(f"Delivery {d['reference']} closed with a mismatch: goods received "
                          f"M{goods:.2f} against invoice M{d['invoice_total']:.2f} "
                          f"(difference M{goods - d['invoice_total']:+.2f}).")
                else:
                    flash(f"Delivery {d['reference']} closed and fully matched.")
                return redirect(url_for('receive', delivery_id=str(d['_id'])))

            flash('Unknown receiving action.')
            return redirect(url_for('receive'))

        # ---------------- Delivery views ------------------------------
        delivery, delivery_lines, recon, delivery_rows = None, [], {}, []
        order_summary = None
        delivery = _load_delivery(deliveries, request.values.get('delivery_id'))
        if delivery:
            delivery_lines = list(transactions.find(
                {'type': 'receive', 'delivery_id': str(delivery['_id'])}
            ).sort('timestamp', 1).limit(500))
            recon = _reconcile_delivery(delivery,
                                        sum(_line_value(l) for l in delivery_lines))
            order_summary = _order_rollup(deliveries, transactions,
                                          delivery.get('order_number'))
        elif not rx_data:
            delivery_rows = list(deliveries.find().sort([('status', 1), ('opened_at', -1)]).limit(200))
            values = _delivery_values(transactions, [str(d['_id']) for d in delivery_rows])
            for d in delivery_rows:
                goods_value, line_count = values.get(str(d['_id']), (0.0, 0))
                d['goods_value'], d['line_count'] = goods_value, line_count
                diff = d['goods_value'] - d.get('invoice_total', 0)
                matched = abs(diff) < 0.01
                d['diff_label'] = 'matched' if matched else f'M{diff:+.2f}'
                d['row_class'] = ('close-to-expire' if d['status'] == 'open'
                                  else ('normal' if matched else 'expired'))

        return render_template(
            'receive.html',
            tx_list=tx_list, nav_links=get_nav_links(),
            message=message, start_date=start_date, end_date=end_date,
            search=search, rx_data=rx_data,
            current_user=session['user']['name'],
            delivery=delivery, delivery_lines=delivery_lines, recon=recon,
            deliveries=delivery_rows, order_summary=order_summary,
            pager=pager, window_defaulted=window_defaulted, unit='receipts'
        )
    except ServerSelectionTimeoutError:
        return render_template(
            'receive.html', tx_list=[], nav_links=get_nav_links(),
            delivery=None, delivery_lines=[], recon={}, deliveries=[], order_summary=None,
            message="Database connection failed.", start_date='', end_date='', search='', rx_data=None,
            current_user=session.get('user', {}).get('name', ''),
            pager={'page': 1, 'pages': 1, 'total': 0, 'page_size': 0, 'has_prev': False, 'has_next': False, 'first': 0, 'last': 0}, window_defaulted=False, unit='receipts'
        ), 500


@app.route('/add-medication', methods=['GET', 'POST'])
@login_required
def add_medication() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can add new medications.')
        return redirect('/reports')
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']
        message = None
        current_user = session['user']['name']

        if request.method == 'POST':
            try:
                med_name       = request.form['med_name'].strip()
                # NEW (Pack pricing): the unit price is derived here too, so an
                # item defined on this page values its stock correctly from the
                # moment it is created rather than only after it is first
                # received on a delivery.
                category       = request.form.get('category') or CATEGORY_PHARM
                if category not in CATEGORIES:
                    category = CATEGORY_PHARM
                pack_size      = int(request.form.get('pack_size') or 1)
                pack_price     = float(request.form['pack_price'])
                schedule       = request.form['schedule']
                initial_balance= int(request.form.get('initial_balance') or 0)
                batch          = (request.form.get('batch') or '').strip()
                expiry_date    = (request.form.get('expiry_date') or '').strip()
                stock_receiver = (request.form.get('stock_receiver') or '').strip()
                if pack_size < 1:
                    raise ValueError('pack size must be at least 1')
                if pack_price < 0:
                    raise ValueError('price cannot be negative')
                if initial_balance < 0:
                    raise ValueError('opening balance cannot be negative')
                retail = _retail_fields(request.form)

                if medications.find_one({'name': med_name}):
                    message = f'Medication "{med_name}" already exists. Use Receiving to add stock.'
                    return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES, web_categories=WEB_CATEGORIES)

                price = _unit_price(pack_price, pack_size)
                medications.insert_one({
                    'name': med_name, 'category': category,
                    'balance': initial_balance, 'batch': batch,
                    'price': price, 'pack_size': pack_size, 'pack_price': pack_price,
                    'expiry_date': expiry_date, 'schedule': schedule,
                    'stock_receiver': stock_receiver, **retail,
                })

                # An opening load is stock that was already on the shelf, not a
                # supplier delivery. It is written as its own transaction type so
                # that it can never be mistaken for one: it has no invoice to
                # reconcile against, it must not appear in the receive list or
                # the delivery totals, and it must not be read as a stock-take
                # correction either. Reports that back-calculate balances treat
                # it exactly like a receipt, which is what it is in stock terms.
                if initial_balance > 0:
                    transactions.insert_one({
                        'type': 'opening_balance',
                        'med_name': med_name, 'quantity': initial_balance,
                        'batch': batch, 'price': price,
                        'pack_size': pack_size, 'pack_price': pack_price,
                        'line_value': initial_balance * pack_price / pack_size,
                        'expiry_date': expiry_date, 'schedule': schedule,
                        'stock_receiver': stock_receiver,
                        'user': current_user, 'timestamp': datetime.utcnow(),
                    })
                    write_audit_entry('CREATE', 'opening_balance', med_name,
                                      f'opening stock of {initial_balance} units '
                                      f'recorded by {current_user}')
                    message = (f'Medication added with an opening balance of '
                               f'{initial_balance} units (unit price M{price:.4f}).')
                else:
                    message = 'Medication added successfully! Receive stock against a delivery.'
                return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES, web_categories=WEB_CATEGORIES)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES, web_categories=WEB_CATEGORIES)

        return render_template('add_medication.html', nav_links=get_nav_links(), message=message,
                     current_user=session.get('user', {}).get('name', ''),
                                                  categories=CATEGORIES, web_categories=WEB_CATEGORIES)
    except ServerSelectionTimeoutError:
        return render_template(
            'add_medication.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later."
        ), 500


@app.route('/edit-medication', methods=['GET', 'POST'])
@login_required
def edit_medication() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can edit medications.')
        return redirect('/reports')
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        message = None

        # FIX (Bug): med_name used to be a URL path segment
        # (/edit-medication/<med_name>). Path segments containing spaces,
        # commas, etc. require percent-encoding, and different hosting
        # platforms decode that path differently before Werkzeug's router
        # ever sees it — this worked on Render (which passes the raw request
        # straight through to gunicorn/Werkzeug) but broke on Vercel (whose
        # Python runtime sits a proxy/adapter in front that normalizes the
        # path inconsistently, leaving a stray layer of %-encoding no matter
        # how the link was built). Query string values and form POST bodies
        # don't go through that same path-segment handling, so they decode
        # identically everywhere. GET now reads med_name from ?med_name=...,
        # POST reads it from the form body (already used a hidden/readonly
        # input for this, so no template change needed there).
        if request.method == 'POST':
            med_name = request.form.get('med_name')
        else:
            med_name = request.args.get('med_name')

        if not med_name:
            message = 'No medication specified.'
            return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES, web_categories=WEB_CATEGORIES,
                                          message=message, med_data=None, med_name=None)

        med = medications.find_one({'name': med_name})
        if not med:
            message = f'Medication "{med_name}" not found.'
            write_app_warning(
                'medication_not_found',
                f'Medication "{med_name}" not found when attempting to edit.',
                {'med_name': med_name}
            )
            return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES, web_categories=WEB_CATEGORIES,
                                          message=message, med_data=None, med_name=med_name)

        med_data = med
        if request.method == 'POST':
            try:
                batch       = request.form['batch']
                # NEW (Pack pricing): the unit price is derived here as well, so
                # a pack size can be corrected without waiting for the item to be
                # received again — which is what the order request needs in order
                # to ask for whole packs.
                category    = request.form.get('category') or CATEGORY_PHARM
                if category not in CATEGORIES:
                    category = CATEGORY_PHARM
                pack_size   = int(request.form.get('pack_size') or 1)
                pack_price  = float(request.form['pack_price'])
                if pack_size < 1:
                    raise ValueError('pack size must be at least 1')
                if pack_price < 0:
                    raise ValueError('price cannot be negative')
                price       = _unit_price(pack_price, pack_size)
                expiry_date = request.form['expiry_date']
                schedule    = request.form['schedule']
                retail      = _retail_fields(request.form)
                medications.update_one(
                    {'name': med_name},
                    # balance deliberately absent — see the note in the template
                    {'$set': {'batch': batch, 'price': price, 'category': category,
                              'pack_size': pack_size, 'pack_price': pack_price,
                              'expiry_date': expiry_date, 'schedule': schedule,
                              **retail}}
                )
                message = 'Medication updated successfully!'
                med_data = medications.find_one({'name': med_name})
                return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES, web_categories=WEB_CATEGORIES,
                                              message=message, med_data=med_data, med_name=med_name)
            except ValueError as e:
                message = f'Invalid input: {str(e)}'
                return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES, web_categories=WEB_CATEGORIES,
                                              message=message, med_data=med_data, med_name=med_name)

        return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES, web_categories=WEB_CATEGORIES,
                                      message=message, med_data=med_data, med_name=med_name)
    except ServerSelectionTimeoutError:
        # FIX: med_name may not be assigned yet if get_mongo_client() itself
        # raised this before reaching that line — read it defensively here
        # rather than risk an UnboundLocalError masking the real DB error.
        fallback_med_name = (request.form.get('med_name') if request.method == 'POST'
                             else request.args.get('med_name'))
        return render_template('edit_medication.html', nav_links=get_nav_links(),
                                          categories=CATEGORIES, web_categories=WEB_CATEGORIES,
                                      message="Database connection failed.", med_data=None, med_name=fallback_med_name), 500


@app.route('/delete-medication', methods=['POST'])
@login_required
def delete_medication() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can delete medications.')
        return redirect('/reports')
    med_name = request.form.get('med_name')
    if not med_name:
        session['message'] = 'No medication specified.'
        return redirect('/reports')
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        med = medications.find_one({'name': med_name})
        if not med:
            session['message'] = f'Medication "{med_name}" not found.'
            write_app_warning(
                'medication_not_found',
                f'Medication "{med_name}" not found when attempting to delete.',
                {'med_name': med_name}
            )
            return redirect('/reports')
        result = medications.delete_one({'name': med_name})
        if result.deleted_count > 0:
            session['message'] = f'Medication "{med_name}" deleted successfully.'
        else:
            session['message'] = f'Failed to delete "{med_name}".'
    except Exception as e:
        session['message'] = f'Error deleting medication: {str(e)}'
    return redirect('/reports')


def _demand_by_med(transactions: Collection, time_filter: Query,
                   med_names: list[str] | None = None) -> dict[str, Doc]:
    """med_name -> {'shortfall', 'events'} — demand that could not be met.

    Kept apart from _movement_by_med on purpose: unmet demand moves no stock,
    so it must never touch a balance calculation. It exists only to correct the
    demand signal, which consumption alone understates precisely when an item
    is stocking out — the case where an accurate figure matters most.
    """
    match = {'type': 'unmet_demand', 'timestamp': time_filter}
    if med_names is not None:
        match['med_name'] = {'$in': med_names}
    try:
        return {r['_id']: {'shortfall': r.get('shortfall', 0) or 0,
                           'events': r.get('events', 0) or 0}
                for r in transactions.aggregate([
                    {'$match': match},
                    {'$group': {'_id': '$med_name',
                                'shortfall': {'$sum': '$shortfall'},
                                'events': {'$sum': 1}}}])}
    except Exception as e:
        app.logger.warning(f"Demand aggregation unavailable, falling back: {e}")
        out: dict[str, Doc] = {}
        for t in transactions.find(match, {'_id': 0, 'med_name': 1, 'shortfall': 1}):
            b = out.setdefault(t.get('med_name'), {'shortfall': 0, 'events': 0})
            b['shortfall'] += t.get('shortfall', 0) or 0
            b['events'] += 1
        return out


_ZERO_DEMAND = {'shortfall': 0, 'events': 0}


def _demand_by_month(transactions: Collection, since: datetime) -> dict[str, dict[tuple[int, int], float]]:
    """med_name -> (year, month) -> units asked for and not supplied.

    The dashboard averages month by month, so it needs unmet demand by month
    too, not one total for the window. Unmet-demand records are few, so they
    are read directly rather than aggregated.
    """
    out: dict[str, dict[tuple[int, int], float]] = {}
    for t in transactions.find({'type': 'unmet_demand', 'timestamp': {'$gte': since}},
                               {'_id': 0, 'med_name': 1, 'shortfall': 1, 'timestamp': 1}):
        ts = t.get('timestamp')
        if not isinstance(ts, datetime):
            continue
        b = out.setdefault(t.get('med_name') or '', {})
        b[(ts.year, ts.month)] = b.get((ts.year, ts.month), 0) + (t.get('shortfall', 0) or 0)
    return out



# NEW (Categories): items are either pharmaceuticals or medical supplies. This is
# a field on the item, NOT a second collection — deliveries, stock takes,
# write-offs, internal issues, ordering and the dashboard then work on supplies
# with no new code and no second set of bugs to keep in step.
CATEGORY_PHARM = 'Pharmaceutical'
CATEGORY_SUPPLY = 'Medical Supply'
CATEGORY_DONOR = 'Donor Funded'
CATEGORIES = [CATEGORY_PHARM, CATEGORY_SUPPLY, CATEGORY_DONOR]
CATEGORY_LABELS = {'pharmaceutical': 'Pharmaceuticals',
                   'supply': 'Medical Supplies',
                   'donor': 'Donor Funded',
                   'both': 'All Categories'}


# ---------------------------------------------------------------------------
# Retail: selling price and the public website listing.
#
# 'price' stays what it always was — the COST of one unit, frozen on each line
# so stock values and margins are honest. 'selling_price' is what the customer
# pays per unit. Prescription-only items are sold against a prescription
# number and are never priced on the website.
# ---------------------------------------------------------------------------
WEB_CATEGORIES = ['Prescription medicines', 'Over-the-counter medicines',
                  'Packaged herbal medicines', 'Supplements & cosmetics',
                  'Medical supplies']
WEB_DESCRIPTION_MAX = 200


def _retail_fields(form: Any) -> Doc:
    """Selling price and website fields from an add/edit product form."""
    raw = (form.get('selling_price') or '').strip()
    selling_price = float(raw) if raw else 0.0
    if selling_price < 0:
        raise ValueError('selling price cannot be negative')
    rx_only = form.get('rx_only') == 'on'
    web_category = form.get('web_category') or ''
    if web_category not in WEB_CATEGORIES:
        web_category = WEB_CATEGORIES[0] if rx_only else WEB_CATEGORIES[1]
    return {
        'selling_price': round(selling_price, 2),
        'rx_only': rx_only,
        'on_website': form.get('on_website') == 'on',
        'web_category': web_category,
        'description': (form.get('description') or '').strip()[:WEB_DESCRIPTION_MAX],
    }


def _category_filter(choice: str | None) -> Query:
    """Query fragment for the chosen category view.

    Items recorded before categories existed have no field at all, so
    'pharmaceutical' must match a MISSING value too — otherwise every existing
    medication would vanish from every report the moment this shipped.
    """
    if choice == 'supply':
        return {'category': CATEGORY_SUPPLY}
    if choice == 'donor':
        return {'category': CATEGORY_DONOR}
    if choice == 'both':
        return {}
    # Pharmaceuticals is expressed as "neither of the others" so that records
    # written before categories existed — which have no field at all — still
    # appear. Asking for category == 'Pharmaceutical' would hide every item
    # already in the database.
    return {'category': {'$nin': [CATEGORY_SUPPLY, CATEGORY_DONOR]}}


def _category_choice() -> str:
    c = (request.values.get('category') or 'pharmaceutical').strip()
    return c if c in ('pharmaceutical', 'supply', 'donor', 'both') else 'pharmaceutical'


def _cat_of(med: Doc) -> str:
    return med.get('category') or CATEGORY_PHARM


def _active(base: Query | None = None) -> Query:
    """Add the not-archived condition to a medications query.

    Archived items keep every transaction they ever had, so historical reports
    still reconcile — they simply stop appearing in the lists you work from.
    """
    q = dict(base or {})
    q['archived'] = {'$ne': True}
    return q


def _name_key(name: str | None) -> str:
    """Sort key for medication names.

    FIX (Bug): lists were sorted with MongoDB's default ordering, which
    compares raw bytes. Every capitalised name therefore sorted ahead of every
    lower-case one — "amoxicillin" landed after "Zinc" — and a name with a
    stray leading space jumped to the very top. Neither looks alphabetical to
    a person reading the report.
    """
    return (name or '').strip().lower()


def _by_name(docs: Iterable[Doc]) -> Docs:
    """Sort medication documents the way a human reads a list."""
    return sorted(docs, key=lambda d: _name_key(d.get('name')))



@app.route('/edit-loss', methods=['POST'])
@login_required
def edit_loss() -> ResponseReturnValue:
    """Correct the quantity or unit price on a recorded loss.

    A loss already moved stock, so changing its quantity has to move stock by
    the DIFFERENCE — not by the new figure. Writing off 60 when it should have
    been 40 means 20 units are still on the shelf and must go back; the
    reverse takes 20 more off. Overwriting the quantity without that
    adjustment would leave the balance permanently wrong while the ledger
    looked correct.

    The price is different: it never moved stock, so correcting it only
    revalues the loss.
    """
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can edit a recorded loss.')
        return redirect('/reports')

    start_date = request.form.get('start_date') or ''
    end_date = request.form.get('end_date') or ''

    def back():
        return redirect(url_for('reports', report_type='expiry_writeoffs',
                                start_date=start_date, end_date=end_date))

    try:
        db = get_mongo_client()['pharmacy_db']
        try:
            tx = db['transactions'].find_one(
                {'_id': ObjectId(request.form.get('loss_id', '')),
                 'type': {'$in': ['expiry_removal', 'damage']}})
        except (InvalidId, TypeError):
            tx = None
        if not tx:
            flash('That loss record was not found.')
            return back()

        try:
            new_qty = int(request.form.get('quantity', ''))
            new_price = float(request.form.get('price', ''))
        except (TypeError, ValueError):
            flash('Quantity must be a whole number and price a number.')
            return back()
        if new_qty < 1:
            flash('Quantity must be at least 1. To cancel a loss entirely, '
                  'reverse it by recording the stock back in.')
            return back()
        if new_price < 0:
            flash('Price cannot be negative.')
            return back()
        # Only touch remarks when the form actually carried the field; an edit
        # posted without it must not silently clear what is there.
        remarks_given = 'remarks' in request.form
        new_remarks = (request.form.get('remarks') or '').strip()

        med_name = tx.get('med_name')
        med = db['medications'].find_one({'name': med_name})
        if not med:
            flash(f'Medication "{med_name}" is no longer on file, so this loss '
                  f'cannot be edited.')
            return back()

        old_qty = abs(tx.get('quantity', 0) or 0)
        old_price = tx.get('price', 0) or 0
        delta = new_qty - old_qty          # >0 means MORE is being written off
        balance = med.get('balance', 0) or 0
        if delta > balance:
            flash(f'Cannot increase this loss to {new_qty}: that needs '
                  f'{delta} more unit(s) and only {balance} are on hand.')
            return back()

        if delta:
            db['medications'].update_one({'name': med_name},
                                         {'$inc': {'balance': -delta}})
        db['transactions'].update_one(
            {'_id': tx['_id']},
            {'$set': {'quantity': -new_qty, 'price': new_price,
                      'line_value': -new_qty * new_price,
                      'edited_by': session['user']['name'],
                      'edited_at': datetime.utcnow()}})

        changes = []
        if old_qty != new_qty:
            changes.append(f'quantity from {old_qty} to {new_qty} '
                           f'(stock on hand from {balance} to {balance - delta})')
        if abs(old_price - new_price) >= 0.0001:
            changes.append(f'unit price from M{old_price:.4f} to M{new_price:.4f}')
        old_remarks = tx.get('remarks') or tx.get('reason') or ''
        if remarks_given and new_remarks != old_remarks:
            db['transactions'].update_one({'_id': tx['_id']},
                                          {'$set': {'remarks': new_remarks}})
            changes.append('remarks updated')
        if not changes:
            flash('Nothing was changed.')
            return back()
        write_audit_entry('UPDATE', 'stock_loss',
                          f'{med_name} ({tx.get("type")})', '; '.join(changes))
        flash(f'{med_name}: loss corrected — {"; ".join(changes)}. '
              f'Value now M{new_qty * new_price:.2f}.')
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return back()


@app.route('/remove-expired', methods=['POST'])
@login_required
def remove_expired() -> ResponseReturnValue:
    """Write a stated number of expired units off the shelf.

    A quantity rather than an all-or-nothing button, because batches expire at
    different times and the balance on hand is often a mix — writing off the
    whole balance when one batch expired would destroy good stock on paper.
    """
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can remove expired stock.')
        return redirect('/reports')
    report_type = request.form.get('report_type', 'expired_list')
    try:
        db = get_mongo_client()['pharmacy_db']
        med_name = (request.form.get('med_name') or '').strip()
        try:
            quantity = int(request.form.get('quantity', ''))
        except (TypeError, ValueError):
            flash('Quantity must be a whole number.')
            return redirect(url_for('reports', report_type=report_type))
        med = db['medications'].find_one({'name': med_name})
        if not med:
            flash(f'Medication "{med_name}" not found.')
            return redirect(url_for('reports', report_type=report_type))
        balance = med.get('balance', 0) or 0
        if quantity < 1:
            flash('Quantity must be at least 1.')
            return redirect(url_for('reports', report_type=report_type))
        if quantity > balance:
            flash(f'Cannot remove {quantity} units of "{med_name}" — only {balance} on hand.')
            return redirect(url_for('reports', report_type=report_type))

        price = med.get('price', 0) or 0
        db['medications'].update_one({'name': med_name}, {'$inc': {'balance': -quantity}})
        # Its own transaction type: this is stock leaving the shelf, but it is
        # neither consumption nor a counting error, and every report that
        # back-calculates a balance has to unwind it as its own thing.
        db['transactions'].insert_one({
            'type': 'expiry_removal',
            'med_name': med_name,
            'quantity': -quantity,               # signed, like an adjustment
            'price': price, 'line_value': -quantity * price,
            'batch': med.get('batch'), 'expiry_date': med.get('expiry_date'),
            'schedule': med.get('schedule'),
            'reason': (request.form.get('reason') or '').strip(),
            'balance_before': balance,
            'user': session['user']['name'], 'timestamp': datetime.utcnow(),
        })
        write_audit_entry('UPDATE', 'expiry_removal', med_name,
                          f'{quantity} unit(s) written off as expired '
                          f'(balance {balance} -> {balance - quantity}, '
                          f'value M{quantity * price:.2f})')
        flash(f'{med_name}: {quantity} unit(s) removed as expired. '
              f'Balance is now {balance - quantity}. Value written off M{quantity * price:.2f}.')
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return redirect(url_for('reports', report_type=report_type))


@app.route('/remove-expired-bulk', methods=['POST'])
@login_required
def remove_expired_bulk() -> ResponseReturnValue:
    """Write off several expired items in one pass.

    The app already knows exactly what has expired, so clearing a month's
    worth should be one review and one button, not two interactions per item.

    Each line keeps its own quantity, pre-filled with the balance but
    editable, because batches expire at different times and writing off a
    whole balance when one batch went out of date destroys good stock on
    paper. Unticking a line leaves it alone.

    A line that cannot be written off does not stop the rest: it is skipped
    and named in the result, so one bad figure never costs the whole pass.
    """
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can remove expired stock.')
        return redirect('/reports')
    report_type = request.form.get('report_type', 'expired_list')
    reason = (request.form.get('bulk_reason') or '').strip()
    try:
        db = get_mongo_client()['pharmacy_db']
        selected = [n.strip() for n in request.form.getlist('sel') if n.strip()]
        if not selected:
            flash('Nothing was ticked, so nothing was removed.')
            return redirect(url_for('reports', report_type=report_type))

        removed, skipped, total_value, total_units = [], [], 0.0, 0
        for med_name in selected:
            raw = (request.form.get(f'qty__{med_name}') or '').strip()
            try:
                quantity = int(raw)
            except (TypeError, ValueError):
                skipped.append(f'{med_name} (quantity "{raw}" is not a number)')
                continue
            med = db['medications'].find_one({'name': med_name})
            if not med:
                skipped.append(f'{med_name} (not found)')
                continue
            balance = med.get('balance', 0) or 0
            if quantity < 1:
                skipped.append(f'{med_name} (quantity must be at least 1)')
                continue
            if quantity > balance:
                skipped.append(f'{med_name} (only {balance} on hand)')
                continue

            price = med.get('price', 0) or 0
            db['medications'].update_one({'name': med_name},
                                         {'$inc': {'balance': -quantity}})
            db['transactions'].insert_one({
                'type': 'expiry_removal',
                'med_name': med_name,
                'quantity': -quantity,
                'price': price, 'line_value': -quantity * price,
                'batch': med.get('batch'), 'expiry_date': med.get('expiry_date'),
                'schedule': med.get('schedule'),
                'reason': reason,
                'balance_before': balance,
                'user': session['user']['name'], 'timestamp': datetime.utcnow(),
            })
            write_audit_entry('UPDATE', 'expiry_removal', med_name,
                              f'{quantity} unit(s) written off as expired in a '
                              f'bulk pass (balance {balance} -> '
                              f'{balance - quantity}, '
                              f'value M{quantity * price:.2f})')
            removed.append(med_name)
            total_units += quantity
            total_value += quantity * price

        parts = []
        if removed:
            parts.append(f"{len(removed)} item{'' if len(removed) == 1 else 's'} "
                         f'written off, {total_units} unit'
                         f"{'' if total_units == 1 else 's'}, "
                         f'value M{total_value:.2f}')
        if skipped:
            parts.append(f"not removed: {'; '.join(skipped)}")
        flash('. '.join(parts) + '.' if parts else 'Nothing was removed.',
              'error' if skipped and not removed else
              ('partial' if skipped else 'success'))
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return redirect(url_for('reports', report_type=report_type))


@app.route('/correct-expiry', methods=['POST'])
@login_required
def correct_expiry() -> ResponseReturnValue:
    """Fix a wrongly recorded expiry date. Moves no stock, by design."""
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can correct expiry dates.')
        return redirect('/reports')
    report_type = request.form.get('report_type', 'expired_list')
    try:
        db = get_mongo_client()['pharmacy_db']
        med_name = (request.form.get('med_name') or '').strip()
        new_date = (request.form.get('expiry_date') or '').strip()
        try:
            datetime.strptime(new_date, '%Y-%m-%d')
        except ValueError:
            flash('Expiry date must be a valid date (YYYY-MM-DD).')
            return redirect(url_for('reports', report_type=report_type))
        med = db['medications'].find_one({'name': med_name})
        if not med:
            flash(f'Medication "{med_name}" not found.')
            return redirect(url_for('reports', report_type=report_type))
        old_date = med.get('expiry_date')
        if old_date == new_date:
            flash(f'{med_name}: expiry date is already {new_date}.')
            return redirect(url_for('reports', report_type=report_type))
        db['medications'].update_one({'name': med_name},
                                     {'$set': {'expiry_date': new_date}})
        write_audit_entry('UPDATE', 'medication', med_name,
                          f'expiry date corrected: {old_date} -> {new_date}')
        flash(f'{med_name}: expiry date corrected from {old_date} to {new_date}. '
              f'No stock was moved.')
    except ServerSelectionTimeoutError:
        flash('Database connection failed. Please try again later.')
    return redirect(url_for('reports', report_type=report_type))


@app.route('/reports', methods=['GET', 'POST'])
@login_required
def reports() -> ResponseReturnValue:
    is_admin = session['user'].get('role') == 'admin'
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        medications = db['medications']
        transactions = db['transactions']

        cat = _category_choice()
        cat_q = _category_filter(cat)
        cat_label = CATEGORY_LABELS[cat]

        report_data = []
        receive_list = []
        stock_data: Docs = []
        controlled_register = []
        order_request = None
        monthly = None
        writeoffs: Docs = []
        writeoff_totals = {'item_count': 0, 'units': 0, 'value': 0.0, 'by_med': []}
        unmet_list = []
        duplicates = []
        unmet_totals: Doc = {'item_count': 0, 'shortfall': 0, 'value': 0.0,
                            'fill_rate': None, 'by_med': []}
        issues = []
        issue_totals = {'lines': 0, 'units': 0, 'value': 0.0, 'dept_count': 0, 'by_dept': [], 'by_med': []}
        sick_leave: Docs = []
        sick_leave_totals = {'patients': 0, 'days': 0, 'average': '-', 'longest': 0, 'repeat': []}
        report_type = None
        start_date = None
        end_date = None
        total_transactions = 0
        message_class = None
        message = session.pop('message', None)
        # Also read any flashed message from a preceding redirect (e.g.
        # access-denied from add/edit/delete medication routes).
        # Honour the category the route set rather than guessing severity
        # from the wording — that read a partly-successful bulk write-off,
        # which names what it skipped, as an outright error.
        if not message:
            message_class, message = _flashed_banner()
        search = None
        report_title = None
        start_dt = None
        end_dt = None

        def matches_search(tx, search_str):
            if not search_str:
                return True
            sl = search_str.lower()
            for field in ['patient', 'med_name', 'company', 'position', 'prescriber',
                          'dispenser', 'stock_receiver', 'order_number', 'supplier',
                          'invoice_number', 'batch', 'user']:
                if sl in str(tx.get(field, '')).lower():
                    return True
            diagnoses = tx.get('diagnoses', [])
            if isinstance(diagnoses, list):
                if sl in ' '.join(str(d).lower() for d in diagnoses):
                    return True
            return False

        stock_report_types = ['stock_on_hand', 'expired_list', 'near_expired_list', 'out_of_stock_list']

        # NEW: also honour a GET with a report_type, so the expiry actions can
        # return the admin to the list they were working in rather than dumping
        # them back at the report menu after every removal.
        if request.method == 'POST' or request.args.get('report_type'):
            report_type = request.values.get('report_type')
            start_date  = request.values.get('start_date')
            end_date    = request.values.get('end_date')
            search      = request.values.get('search')

            if report_type == 'inventory' and not start_date:
                # The form fills this in, but a report opened without dates
                # still gets the three months its order figures need.
                today_local = local_now().date()
                start_date = _months_back(today_local, INVENTORY_DEFAULT_MONTHS).strftime('%Y-%m-%d')
                end_date = end_date or today_local.strftime('%Y-%m-%d')

            if report_type:
                try:
                    if start_date:
                        start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    if end_date:
                        end_dt = (datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                                  + timedelta(days=1) - timedelta(seconds=1))

                    if report_type in stock_report_types:
                        # NEW: default to today rather than refusing. "What is
                        # expired?" is a question about now, and the expiry
                        # actions redirect back here without a date.
                        if not end_date:
                            end_date = datetime.utcnow().strftime('%Y-%m-%d')
                            end_dt = (datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                                      + timedelta(days=1) - timedelta(seconds=1))
                        report_date     = datetime.strptime(end_date, '%Y-%m-%d').date()
                        threshold_date  = report_date + timedelta(days=30)
                        now_dt          = datetime.now(timezone.utc)
                        med_filter      = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        all_meds        = _by_name(medications.find(_active({**med_filter, **cat_q}), {'_id': 0}))
                        stock_data = []
                        # FIX (Performance): one aggregation for every medication
                        # instead of one per medication. Scoped by name only when a
                        # search has narrowed the set, so the $in list stays small.
                        movement = _movement_by_med(
                            transactions, {'$gt': end_dt, '$lte': now_dt},
                            [m['name'] for m in all_meds] if search else None)
                        for med in all_meds:
                            med_name = med['name']
                            current_balance = med.get('balance', 0)
                            # NEW (Stock Take): 'adjustment' transactions are
                            # signed and must be unwound too, or a count taken
                            # after the report date would distort the balance
                            # reported as at that date.
                            mv = movement.get(med_name, _ZERO_MOVEMENT)
                            balance_at_date = max(0, current_balance - mv['received']
                                                  + mv['dispensed'] + mv['issued']
                                                  - mv['adjusted'] - mv['expired']
                                                  - mv['damaged'])

                            expiry_str = med.get('expiry_date')
                            expiry_dt = None
                            if expiry_str:
                                try:
                                    date_part = expiry_str.split('T')[0]
                                    expiry_dt = datetime.strptime(date_part, '%Y-%m-%d').date()
                                except ValueError as e:
                                    app.logger.warning(f"Invalid expiry_date '{expiry_str}' for '{med_name}': {e}")

                            if not med.get('batch'):
                                med['batch'] = 'N/A'

                            if balance_at_date == 0:
                                status = 'out-of-stock'
                            elif expiry_dt is None:
                                status = 'normal'
                            elif expiry_dt < report_date:
                                status = 'expired'
                            elif expiry_dt <= threshold_date:
                                status = 'close-to-expire'
                            else:
                                status = 'normal'

                            med_copy = med.copy()
                            med_copy['balance'] = balance_at_date
                            med_copy['status']  = status
                            stock_data.append(med_copy)

                        if report_type == 'stock_on_hand':
                            report_title = f'Stock on Hand as of {end_date}'
                        elif report_type == 'expired_list':
                            stock_data   = [m for m in stock_data if m['status'] == 'expired']
                            report_title = f'Expired Drugs List as of {end_date}'
                        elif report_type == 'near_expired_list':
                            stock_data   = [m for m in stock_data if m['status'] == 'close-to-expire']
                            report_title = f'Near Expired Drug List as of {end_date}'
                        elif report_type == 'out_of_stock_list':
                            stock_data   = [m for m in stock_data if m['status'] == 'out-of-stock']
                            report_title = f'Out of Stock List as of {end_date}'

                    elif report_type == 'inventory':
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        med_filter   = {'name': {'$regex': search or '', '$options': 'i'}} if search else {}
                        # FIX (Bug): the .limit(300) that used to be here silently
                        # dropped every medication past the 300th from the report —
                        # no message, no indication that anything was missing. It
                        # only existed to bound the per-medication query loop below,
                        # which no longer exists, so the cap goes with it.
                        meds         = _by_name(medications.find(_active({**med_filter, **cat_q}),
                                                 {'_id': 0, 'name': 1, 'balance': 1, 'category': 1}))
                        days_in_period = max(1, (end_dt.date() - start_dt.date()).days + 1)
                        # FIX (Performance): one aggregation for the whole report
                        # instead of one per medication.
                        movement = _movement_by_med(
                            transactions, {'$gte': start_dt, '$lte': end_dt},
                            [m['name'] for m in meds] if search else None)
                        demand = _demand_by_med(
                            transactions, {'$gte': start_dt, '$lte': end_dt},
                            [m['name'] for m in meds] if search else None)
                        # Everything that moved AFTER the period. The stored
                        # balance is today's; a report for January has to take
                        # February onwards back off it, or January's closing
                        # balance is really today's, its beginning balance is
                        # worked back from the wrong figure, and the row is
                        # flagged as records missing when nothing is.
                        later = _movement_by_med(
                            transactions, {'$gt': end_dt, '$lte': datetime.now(timezone.utc)},
                            [m['name'] for m in meds] if search else None)

                        def _tidy(v):
                            return int(v) if isinstance(v, float) and v.is_integer() else round(v, 2)

                        for med in meds:
                            med_name = med['name']
                            mv = movement.get(med_name, _ZERO_MOVEMENT)
                            dispensed = mv['dispensed']
                            received  = mv['received']
                            # NEW (Stock Take): signed net of every physical-count
                            # correction posted in the period. Unwinding it here is
                            # what keeps the row internally consistent:
                            #   Beginning + Received - Dispensed + Adjustment = Current
                            adjustment        = mv['adjusted']
                            # NEW (Expiry): written-off stock is its own movement —
                            # neither consumption nor a counting error — so it is
                            # unwound separately or the beginning balance is wrong
                            # for every period containing a write-off.
                            expired           = mv['expired']
                            # NEW: stock issued to a department is CONSUMED — it
                            # left the store and has to be replaced — whereas
                            # damage is LOST. Both reduce stock, only one is
                            # demand, so they are tracked separately.
                            issued            = mv['issued']
                            damaged           = mv['damaged']
                            # The balance at the end of the period: today's,
                            # with everything since unwound, as Stock on Hand
                            # does. For a period ending today nothing is later
                            # and this is the stored balance unchanged.
                            lt = later.get(med_name, _ZERO_MOVEMENT)
                            current_balance   = (med.get('balance', 0) - lt['received']
                                                 + lt['dispensed'] + lt['issued']
                                                 - lt['adjusted'] - lt['expired']
                                                 - lt['damaged'])
                            current_balance   = int(current_balance) if float(current_balance).is_integer() else round(current_balance, 2)
                            beginning_balance = max(0, current_balance - received + dispensed
                                                    + issued - adjustment - expired - damaged)
                            # Consumption is everything issued out of the store,
                            # to a patient or to a department. Excluding internal
                            # issues would understate demand for exactly the items
                            # the wards rely on most.
                            consumption       = dispensed + issued
                            avg_daily         = consumption / days_in_period
                            # AMC (Average Monthly Consumption): consumption in the
                            # period scaled to a 30-day month, so the figure stays
                            # comparable whatever period length is selected.
                            amc               = avg_daily * 30
                            # NEW (Unmet demand): true demand is what was asked
                            # for, not what happened to be on the shelf. AMC is
                            # left as consumption — the recognised figure — and
                            # the demand-based version sits beside it, so the
                            # gap between the two is visible rather than buried.
                            unmet             = demand.get(med_name, _ZERO_DEMAND)['shortfall']
                            # A whole number, because nobody orders 41.37 of
                            # anything, rounded to the nearest unit (a half goes
                            # up, as on paper - Python's own round() would send
                            # 40.5 down to 40).
                            adjusted_amc      = int((consumption + unmet)
                                                    / days_in_period * 30 + 0.5)
                            # Ordered from the Adjusted AMC, since it is now the
                            # only AMC the table shows: a month in hand plus two
                            # months of lead time, less what is on the shelf.
                            # Working it out from the plain AMC would order from
                            # a figure nobody can see, and under-order exactly
                            # the items that keep running out.
                            amount_to_order   = max(0, 3 * adjusted_amc - current_balance)
                            # Everything that left the shelf, whatever the
                            # reason: dispensed, issued to a department, expired,
                            # damaged, and the net of stock-take corrections.
                            # Expired, damaged and corrections are stored signed
                            # (negative when stock goes), so they are subtracted;
                            # stock FOUND at a count therefore comes off Issued,
                            # as asked. One column, so the row always reads
                            #   Beginning + Received - Issued = Current
                            # and the breakdown is in the registers, the expiry
                            # write-offs and Audit > Stock Take.
                            issued_total      = (dispensed + issued - adjustment
                                                 - expired - damaged)

                            report_data.append({
                                'med_name': med_name, 'category': _cat_of(med),
                                'beginning_balance': beginning_balance,
                                'dispensed': dispensed, 'received': received,
                                'adjustment': adjustment, 'expired': expired,
                                'issued': issued, 'damaged': damaged,
                                # NEW: non-admins don't see the Adjustment column,
                                # so their Dispensed figure has to absorb it or the
                                # row won't add up. Folding it in here isn't a fudge
                                # — it is literally what the two discrepancy types
                                # mean. A shortfall (negative adjustment) is stock
                                # that WAS issued but never recorded, so it belongs
                                # in Dispensed. A surplus (positive adjustment) is
                                # an issue that was recorded but never happened, so
                                # it comes back out of Dispensed. Beginning balance
                                # stays truthful either way, and
                                #   Beginning + Received - Dispensed = Current
                                # holds for the non-admin view.
                                'dispensed_effective': dispensed - adjustment - expired - damaged,
                                'current_balance': current_balance,
                                'issued_total': _tidy(issued_total),
                                # False only when the records cannot explain
                                # the shelf: the back-calculated beginning would
                                # be negative and is shown as 0, so the row no
                                # longer adds up. Flagged on the page rather
                                # than forced to balance with an invented figure.
                                'reconciles': (beginning_balance + received
                                               - issued_total == current_balance),
                                'amc': _tidy(amc),
                                'unmet': unmet, 'adjusted_amc': adjusted_amc,
                                'fill_rate': (round(consumption / (consumption + unmet) * 100, 1)
                                              if (consumption + unmet) else None),
                                'amount_to_order': amount_to_order
                            })

                    elif report_type == 'receive_list':
                        base_query: Query = {'type': 'receive'}
                        if start_date and end_date:
                            base_query['timestamp'] = {'$gte': start_dt, '$lte': end_dt}
                        if search:
                            base_query['$or'] = [
                                {'med_name':       {'$regex': search, '$options': 'i'}},
                                {'batch':          {'$regex': search, '$options': 'i'}},
                                {'supplier':       {'$regex': search, '$options': 'i'}},
                                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                                {'order_number':   {'$regex': search, '$options': 'i'}},
                                {'invoice_number': {'$regex': search, '$options': 'i'}},
                                {'expiry_date':    {'$regex': search, '$options': 'i'}},
                            ]
                        receive_list = list(transactions.find(base_query).sort('timestamp', 1).limit(10000))

                    elif report_type == 'unmet_demand':
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        q = {'type': 'unmet_demand',
                             'timestamp': {'$gte': start_dt, '$lte': end_dt}}
                        if search:
                            q['$or'] = [
                                {'med_name':   {'$regex': search, '$options': 'i'}},
                                {'patient':    {'$regex': search, '$options': 'i'}},
                                {'prescriber': {'$regex': search, '$options': 'i'}},
                                {'user':       {'$regex': search, '$options': 'i'}},
                            ]
                        unmet_list = list(transactions.find(q).sort('timestamp', -1).limit(2000))
                        disp = _movement_by_med(transactions, {'$gte': start_dt, '$lte': end_dt})
                        by_med = {}
                        for u in unmet_list:
                            entry = by_med.setdefault(u.get('med_name', ''),
                                                  {'count': 0, 'shortfall': 0, 'value': 0.0})
                            entry['count'] += 1
                            entry['shortfall'] += u.get('shortfall', 0) or 0
                            entry['value'] += u.get('shortfall_value', 0) or 0
                        rows_by_med = []
                        for k, v in sorted(by_med.items(), key=lambda kv: -kv[1]['shortfall']):
                            d = disp.get(k, _ZERO_MOVEMENT)['dispensed']
                            total = d + v['shortfall']
                            rows_by_med.append({'med_name': k, **v, 'dispensed': d,
                                                'fill_rate': round(d / total * 100, 1) if total else None})
                        tot_short = sum(v['shortfall'] for v in by_med.values())
                        tot_disp = sum(r['dispensed'] for r in rows_by_med)
                        unmet_totals = {
                            'item_count': len(by_med),
                            'shortfall': tot_short,
                            'value': sum(v['value'] for v in by_med.values()),
                            'fill_rate': (round(tot_disp / (tot_disp + tot_short) * 100, 1)
                                          if (tot_disp + tot_short) else None),
                            'by_med': rows_by_med,
                        }
                        report_title = 'Unmet Demand'

                    elif report_type == 'expiry_writeoffs':
                        # NEW: the record of what was actually removed, as
                        # opposed to expired_list, which is a work list of what
                        # still needs removing. Once stock is written off it
                        # leaves that list entirely, so without this report the
                        # loss would only survive as an audit-log line.
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        q = {'type': {'$in': ['expiry_removal', 'damage']},
                             'timestamp': {'$gte': start_dt, '$lte': end_dt}}
                        if search:
                            q['$or'] = [
                                {'med_name': {'$regex': search, '$options': 'i'}},
                                {'reason':   {'$regex': search, '$options': 'i'}},
                                {'user':     {'$regex': search, '$options': 'i'}},
                                {'batch':    {'$regex': search, '$options': 'i'}},
                            ]
                        raw = list(transactions.find(q).sort('timestamp', -1).limit(2000))
                        # Records written before the expiry date was captured on
                        # damage fall back to the item's recorded expiry.
                        med_expiry = {m['name']: m.get('expiry_date') for m in
                                      medications.find({}, {'_id': 0, 'name': 1,
                                                            'expiry_date': 1})}
                        writeoffs, by_med = [], {}
                        for w in raw:
                            units = abs(w.get('quantity', 0) or 0)
                            value = abs(w.get('line_value') if w.get('line_value') is not None
                                        else units * (w.get('price', 0) or 0))
                            writeoffs.append({
                                'timestamp': w.get('timestamp'), 'med_name': w.get('med_name'),
                                'units': units, 'price': w.get('price', 0) or 0,
                                'value': value, 'batch': w.get('batch'),
                                'expiry_date': (w.get('expiry_date')
                                                or med_expiry.get(w.get('med_name'))),
                                'reason': w.get('reason'),
                                # Free text the admin writes. Falls back to the
                                # recorded reason so reformatting to the register
                                # columns drops nothing already captured.
                                'remarks': w.get('remarks') or w.get('reason') or '',
                                'id': str(w.get('_id')),
                                'kind': 'Damage' if w.get('type') == 'damage' else 'Expiry',
                                'user': w.get('user'),
                            })
                            entry = by_med.setdefault(w.get('med_name', ''),
                                                  {'count': 0, 'units': 0, 'value': 0.0})
                            entry['count'] += 1
                            entry['units'] += units
                            entry['value'] += value
                        writeoff_totals = {
                            # NOT 'items': Jinja resolves attribute access before
                            # subscript, so writeoff_totals.items would render the
                            # dict's .items METHOD instead of this value.
                            'item_count': len(by_med),
                            'units': sum(w['units'] for w in writeoffs),
                            'value': sum(w['value'] for w in writeoffs),
                            'by_med': [{'med_name': k, **v} for k, v in
                                       sorted(by_med.items(), key=lambda kv: -kv[1]['value'])],
                        }
                        report_title = 'Stock Losses'

                    elif report_type == 'monthly_report':
                        # NEW (Costing): a month at cost. Everything is valued
                        # from the price stored ON each transaction, so the
                        # report is reproducible — re-running it after a price
                        # change gives the same answer, which a lookup against
                        # today's price would not.
                        if not start_dt:
                            raise ValueError('A start date is required; the report covers the period containing it.')
                        # NEW: the same report over any period. The start date
                        # picks a point and the whole surrounding period is
                        # covered, so nobody has to work out where a week or a
                        # quarter begins.
                        period_kind = request.values.get('period', 'month')
                        if period_kind not in ('day', 'week', 'month', 'quarter', 'year'):
                            period_kind = 'month'
                        anchor = start_dt
                        if period_kind == 'day':
                            m_start = datetime(anchor.year, anchor.month, anchor.day)
                            m_end = m_start + timedelta(days=1) - timedelta(seconds=1)
                            label = m_start.strftime('%A, %d %B %Y')
                        elif period_kind == 'week':
                            wd = datetime(anchor.year, anchor.month, anchor.day)
                            m_start = wd - timedelta(days=wd.weekday())      # Monday
                            m_end = m_start + timedelta(days=7) - timedelta(seconds=1)
                            label = (f"Week of {m_start.strftime('%d %b')} to "
                                     f"{(m_end).strftime('%d %b %Y')}")
                        elif period_kind == 'quarter':
                            quarter = (anchor.month - 1) // 3
                            m_start = datetime(anchor.year, quarter * 3 + 1, 1)
                            m_end = (datetime(anchor.year + (quarter == 3), 1 if quarter == 3 else quarter * 3 + 4, 1)
                                     - timedelta(seconds=1))
                            label = f'Q{quarter + 1} {anchor.year}'
                        elif period_kind == 'year':
                            m_start = datetime(anchor.year, 1, 1)
                            m_end = datetime(anchor.year + 1, 1, 1) - timedelta(seconds=1)
                            label = str(anchor.year)
                        else:
                            m_start = datetime(anchor.year, anchor.month, 1)
                            m_end = (datetime(m_start.year + (m_start.month == 12),
                                              1 if m_start.month == 12 else m_start.month + 1, 1)
                                     - timedelta(seconds=1))
                            label = m_start.strftime('%B %Y')
                        period = {'$gte': m_start, '$lte': m_end}

                        lines = list(transactions.find(
                            {'timestamp': period,
                             'type': {'$in': ['dispense', 'receive', 'adjustment',
                                              'opening_balance', 'expiry_removal',
                                              'internal_issue', 'damage']}}))
                        cur_price = {m['name']: (m.get('price', 0) or 0)
                                     for m in medications.find({}, {'_id': 0, 'name': 1, 'price': 1})}

                        def valued(l):
                            """Prefer the price frozen on the transaction."""
                            if l.get('line_value') is not None:
                                return l['line_value'], True
                            if l.get('price'):
                                return (l.get('quantity', 0) or 0) * l['price'], True
                            return (l.get('quantity', 0) or 0) * cur_price.get(l.get('med_name'), 0), False

                        dispensed_value = received_value = opening_load_value = 0.0
                        adjustment_value = expired_value = 0.0
                        issued_value = damaged_value = 0.0
                        issued_lines = damaged_lines = 0
                        by_dept = {}
                        items_dispensed = receipt_lines = adjustment_lines = 0
                        expired_lines = 0
                        estimated_lines = 0
                        visits, patients = set(), set()
                        per_item: dict[str, Doc] = {}
                        for l in lines:
                            v, exact = valued(l)
                            t = l.get('type')
                            if t == 'dispense':
                                dispensed_value += v
                                items_dispensed += 1
                                if not exact:
                                    estimated_lines += 1
                                if l.get('transaction_id'):
                                    visits.add(l['transaction_id'])
                                if l.get('patient'):
                                    patients.add(l['patient'])
                                entry = per_item.setdefault(l.get('med_name', ''), {'units': 0, 'value': 0.0})
                                entry['units'] += l.get('quantity', 0) or 0
                                entry['value'] += v
                            elif t == 'receive':
                                received_value += v
                                receipt_lines += 1
                            elif t == 'opening_balance':
                                opening_load_value += v
                            elif t == 'expiry_removal':
                                expired_value += v
                                expired_lines += 1
                            elif t == 'internal_issue':
                                issued_value += v
                                issued_lines += 1
                                dept = by_dept.setdefault(l.get('department', 'Unspecified'),
                                                       {'lines': 0, 'value': 0.0})
                                dept['lines'] += 1
                                dept['value'] += v
                            elif t == 'damage':
                                damaged_value += v
                                damaged_lines += 1
                            else:
                                adjustment_value += v
                                adjustment_lines += 1

                        # Deliveries and orders, counted by the month the invoice landed.
                        month_dels = list(db['deliveries'].find({'opened_at': period}))
                        invoice_total = sum(d.get('invoice_total', 0) or 0 for d in month_dels)
                        orders: dict[Any, float] = {}
                        for d in month_dels:
                            orders.setdefault(d.get('order_number'), d.get('order_total', 0) or 0)

                        # Closing stock at today's prices; opening derived from it.
                        closing_value = sum((m.get('balance', 0) or 0) * (m.get('price', 0) or 0)
                                            for m in medications.find({}, {'_id': 0, 'balance': 1, 'price': 1}))
                        after = _movement_by_med(transactions, {'$gt': m_end, '$lte': datetime.utcnow()})
                        opening_value = closing_value
                        for m in medications.find({}, {'_id': 0, 'name': 1, 'balance': 1, 'price': 1}):
                            mv = after.get(m['name'], _ZERO_MOVEMENT)
                            p = m.get('price', 0) or 0
                            bal_at_end = max(0, (m.get('balance', 0) or 0)
                                             - mv['received'] + mv['dispensed'] - mv['adjusted'])
                            opening_value += (bal_at_end - (m.get('balance', 0) or 0)) * p
                        # roll back through this month's own movements too
                        month_mv = _movement_by_med(transactions, period)
                        for m in medications.find({}, {'_id': 0, 'name': 1, 'price': 1}):
                            mv = month_mv.get(m['name'], _ZERO_MOVEMENT)
                            p = m.get('price', 0) or 0
                            opening_value -= (mv['received'] - mv['dispensed'] + mv['adjusted']) * p
                        opening_value = max(0.0, opening_value)

                        top = sorted(per_item.items(), key=lambda kv: -kv[1]['value'])[:15]
                        monthly = {
                            'label': label, 'period_kind': period_kind,
                            'starts': m_start.strftime('%Y-%m-%d'),
                            'ends': m_end.strftime('%Y-%m-%d'),
                            'opening_value': opening_value,
                            'closing_value': closing_value,
                            'received_value': received_value,
                            'opening_load_value': opening_load_value,
                            'dispensed_value': dispensed_value,
                            'adjustment_value': adjustment_value,
                            'adjustment_lines': adjustment_lines,
                            'expired_value': expired_value,
                            'expired_lines': expired_lines,
                            'issued_value': issued_value, 'issued_lines': issued_lines,
                            'damaged_value': damaged_value, 'damaged_lines': damaged_lines,
                            'by_dept': [{'name': k, **v} for k, v in
                                        sorted(by_dept.items(), key=lambda kv: -kv[1]['value'])],
                            'receipt_lines': receipt_lines,
                            'delivery_count': len(month_dels),
                            'invoice_count': len(month_dels),
                            'invoice_total': invoice_total,
                            'order_count': len(orders),
                            'order_total': sum(orders.values()),
                            'invoice_gap': invoice_total - received_value,
                            'visits': len(visits), 'patients': len(patients),
                            'items_dispensed': items_dispensed,
                            'items_per_visit': f'{items_dispensed / len(visits):.1f}' if visits else '-',
                            'cost_per_visit': dispensed_value / len(visits) if visits else 0.0,
                            # Cost per PATIENT is not cost per visit: a patient who
                            # attends three times in the period is one patient and
                            # three visits, so the two answer different questions —
                            # what a visit costs to serve, versus what a person
                            # costs to care for over the period.
                            'cost_per_patient': dispensed_value / len(patients) if patients else 0.0,
                            'visits_per_patient': (f'{len(visits) / len(patients):.1f}'
                                                   if patients else '-'),
                            'cost_per_item': dispensed_value / items_dispensed if items_dispensed else 0.0,
                            'estimated_lines': estimated_lines,
                            'top_items': [
                                {'med_name': k, 'units': v['units'], 'value': v['value'],
                                 'share': round(v['value'] / dispensed_value * 100, 1)
                                 if dispensed_value else 0}
                                for k, v in top],
                        }
                        report_title = f'Period Report — {label}'

                    elif report_type == 'order_request':
                        # NEW (Order Request): turns "amount to order" into an
                        # actual requisition. Two things make it usable rather
                        # than merely arithmetic:
                        #   1. quantities round UP to whole packs, because that
                        #      is how the supplier sells — asking for 137
                        #      tablets when they come in boxes of 100 is not an
                        #      order anyone can fill;
                        #   2. items with no recorded consumption are left out.
                        #      Without an AMC there is no basis for a quantity,
                        #      and inventing one is worse than leaving the call
                        #      to the pharmacist.
                        try:
                            cover_months = int(request.form.get('cover_months', 3))
                        except (TypeError, ValueError):
                            cover_months = 3
                        if cover_months not in (2, 3, 4, 6):
                            cover_months = 3

                        # AMC over the last 90 days, matching the basis the
                        # inventory report and dashboard already use.
                        window_end   = datetime.utcnow()
                        window_start = window_end - timedelta(days=90)
                        req_filter = ({'name': {'$regex': search or '', '$options': 'i'}}
                                      if search else {})
                        req_meds  = _by_name(medications.find(_active({**req_filter, **cat_q}), {'_id': 0}))
                        movement  = _movement_by_med(
                            transactions, {'$gte': window_start, '$lte': window_end},
                            [m['name'] for m in req_meds] if search else None)
                        # Ordering is precisely where demand matters more than
                        # consumption: an item that ran out dispensed less than
                        # was asked for, and ordering to that lower figure is how
                        # a stockout becomes permanent.
                        demand    = _demand_by_med(
                            transactions, {'$gte': window_start, '$lte': window_end},
                            [m['name'] for m in req_meds] if search else None)

                        by_supplier: dict[str, Docs] = {}
                        unpriced, unsized = 0, 0
                        for med in req_meds:
                            name    = med.get('name', '')
                            mvd      = movement.get(name, _ZERO_MOVEMENT)
                            consumed = mvd['dispensed'] + mvd['issued']
                            unmet    = demand.get(name, _ZERO_DEMAND)['shortfall']
                            plain_amc = consumed / 3.0         # 90 days -> per month
                            amc      = (consumed + unmet) / 3.0
                            if amc <= 0:
                                continue
                            balance = med.get('balance', 0) or 0
                            mos     = balance / amc
                            if mos >= cover_months:
                                continue
                            shortfall = amc * cover_months - balance
                            # An item whose pack size has never been recorded is
                            # NOT the same as one that genuinely comes in singles.
                            # Treating them alike produced a plausible-looking
                            # request ("order 300 packs") that a supplier could
                            # not fill, with nothing on the page to show it was a
                            # guess. Quantities still fall back to singles, but
                            # the row says so.
                            pack_known = med.get('pack_size') is not None
                            pack_size  = med.get('pack_size') or 1
                            packs     = int(-(-shortfall // pack_size))   # ceiling
                            units     = packs * pack_size
                            pack_price = med.get('pack_price')
                            if pack_price is None:
                                unpriced += 1
                            if not pack_known:
                                unsized += 1
                            line_cost = packs * (pack_price or 0)
                            supplier  = med.get('supplier') or 'Supplier not recorded'
                            by_supplier.setdefault(supplier, []).append({
                                'med_name': name, 'amc': round(amc, 1),
                                'plain_amc': round(plain_amc, 1),
                                # expressed per month, like the AMC figures it
                                # sits between — a 90-day total in that row would
                                # have read as a monthly rate
                                'unmet': round(unmet / 3.0, 1), 'unmet_units': unmet,
                                'balance': balance,
                                'mos_label': 'Out of stock' if balance == 0 else f'{mos:.1f}',
                                'shortfall': int(round(shortfall)),
                                'pack_size': pack_size, 'pack_known': pack_known,
                                'packs': packs, 'units': units,
                                'pack_price': pack_price, 'line_cost': line_cost,
                                'status': ('expired' if balance == 0 or mos < 1
                                           else ('close-to-expire' if mos < 2 else 'normal')),
                            })

                        groups = [{'supplier': sup,
                                   'lines': sorted(lines, key=lambda l: _name_key(l['med_name'])),
                                   'total': sum(l['line_cost'] for l in lines)}
                                  for sup, lines in sorted(by_supplier.items(),
                                                           key=lambda kv: kv[0].lower())]
                        order_request = {
                            'reference': f"REQ-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}",
                            'raised_on': datetime.utcnow().strftime('%Y-%m-%d'),
                            'raised_by': session['user']['name'],
                            'cover_months': cover_months,
                            'groups': groups,
                            'line_count': sum(len(g['lines']) for g in groups),
                            'total_packs': sum(l['packs'] for g in groups for l in g['lines']),
                            'grand_total': sum(g['total'] for g in groups),
                            'unpriced': unpriced,
                            'unsized': unsized,
                            'demand_adjusted': sum(1 for g in by_supplier.values()
                                                   for l in g if l['unmet_units']),
                        }
                        report_title = 'Order Request'

                    elif report_type == 'controlled_drug_register':
                        if not start_dt or not end_dt:
                            raise ValueError('Start and end dates are required for this report type.')
                        # FIX (Performance): fetch the controlled medications once
                        # and index them by name, instead of a find_one per drug
                        # inside the loop below. This was the last query loop in
                        # the app; the arithmetic is untouched.
                        controlled_docs = {m['name']: m for m in
                                           medications.find({'schedule': 'controlled'}, {'_id': 0})}
                        controlled_meds = list(controlled_docs)
                        if controlled_meds:
                            # NEW (Stock Take): adjustments are included so the
                            # register's running balance still lands on the
                            # current balance after a physical count.
                            all_tx = list(transactions.find({
                                'med_name': {'$in': controlled_meds},
                                'type': {'$in': ['receive', 'dispense', 'adjustment',
                                                 'opening_balance', 'expiry_removal',
                                                 'internal_issue', 'damage']},
                                'timestamp': {'$gte': start_dt, '$lte': end_dt}
                            }).sort('timestamp', 1).limit(10000))

                            tx_by_med = defaultdict(list)
                            for tx in all_tx:
                                tx_by_med[tx['med_name']].append(tx)

                            for med_name in sorted(controlled_meds, key=_name_key):
                                found = controlled_docs.get(med_name)
                                if not found:
                                    continue
                                med = found
                                try:
                                    current_balance    = med.get('balance', 0)
                                    med_txs            = tx_by_med[med_name]
                                    # Every movement, by its own direction —
                                    # issues to a department and write-offs
                                    # included. Leaving those out is what let
                                    # the register disagree with the shelf.
                                    deltas             = [_controlled_delta(t) for t in med_txs]
                                    received_in_period = sum(d for d in deltas if d > 0)
                                    issued_in_period   = sum(-d for d in deltas if d < 0)
                                    # Worked backwards from the shelf, so the
                                    # register and the balance cannot disagree.
                                    beginning_balance  = current_balance - sum(deltas)
                                    running_bal        = beginning_balance
                                    running_entries    = []
                                    for tx, delta in zip(med_txs, deltas):
                                        running_bal += delta
                                        tx_copy = tx.copy()
                                        tx_copy['balance_after'] = running_bal
                                        tx_copy['movement'] = CONTROLLED_MOVEMENT_LABEL.get(
                                            tx.get('type'), tx.get('type', ''))
                                        # Shown in an In or an Out column, so
                                        # the figure itself never needs a
                                        # minus sign to be understood.
                                        tx_copy['in_qty'] = delta if delta > 0 else None
                                        tx_copy['out_qty'] = -delta if delta < 0 else None
                                        running_entries.append(tx_copy)
                                    controlled_register.append({
                                        'med_name': med_name,
                                        'beginning_balance': beginning_balance,
                                        'ending_balance': current_balance,
                                        'received': received_in_period,
                                        'dispensed': issued_in_period,
                                        # The opening balance is worked
                                        # backwards from the shelf, so the
                                        # column always adds up — what it
                                        # cannot do is start below zero. That
                                        # means more left the cupboard than
                                        # could ever have been in it, which
                                        # is a movement recorded outside this
                                        # period or a balance edited directly.
                                        'reconciles': beginning_balance >= 0,
                                        'transactions': [e for e in running_entries if matches_search(e, search)]
                                    })
                                except Exception as qe:
                                    app.logger.error(f"Query failed for controlled med {med_name}: {qe}")
                                    continue

                except ValueError as e:
                    message      = f'Invalid input: {str(e)}'
                    report_type  = None
                    start_date   = end_date = search = None
                    report_data  = receive_list = stock_data = controlled_register = []
                    report_title = None
                    order_request = None
                    monthly = None
                    writeoffs = []
                    writeoff_totals = {'item_count': 0, 'units': 0, 'value': 0.0, 'by_med': []}
                    unmet_list = []
                    duplicates = []
                    unmet_totals = {'item_count': 0, 'shortfall': 0, 'value': 0.0, 'fill_rate': None, 'by_med': []}
                    issues = []
                    issue_totals = {'lines': 0, 'units': 0, 'value': 0.0, 'dept_count': 0, 'by_dept': [], 'by_med': []}
                    sick_leave = []
                    sick_leave_totals = {'patients': 0, 'days': 0, 'average': '-', 'longest': 0, 'repeat': []}
            else:
                message = 'Please select a report type.'

        # The download is the report on screen, built from exactly the same
        # data — never a second query that could disagree with it.
        if request.form.get('export') == 'csv':
            made = _report_csv(report_type, {
                'report_data': report_data, 'stock_data': stock_data,
                'receive_list': receive_list, 'order_request': order_request,
                'controlled_register': controlled_register, 'monthly': monthly,
                'writeoffs': writeoffs, 'unmet_list': unmet_list,
                'issues': issues, 'sick_leave': sick_leave,
                'duplicates': duplicates,
            }, is_admin=is_admin)
            if made:
                filename, body = made
                resp = make_response(body)
                resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
                resp.headers['Content-Disposition'] = \
                    f'attachment; filename="{filename}"'
                return resp
            flash('That report has no table to download.')

        return render_template(
            'reports.html',
            report_type=report_type, report_data=report_data,
            receive_list=receive_list, stock_data=stock_data,
            controlled_register=controlled_register, order_request=order_request,
            category=cat, category_label=cat_label,
            monthly=monthly, writeoffs=writeoffs, writeoff_totals=writeoff_totals,
            unmet_list=unmet_list, unmet_totals=unmet_totals,
            duplicates=duplicates,
            issues=issues, issue_totals=issue_totals,
            sick_leave=sick_leave, sick_leave_totals=sick_leave_totals,
            start_date=start_date, end_date=end_date,
            default_start=local_now().replace(day=1).strftime('%Y-%m-%d'),
            default_end=local_now().strftime('%Y-%m-%d'),
            inventory_start=_months_back(local_now().date(), INVENTORY_DEFAULT_MONTHS).strftime('%Y-%m-%d'),
            total_transactions=total_transactions,
            nav_links=get_nav_links(), message=message,
            message_class=message_class, current_user=session['user']['name'],
            search=search, report_title=report_title, is_admin=is_admin
        )
    except ServerSelectionTimeoutError:
        return render_template(
            'reports.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            report_type=None, report_data=[], receive_list=[], stock_data=[],
            message_class='error',
            current_user=session.get('user', {}).get('name', ''),
            default_start=local_now().replace(day=1).strftime('%Y-%m-%d'),
            default_end=local_now().strftime('%Y-%m-%d'),
            inventory_start=_months_back(local_now().date(), INVENTORY_DEFAULT_MONTHS).strftime('%Y-%m-%d'),
            controlled_register=[], order_request=None, monthly=None,
            category='pharmaceutical', category_label=CATEGORY_LABELS['pharmaceutical'],
            writeoffs=[], writeoff_totals={'item_count': 0, 'units': 0, 'value': 0.0, 'by_med': []},
            unmet_list=[], unmet_totals={'item_count': 0, 'shortfall': 0, 'value': 0.0, 'fill_rate': None, 'by_med': []},
            issues=[], issue_totals={'lines': 0, 'units': 0, 'value': 0.0, 'dept_count': 0, 'by_dept': [], 'by_med': []},
            sick_leave=[], sick_leave_totals={'patients': 0, 'days': 0, 'average': '-', 'longest': 0, 'repeat': []},
            start_date=None, end_date=None,
            total_transactions=0, search=None, report_title=None,
            duplicates=[],
            is_admin=is_admin
        ), 500


# NEW: admin-only audit trail. Gives admins visibility into every edit/delete
# (from audit_logger.py's audit_log collection) and every unhandled exception
# (from error_logger.py's error_logs collection) across the whole app.
# -----------------------------------------------------------------------
# NEW (Dashboard): consumption trend and stock-health overview.
#
# PERFORMANCE NOTE: the reports() route queries transactions once per
# medication in a Python loop, which is fine for one report but would be
# ruinous here — this page needs every medication across several months at
# once. Instead the whole window is fetched in ONE aggregation grouped by
# medication and month, and everything else is computed in memory. Two DB
# round trips total (three for admins), regardless of how many medications
# exist.
# -----------------------------------------------------------------------
DASHBOARD_MONTH_CHOICES = [3, 6, 12]
MONTH_ABBR = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _month_sequence(n: int) -> list[tuple[int, int]]:
    """The last n calendar months as (year, month), oldest first."""
    now = datetime.utcnow()
    y, m = now.year, now.month
    seq = []
    for _ in range(n):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    seq.reverse()
    return seq


def _monthly_movement(transactions: Collection, window_start: datetime) -> dict[str, dict[tuple[int, int], Doc]]:
    """med_name -> (year, month) -> {'dispensed','received','adjusted'}."""
    movement: defaultdict[str, dict[tuple[int, int], Doc]] = defaultdict(dict)
    pipeline = [
        {'$match': {'timestamp': {'$gte': window_start},
                    'type': {'$in': ['dispense', 'receive', 'adjustment', 'opening_balance', 'expiry_removal',
                       'internal_issue', 'damage']}}},
        {'$group': {
            '_id': {'med': '$med_name',
                    'y': {'$year': '$timestamp'},
                    'm': {'$month': '$timestamp'}},
            'dispensed': {'$sum': {'$cond': [{'$eq': ['$type', 'dispense']}, '$quantity', 0]}},
            'received':  {'$sum': {'$cond': [{'$in': ['$type', ['receive', 'opening_balance']]},
                                             '$quantity', 0]}},
            'adjusted':  {'$sum': {'$cond': [{'$eq': ['$type', 'adjustment']}, '$quantity', 0]}},
            'expired':   {'$sum': {'$cond': [{'$eq': ['$type', 'expiry_removal']}, '$quantity', 0]}},
            'issued':    {'$sum': {'$cond': [{'$eq': ['$type', 'internal_issue']}, '$quantity', 0]}},
            'damaged':   {'$sum': {'$cond': [{'$eq': ['$type', 'damage']}, '$quantity', 0]}},
        }}
    ]
    try:
        for row in transactions.aggregate(pipeline):
            k = row['_id']
            movement[k['med']][(k['y'], k['m'])] = {
                'dispensed': row.get('dispensed', 0) or 0,
                'received':  row.get('received', 0) or 0,
                'adjusted':  row.get('adjusted', 0) or 0,
                'expired':   row.get('expired', 0) or 0,
                'issued':    row.get('issued', 0) or 0,
                'damaged':   row.get('damaged', 0) or 0,
            }
        return movement
    except Exception as e:
        # Fall back to a client-side pass rather than failing the page — the
        # $year/$month grouping is standard, but this keeps the dashboard
        # working on any backend that doesn't support it.
        app.logger.warning(f"Dashboard aggregation unavailable, falling back: {e}")
        movement = defaultdict(dict)
        cursor = transactions.find(
            {'timestamp': {'$gte': window_start},
             'type': {'$in': ['dispense', 'receive', 'adjustment', 'opening_balance', 'expiry_removal',
                       'internal_issue', 'damage']}},
            {'_id': 0, 'med_name': 1, 'type': 1, 'quantity': 1, 'timestamp': 1}
        )
        for tx in cursor:
            ts = tx.get('timestamp')
            if not isinstance(ts, datetime):
                continue
            bucket = movement[tx.get('med_name')].setdefault(
                (ts.year, ts.month), dict(_ZERO_MOVEMENT))
            qty = tx.get('quantity', 0) or 0
            k = tx.get('type')
            if k == 'dispense':
                bucket['dispensed'] += qty
            elif k in ('receive', 'opening_balance'):
                bucket['received'] += qty
            elif k == 'expiry_removal':
                bucket['expired'] += qty
            elif k == 'internal_issue':
                bucket['issued'] += qty
            elif k == 'damage':
                bucket['damaged'] += qty
            else:
                bucket['adjusted'] += qty
        return movement


# -----------------------------------------------------------------------
# FIX (Performance): movement totals for a whole set of medications in ONE
# aggregation, replacing the per-medication query loop that reports() used to
# run.
#
# The old pattern issued two round trips per medication (a find plus an
# aggregate), so an inventory report over a 260-item formulary meant roughly
# 520 sequential round trips to Atlas — slow enough to risk a serverless
# timeout, and unnecessary, since a single $group by med_name returns exactly
# the same numbers.
#
# It was also a correctness problem: each of those queries sat in its own
# try/except that fell back to zeros, so one flaky query produced a
# plausible-looking row with fabricated figures in it. With a single query a
# failure is visible instead of silent.
# -----------------------------------------------------------------------
_ZERO_MOVEMENT = {'dispensed': 0, 'received': 0, 'adjusted': 0, 'expired': 0,
                  'issued': 0, 'damaged': 0}


def _movement_by_med(transactions: Collection, time_filter: Query,
                     med_names: list[str] | None = None) -> dict[str, Doc]:
    """med_name -> {'dispensed', 'received', 'adjusted'} over a time window.

    'adjusted' is the signed net of stock-take corrections; callers must
    unwind it when back-calculating a balance, or a physical count will
    silently distort every period that contains it.
    """
    # NEW (Opening balances): an opening load is stock arriving, so every
    # back-calculated beginning balance has to account for it exactly as it
    # does a receipt. Omitting it would make every period containing a go-live
    # load report a beginning balance that is too high.
    match = {'timestamp': time_filter,
             'type': {'$in': ['dispense', 'receive', 'adjustment', 'opening_balance', 'expiry_removal',
                       'internal_issue', 'damage']}}
    if med_names is not None:
        match['med_name'] = {'$in': med_names}
    pipeline: list[Doc] = [
        {'$match': match},
        {'$group': {
            '_id': '$med_name',
            'dispensed': {'$sum': {'$cond': [{'$eq': ['$type', 'dispense']}, '$quantity', 0]}},
            'received':  {'$sum': {'$cond': [{'$in': ['$type', ['receive', 'opening_balance']]},
                                             '$quantity', 0]}},
            'adjusted':  {'$sum': {'$cond': [{'$eq': ['$type', 'adjustment']}, '$quantity', 0]}},
            'expired':   {'$sum': {'$cond': [{'$eq': ['$type', 'expiry_removal']}, '$quantity', 0]}},
            'issued':    {'$sum': {'$cond': [{'$eq': ['$type', 'internal_issue']}, '$quantity', 0]}},
            'damaged':   {'$sum': {'$cond': [{'$eq': ['$type', 'damage']}, '$quantity', 0]}},
        }}
    ]
    try:
        return {r['_id']: {'dispensed': r.get('dispensed', 0) or 0,
                           'received':  r.get('received', 0) or 0,
                           'adjusted':  r.get('adjusted', 0) or 0,
                           'expired':   r.get('expired', 0) or 0,
                           'issued':    r.get('issued', 0) or 0,
                           'damaged':   r.get('damaged', 0) or 0}
                for r in transactions.aggregate(pipeline)}
    except Exception as e:
        app.logger.warning(f"Movement aggregation unavailable, falling back: {e}")
        out: dict[str, Doc] = {}
        for tx in transactions.find(match, {'_id': 0, 'med_name': 1, 'type': 1, 'quantity': 1}):
            b = out.setdefault(tx.get('med_name'), dict(_ZERO_MOVEMENT))
            qty = tx.get('quantity', 0) or 0
            k = tx.get('type')
            if k == 'dispense':
                b['dispensed'] += qty
            elif k in ('receive', 'opening_balance'):
                b['received'] += qty
            elif k == 'expiry_removal':
                b['expired'] += qty
            elif k == 'internal_issue':
                b['issued'] += qty
            elif k == 'damage':
                b['damaged'] += qty
            else:
                b['adjusted'] += qty
        return out


def _activity_by_month(transactions: Collection, window_start: datetime) -> dict[tuple[int, int], Doc]:
    """(year, month) -> counts of dispensing/receiving ACTIVITY, not units.

    Summing `quantity` across different medications adds tablets to vials to
    millilitres — arithmetically valid, semantically empty. These are the
    countable events instead: how many visits, how many prescription items,
    how many patients, how many deliveries.

    A dispense writes one document per medication line, all sharing a
    transaction_id for the visit, so lines and visits are different numbers
    and both are worth knowing.
    """
    buckets: defaultdict[tuple[int, int], Doc] = defaultdict(
        lambda: {'visits': set(), 'lines': 0, 'patients': set(),
                                   'receipts': 0, 'deliveries': set(), 'adjusted_lines': 0})
    pipeline = [
        {'$match': {'timestamp': {'$gte': window_start},
                    'type': {'$in': ['dispense', 'receive', 'adjustment']}}},
        {'$group': {
            '_id': {'y': {'$year': '$timestamp'}, 'm': {'$month': '$timestamp'}, 't': '$type'},
            'lines': {'$sum': 1},
            'visits': {'$addToSet': '$transaction_id'},
            'patients': {'$addToSet': '$patient'},
            'orders': {'$addToSet': '$order_number'},
        }}
    ]

    def _clean(vals):
        return {v for v in (vals or []) if v not in (None, '', 'N/A')}

    try:
        rows = list(transactions.aggregate(pipeline))
        for r in rows:
            k = r['_id']
            b = buckets[(k['y'], k['m'])]
            if k['t'] == 'dispense':
                b['lines'] += r.get('lines', 0)
                b['visits'] |= _clean(r.get('visits'))
                b['patients'] |= _clean(r.get('patients'))
            elif k['t'] == 'receive':
                b['receipts'] += r.get('lines', 0)
                b['deliveries'] |= _clean(r.get('orders'))
            else:
                b['adjusted_lines'] += r.get('lines', 0)
        return buckets
    except Exception as e:
        app.logger.warning(f"Activity aggregation unavailable, falling back: {e}")
        cursor = transactions.find(
            {'timestamp': {'$gte': window_start},
             'type': {'$in': ['dispense', 'receive', 'adjustment']}},
            {'_id': 0, 'type': 1, 'timestamp': 1, 'transaction_id': 1,
             'patient': 1, 'order_number': 1})
        for tx in cursor:
            ts = tx.get('timestamp')
            if not isinstance(ts, datetime):
                continue
            b = buckets[(ts.year, ts.month)]
            if tx.get('type') == 'dispense':
                b['lines'] += 1
                b['visits'] |= _clean([tx.get('transaction_id')])
                b['patients'] |= _clean([tx.get('patient')])
            elif tx.get('type') == 'receive':
                b['receipts'] += 1
                b['deliveries'] |= _clean([tx.get('order_number')])
            else:
                b['adjusted_lines'] += 1
        return buckets


# -----------------------------------------------------------------------
# FIX (Performance/Scalability): /dispense and /receive used to fetch the
# ENTIRE transaction history with no limit and render every row into one HTML
# page. Measured at 6,000 transactions that produced a 6.5 MB page; at a few
# thousand dispensing lines a month it reaches tens of megabytes within a
# year, on the page dispensers open every single time they dispense.
#
# Two changes fix it:
#   1. A default date window, so an unfiltered visit shows the current month
#      rather than all history. A search overrides it — searching should look
#      across everything, not just this month, or the box is useless for
#      finding an old record.
#   2. Real pagination, so even a wide window can't produce an unbounded page.
# -----------------------------------------------------------------------
DISPENSE_PAGE_SIZE = 25    # visits per page (a visit may hold several lines)
RECEIVE_PAGE_SIZE  = 50    # receipt lines per page


def _requested_page() -> int:
    try:
        return max(1, int(request.values.get('page', 1)))
    except (TypeError, ValueError):
        return 1


INVENTORY_DEFAULT_MONTHS = 3


def _months_back(d: date, months: int) -> date:
    """The same day of the month, `months` earlier.

    Clamped to the month's last day, so 31 May less three months is
    28 or 29 February rather than an error.
    """
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def _default_window(start_date: str | None, end_date: str | None, search: str | None) -> tuple[str | None, str | None, bool]:
    """Fall back to the current month when nothing else narrows the list.

    Returns (start_date, end_date, defaulted). A search is deliberately NOT
    date-limited: someone looking up a patient or an invoice from March should
    find it without first working out which month to ask for.
    """
    if start_date or end_date or search:
        return start_date, end_date, False
    today = datetime.utcnow().date()
    return today.replace(day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d'), True


def _pager(page: int, page_size: int, total: int) -> Doc:
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    return {'page': page, 'pages': pages, 'total': total, 'page_size': page_size,
            'has_prev': page > 1, 'has_next': page < pages,
            'first': 0 if total == 0 else (page - 1) * page_size + 1,
            'last': min(page * page_size, total)}



# ---------------------------------------------------------------------------
# CSV download.
#
# Every report was print-only, so anything that had to leave the pharmacy —
# the requisition above all — was read off the screen and typed again
# somewhere else. The figures are already computed; handing them over as a
# file costs nothing and removes a whole re-keying job.
#
# Each report names its columns explicitly rather than dumping dictionary
# keys, so the file reads the way the printed report does and does not change
# shape whenever an internal field is added.
# ---------------------------------------------------------------------------
# The inventory table shows the same columns to everyone: Issued is one
# column with every outward movement folded into it, so there is nothing an
# admin sees that a non-admin's row would fail to add up without. The
# download follows the screen exactly.
INVENTORY_COLUMNS = [
    ('med_name', 'Item'), ('category', 'Category'),
    ('beginning_balance', 'Beginning Balance'), ('received', 'Received'),
    ('issued_total', 'Issued'), ('current_balance', 'Current Balance'),
    ('adjusted_amc', 'Adjusted AMC'), ('amount_to_order', 'Amount to Order')]
INVENTORY_COLUMNS_ADMIN = INVENTORY_COLUMNS
INVENTORY_COLUMNS_USER = INVENTORY_COLUMNS

CSV_REPORTS = {
    'inventory': ('Inventory Report', 'report_data', INVENTORY_COLUMNS_USER),
    'stock_on_hand': ('Stock on Hand', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch'), ('price', 'Unit Price')]),
    'expired_list': ('Expired Stock', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch'), ('price', 'Unit Price')]),
    'near_expired_list': ('Expiring Soon', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch'), ('price', 'Unit Price')]),
    'out_of_stock_list': ('Out of Stock', 'stock_data', [
        ('name', 'Medication'), ('balance', 'Balance'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch')]),
    'expiry_writeoffs': ('Losses', 'writeoffs', [
        ('date', 'Date'), ('med_name', 'Item Description'), ('quantity', 'Qty'),
        ('expiry_date', 'Expiry Date'), ('batch', 'Batch/Lot'),
        ('line_value', 'Total Cost'), ('remarks', 'Remarks')]),
    'duplicate_visits': ('Possible Double-Dispensing', 'duplicates', [
        ('patient', 'Patient'), ('med_name', 'Medication'),
        ('quantity', 'Quantity'), ('first', 'First recorded'),
        ('second', 'Recorded again'), ('minutes', 'Minutes apart'),
        ('units', 'Units counted twice'), ('value', 'Value counted twice'),
        ('dispenser', 'Dispenser')]),
    'unmet_demand': ('Unmet Demand', 'unmet_list', [
        ('date', 'Date'), ('med_name', 'Medication'),
        ('quantity_requested', 'Requested'), ('patient', 'Patient')]),
    'receive_list': ('Receipts', 'receive_list', [
        ('date', 'Date'), ('med_name', 'Medication'), ('quantity', 'Qty'),
        ('batch', 'Batch'), ('expiry_date', 'Expiry'), ('supplier', 'Supplier'),
        ('invoice_number', 'Invoice'), ('line_value', 'Value')]),
    'controlled_drug_register': ('Controlled Drug Register', 'controlled_register', [
        ('date', 'Date'), ('med_name', 'Medication'), ('movement', 'Movement'),
        ('in_qty', 'In'), ('out_qty', 'Out'), ('balance_after', 'Balance'),
        ('patient', 'Patient'), ('user', 'By')]),
}

ORDER_REQUEST_COLUMNS = [
    ('supplier', 'Supplier'), ('med_name', 'Medication'), ('amc', 'AMC'),
    ('balance', 'Balance on Hand'), ('mos_label', 'Months of Stock'),
    ('shortfall', 'Shortfall (units)'), ('pack_size', 'Pack Size'),
    ('packs', 'Packs to Order'), ('units', 'Units Received'),
    ('pack_price', 'Price per Pack'), ('line_cost', 'Line Cost'),
]


def _csv_value(v: Any) -> str:
    """Flatten anything a report row might hold into one cell."""
    if v is None:
        return ''
    if isinstance(v, (list, tuple)):
        return '; '.join(str(x) for x in v)
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    return str(v)


def _report_csv(report_type: str | None, ctx: Doc, is_admin: bool = False) -> tuple[str, str] | None:
    """(filename, csv text) for the report on screen, or None if it has none."""
    if report_type == 'controlled_drug_register':
        # The register is grouped by medication, each group holding its own
        # movements. Handing those groups straight to the writer produced one
        # blank line per medication and not a single movement — the file had
        # the right headings over nothing at all.
        rows = []
        for group in ctx.get('controlled_register') or []:
            for tx in group.get('transactions') or []:
                row = dict(tx)
                row['med_name'] = group.get('med_name', '')
                ts = tx.get('timestamp')
                if not row.get('date') and ts:
                    row['date'] = ts.strftime('%Y-%m-%d')
                rows.append(row)
        columns = CSV_REPORTS['controlled_drug_register'][2]
        title = 'Controlled Drug Register'
    elif report_type == 'inventory':
        columns = INVENTORY_COLUMNS_ADMIN if is_admin else INVENTORY_COLUMNS_USER
        rows = ctx.get('report_data') or []
        title = 'Inventory Report'
    elif report_type == 'order_request':
        order = ctx.get('order_request') or {}
        rows = []
        for group in order.get('groups') or []:
            for line in group.get('lines') or []:
                row = dict(line)
                row['supplier'] = group.get('supplier', '')
                rows.append(row)
        columns = ORDER_REQUEST_COLUMNS
        title = 'Order Request'
    else:
        spec = CSV_REPORTS.get(report_type or '')
        if not spec:
            return None
        title, key, columns = spec
        rows = ctx.get(key) or []

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _f, label in columns])
    for row in rows:
        writer.writerow([_csv_value(row.get(f)) for f, _lab in columns])
    stamp = local_now().strftime('%Y%m%d')
    name = title.lower().replace(' ', '-')
    return f'{name}-{stamp}.csv', out.getvalue()












def _clinical_by_month(transactions: Collection, window_start: datetime,
                       months: list[tuple[int, int]]) -> Doc:
    """Sales (episodes) and distinct named customers in the window.

    Counted per SALE, not per line: a sale of three items is one sale. The
    dispense records are one document per line, so counting rows would
    multiply the figure by the basket size.
    """
    idx = {ym: i for i, ym in enumerate(months)}
    seen, patients = set(), set()
    cursor = transactions.find(
        {'type': 'dispense', 'timestamp': {'$gte': window_start}},
        {'_id': 0, 'timestamp': 1, 'transaction_id': 1, 'patient': 1})
    for t in cursor:
        ts = t.get('timestamp')
        if not isinstance(ts, datetime) or idx.get((ts.year, ts.month)) is None:
            continue
        tid = t.get('transaction_id')
        if tid in seen:
            continue
        seen.add(tid)
        if t.get('patient'):
            patients.add(t['patient'])
    return {'episodes': len(seen), 'patients': patients}


def _parse_expiry(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).split('T')[0], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5





TEMP_LOCATIONS = ['Dispensary', 'Store 1', 'Store 2', 'Fridge']
TEMP_SESSIONS = ['Morning', 'Afternoon']
# Each room keeps its own instrument permanently, so the number is a property
# of the location and never has to be typed. If an instrument is ever replaced
# by one with a different number, change it here rather than on every reading.
TEMP_THERMOMETERS = {'Dispensary': '1', 'Store 1': '2',
                     'Store 2': '3', 'Fridge': '4'}
# Before noon is the morning round, noon onwards is the afternoon round.
TEMP_NOON_HOUR = 12

# Acceptable storage range per location, and the "cold spot"/"hot spot" warning
# bands the official chart names in its legend box.
TEMP_LIMITS = {
    'Dispensary': (15.0, 25.0),
    'Store 1': (15.0, 25.0),
    'Store 2': (15.0, 25.0),
    'Fridge': (2.0, 8.0),
}
TEMP_SPOTS = {
    'Dispensary': ((15.0, 17.5), (23.0, 25.0)),
    'Store 1': ((15.0, 17.5), (23.0, 25.0)),
    'Store 2': ((15.0, 17.5), (23.0, 25.0)),
    'Fridge': ((2.0, 3.0), (7.0, 8.0)),
}
TEMP_MIN_PLAUSIBLE, TEMP_MAX_PLAUSIBLE = -50.0, 80.0

# The temperature axis of SR-TC-001 is a stack of 14 cells with only five of
# them labelled: +30, +26, +22, +18, +15. Note the spacing is NOT uniform —
# the first three gaps span 4 degrees over three rows, the last spans 3. That
# is deliberate on the form: +15 is the lower acceptable limit, so it was put
# on a row line rather than left to fall between two. Positions are therefore
# interpolated BETWEEN the printed labels, which puts a reading exactly where a
# person reading the same labels would place it by hand.
#
# The store-room scale is the official one. The fridge scale is the same grid
# re-labelled for 2-8 degree storage, since the reference chart covers store
# rooms only.
# One row is one whole degree, on every chart.
#
# The scale used to copy the five labels printed on the paper form - +30,
# +26, +22, +18, +15 - three rows apart. But 30 to 26 is four degrees and 18
# to 15 is three, so the rows meant 1 1/3 degrees at the top of the chart and
# 1 degree at the bottom: the spacing did not match the numbering, and the
# lower half of every room chart was stretched. The fridge was 2/3 of a degree
# a row, so no grid line fell on a whole degree. Now the numbers and the lines
# agree everywhere, every printed label is still there, and the ones between
# them are filled in.
#
# 'top' is the warmest row and 'bottom' the coldest; a reading beyond either
# end is drawn on the end row, never dropped.
TEMP_SCALES = {
    'room': {'top': 30, 'bottom': 14},
    'fridge': {'top': 10, 'bottom': 1},
}
# The grid is the same height whatever the scale, so the chart keeps its
# shape on paper; a scale with more degrees gets thinner rows.
TEMP_GRID_H = 14 * 26

# The chart's own convention, printed in its legend: actual in black pen,
# minimum in blue, maximum in red. Each series also carries its own marker
# shape — these charts are photocopied, and once the colour is gone red and
# blue are the same grey. Shape survives that; colour does not.
TEMP_ROWS = TEMP_SCALES['room']['top'] - TEMP_SCALES['room']['bottom'] + 1

TEMP_SERIES = [
    ('max', 'Maximum', '#cc0000', 'circle', 'Red'),
    ('min', 'Minimum', '#0047b3', 'square', 'Blue'),
    ('actual', 'Actual', '#000000', 'triangle', 'Black'),
]
# The legend box is worded and ordered as the printed form has it.
TEMP_LEGEND_ORDER = ['actual', 'min', 'max']
# What each pen actually records at this pharmacy: one check in the morning and
# one in the afternoon, with the day's average plotted between them. The form
# only has three pens, so they are used this way.
TEMP_PEN_MEANING = {'min': '= morning check',
                    'max': '= afternoon check',
                    'actual': '= average'}


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime, int]:
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end, (end - start).days


def _temp_scale(location: str) -> dict[str, Any]:
    """The rows of a chart: how many, what each is labelled, and where."""
    s = TEMP_SCALES['fridge' if location == 'Fridge' else 'room']
    top, bottom = s['top'], s['bottom']
    rows = top - bottom + 1
    return {
        'rows': rows,
        # Two anchors make it a straight line: row 0 is the top degree and
        # each row below is one degree colder.
        'anchors': [(0, float(top)), (rows - 1, float(bottom))],
        'labelled': {r: f'+{top - r}°C' for r in range(rows)},
    }


def _temp_row(value: float, anchors: Sequence[tuple[int, float]]) -> float:
    """Row position (fractional, 0 at the top) for a temperature.

    Interpolates between the printed labels, so a value lands where a person
    reading the same labels would put it. Out-of-scale values clamp to the end
    rows rather than being dropped — a reading off the top of the chart is
    exactly the one that must not go missing.
    """
    if value >= anchors[0][1]:
        return 0.0
    if value <= anchors[-1][1]:
        return float(anchors[-1][0])
    for (r1, t1), (r2, t2) in zip(anchors, anchors[1:]):
        if t2 <= value <= t1:
            if t1 == t2:
                return float(r1)
            return r1 + (t1 - value) / (t1 - t2) * (r2 - r1)
    return float(anchors[-1][0])


def _temp_marker(shape: str, x: float, y: float, colour: str) -> str:
    if shape == 'circle':
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{colour}" '
                f'stroke="#ffffff" stroke-width="1"/>')
    if shape == 'square':
        return (f'<rect x="{x - 2.9:.1f}" y="{y - 2.9:.1f}" width="5.8" height="5.8" '
                f'fill="{colour}" stroke="#ffffff" stroke-width="1"/>')
    return (f'<polygon points="{x:.1f},{y - 3.8:.1f} {x + 3.4:.1f},{y + 2.5:.1f} '
            f'{x - 3.4:.1f},{y + 2.5:.1f}" fill="{colour}" '
            f'stroke="#ffffff" stroke-width="1"/>')


def _reading_temp(r: Doc | None) -> float | None:
    """The single temperature on a reading, or None.

    One figure is recorded per check. Readings saved under the earlier
    three-figure shape (max / min / actual per session) fall back to their
    'actual' value, which was the reading on the dial at check time and so
    means the same thing — otherwise anything already entered would vanish
    from the chart the moment this changed.
    """
    if not r:
        return None
    v = r.get('temp')
    if v is None:
        v = r.get('actual')
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _temperature_chart(readings: Iterable[Doc], year: int, month: int, location: str,
                       thermometer: str = '') -> str:
    """SR-TC-001 reproduced as SVG, already filled in from the recorded data.

    Server-rendered with no JavaScript, so the printed sheet is exactly what
    the screen shows. Everything interpolated into the output is either a
    number, a fixed constant, or escaped.
    """
    start, _, days_in_month = _month_bounds(year, month)
    lo_lim, hi_lim = TEMP_LIMITS.get(location, (15.0, 25.0))
    cold, hot = TEMP_SPOTS.get(location, ((15.0, 17.5), (23.0, 25.0)))
    scale = _temp_scale(location)
    anchors, labelled = scale['anchors'], scale['labelled']
    n_rows = scale['rows']

    by_slot = {}
    for r in readings:
        idx = (r['date'].day - 1) * 2 + (1 if r.get('session') == 'Afternoon' else 0)
        by_slot[idx] = r

    W, H = 1190, 648
    M = 8                      # page margin
    LBL = 104                  # left label column
    DAYS, COLS = 31, 62        # the form always prints 31 days
    gx, gw = M + LBL, W - 2 * M - LBL
    cw = gw / COLS
    head_y, head_h = 96, 22    # "Days of the month" strip
    ti_h = 15                  # "Time interval" strip
    row_h = TEMP_GRID_H / n_rows
    grid_top = head_y + head_h + ti_h
    grid_bot = grid_top + TEMP_GRID_H
    init_y, time_y = grid_bot, grid_bot + 20
    table_bot = time_y + 20

    def cx(i):
        return gx + (i + 0.5) * cw

    def ry(row):
        return grid_top + (row + 0.5) * row_h

    def ty(value):
        return ry(_temp_row(value, anchors))

    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" '
         f'role="img" aria-label="Store room temperature chart" '
         f'style="background:#ffffff;font-family:Arial,Helvetica,sans-serif">',
         f'<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>']

    def box(x, y, w, h, sw=1):
        o.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                 f'fill="none" stroke="#000000" stroke-width="{sw}"/>')

    def txt(x, y, s, size=10, anchor='start', weight='normal', fill='#000000'):
        o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
                 f'text-anchor="{anchor}" font-weight="{weight}" fill="{fill}">'
                 f'{escape(str(s))}</text>')

    # ---- title block, matching the form's own header --------------------
    box(M, 8, W - 2 * M, 56)
    o.append(f'<line x1="{M + 86}" y1="8" x2="{M + 86}" y2="64" '
             f'stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{W - M - 196}" y1="8" x2="{W - M - 196}" y2="64" '
             f'stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{W - M - 196}" y1="36" x2="{W - M}" y2="36" '
             f'stroke="#000000" stroke-width="1"/>')
    txt(M + 43, 40, 'Lesotho', 8, 'middle', fill='#666666')
    txt(M + 43, 50, 'Ministry of Health', 7, 'middle', fill='#666666')
    txt(M + 98, 32, 'STORE ROOM TEMPERATURE CHART', 16, weight='bold')
    txt(M + 98, 54, f'Month: {start:%B}', 11)
    txt(M + 260, 54, f'Year: {year}', 11)
    txt(M + 400, 54, f'Location: {location}', 11, weight='bold')
    txt(M + 660, 40, 'M – Morning', 10)
    txt(M + 660, 54, 'A – Afternoon', 10)
    txt(W - M - 188, 26, 'Document no: SR-TC-001', 9.5)
    txt(W - M - 188, 54, 'Effective date: 12/2018', 9.5)

    # ---- out-of-range tint, so a breach is visible at a glance ----------
    # Very light: it must not survive a photocopy as anything that could be
    # mistaken for a mark.
    top_lim_y, bot_lim_y = ty(hi_lim), ty(lo_lim)
    o.append(f'<rect x="{gx:.1f}" y="{grid_top:.1f}" width="{gw:.1f}" '
             f'height="{top_lim_y - grid_top:.1f}" fill="#fdf0f0"/>')
    o.append(f'<rect x="{gx:.1f}" y="{bot_lim_y:.1f}" width="{gw:.1f}" '
             f'height="{grid_bot - bot_lim_y:.1f}" fill="#eef3fb"/>')

    # ---- the grid -------------------------------------------------------
    box(M, head_y, W - 2 * M, table_bot - head_y)
    for y in (head_y + head_h, grid_top, grid_bot, init_y + 20):
        o.append(f'<line x1="{M}" y1="{y:.1f}" x2="{W - M}" y2="{y:.1f}" '
                 f'stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{gx:.1f}" y1="{head_y}" x2="{gx:.1f}" '
             f'y2="{table_bot:.1f}" stroke="#000000" stroke-width="1"/>')
    for row in range(1, n_rows):
        y = grid_top + row * row_h
        o.append(f'<line x1="{M}" y1="{y:.1f}" x2="{W - M}" y2="{y:.1f}" '
                 f'stroke="#9e9e9e" stroke-width="0.5"/>')

    txt(M + 4, head_y + 10, 'Days of the', 8.5)
    txt(M + 4, head_y + 19, 'month', 8.5)
    txt(M + 4, grid_top - 4, 'Time interval', 8.5)
    txt(M + 4, init_y + 14, 'Initials', 9)
    txt(M + 4, time_y + 14, 'Write time', 9)

    for row, label in labelled.items():
        txt(M + 4, ry(row) + 4, label, 9.5)

    for d in range(1, DAYS + 1):
        i = (d - 1) * 2
        x0, x1 = gx + i * cw, gx + (i + 2) * cw
        real = d <= days_in_month
        if not real:
            # The form always has 31 columns; a short month greys the surplus
            # so nobody reads a blank column as a missed check.
            o.append(f'<rect x="{x0:.1f}" y="{head_y:.1f}" width="{2 * cw:.1f}" '
                     f'height="{table_bot - head_y:.1f}" fill="#f0f0f0"/>')
        o.append(f'<line x1="{x1:.1f}" y1="{head_y}" x2="{x1:.1f}" '
                 f'y2="{table_bot:.1f}" stroke="#000000" stroke-width="1"/>')
        o.append(f'<line x1="{gx + (i + 1) * cw:.1f}" y1="{head_y + head_h:.1f}" '
                 f'x2="{gx + (i + 1) * cw:.1f}" y2="{table_bot:.1f}" '
                 f'stroke="#9e9e9e" stroke-width="0.5"/>')
        txt((x0 + x1) / 2, head_y + 16, d, 9.5, 'middle', 'bold',
            '#000000' if real else '#999999')
        if real:
            txt(cx(i), grid_top - 4, 'm', 8, 'middle')
            txt(cx(i + 1), grid_top - 4, 'a', 8, 'middle')

    # ---- the plotted readings -------------------------------------------
    # One reading is taken each morning and each afternoon. The form has three
    # pens, so they are used the way this pharmacy uses them: the morning
    # reading is marked in blue in that day's 'm' column, the afternoon
    # reading in red in the 'a' column, and the day's average in black
    # between the two.
    #
    # Morning is always blue even on a day the afternoon turns out cooler, so
    # the red line dips under the blue one. That crossing is left visible on
    # purpose: it is a real and unusual day, and sorting the two readings by
    # size to keep the lines apart would hide it.
    #
    # Lines break wherever a check is missing. Joining across a gap would draw
    # readings that were never taken, and a missed check is precisely what
    # this chart exists to show. The average needs BOTH readings, so a single
    # missed check breaks the black line for that day too.
    def _series_points(key):
        pts = []
        for d in range(days_in_month):
            m = _reading_temp(by_slot.get(d * 2))
            a = _reading_temp(by_slot.get(d * 2 + 1))
            if key == 'min':
                v, x = m, cx(d * 2)
            elif key == 'max':
                v, x = a, cx(d * 2 + 1)
            else:
                v = None if (m is None or a is None) else (m + a) / 2.0
                x = gx + (d * 2 + 1) * cw        # midway between m and a
            pts.append((x, v))
        return pts

    for key, _label, colour, shape, _pen in TEMP_SERIES:
        # One line per series, drawn straight through every reading there is.
        # A missed check is not marked on the line itself — it shows up as a
        # blank column in the Initials and Write time rows underneath, and as
        # a dash in the register table below the chart.
        plotted = [(x, ty(v)) for x, v in _series_points(key) if v is not None]
        if len(plotted) > 1:
            pts_attr = ' '.join(f'{x:.1f},{y:.1f}' for x, y in plotted)
            o.append(f'<polyline points="{pts_attr}" fill="none" '
                     f'stroke="{colour}" stroke-width="1.3" '
                     f'stroke-linejoin="round"/>')
        for x, y in plotted:
            o.append(_temp_marker(shape, x, y, colour))

    # ---- initials and time of check, as the form provides for -----------
    for i in range(days_in_month * 2):
        slot = by_slot.get(i)
        if not slot:
            continue
        if slot.get('initials'):
            txt(cx(i), init_y + 14, str(slot['initials'])[:4], 7.5, 'middle')
        if slot.get('check_time'):
            # Rotated: at 62 columns two horizontal times per day overlap.
            tx_, ty_ = cx(i), time_y + 17
            o.append(f'<text x="{tx_:.1f}" y="{ty_:.1f}" font-size="7" '
                     f'text-anchor="start" transform="rotate(-90 {tx_:.1f} {ty_:.1f})" '
                     f'fill="#000000">{escape(str(slot["check_time"])[:5])}</text>')

    # ---- footer: signing block and the form's two legend boxes ----------
    fy = table_bot + 6
    box(M, fy, W - 2 * M, H - fy - 6)
    o.append(f'<line x1="{W - M - 560}" y1="{fy:.1f}" x2="{W - M - 560}" '
             f'y2="{H - 6}" stroke="#000000" stroke-width="1"/>')
    o.append(f'<line x1="{W - M - 230}" y1="{fy:.1f}" x2="{W - M - 230}" '
             f'y2="{H - 6}" stroke="#000000" stroke-width="1"/>')
    txt(M + 8, fy + 20, f'Thermometer Number: {thermometer or "____________________"}', 10)
    txt(M + 300, fy + 20, f'Month: {start:%B}', 10)
    txt(M + 430, fy + 20, f'Year: {year}', 10)
    txt(M + 8, fy + 44, 'Reviewed by: ______________________________', 10)
    txt(M + 300, fy + 44, 'Date: ____________________', 10)

    # The form's own legend wording, kept as printed. What each pen records
    # here is spelled out beside it, because the sheet is filed on its own and
    # "Minimum" meaning "the morning check" is not self-evident off the page.
    lx = W - M - 552
    series_by_key = {k: (lbl, col, sh, pen) for k, lbl, col, sh, pen in TEMP_SERIES}
    for n, key in enumerate(TEMP_LEGEND_ORDER):
        label, colour, shape, pen = series_by_key[key]
        ly = fy + 17 + n * 15
        txt(lx, ly, f'{label}....', 9.5, weight='bold')
        txt(lx + 66, ly, 'use', 9.5)
        txt(lx + 88, ly, f'{pen} pen....', 9.5, fill=colour)
        o.append(_temp_marker(shape, lx + 164, ly - 3, colour))
        txt(lx + 178, ly, TEMP_PEN_MEANING[key], 8, fill='#555555')

    rx = W - M - 222
    txt(rx, fy + 15, f'Acceptable Range: {lo_lim:g} °C to {hi_lim:g} °C', 9)
    txt(rx, fy + 29, f'Cold spot + {cold[0]:g}°C to + {cold[1]:g}°C', 9)
    txt(rx, fy + 43, f'Hot spot + {hot[0]:g}°C to + {hot[1]:g}°C', 9)

    o.append('</svg>')
    return '\n'.join(o)


@app.route('/temperature', methods=['GET', 'POST'])
@login_required
def temperature() -> ResponseReturnValue:
    current_user = session['user']['name']
    message_class, message = _flashed_banner()
    is_admin = session['user'].get('role') == 'admin'
    today = datetime.utcnow().date()

    def chosen_location(src):
        loc = (src.get('location') or '').strip()
        return loc if loc in TEMP_LOCATIONS else TEMP_LOCATIONS[0]

    try:
        db = get_mongo_client()['pharmacy_db']
        logs = db['temperature_logs']

        if request.method == 'POST':
            action = request.form.get('action')
            loc = chosen_location(request.form)

            if action == 'delete':
                if not is_admin:
                    flash('Access denied. Only admins can delete a reading.')
                    return redirect(url_for('temperature'))
                try:
                    doc = logs.find_one({'_id': ObjectId(request.form.get('reading_id', ''))})
                except (InvalidId, TypeError):
                    doc = None
                if not doc:
                    flash('That reading was not found.')
                else:
                    logs.delete_one({'_id': doc['_id']})
                    write_audit_entry(
                        'DELETE', 'temperature_reading',
                        f"{doc.get('location')} {doc['date']:%Y-%m-%d} {doc.get('session')}",
                        f"removed reading {_reading_temp(doc)}°C, recorded by "
                        f"{doc.get('recorded_by') or 'unknown'}")
                    flash(f"Reading deleted for {doc.get('location')} on "
                          f"{doc['date']:%Y-%m-%d} ({doc.get('session')}).", 'success')
                return redirect(url_for('temperature', location=loc,
                                        month=request.form.get('month') or ''))

            if action == 'record':
                sess = (request.form.get('session') or '').strip()
                if sess not in TEMP_SESSIONS:
                    flash('Choose morning or afternoon.')
                    return redirect(url_for('temperature', location=loc))
                try:
                    day = datetime.strptime(request.form['date'], '%Y-%m-%d')
                except (KeyError, ValueError):
                    flash('Enter a valid date.')
                    return redirect(url_for('temperature', location=loc))
                if day.date() > today:
                    flash('A reading cannot be dated in the future.')
                    return redirect(url_for('temperature', location=loc))
                try:
                    temp = float(request.form['temp'])
                except (KeyError, ValueError):
                    flash('The temperature must be a number.')
                    return redirect(url_for('temperature', location=loc))

                if not (TEMP_MIN_PLAUSIBLE <= temp <= TEMP_MAX_PLAUSIBLE):
                    flash(f'{temp:g}°C is outside the range a room thermometer '
                          f'can read — check for a mistyped figure.')
                    return redirect(url_for('temperature', location=loc))

                prior = logs.find_one({'location': loc, 'date': day, 'session': sess})
                doc = {
                    'location': loc, 'date': day, 'session': sess,
                    'temp': temp,
                    # The official chart has an Initials row and a Write time
                    # row under the grid, so both are captured per check.
                    'initials': (request.form.get('initials') or '').strip()[:4],
                    'check_time': (request.form.get('check_time') or '').strip()[:5],
                    'thermometer': (request.form.get('thermometer') or '').strip(),
                    'note': (request.form.get('note') or '').strip(),
                    'recorded_by': current_user, 'timestamp': datetime.utcnow(),
                }
                if prior:
                    # A correction keeps who took the reading and when, and
                    # adds what it was changed from, by whom - on the reading
                    # itself, so the register can say so, not only in the
                    # audit log. It used to overwrite the original recorder.
                    for k in ('recorded_by', 'timestamp'):
                        doc.pop(k, None)
                    doc['corrected_by'] = current_user
                    doc['corrected_at'] = datetime.utcnow()
                    # Drop the superseded three-figure fields, so a corrected
                    # reading cannot leave a stale 'actual' behind for the
                    # backward-compatible reader to pick up instead.
                    logs.update_one({'_id': prior['_id']},
                                    {'$set': doc,
                                     '$unset': {'max': '', 'min': '', 'actual': ''},
                                     '$push': {'corrections': {
                                         'from': _reading_temp(prior), 'to': temp,
                                         'by': current_user,
                                         'at': datetime.utcnow()}}})
                    write_audit_entry(
                        'UPDATE', 'temperature_reading',
                        f'{loc} {day:%Y-%m-%d} {sess}',
                        f'{_reading_temp(prior)}°C -> {temp}°C')
                    verb = 'corrected'
                else:
                    logs.insert_one(doc)
                    write_audit_entry('CREATE', 'temperature_reading',
                                      f'{loc} {day:%Y-%m-%d} {sess}',
                                      f'{temp}°C')
                    verb = 'recorded'

                lo_lim, hi_lim = TEMP_LIMITS.get(loc, (15.0, 25.0))
                if temp > hi_lim or temp < lo_lim:
                    flash(f'{loc} {day:%Y-%m-%d} {sess.lower()} {verb} at '
                          f'{temp:g}°C, outside the acceptable '
                          f'{lo_lim:g}–{hi_lim:g}°C range.', 'partial')
                else:
                    flash(f'{loc} {day:%Y-%m-%d} {sess.lower()} {verb} at '
                          f'{temp:g}°C.', 'success')
                return redirect(url_for('temperature', location=loc,
                                        month=f'{day.year:04d}-{day.month:02d}'))

            if action == 'record_round':
                # The whole round in one submission. Date, session, time,
                # thermometer and initials are all derived, so the only thing
                # typed is the temperatures themselves.
                now = local_now()
                sess = local_session()
                day = datetime(now.year, now.month, now.day)
                stamp = now.strftime('%H:%M')
                recorder_initials = ''.join(
                    w[0] for w in current_user.split() if w)[:4].upper()

                saved, corrected, breaches, rejected = [], [], [], []
                for idx, loc_name in enumerate(TEMP_LOCATIONS):
                    raw = (request.form.get(f'temp_{idx}') or '').strip()
                    if not raw:
                        continue          # not checked: record nothing
                    try:
                        temp = float(raw)
                    except ValueError:
                        rejected.append(f'{loc_name} ("{raw}" is not a number)')
                        continue
                    if not (TEMP_MIN_PLAUSIBLE <= temp <= TEMP_MAX_PLAUSIBLE):
                        rejected.append(f'{loc_name} ({temp:g}°C is not a '
                                        f'reading a thermometer can give)')
                        continue

                    prior = logs.find_one({'location': loc_name, 'date': day,
                                           'session': sess})
                    doc = {
                        'location': loc_name, 'date': day, 'session': sess,
                        'temp': temp,
                        'initials': recorder_initials,
                        'check_time': stamp,
                        'thermometer': TEMP_THERMOMETERS.get(loc_name, ''),
                        'note': '',
                        'recorded_by': current_user,
                        'timestamp': datetime.utcnow(),
                    }
                    if prior:
                        # Same rule as the single-reading form: a correction
                        # keeps who took the reading and records the change.
                        for k in ('recorded_by', 'timestamp'):
                            doc.pop(k, None)
                        doc['corrected_by'] = current_user
                        doc['corrected_at'] = datetime.utcnow()
                        logs.update_one(
                            {'_id': prior['_id']},
                            {'$set': doc,
                             '$unset': {'max': '', 'min': '', 'actual': ''},
                             '$push': {'corrections': {
                                 'from': _reading_temp(prior), 'to': temp,
                                 'by': current_user, 'at': datetime.utcnow()}}})
                        write_audit_entry(
                            'UPDATE', 'temperature_reading',
                            f'{loc_name} {day:%Y-%m-%d} {sess}',
                            f'{_reading_temp(prior)}°C -> {temp}°C (round entry)')
                        corrected.append(f'{loc_name} {temp:g}°C')
                    else:
                        logs.insert_one(doc)
                        write_audit_entry(
                            'CREATE', 'temperature_reading',
                            f'{loc_name} {day:%Y-%m-%d} {sess}',
                            f'{temp}°C (round entry)')
                        saved.append(f'{loc_name} {temp:g}°C')
                    lo_lim, hi_lim = TEMP_LIMITS.get(loc_name, (15.0, 25.0))
                    if temp > hi_lim or temp < lo_lim:
                        breaches.append(f'{loc_name} {temp:g}°C')

                parts = []
                if saved:
                    parts.append(f"{sess} round recorded: {', '.join(saved)}")
                if corrected:
                    parts.append(f"corrected: {', '.join(corrected)}")
                if rejected:
                    parts.append(f"not recorded: {'; '.join(rejected)}")
                if not parts:
                    flash('Nothing was entered, so nothing was recorded.')
                    tone = None
                else:
                    if breaches:
                        parts.append('OUTSIDE RANGE: ' + ', '.join(breaches))
                    tone = 'error' if rejected else (
                        'partial' if breaches else 'success')
                    flash('. '.join(parts) + '.', tone)
                return redirect(url_for('temperature',
                                        location=chosen_location(request.form),
                                        month=request.form.get('month') or ''))

            flash('Unknown action.')
            return redirect(url_for('temperature'))

        # ----- GET: one location, one calendar month --------------------
        location = chosen_location(request.args)
        raw_month = (request.args.get('month') or '').strip()
        try:
            year, month = int(raw_month[:4]), int(raw_month[5:7])
            datetime(year, month, 1)
        except (ValueError, IndexError):
            year, month = today.year, today.month
        start, end, days_in_month = _month_bounds(year, month)

        readings = list(logs.find({'location': location,
                                   'date': {'$gte': start, '$lt': end}})
                        .sort([('date', 1), ('session', 1)]))
        lo_lim, hi_lim = TEMP_LIMITS.get(location, (15.0, 25.0))

        per_day: dict[int, Doc] = {}
        breach_count = 0
        for r in readings:
            slot = per_day.setdefault(r['date'].day, {})
            slot[r.get('session')] = r
            v = _reading_temp(r)
            if v is not None and (v > hi_lim or v < lo_lim):
                breach_count += 1

        # Each room keeps its own instrument, so the sheet's thermometer number
        # comes from the location rather than from whatever was last typed.
        # Readings entered before that mapping existed fall back to their own
        # stored number, so an older month still prints what it was checked on.
        thermometer = TEMP_THERMOMETERS.get(location, '')
        if not thermometer:
            for r in sorted(readings, key=lambda r: r['timestamp'], reverse=True):
                if r.get('thermometer'):
                    thermometer = r['thermometer']
                    break
        default_initials = ''.join(w[0] for w in current_user.split() if w)[:4].upper()

        # ?edit=<id> opens the single-reading form already filled with that
        # reading, so a correction is changing one figure, not retyping five.
        # Saving goes through the same route as any reading, which corrects
        # the entry for that room, date and round rather than adding one.
        edit_reading = None
        if request.args.get('edit') and is_admin:
            try:
                er = logs.find_one({'_id': ObjectId(request.args['edit'])})
            except (InvalidId, TypeError):
                er = None
            if er:
                edit_reading = {
                    'date': er['date'].strftime('%Y-%m-%d'),
                    'location': er.get('location'),
                    'session': er.get('session'),
                    'temp': _reading_temp(er),
                    'initials': er.get('initials') or '',
                    'check_time': er.get('check_time') or '',
                    'note': er.get('note') or '',
                    'recorded_by': er.get('recorded_by') or '',
                }

        # ----- the round panel: one box per room, everything else derived ---
        now_local = local_now()
        round_session = local_session()
        round_day = datetime(now_local.year, now_local.month, now_local.day)
        already = {d['location']: _reading_temp(d) for d in logs.find(
            {'date': round_day, 'session': round_session})}
        round_rows = []
        for loc_name in TEMP_LOCATIONS:
            lo_l, hi_l = TEMP_LIMITS.get(loc_name, (15.0, 25.0))
            round_rows.append({
                'location': loc_name,
                'thermometer': TEMP_THERMOMETERS.get(loc_name, '-'),
                'lo': lo_l, 'hi': hi_l,
                'existing': already.get(loc_name),
            })

        rows, days_complete = [], 0
        for d in range(1, days_in_month + 1):
            slot = per_day.get(d, {})
            am, pm = slot.get('Morning'), slot.get('Afternoon')
            if am and pm:
                days_complete += 1
            breach, ids, notes, by = False, [], [], []
            inits, times = [], []
            # Morning, afternoon, and the average the black line plots. The
            # average is shown only when both checks were done — averaging one
            # reading with nothing would invent a figure for a missed check.
            am_t, pm_t = _reading_temp(am), _reading_temp(pm)
            cells = [am_t, pm_t,
                     None if (am_t is None or pm_t is None)
                     else round((am_t + pm_t) / 2.0, 1)]
            for r in (am, pm):
                v = _reading_temp(r)
                if r:
                    if v is not None and (v > hi_lim or v < lo_lim):
                        breach = True
                    if r.get('note'):
                        notes.append(r['note'])
                    if r.get('corrections'):
                        last = r['corrections'][-1]
                        if last.get('from') is not None and last.get('from') != last.get('to'):
                            notes.append(f"{(r.get('session') or '').lower()} corrected "
                                         f"from {last['from']:g}°C by {last.get('by', '?')}")
                    if r.get('recorded_by') and r['recorded_by'] not in by:
                        by.append(r['recorded_by'])
                    if r.get('initials'):
                        inits.append(r['initials'])
                    if r.get('check_time'):
                        times.append(r['check_time'])
            for label, r in (('Morning', am), ('Afternoon', pm)):
                if r:
                    ids.append((label, str(r['_id'])))
            rows.append({'day': d, 'cells': cells, 'breach': breach, 'ids': ids,
                         'note': '; '.join(notes), 'by': ', '.join(by),
                         'initials': ' / '.join(inits), 'times': ' / '.join(times)})

        if request.values.get('export') == 'csv':
            # One row per reading rather than the chart's grid: a grid is for
            # looking at, a row per reading is what a spreadsheet can count.
            return _register_csv_response('temperature', readings)

        # Every room's chart for the month, on one page. Choosing a room one
        # at a time meant four page loads to see whether anything had drifted
        # out of range anywhere, and the question asked of this page is
        # almost always "is everything all right?" — which is a question
        # about all of them at once. One query for the month, split by room.
        month_readings = list(logs.find({'date': {'$gte': start, '$lt': end}})
                              .sort([('date', 1), ('session', 1)]))
        charts: Docs = []
        for loc_name in TEMP_LOCATIONS:
            loc_readings = [r for r in month_readings
                            if r.get('location') == loc_name]
            lo_l, hi_l = TEMP_LIMITS.get(loc_name, (15.0, 25.0))
            loc_breaches = sum(
                1 for r in loc_readings
                if (v := _reading_temp(r)) is not None and (v > hi_l or v < lo_l))
            loc_thermo = TEMP_THERMOMETERS.get(loc_name, '')
            if not loc_thermo:
                for r in sorted(loc_readings, key=lambda r: r['timestamp'],
                                reverse=True):
                    if r.get('thermometer'):
                        loc_thermo = r['thermometer']
                        break
            charts.append({
                'location': loc_name, 'lo': lo_l, 'hi': hi_l,
                'recorded': len(loc_readings), 'breaches': loc_breaches,
                'thermometer': loc_thermo,
                'svg': _temperature_chart(loc_readings, year, month,
                                          loc_name, loc_thermo),
            })

        return render_template(
            'temperature.html', nav_links=get_nav_links(), message=message,
            message_class=message_class, is_admin=is_admin,
            locations=TEMP_LOCATIONS, sessions=TEMP_SESSIONS,
            location=location, today=today.strftime('%Y-%m-%d'),
            month_value=f'{year:04d}-{month:02d}',
            month_label=start.strftime('%B %Y'),
            limit_lo=lo_lim, limit_hi=hi_lim,
            days_in_month=days_in_month, days_complete=days_complete,
            recorded_count=len(readings), expected_count=days_in_month * 2,
            breach_count=breach_count,
            rows=rows, thermometer=thermometer, default_initials=default_initials,
            round_rows=round_rows, round_session=round_session,
            round_time=now_local.strftime('%H:%M'),
            round_date_label=now_local.strftime('%A %d %B %Y'),
            charts=charts, edit_reading=edit_reading,
            chart=_temperature_chart(readings, year, month, location, thermometer))

    except ServerSelectionTimeoutError:
        return render_template(
            'temperature.html', nav_links=get_nav_links(),
            message='Database connection failed. Please try again later.',
            message_class='error', is_admin=is_admin,
            locations=TEMP_LOCATIONS, sessions=TEMP_SESSIONS,
            location=TEMP_LOCATIONS[0], today=today.strftime('%Y-%m-%d'),
            month_value=f'{today.year:04d}-{today.month:02d}',
            month_label=today.strftime('%B %Y'),
            limit_lo=15.0, limit_hi=25.0, days_in_month=30, days_complete=0,
            recorded_count=0, expected_count=60, breach_count=0,
            rows=[], thermometer='', default_initials='',
            round_rows=[], round_session=local_session(),
            round_time=local_now().strftime('%H:%M'),
            round_date_label=local_now().strftime('%A %d %B %Y'),
            charts=[], edit_reading=None, chart=''), 500


CONTROLLED_DIRECTION = {
    'opening_balance': +1,
    'receive': +1,
    'dispense': -1,
    'internal_issue': -1,
    'damage': -1,
    'expiry_removal': -1,
    'adjustment': 0,      # genuinely either way: the sign IS the information
}

# What a register has to call these in words. "expiry_removal" is a column in
# a database, not an entry in a register someone may have to answer for.
CONTROLLED_MOVEMENT_LABEL = {
    'opening_balance': 'Opening balance',
    'receive': 'Received',
    'dispense': 'Dispensed to patient',
    'internal_issue': 'Issued to department',
    'damage': 'Damaged — removed',
    'expiry_removal': 'Expired — removed',
    'adjustment': 'Stock-take adjustment',
}


def _controlled_delta(tx: Doc) -> float:
    """What this movement does to the balance: negative means it left."""
    direction = CONTROLLED_DIRECTION.get(tx.get('type') or '', 0)
    qty = tx.get('quantity', 0) or 0
    if direction == 0:
        return qty              # an adjustment carries its own sign
    return direction * abs(qty)




















@app.route('/dashboard', methods=['GET'])
@login_required
def dashboard() -> ResponseReturnValue:
    is_admin = session['user'].get('role') == 'admin'
    message = None

    # --- request options ------------------------------------------------
    try:
        months_back = int(request.args.get('months', 6))
    except (TypeError, ValueError):
        months_back = 6
    if months_back not in DASHBOARD_MONTH_CHOICES:
        months_back = 6

    sort_by = request.args.get('sort', 'urgency')
    if sort_by not in ('urgency', 'amc', 'trend', 'name'):
        sort_by = 'urgency'

    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    if limit not in (0, 25, 50, 100):
        limit = 50

    search = (request.args.get('search') or '').strip()
    cat = _category_choice()

    months        = _month_sequence(months_back)
    month_labels  = [f"{MONTH_ABBR[m]} {y}" for y, m in months]
    month_labels[-1] += ' (MTD)'
    window_start  = datetime(months[0][0], months[0][1], 1)
    now           = datetime.utcnow()
    today         = now.date()
    current_ym    = (now.year, now.month)
    # The current month is only part-elapsed, so including it would drag every
    # AMC down. It is still shown as a column, just excluded from the average.
    amc_months    = [ym for ym in months if ym != current_ym] or months

    empty = {'val_dispensed': ['0'] * len(months), 'val_received': ['0'] * len(months),
             'val_adjusted': ['0'] * len(months)}
    act_empty = {'visits': [0] * len(months), 'lines': [0] * len(months),
                 'per_visit': ['-'] * len(months), 'patients': [0] * len(months),
                 'receipts': [0] * len(months), 'deliveries': [0] * len(months),
                 'adjusted_lines': [0] * len(months)}
    st_empty: dict[str, list[Any]] = {'counted': [0] * len(months), 'agreed': [0] * len(months),
                'short_units': [0] * len(months), 'over_units': [0] * len(months),
                'accuracy': ['-'] * len(months)}

    try:
        db = get_mongo_client()['pharmacy_db']
        movement = _monthly_movement(db['transactions'], window_start)
        all_meds = _by_name(db['medications'].find(_active(_category_filter(cat)), {'_id': 0}))

        rows = []
        # Units are only meaningful per medication, so the cross-medication
        # totals are kept as VALUE (money adds up across dosage forms; units
        # do not) plus the countable activity figures fetched below.
        n_m = len(months)
        totals: dict[str, list[Any]] = {'val_dispensed': [0.0] * n_m, 'val_received': [0.0] * n_m,
                  'val_adjusted': [0.0] * n_m}
        activity = _activity_by_month(db['transactions'], window_start)
        demand = _demand_by_med(db['transactions'], {'$gte': window_start})
        demand_month = _demand_by_month(db['transactions'], window_start)
        clinical = _clinical_by_month(db['transactions'], window_start, months)

        for med in all_meds:
            name    = med.get('name', '')
            by_month = movement.get(name, {})
            # Consumption, counted as the inventory report and the order
            # request count it: dispensed to patients plus issued to
            # departments. Counting dispensing alone made every item the wards
            # draw on look slower-moving here than on the other two pages.
            def used(ym: tuple[int, int]) -> float:
                b = by_month.get(ym, {})
                return b.get('dispensed', 0) + b.get('issued', 0)
            monthly  = [used(ym) for ym in months]
            for i, ym in enumerate(months):
                b = by_month.get(ym, {})
                # Valued at the medication's CURRENT unit price — transactions
                # don't store a price at dispense time, so this is an
                # approximation, and it is labelled as one on the page.
                unit = med.get('price', 0) or 0
                totals['val_dispensed'][i] += b.get('dispensed', 0) * unit
                totals['val_received'][i]  += b.get('received', 0) * unit
                totals['val_adjusted'][i]  += b.get('adjusted', 0) * unit

            # Demand for each completed month: what was used plus what was
            # asked for and could not be supplied - the Adjusted AMC basis.
            # A whole number, rounded half up, as the inventory report has it.
            unmet_by = demand_month.get(name, {})
            complete = [used(ym) + unmet_by.get(ym, 0) for ym in amc_months]
            amc      = (float(int(sum(complete) / len(complete) + 0.5))
                        if complete else 0.0)
            balance  = med.get('balance', 0) or 0
            price    = med.get('price', 0) or 0

            # Months of stock: how long today's balance lasts at the recent rate.
            mos = (balance / amc) if amc > 0 else None
            if balance == 0:
                status, mos_label = 'expired', 'Out of stock'
            elif mos is None:
                status, mos_label = 'normal', 'No recent use'
            elif mos < 1:
                status, mos_label = 'expired', f'{mos:.1f}'
            elif mos < 2:
                status, mos_label = 'close-to-expire', f'{mos:.1f}'
            else:
                status, mos_label = 'normal', f'{mos:.1f}'

            # Trend: last completed month against AMC.
            last_complete = complete[-1] if complete else 0
            if amc <= 0:
                trend_label = '-'
            else:
                pct = (last_complete - amc) / amc * 100
                if abs(pct) < 10:
                    trend_label = 'steady'
                else:
                    trend_label = f"{'up' if pct > 0 else 'down'} {abs(pct):.0f}%"

            # Pattern: how erratic demand has been. An item with the same AMC
            # but lumpy demand needs a bigger buffer, which AMC alone hides.
            if amc > 0 and len(complete) >= 3:
                cv = _stdev(complete) / amc
                pattern = 'Steady' if cv < 0.25 else ('Variable' if cv < 0.75 else 'Erratic')
            else:
                pattern = '-'

            # Same target as the inventory report: one month's cover plus two
            # months' lead time.
            suggested = max(0, amc * 3 - balance) if amc > 0 else 0
            peak      = monthly.index(max(monthly)) if any(monthly) else None

            unmet_units = demand.get(name, _ZERO_DEMAND)['shortfall']
            rows.append({
                'med_name': name, 'monthly': monthly, 'unmet': unmet_units,
                'fill_rate': (round(sum(monthly) / (sum(monthly) + unmet_units) * 100)
                              if (sum(monthly) + unmet_units) else None),
                'amc': int(amc), 'amc_raw': amc,
                'trend_label': trend_label,
                'trend_abs': abs((last_complete - amc) / amc) if amc > 0 else -1,
                'pattern': pattern, 'balance': balance,
                'mos': mos, 'mos_label': mos_label, 'status': status,
                'suggested_order': int(round(suggested)),
                'peak': peak, 'value': balance * price,
                'expiry': _parse_expiry(med.get('expiry_date')),
                'expiry_raw': med.get('expiry_date') or '-',
                'total_consumption': sum(monthly),
            })

        # --- activity rows ------------------------------------------------
        act: Doc = {'visits': [], 'lines': [], 'per_visit': [], 'patients': [],
               'receipts': [], 'deliveries': [], 'adjusted_lines': []}
        for ym in months:
            b = activity.get(ym) or {'visits': set(), 'lines': 0, 'patients': set(),
                                     'receipts': 0, 'deliveries': set(), 'adjusted_lines': 0}
            visits = len(b['visits'])
            act['visits'].append(visits)
            act['lines'].append(b['lines'])
            act['per_visit'].append(f"{b['lines'] / visits:.1f}" if visits else '-')
            act['patients'].append(len(b['patients']))
            act['receipts'].append(b['receipts'])
            act['deliveries'].append(len(b['deliveries']))
            act['adjusted_lines'].append(b['adjusted_lines'])
        # Distinct patients across the whole window, so a patient attending in
        # two months is counted once — the same basis as the period report.
        patient_total = len(clinical.get('patients') or ())
        dispensed_money = sum(totals['val_dispensed'])
        cost_per_patient_label = (f'M{dispensed_money / patient_total:,.2f}'
                                  if patient_total else '-')
        totals['val_dispensed'] = [f"{v:,.0f}" for v in totals['val_dispensed']]
        totals['val_received']  = [f"{v:,.0f}" for v in totals['val_received']]
        totals['val_adjusted']  = [f"{v:,.0f}" for v in totals['val_adjusted']]

        # --- tiles (whole pharmacy, never narrowed by the search box) ----
        out_of_stock = [r for r in rows if r['balance'] == 0]
        under_one    = [r for r in rows if r['balance'] > 0 and r['mos'] is not None and r['mos'] < 1]
        expired_now  = [r for r in rows if r['expiry'] and r['expiry'] < today and r['balance'] > 0]
        expiring_90  = [r for r in rows if r['expiry'] and today <= r['expiry'] <= today + timedelta(days=90)
                        and r['balance'] > 0]
        stock_value  = sum(r['value'] for r in rows)
        at_risk      = sum(r['value'] for r in expiring_90)

        total_disp = sum(sum(r['monthly']) for r in rows)
        total_unmet = sum(r['unmet'] for r in rows)
        fill = (total_disp / (total_disp + total_unmet) * 100) if (total_disp + total_unmet) else None
        short_items = [r for r in rows if r['unmet']]
        tiles = [
            {'label': 'Items tracked', 'value': len(rows), 'sub': '', 'bg': '#eef2f7', 'fg': '#1a3a5c'},
            {'label': 'Fill rate', 'value': f'{fill:.1f}%' if fill is not None else '-',
             'sub': f'{total_unmet:,} unit(s) asked for and not supplied',
             'bg': ('#f8d7da' if fill is not None and fill < 90
                    else ('#fff3cd' if fill is not None and fill < 98 else '#eef2f7')),
             'fg': ('#721c24' if fill is not None and fill < 90
                    else ('#856404' if fill is not None and fill < 98 else '#1a3a5c'))},
            {'label': 'Items that ran short', 'value': len(short_items),
             'sub': 'demand could not be met',
             'bg': '#f8d7da' if short_items else '#eef2f7',
             'fg': '#721c24' if short_items else '#1a3a5c'},
            {'label': 'Out of stock', 'value': len(out_of_stock), 'sub': 'balance is zero',
             'bg': '#f8d7da' if out_of_stock else '#eef2f7', 'fg': '#721c24' if out_of_stock else '#1a3a5c'},
            {'label': 'Under 1 month of stock', 'value': len(under_one), 'sub': 'reorder now',
             'bg': '#f8d7da' if under_one else '#eef2f7', 'fg': '#721c24' if under_one else '#1a3a5c'},
            {'label': 'Expired on shelf', 'value': len(expired_now), 'sub': 'remove from stock',
             'bg': '#f8d7da' if expired_now else '#eef2f7', 'fg': '#721c24' if expired_now else '#1a3a5c'},
            {'label': 'Expiring in 90 days', 'value': len(expiring_90), 'sub': f'M{at_risk:,.2f} at risk',
             'bg': '#fff3cd' if expiring_90 else '#eef2f7', 'fg': '#856404' if expiring_90 else '#1a3a5c'},
            {'label': 'Stock value', 'value': f'M{stock_value:,.0f}', 'sub': 'balance x unit price',
             'bg': '#eef2f7', 'fg': '#1a3a5c'},
            {'label': 'Cost per patient', 'value': cost_per_patient_label,
             'sub': f'{patient_total:,} patient(s) over the period',
             'bg': '#eef2f7', 'fg': '#1a3a5c'},
        ]

        # --- expiring panel ---------------------------------------------
        expiring = []
        for r in sorted(expiring_90, key=lambda r: r['expiry']):
            days_left = (r['expiry'] - today).days
            # What will still be sitting there at expiry if it keeps moving
            # at its AMC — the quantity actually at risk of write-off.
            projected_use = r['amc_raw'] * (days_left / 30.0)
            expiring.append({
                'med_name': r['med_name'], 'expiry_date': r['expiry_raw'],
                'days_left': days_left, 'balance': r['balance'], 'value': r['value'],
                'amc': r['amc'], 'likely_unused': max(0, int(round(r['balance'] - projected_use))),
                'status': 'expired' if days_left <= 30 else 'close-to-expire',
            })

        # --- table: search, sort, limit ---------------------------------
        table_rows = rows
        if search:
            needle = search.lower()
            table_rows = [r for r in table_rows if needle in r['med_name'].lower()]
        total_items = len(table_rows)

        if sort_by == 'name':
            table_rows = sorted(table_rows, key=lambda r: _name_key(r['med_name']))
        elif sort_by == 'amc':
            table_rows = sorted(table_rows, key=lambda r: -r['amc_raw'])
        elif sort_by == 'trend':
            table_rows = sorted(table_rows, key=lambda r: -r['trend_abs'])
        else:  # urgency — emptiest shelves first, items with no demand last
            table_rows = sorted(
                table_rows,
                key=lambda r: (0 if r['balance'] == 0 else 1,
                               r['mos'] if r['mos'] is not None else float('inf'),
                               -r['amc_raw'])
            )
        if limit:
            table_rows = table_rows[:limit]

        # --- stock take panels (admin only) -----------------------------
        st_totals, top_discrepancies, never_counted = st_empty, [], []
        if is_admin:
            counts = list(db['stock_take_counts'].find(
                {'timestamp': {'$gte': window_start}},
                {'_id': 0, 'med_name': 1, 'variance': 1, 'timestamp': 1}))
            idx = {ym: i for i, ym in enumerate(months)}
            st_totals = {'counted': [0] * len(months), 'agreed': [0] * len(months),
                         'short_units': [0] * len(months), 'over_units': [0] * len(months),
                         'accuracy': ['-'] * len(months)}
            net_by_med: defaultdict[str, Doc] = defaultdict(
                lambda: {'net': 0, 'count': 0, 'last': None})
            for c in counts:
                ts = c.get('timestamp')
                v = c.get('variance', 0) or 0
                if isinstance(ts, datetime) and (ts.year, ts.month) in idx:
                    i = idx[(ts.year, ts.month)]
                    st_totals['counted'][i] += 1
                    if v == 0:
                        st_totals['agreed'][i] += 1
                    elif v < 0:
                        st_totals['short_units'][i] += -v
                    else:
                        st_totals['over_units'][i] += v
                if v != 0:
                    e = net_by_med[c.get('med_name', '')]
                    e['net'] += v
                    e['count'] += 1
                    if isinstance(ts, datetime) and (e['last'] is None or ts > e['last']):
                        e['last'] = ts
            for i in range(len(months)):
                total = st_totals['counted'][i]
                st_totals['accuracy'][i] = f"{st_totals['agreed'][i] / total * 100:.0f}%" if total else '-'

            top_discrepancies = sorted(
                ({'med_name': k, 'net': v['net'], 'count': v['count'],
                  'label': DISCREPANCY_LABELS[classify_variance(v['net'])],
                  'last_counted': v['last'].strftime('%Y-%m-%d') if v['last'] else '-'}
                 for k, v in net_by_med.items() if v['net'] != 0),
                key=lambda d: -abs(d['net'])
            )[:15]

            ever_counted = set(db['stock_take_counts'].distinct('med_name'))
            never_counted = sorted(
                ({'med_name': r['med_name'], 'balance': r['balance'], 'value': r['value']}
                 for r in rows if r['balance'] > 0 and r['med_name'] not in ever_counted),
                key=lambda n: -n['value']
            )[:25]

        return render_template(
            'dashboard.html', nav_links=get_nav_links(), message=message,
            is_admin=is_admin, month_labels=month_labels, months_back=months_back,
            month_choices=DASHBOARD_MONTH_CHOICES, sort_by=sort_by, limit=limit,
            search=search, category=cat, category_label=CATEGORY_LABELS[cat],
            tiles=tiles, totals=totals, act=act, clinical=clinical,
            rows=table_rows,
            total_items=total_items, expiring=expiring, st_totals=st_totals,
            top_discrepancies=top_discrepancies, never_counted=never_counted,
        )

    except ServerSelectionTimeoutError:
        return render_template(
            'dashboard.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            is_admin=is_admin, month_labels=month_labels, months_back=months_back,
            month_choices=DASHBOARD_MONTH_CHOICES, sort_by=sort_by, limit=limit,
            search=search, category='pharmaceutical',
            category_label=CATEGORY_LABELS['pharmaceutical'],
            tiles=[], totals=empty, act=act_empty, rows=[], total_items=0,
            clinical={'episodes': 0, 'patients': set()},
            expiring=[], st_totals=st_empty, top_discrepancies=[], never_counted=[],
        ), 500


# -----------------------------------------------------------------------
# NEW (Stock Take): physical count with immediate, per-item correction.
#
# A discrepancy between the shelf and the system is always one of exactly two
# things, and the sign of the variance tells you which:
#
#   counted < system  -> stock left the shelf but was never recorded
#                        ("issued, not recorded")
#   counted > system  -> an issue was recorded that never actually happened
#                        ("recorded, not issued")
#
# so the type is derived, never asked for.
# -----------------------------------------------------------------------
DISCREPANCY_ISSUED_NOT_RECORDED = 'issued_not_recorded'
DISCREPANCY_RECORDED_NOT_ISSUED = 'recorded_not_issued'
DISCREPANCY_NONE = 'none'

DISCREPANCY_LABELS = {
    DISCREPANCY_ISSUED_NOT_RECORDED: 'Issued, not recorded',
    DISCREPANCY_RECORDED_NOT_ISSUED: 'Recorded, not issued',
    DISCREPANCY_NONE: 'Agrees',
}


def classify_variance(variance: float) -> str:
    """variance = physical count - system balance."""
    if variance < 0:
        return DISCREPANCY_ISSUED_NOT_RECORDED
    if variance > 0:
        return DISCREPANCY_RECORDED_NOT_ISSUED
    return DISCREPANCY_NONE


def _flashed_banner() -> tuple[str | None, str | None]:
    """(css_class, message) for the banner, with Flask's default normalised.

    flash('text') with no category stores the category as the literal string
    'message'. The templates then render class="message message", which is
    truthy, so the {% else %}error{% endif %} fallback never fired and every
    plain error banner rendered unstyled instead of red. Mapping the default
    onto 'error' here fixes it once for every page rather than in each
    template, and routes that pass a category keep it.
    """
    flashed = get_flashed_messages(with_categories=True)
    if not flashed:
        return None, None
    category, text = cast('list[tuple[str, str]]', flashed)[0]
    return ('error' if category in (None, '', 'message') else category), text


def write_audit_entry(action: str, target_type: str, target_id: Any, changes: Any) -> None:
    """Write straight to audit_log in the shape AUDIT_TEMPLATE renders.

    audit_logger.py's decorators only wrap the dispense/receive/medication
    routes, so stock-take corrections would otherwise never appear under
    'Edits & Deletes' even though they change a balance. Best-effort: an audit
    write must never be the reason a count fails to save.
    """
    try:
        db = get_mongo_client()['pharmacy_db']
        db['audit_log'].insert_one({
            'timestamp': datetime.utcnow(),
            'action': action,
            'target_type': target_type,
            'target_id': target_id,
            'user': session.get('user', {}).get('name', 'unknown'),
            'ip': request.remote_addr,
            'changes': changes,
        })
    except Exception as e:
        app.logger.warning(f"Failed to write audit entry for {target_type}/{target_id}: {e}")


@app.route('/stock-take', methods=['GET', 'POST'])
@login_required
def stock_take() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can perform a stock take.')
        return redirect('/reports')

    current_user = session['user']['name']
    flashed = get_flashed_messages()
    message = flashed[0] if flashed else None

    try:
        db = get_mongo_client()['pharmacy_db']
        medications  = db['medications']
        transactions = db['transactions']
        stock_takes  = db['stock_takes']
        counts_col   = db['stock_take_counts']

        stock_take_id = request.values.get('stock_take_id')

        if request.method == 'POST':
            action = request.form.get('action')

            # --- Open a new stock take -----------------------------------
            if action == 'open':
                if stock_takes.find_one({'status': 'open'}):
                    flash('A stock take is already open. Continue or close it first.')
                    return redirect(url_for('stock_take'))
                reference = f"ST-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}"
                try:
                    new_id = stock_takes.insert_one({
                        'reference': reference,
                        'status': 'open',
                        'note': (request.form.get('note') or '').strip(),
                        'started_by': current_user,
                        'started_at': datetime.utcnow(),
                        'closed_by': None,
                        'closed_at': None,
                    }).inserted_id
                except DuplicateKeyError:
                    flash('Could not generate a unique reference. Please try again.')
                    return redirect(url_for('stock_take'))
                write_audit_entry('CREATE', 'stock_take', reference,
                                  f"Stock take {reference} opened by {current_user}")
                flash(f'Stock take {reference} opened.')
                return redirect(url_for('stock_take', stock_take_id=str(new_id)))

            # --- Post a whole count sheet at once -------------------------
            if action == 'count_sheet':
                st = _load_stock_take(stock_takes, request.form.get('stock_take_id'))
                if not st:
                    flash('Stock take not found.')
                    return redirect(url_for('stock_take'))
                if st['status'] != 'open':
                    flash(f"Stock take {st['reference']} is closed.")
                    return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

                names = request.form.getlist('med_name')
                posted = agreed = short = over = skipped = 0
                details_only = detail_changes = bad_dates = confirmed = 0
                # The expiry and batch boxes arrive already filled with what
                # is on file, so a box sent back unchanged is not an entry.
                # Without this, posting the sheet would record a "correction"
                # on every row of it.
                on_file = {m['name']: m for m in medications.find(
                    {'name': {'$in': names}},
                    {'_id': 0, 'name': 1, 'expiry_date': 1, 'batch': 1})}
                for i, name in enumerate(names):
                    raw = (request.form.get(f'counted__{i}') or '').strip()
                    expiry = (request.form.get(f'expiry__{i}') or '').strip()
                    batch = (request.form.get(f'batch__{i}') or '').strip()
                    agree = request.form.get(f'agree__{i}') == 'on'
                    current = on_file.get(name) or {}
                    if expiry and expiry == _expiry_iso(current.get('expiry_date')):
                        expiry = ''
                    if batch and batch == (current.get('batch') or '').strip():
                        batch = ''
                    # A figure typed in wins over the tick: if somebody ticked
                    # "agrees" and then counted something different, the
                    # number they wrote down is the one that happened.
                    if raw != '':
                        agree = False

                    # A date the app cannot read is dropped rather than
                    # written, and said out loud. The count on the same row
                    # still posts: losing a counted quantity over a mistyped
                    # date would be the worse trade.
                    if expiry and not _parse_expiry(expiry):
                        bad_dates += 1
                        expiry = ''

                    if raw == '' and not expiry and not batch and not agree:
                        continue          # blank row: nothing entered, leave it

                    counted: int | None = None
                    if raw != '':
                        try:
                            counted = int(raw)
                        except ValueError:
                            skipped += 1
                            continue
                        if counted < 0:
                            skipped += 1
                            continue

                    outcome = _record_count(
                        medications, transactions, counts_col, st, name, counted,
                        (request.form.get(f'note__{i}') or '').strip(), current_user,
                        expiry=expiry or None, batch=batch or None, agree=agree)
                    if outcome is None:
                        skipped += 1
                        continue
                    if agree:
                        confirmed += 1
                    detail_changes += outcome['detail_changes']
                    if not outcome['counted']:
                        details_only += 1
                        continue
                    posted += 1
                    if outcome['variance'] == 0:
                        agreed += 1
                    elif outcome['variance'] < 0:
                        short += 1
                    else:
                        over += 1

                if posted:
                    msg = (f'{posted} count(s) posted to {st["reference"]}: '
                           f'{agreed} agreed, {short} short, {over} over.')
                    if confirmed:
                        msg += (f' {confirmed} of the agreed were ticked as '
                                f'matching the system rather than counted.')
                elif details_only:
                    msg = f'No counts were entered in {st["reference"]}.'
                else:
                    msg = 'No counts were entered.'
                if details_only:
                    msg += (f' {details_only} row(s) had an expiry or batch '
                            f'but no count, so their balances were left alone.')
                if detail_changes:
                    msg += f' {detail_changes} expiry/batch detail(s) updated.'
                if bad_dates:
                    msg += (f' {bad_dates} expiry date(s) were not in '
                            f'YYYY-MM-DD form and were not saved.')
                if skipped:
                    msg += f' {skipped} row(s) skipped as invalid or unknown.'
                flash(msg)
                return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

            # --- Record one physical count -------------------------------
            if action == 'count':
                st = _load_stock_take(stock_takes, request.form.get('stock_take_id'))
                if not st:
                    flash('Stock take not found.')
                    return redirect(url_for('stock_take'))
                if st['status'] != 'open':
                    flash(f"Stock take {st['reference']} is closed. No further counts can be added.")
                    return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

                med_name = (request.form.get('med_name') or '').strip()
                raw = (request.form.get('counted') or '').strip()
                expiry = (request.form.get('expiry_date') or '').strip()
                batch = (request.form.get('batch') or '').strip()

                counted = None
                if raw != '':
                    try:
                        counted = int(raw)
                    except ValueError:
                        flash('Physical count must be a whole number.')
                        return redirect(url_for('stock_take',
                                                stock_take_id=str(st['_id'])))
                    if counted < 0:
                        flash('Physical count cannot be negative.')
                        return redirect(url_for('stock_take',
                                                stock_take_id=str(st['_id'])))
                if counted is None and not expiry and not batch:
                    flash('Enter a count, or an expiry date or batch number.')
                    return redirect(url_for('stock_take',
                                            stock_take_id=str(st['_id'])))
                if expiry and not _parse_expiry(expiry):
                    flash('The expiry date must be in YYYY-MM-DD form.')
                    return redirect(url_for('stock_take',
                                            stock_take_id=str(st['_id'])))

                # One implementation, shared with the count sheet. This used
                # to be a second copy of the same ninety lines, which is how
                # the two could have drifted apart without anything failing.
                outcome = _record_count(
                    medications, transactions, counts_col, st, med_name, counted,
                    (request.form.get('note') or '').strip(), current_user,
                    expiry=expiry or None, batch=batch or None)
                if outcome is None:
                    flash(f'Medication "{med_name}" not found. '
                          f'Add it first, then count it.')
                    return redirect(url_for('stock_take',
                                            stock_take_id=str(st['_id'])))

                bits = []
                if outcome['counted']:
                    variance = outcome['variance']
                    if variance == 0:
                        bits.append(f'counted {counted}. Agrees with the system')
                    else:
                        dtype = classify_variance(variance)
                        bits.append(
                            f'counted {counted}, system had '
                            f'{counted - variance:g} ({variance:+g}). Recorded as '
                            f'"{DISCREPANCY_LABELS[dtype]}" and the balance is '
                            f'now {counted}')
                if outcome['detail_changes']:
                    bits.append(f"{outcome['detail_changes']} detail(s) corrected")
                elif not outcome['counted']:
                    bits.append('expiry and batch recorded; the balance was '
                                'left as it was')
                flash(f"{med_name}: {'; '.join(bits)}.")
                return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

            # --- Close a stock take --------------------------------------
            if action == 'close':
                st = _load_stock_take(stock_takes, request.form.get('stock_take_id'))
                if not st:
                    flash('Stock take not found.')
                    return redirect(url_for('stock_take'))
                if st['status'] != 'open':
                    flash(f"Stock take {st['reference']} is already closed.")
                    return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))
                stock_takes.update_one(
                    {'_id': st['_id'], 'status': 'open'},
                    {'$set': {'status': 'closed', 'closed_by': current_user,
                              'closed_at': datetime.utcnow()}}
                )
                write_audit_entry('UPDATE', 'stock_take', st['reference'],
                                  f"Stock take {st['reference']} closed by {current_user}")
                flash(f"Stock take {st['reference']} closed.")
                return redirect(url_for('stock_take', stock_take_id=str(st['_id'])))

            flash('Unknown stock take action.')
            return redirect(url_for('stock_take'))

        # ----- GET ------------------------------------------------------
        active = _load_stock_take(stock_takes, stock_take_id) if stock_take_id else None

        if active:
            counts = list(counts_col.find({'stock_take_id': str(active['_id'])})
                          .sort('timestamp', -1).limit(2000))
            for c in counts:
                c['discrepancy_label'] = DISCREPANCY_LABELS.get(c.get('discrepancy_type'), '-')
            # The sheet used to carry names only, so the count was blind. It
            # now shows what the system has — balance, expiry and batch — so a
            # pharmacist short of time can walk a shelf and act only where the
            # shelf and the system disagree. That was asked for, knowing the
            # trade: a figure on the page is easier to agree with than to
            # check. So agreeing is a tick per row, recorded as "confirmed"
            # rather than "counted", and the register keeps the difference.
            sheet_docs = _by_name(medications.find(
                _active(), {'_id': 0, 'name': 1, 'balance': 1,
                            'expiry_date': 1, 'batch': 1}))
            med_names = [m['name'] for m in sheet_docs]
            # Items already counted in THIS stock take drop off the sheet, so
            # the list shrinks as the work is done and nothing is counted twice
            # by accident.
            done = set(counts_col.distinct(
                'med_name',
                {'stock_take_id': str(active['_id']), 'counted': {'$ne': None}}))
            uncounted = [m for m in med_names if m not in done]
            sheet = [{'name': m['name'], 'balance': m.get('balance', 0),
                      'expiry': _expiry_iso(m.get('expiry_date')),
                      'batch': (m.get('batch') or '').strip()}
                     for m in sheet_docs if m['name'] not in done]
            if request.values.get('export') == 'csv':
                return _register_csv_response('stock_take', counts)

            return render_template(
                'stock_take.html', nav_links=get_nav_links(), message=message,
                active=active, counts=counts, med_names=med_names,
                uncounted=uncounted, sheet=sheet, sessions=[],
                agreed_count=sum(1 for c in counts if c.get('variance') == 0),
                issued_not_recorded=sum(1 for c in counts
                                        if (c.get('variance') or 0) < 0),
                recorded_not_issued=sum(1 for c in counts
                                        if (c.get('variance') or 0) > 0),
            )

        sessions = list(stock_takes.find().sort([('status', 1), ('started_at', -1)]).limit(200))
        for s in sessions:
            sid = str(s['_id'])
            s['counted_lines']     = counts_col.count_documents(
                {'stock_take_id': sid, 'counted': {'$ne': None}})
            s['discrepancy_lines'] = counts_col.count_documents(
                {'stock_take_id': sid, 'variance': {'$nin': [0, None]}})
        return render_template(
            'stock_take.html', nav_links=get_nav_links(), message=message,
            active=None, sessions=sessions, counts=[], med_names=[], uncounted=[],
            sheet=[],
            agreed_count=0, issued_not_recorded=0, recorded_not_issued=0,
        )

    except ServerSelectionTimeoutError:
        return render_template(
            'stock_take.html', nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            active=None, sessions=[], counts=[], med_names=[], uncounted=[],
            agreed_count=0, issued_not_recorded=0, recorded_not_issued=0,
        ), 500


def _record_count(medications: Collection, transactions: Collection,
                  counts_col: Collection, st: Doc, med_name: str,
                  counted: float | None, note: str, user: str,
                  expiry: str | None = None,
                  batch: str | None = None,
                  agree: bool = False) -> Doc | None:
    """Apply one line of a stock take. None if the medication is unknown.

    The one place a count is recorded. The single-item path and the count
    sheet both come through here, so the sheet cannot drift into being a
    second, subtly different implementation.

    The quantity is optional, because a stock take also turns up an expiry
    date or a batch number that does not match what is on file, and noticing
    that is worth recording even when the shelf is not being recounted. A
    line with no quantity corrects the detail and leaves the balance exactly
    as it was: no adjustment is posted, and it does not count as an item
    counted.

    Whatever is given replaces what is on file — the shelf is the truth, and
    that is what a stock take is for — but the value it replaced is kept on
    the line, so the register shows the change rather than quietly absorbing
    it.

    agree=True is the tick on the sheet that says "the shelf matches the
    figure on the system". It is recorded as a count equal to the balance at
    the moment it is posted, read in the same operation that applies any
    expiry or batch correction, so a dispense in between cannot turn a
    confirmation into a false discrepancy. The line is marked as confirmed
    rather than counted, so the register never claims a number was written
    down when it was ticked.
    """
    updates: Doc = {}
    if counted is not None:
        updates['balance'] = counted
    if expiry:
        updates['expiry_date'] = expiry
    if batch:
        updates['batch'] = batch
    if not updates and not agree:
        return None                     # nothing was entered on this line

    if updates:
        before = medications.find_one_and_update(
            {'name': med_name}, {'$set': updates},
            return_document=ReturnDocument.BEFORE)
    else:
        before = medications.find_one({'name': med_name})
    if before and agree and counted is None:
        counted = before.get('balance', 0)
    if not before:
        write_app_warning(
            'medication_not_found',
            f'Medication "{med_name}" not found during stock take {st["reference"]}.',
            {'med_name': med_name, 'stock_take': st['reference']})
        return None

    now = datetime.utcnow()
    system_balance = before.get('balance', 0)
    variance: float | None = None
    dtype: str | None = None
    balance_change = ''
    if counted is not None:
        variance = counted - system_balance
        dtype = classify_variance(variance)
        balance_change = (
            f'confirmed: shelf agrees with the system ({system_balance})'
            if agree and 'balance' not in updates else
            f'balance: {system_balance} -> {counted} '
            f'(variance {variance:+g}, {DISCREPANCY_LABELS[dtype].lower()})')

    # The correction is posted to the ledger as its own signed type. Every
    # report back-calculates a beginning balance from the current balance and
    # the movements in between, so a balance corrected without a matching
    # entry would break the arithmetic for every period containing the count.
    # A detail-only line moves nothing, so it posts nothing.
    if variance:
        transactions.insert_one({
            'type': 'adjustment', 'med_name': med_name, 'quantity': variance,
            'batch': before.get('batch'), 'price': before.get('price'),
            'expiry_date': before.get('expiry_date'),
            'schedule': before.get('schedule'),
            'system_balance': system_balance, 'counted': counted,
            'discrepancy_type': dtype,
            'stock_take_id': str(st['_id']), 'stock_take_reference': st['reference'],
            'user': user, 'timestamp': now,
        })

    line: Doc = {
        'stock_take_id': str(st['_id']), 'reference': st['reference'],
        'med_name': med_name, 'system_balance': system_balance,
        'counted': counted, 'variance': variance, 'discrepancy_type': dtype,
        'note': note, 'counted_by': user, 'timestamp': now,
        'method': ('confirmed' if agree and 'balance' not in updates
                   else ('counted' if 'balance' in updates else 'detail')),
    }
    changes = []
    if expiry:
        line['expiry_before'] = before.get('expiry_date')
        line['expiry_after'] = expiry
        if str(before.get('expiry_date') or '') != expiry:
            changes.append(f"expiry: {before.get('expiry_date') or '(none)'} -> {expiry}")
    if batch:
        line['batch_before'] = before.get('batch')
        line['batch_after'] = batch
        if str(before.get('batch') or '') != batch:
            changes.append(f"batch: {before.get('batch') or '(none)'} -> {batch}")
    counts_col.insert_one(line)

    if balance_change:
        changes.insert(0, balance_change)
    write_audit_entry('UPDATE', 'stock_take_count',
                      f"{st['reference']} / {med_name}", '; '.join(changes))
    return {'variance': variance, 'counted': counted is not None,
            'detail_changes': len(changes) - (1 if counted is not None else 0)}


def _expiry_iso(raw: Any) -> str:
    """An expiry as the date box holds it, YYYY-MM-DD, or '' if there is none.

    Stored expiries are mostly that already, but some carry a time and some
    are datetimes, so both the sheet and the comparison on posting go through
    here and agree about what "unchanged" means.
    """
    d = _parse_expiry(raw)
    return d.isoformat() if d else ''


def _load_stock_take(stock_takes: Collection, raw_id: str | None) -> Doc | None:
    """Resolve a stock take by its string _id, tolerating a malformed value."""
    if not raw_id:
        return None
    try:
        return stock_takes.find_one({'_id': ObjectId(raw_id)})
    except (InvalidId, TypeError):
        return None


@app.route('/audit', methods=['GET'])
@login_required
def audit_log() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Access denied. Only admins can view the audit log.', 'error')
        return redirect('/reports')

    view = request.args.get('view', 'changes')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    search = request.args.get('search')
    # FIX (Bug): flash() is used throughout this app but get_flashed_messages()
    # was never called anywhere, so every flash() message silently vanished.
    # Pull any flashed messages into the same `message` variable AUDIT_TEMPLATE
    # already renders.
    flashed = get_flashed_messages()
    message = flashed[0] if flashed else None
    entries = []

    try:
        client = get_mongo_client()
        db = client['pharmacy_db']

        date_query: Query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt

        if view == 'errors':
            query: Query = {}
            if date_query:
                query['timestamp'] = date_query
            if search:
                query['$or'] = [
                    {'path':     {'$regex': search, '$options': 'i'}},
                    {'endpoint': {'$regex': search, '$options': 'i'}},
                    {'method':   {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['error_logs'].find(query).sort('timestamp', -1).limit(500)
            )
        elif view == 'stocktake':
            # NEW (Stock Take): every physical count and the correction it
            # produced. ?only=discrepancies hides the lines that agreed.
            query = {}
            if date_query:
                query['timestamp'] = date_query
            if request.args.get('only') == 'discrepancies':
                query['variance'] = {'$ne': 0}
            if search:
                query['$or'] = [
                    {'med_name':         {'$regex': search, '$options': 'i'}},
                    {'reference':        {'$regex': search, '$options': 'i'}},
                    {'counted_by':       {'$regex': search, '$options': 'i'}},
                    {'discrepancy_type': {'$regex': search, '$options': 'i'}},
                    {'note':             {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['stock_take_counts'].find(query).sort('timestamp', -1).limit(500)
            )
            for e in entries:
                e['discrepancy_label'] = DISCREPANCY_LABELS.get(e.get('discrepancy_type'), '-')
        elif view == 'warnings':
            query = {}
            if date_query:
                query['timestamp'] = date_query
            if search:
                query['$or'] = [
                    {'warning_type': {'$regex': search, '$options': 'i'}},
                    {'message':      {'$regex': search, '$options': 'i'}},
                    {'user':         {'$regex': search, '$options': 'i'}},
                    {'path':         {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['app_warnings'].find(query).sort('timestamp', -1).limit(500)
            )
        else:
            view = 'changes'  # normalize any unexpected value
            query = {}
            if date_query:
                query['timestamp'] = date_query
            if search:
                query['$or'] = [
                    {'action':      {'$regex': search, '$options': 'i'}},
                    {'target_type': {'$regex': search, '$options': 'i'}},
                    {'target_id':   {'$regex': search, '$options': 'i'}},
                    {'user':        {'$regex': search, '$options': 'i'}},
                ]
            entries = list(
                db['audit_log'].find(query).sort('timestamp', -1).limit(500)
            )

        if request.values.get('export') == 'csv':
            return _register_csv_response('audit', entries)

        return render_template(
            'audit.html',
            nav_links=get_nav_links(),
            message=message,
            view=view,
            entries=entries,
            start_date=start_date,
            end_date=end_date,
            search=search
        )
    except ServerSelectionTimeoutError:
        return render_template(
            'audit.html',
            nav_links=get_nav_links(),
            message="Database connection failed. Please try again later.",
            view=view, entries=[], start_date=start_date, end_date=end_date, search=search
        ), 500


@app.route('/sales/void', methods=['POST'])
@login_required
def void_sale() -> ResponseReturnValue:
    """Admin only: cancel a sale, put its stock back, keep the receipt on file.

    The receipt stays (marked VOIDED, with who and why) so the sequence of
    receipt numbers has no gaps an auditor would have to explain. Its stock
    lines are removed, so every report reads as if the sale never happened.
    """
    if session['user'].get('role') != 'admin':
        flash('Only an admin can void a sale.', 'error')
        return redirect(url_for('sales'))
    receipt_no = request.form.get('receipt_no') or ''
    reason = (request.form.get('reason') or '').strip()[:200]
    if not reason:
        flash('Give a reason for voiding the sale.', 'error')
        return redirect(url_for('receipt', receipt_no=receipt_no))
    db = get_mongo_client()['pharmacy_db']
    sale = db['sales'].find_one_and_update(
        {'receipt_no': receipt_no, 'status': 'completed'},
        {'$set': {'status': 'voided', 'voided_by': session['user']['name'],
                  'voided_at': datetime.utcnow(), 'void_reason': reason}})
    if not sale:
        flash('That sale was not found or is already voided.', 'error')
        return redirect(url_for('sales'))
    for tx in db['transactions'].find({'transaction_id': sale['transaction_id'], 'type': 'dispense'}):
        db['medications'].update_one({'name': tx['med_name']}, {'$inc': {'balance': tx['quantity']}})
    db['transactions'].delete_many({'transaction_id': sale['transaction_id'], 'type': 'dispense'})
    write_audit_entry('VOID', 'sale', receipt_no,
                      f'voided by {session["user"]["name"]}: {reason}; '
                      f'M{sale.get("total", 0):.2f} returned to stock')
    flash(f'Sale {receipt_no} voided and its stock returned.', 'success')
    return redirect(url_for('receipt', receipt_no=receipt_no))


@app.route('/edit-receive/<receive_id>', methods=['GET', 'POST'])
@login_required
def edit_receive(receive_id: str) -> ResponseReturnValue:
    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        transactions = db['transactions']
        medications  = db['medications']

        start_date = request.values.get('start_date')
        end_date   = request.values.get('end_date')
        search     = request.values.get('search')
        current_user = session['user']['name']

        base_query: Query = {'type': 'receive'}
        date_query: Query = {}
        if start_date:
            date_query['$gte'] = datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1) - timedelta(seconds=1)
            date_query['$lte'] = end_dt
        if date_query:
            base_query['timestamp'] = date_query
        if search:
            base_query['$or'] = [
                {'med_name':       {'$regex': search, '$options': 'i'}},
                {'batch':          {'$regex': search, '$options': 'i'}},
                {'supplier':       {'$regex': search, '$options': 'i'}},
                {'stock_receiver': {'$regex': search, '$options': 'i'}},
                {'order_number':   {'$regex': search, '$options': 'i'}},
                {'invoice_number': {'$regex': search, '$options': 'i'}},
                {'expiry_date':    {'$regex': search, '$options': 'i'}},
            ]
        # FIX (Performance): this route renders the same list template, so it
        # carried the same unbounded fetch. It only ever shows the first page
        # behind the edit form, so it is capped outright.
        page = _requested_page()
        total_lines = transactions.count_documents(base_query)
        pager = _pager(page, RECEIVE_PAGE_SIZE, total_lines)
        tx_list = list(transactions.find(base_query)
                       .sort('timestamp', -1)
                       .skip((pager['page'] - 1) * RECEIVE_PAGE_SIZE)
                       .limit(RECEIVE_PAGE_SIZE))

        rx_data = None
        message = None
        try:
            oid = ObjectId(receive_id)
            rx  = transactions.find_one({'_id': oid, 'type': 'receive'})
            if rx:
                rx_data = rx
                rx_data['receive_id'] = str(rx['_id'])
            else:
                return redirect(url_for('receive', start_date=start_date or '',
                                        end_date=end_date or '', search=search or ''))
        except (InvalidId, TypeError):
            return redirect(url_for('receive', start_date=start_date or '',
                                    end_date=end_date or '', search=search or ''))

        # The line's delivery, if it came in on one: supplier, order, invoice
        # and receiver belong to the delivery and are shown, not edited here.
        line_delivery = None
        if rx_data and rx_data.get('delivery_id'):
            line_delivery = _load_delivery(db['deliveries'], rx_data.get('delivery_id'))
        if rx_data:
            # Lines received before pack pricing carry a unit price only.
            rx_data.setdefault('pack_size', 1)
            if not rx_data.get('pack_price'):
                rx_data['pack_price'] = round((rx_data.get('price') or 0)
                                              * (rx_data.get('pack_size') or 1), 2)

        if request.method == 'POST' and rx_data:
            # Rewritten to match Add a Line. It used to ask for a schedule and a
            # unit price, re-date the receipt to the moment of the edit (moving
            # it into this month's figures), take the old quantity off the
            # shelf before checking the form (so a mistyped box left the
            # balance short), create a new medication from a mistyped name,
            # and let one line's supplier and invoice drift from its delivery.
            old_rx = rx_data
            problems = []
            med_name = (request.form.get('med_name') or '').strip()
            try:
                quantity = int(request.form.get('quantity') or '')
                if quantity < 1:
                    problems.append('the quantity must be at least 1')
            except ValueError:
                quantity = 0
                problems.append('the quantity must be a whole number of single units')
            try:
                pack_size = int(request.form.get('pack_size') or 1)
                if pack_size < 1:
                    problems.append('the pack size must be at least 1')
            except ValueError:
                pack_size = 1
                problems.append('the pack size must be a whole number')
            try:
                pack_price = float(request.form.get('pack_price') or '')
                if pack_price < 0:
                    problems.append('the price cannot be negative')
            except ValueError:
                pack_price = 0.0
                problems.append('enter the price per pack')
            batch = (request.form.get('batch') or '').strip()
            if not batch:
                problems.append('enter the batch')
            expiry_date = (request.form.get('expiry_date') or '').strip()
            try:
                datetime.strptime(expiry_date, '%Y-%m-%d')
            except ValueError:
                problems.append('enter a valid expiry date')
            new_med = medications.find_one({'name': med_name}) if med_name else None
            if not new_med:
                problems.append(f'"{med_name}" is not on the medication list - choose it '
                                f'from the suggestions')
            legacy: Doc = {}
            if not line_delivery:
                # A line from before deliveries keeps these on the line itself.
                for f in ('stock_receiver', 'order_number', 'supplier', 'invoice_number'):
                    legacy[f] = (request.form.get(f) or '').strip() or old_rx.get(f, '')
            old_med_name = old_rx['med_name']
            old_qty = old_rx.get('quantity', 0) or 0
            # Taking the stock back off cannot leave a negative shelf: if it
            # has been dispensed since, it has to be corrected at a stock take.
            if not problems and new_med is not None:
                if new_med['name'] == old_med_name:
                    shortfall = (new_med.get('balance', 0) or 0) - old_qty + quantity
                    if shortfall < 0:
                        problems.append(f'only {_num(new_med.get("balance", 0))} of '
                                        f'{old_med_name} is left on the shelf, so the '
                                        f'quantity cannot come down by {old_qty - quantity}')
                else:
                    om = medications.find_one({'name': old_med_name}) or {}
                    if (om.get('balance', 0) or 0) < old_qty:
                        problems.append(f'{old_med_name} has only {_num(om.get("balance", 0))} '
                                        f'left, so the {old_qty} received cannot be moved '
                                        f'to another item')
            if problems or new_med is None:
                message = 'Nothing was changed: ' + '; '.join(problems) + '.'
            else:
                price = _unit_price(pack_price, pack_size)
                line_value = quantity * pack_price / pack_size
                if new_med['name'] == old_med_name:
                    medications.update_one({'name': old_med_name},
                                           {'$inc': {'balance': quantity - old_qty}})
                else:
                    medications.update_one({'name': old_med_name},
                                           {'$inc': {'balance': -old_qty}})
                    medications.update_one({'name': med_name},
                                           {'$inc': {'balance': quantity}})
                # The item's current batch, expiry and price are this line's only
                # if it is the item's latest receipt; correcting an old line must
                # not put a finished batch back on file as the current one.
                latest = transactions.find_one({'type': 'receive', 'med_name': med_name},
                                               sort=[('timestamp', -1)])
                if not latest or latest['_id'] == old_rx['_id'] or \
                        (latest.get('timestamp') or datetime.min) <= (old_rx.get('timestamp') or datetime.min):
                    medications.update_one({'name': med_name}, {'$set': {
                        'batch': batch, 'price': price, 'expiry_date': expiry_date,
                        'pack_size': pack_size, 'pack_price': pack_price}})
                fields: Doc = {'med_name': med_name, 'quantity': quantity, 'batch': batch,
                               'price': price, 'pack_size': pack_size, 'pack_price': pack_price,
                               'line_value': line_value, 'expiry_date': expiry_date,
                               'schedule': new_med.get('schedule', 'not controlled'), **legacy}
                changes = {k: {'from': old_rx.get(k), 'to': v} for k, v in fields.items()
                           if k not in ('line_value', 'schedule') and old_rx.get(k) != v}
                transactions.update_one({'_id': old_rx['_id']}, {
                    '$set': {**fields, 'edited_by': current_user,
                             'edited_at': datetime.utcnow()},
                    '$push': {'corrections': {'at': datetime.utcnow(), 'by': current_user,
                                              'changes': changes}}})
                said = '; '.join(f"{k} {v['from']} -> {v['to']}" for k, v in changes.items()) \
                    or 'saved with no change'
                ref = old_rx.get('delivery_reference') or str(old_rx['_id'])
                write_audit_entry('UPDATE', 'receive', ref, f'line corrected: {said}')
                flash(f'{med_name}: receive line corrected successfully ({said}).', 'success')
                if line_delivery:
                    return redirect(url_for('receive', delivery_id=str(line_delivery['_id'])))
                return redirect(url_for('receive', start_date=start_date or '',
                                        end_date=end_date or '', search=search or ''))

        return render_template(
            'receive.html',
            tx_list=tx_list, nav_links=get_nav_links(),
            message=message, start_date=start_date, end_date=end_date,
            search=search, rx_data=rx_data, line_delivery=line_delivery,
            current_user=session['user']['name'],
            delivery=None, delivery_lines=[], recon={}, deliveries=[], order_summary=None,
            pager=pager, window_defaulted=False, unit='receipts'
        )
    except ServerSelectionTimeoutError:
        return "Database connection failed.", 500


@app.route('/delete-receive', methods=['POST'])
@login_required
def delete_receive() -> ResponseReturnValue:
    if session['user'].get('role') != 'admin':
        flash('Only admins can delete receive transactions.', 'error')
        return redirect(url_for('receive'))

    receive_id = request.form.get('receive_id')
    if not receive_id:
        flash('No transaction selected.', 'error')
        return redirect(url_for('receive'))

    try:
        oid = ObjectId(receive_id)
    except InvalidId:
        flash('Invalid receive ID.', 'error')
        return redirect(url_for('receive'))

    try:
        client = get_mongo_client()
        db = client['pharmacy_db']
        transactions = db['transactions']
        medications  = db['medications']

        rx = transactions.find_one({'_id': oid, 'type': 'receive'})
        if not rx:
            flash('Transaction not found.', 'error')
            write_app_warning(
                'transaction_not_found',
                f'Receive transaction "{receive_id}" not found when attempting to delete.',
                {'receive_id': receive_id}
            )
            return redirect(url_for('receive'))

        medications.update_one({'name': rx['med_name']}, {'$inc': {'balance': -rx['quantity']}})
        transactions.delete_one({'_id': oid})
        flash('Receive transaction deleted – stock reduced.', 'success')
    except Exception as e:
        flash(f'Delete failed: {str(e)}', 'error')

    return redirect(url_for('receive',
                            start_date=request.form.get('start_date'),
                            end_date=request.form.get('end_date'),
                            search=request.form.get('search')))


# -----------------------------------------------------------------------
# API endpoints
# -----------------------------------------------------------------------

REGISTER_CSV = {
    'audit': ('Audit Log', [
        ('timestamp', 'When'), ('user', 'User'), ('action', 'Action'),
        ('target_type', 'Record type'), ('target_id', 'Record'),
        ('changes', 'What changed'), ('ip', 'From')]),
    'key_register': ('Key Register', [
        ('ref', 'Ref'), ('issued_date', 'Issued'), ('issued_time', 'Time out'),
        ('recipient_name', 'Issued to'), ('recipient_role', 'Role'),
        ('purpose', 'Purpose'), ('also_on_duty', 'Also on duty'),
        ('returned_date', 'Returned'), ('returned_time', 'Time in'),
        ('returning_officer', 'Returned by'),
        ('receiving_officer', 'Received by'), ('pharmacist', 'Pharmacist'),
        ('comment', 'Comment'), ('user', 'Recorded by')]),
    'temperature': ('Temperature Register', [
        ('date', 'Date'), ('session', 'Round'), ('location', 'Room'),
        ('thermometer', 'Thermometer'), ('temp', 'Temperature C'),
        ('initials', 'Initials'), ('check_time', 'Write time'),
        ('user', 'Recorded by')]),
    'loans': ('Equipment Loans', [
        ('loan_ref', 'Ref'), ('item_code', 'Item'), ('item_type', 'Type'),
        ('patient_name', 'Patient'),
        ('patient_contact', 'Contact'), ('issued_date', 'Issued'),
        ('due_date', 'Due back'), ('returned_date', 'Returned'),
        ('days', 'Days out'), ('status', 'Status'),
        ('condition_out', 'Condition out'), ('condition_in', 'Condition back'),
        ('issued_by', 'Issued by'), ('note', 'Note')]),
    'stock_take': ('Stock Take', [
        ('med_name', 'Medication'), ('system_balance', 'On system'),
        ('counted', 'Counted'), ('variance', 'Variance'),
        ('expiry_before', 'Expiry was'), ('expiry_after', 'Expiry now'),
        ('batch_before', 'Batch was'), ('batch_after', 'Batch now'),
        ('counted_by', 'Counted by'), ('timestamp', 'When'),
        ('note', 'Note')]),
}


def _register_csv(kind: str, rows: Iterable[Doc]) -> tuple[str, str]:
    """(filename, csv text) for one of the registers."""
    title, columns = REGISTER_CSV[kind]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _f, label in columns])
    for row in rows or []:
        writer.writerow([_csv_value(row.get(f) if hasattr(row, 'get') else None)
                         for f, _lab in columns])
    stamp = local_now().strftime('%Y%m%d')
    return f"{title.lower().replace(' ', '-')}-{stamp}.csv", out.getvalue()


def _register_csv_response(kind: str, rows: Iterable[Doc]) -> Response:
    """The download itself.

    An empty register still hands over the file, with its column headings and
    no rows. The first version refused and flashed a message instead — but
    these pages do not all render flashed messages, so on the audit log the
    button did nothing at all and said nothing about why. A file that says
    "no rows" is honest; a button that silently does nothing is not.
    """
    filename, body = _register_csv(kind, rows)
    resp = make_response(body)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp






# ---------------------------------------------------------------------
# Getting everything out.
#
# Every other download in this app answers one question — a month's
# dispensing, a stock-on-hand list. None of them is the REGISTER. Years of
# dispensing, the controlled drug movements, the key register and the
# temperature record live in one database in one cluster, and a MongoDB
# Atlas free tier takes no backups of its own. If that cluster is lost, or
# the account lapses, the register goes with it and there is nothing on
# paper to go back to.
#
# So: one button that writes the whole thing to a file the pharmacy keeps.
# JSON Lines rather than CSV, because this is a backup and has to come back
# in — one document per line, the shape Mongo itself reads with
# `mongoimport`. The archive also carries a plain-English README, because
# the person who needs it will be having a bad day and may not be you.
# ---------------------------------------------------------------------
BACKUP_COLLECTIONS = [
    'medications', 'transactions', 'sales', 'counters', 'deliveries',
    'stock_takes', 'stock_take_counts', 'temperature_logs', 'users',
    'audit_log', 'app_warnings', 'error_logs',
]


def _json_safe(value: Any) -> Any:
    """Anything Mongo stores, written so it can be read back."""
    if isinstance(value, ObjectId):
        return {'$oid': str(value)}
    if isinstance(value, datetime):
        # Stored naive, in UTC, like everything else this app writes.
        return {'$date': value.replace(microsecond=0).isoformat() + 'Z'}
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


BACKUP_README = """The Healing Hub Pharmacy — full database backup
===================================

Taken : {when}
By    : {who}
Source: {db}

WHAT THIS IS
    Every record the pharmacy system holds, at the moment above. One file
    per collection. Each line of a .jsonl file is one record.

    This is the whole register: dispensing history, the controlled drug
    movements, deliveries, stock takes, the temperature record, the key
    register, equipment loans and the audit log.

WHAT IS IN IT
{counts}

KEEP IT SOMEWHERE ELSE
    A backup on the same machine as the thing it is backing up is not a
    backup. Put a copy somewhere that would survive losing this computer.

    Passwords are stored only as one-way hashes and cannot be read out of
    this file, but it still holds patient names and prescription details. Treat
    it exactly as you would the paper register: it is confidential.

PUTTING IT BACK
    Each file restores with mongoimport, one collection at a time:

        mongoimport --uri "<your connection string>" \
                    --collection transactions \
                    --file transactions.jsonl

    Restore into an EMPTY database first and check it before pointing the
    live app at it. --drop replaces a collection outright, so only reach
    for it when you are certain.
"""


def _backup_archive(db: Database, taken_by: str) -> tuple[str, bytes]:
    """(filename, bytes) — the whole database as a dated zip."""
    buf = io.BytesIO()
    counts: dict[str, Any] = {}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in BACKUP_COLLECTIONS:
            lines = []
            try:
                for doc in db[name].find():
                    lines.append(json.dumps(_json_safe(doc),
                                            ensure_ascii=False))
            except Exception as e:
                # One unreadable collection must not cost the pharmacy the
                # other fourteen. Say so in the archive rather than failing.
                app.logger.warning(f'backup: {name} unavailable: {e}')
                zf.writestr(f'{name}.FAILED.txt',
                            f'This collection could not be read: {e}\n')
                counts[name] = 'could not be read'
                continue
            counts[name] = len(lines)
            zf.writestr(f'{name}.jsonl', '\n'.join(lines) + ('\n' if lines else ''))
        when = local_now().strftime('%Y-%m-%d %H:%M')
        width = max((len(n) for n in counts), default=10)
        rows = '\n'.join(f'    {n.ljust(width)}  {counts[n]}'
                          for n in BACKUP_COLLECTIONS if n in counts)
        zf.writestr('README.txt', BACKUP_README.format(
            when=when, who=taken_by, db='pharmacy_db', counts=rows))
        zf.writestr('manifest.json', json.dumps({
            'taken_at': when, 'taken_by': taken_by,
            'database': 'pharmacy_db', 'counts': counts,
            'format': 'one JSON document per line (mongoimport)',
        }, indent=1))
    stamp = local_now().strftime('%Y%m%d-%H%M')
    return f'healing-hub-backup-{stamp}.zip', buf.getvalue()


@app.route('/backup', methods=['GET'])
@login_required
def backup_download() -> ResponseReturnValue:
    """The whole register, in one file, for the pharmacy to keep."""
    if session['user'].get('role') != 'admin':
        flash('Only an administrator can download the full backup.')
        return redirect(url_for('reports'))
    try:
        db = get_mongo_client()['pharmacy_db']
        taken_by = session['user']['name']
        filename, blob = _backup_archive(db, taken_by)
    except ServerSelectionTimeoutError:
        flash('Database connection failed, so no backup was taken. '
              'Nothing was changed.')
        return redirect(url_for('reports'))
    write_audit_entry('EXPORT', 'database_backup', filename,
                      f'Full database backup downloaded by {taken_by}.')
    resp = make_response(blob)
    resp.headers['Content-Type'] = 'application/zip'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp






MEDICATION_SEED_NAMES = [
    # Taken 2026-10-20 from the pharmacy's own Stock on Hand listing.
    '3HP 300/300mg',
    'ABC/3TC 600/300mg',
    'Acetylsalisylic Acid, 100 mg',
    'Acetylsalisylic Acid, 300 mg',
    'Activated Charcoal, 050 g',
    'Actrapid, 100 IU',
    'Acyclovir Cre 5 Perc, 010 mg',
    'Acyclovir, 800 mg',
    'Adalat, 030 mg',
    'Adcorectic, 050 mg',
    'Adenosine, 006 mg',
    'Adrenalin Hcl Inj, 001 mg',
    'Alcohol Swab',
    'Alcophyllex Syrup',
    'Alcophyllin Syrup',
    'Allopurinol, 100 mg',
    'Aminophyllin Injection, 250 mg',
    'Amiodarone, 006 mg',
    'Amitryptyline, 025 mg',
    'Amlodipine, 010 mg',
    'Amoxycillin Cap, 250 mg',
    'Amoxyclav Injection, 1200 mg',
    'Amoxyclav, 375 mg',
    'Amoxyclav, 625 mg',
    'Anti Haemorrhoidal Suppositories, 100 mg',
    'Anti Snake Bite Serum, 010 ml',
    'Antirubbies, 2.5 IU',
    'Arachis Oil, 020 ml',
    'Asccorbic Acid Tab - Chewable, 250 mg',
    'Atenolol, 050 mg',
    'Atenolol, 100 mg',
    'Atorvastatin, 010 mg',
    'Atropine Injection, 0.5 mg',
    'Azithromycin, 500 mg',
    'AZT/3TC 300/150mg',
    'Baclofen, 010 mg',
    'Beclomethasone Inhaler, 200 MID',
    'Benzathine Pen, 2.4 MU',
    'Benzoic Salicylic Ointment (Whitfield), 500 g',
    'Benzyl Benzoate, 100 ml',
    'Betamethasone Cream, 500 g',
    'Bisacodyl Tab, 005 mg',
    'Calamine Lotion, 100 ml',
    'Calcium Gluconate Tabs, 300 mg',
    'Captopril Tab, 050 mg',
    'Carbamazepine, 200 mg',
    'Carvedilol, 12.5mg',
    'Ceftriaxone Injection, 1000 mg',
    'Ceftriaxone, 250 mg',
    'Celebrex, 200 mg',
    'Cetrizine, 010 mg',
    'Chlopromazine, 025 mg',
    'Chloramphenicol Caps, 250 mg',
    'Chloramphenicol Eye Drops, 010 ml',
    'Chloramphenicol Eye Oint, 005 g',
    'Chlorhexide Mouth Wash, 100 ml',
    'Chloro Ear Drops, 020 ml',
    'Chlorpheniramine Tabs, 004 mg',
    'Cimetidine Injection, 200 mg',
    'Cimetidine Tabs, 200 mg',
    'Cimetidine tabs, 400 mg',
    'Cipro Eye Drops, 010 ml',
    'Ciprofloxacin, 500 mg',
    'Clarythromycin, 500 mg',
    'Clinical Thermometer',
    'Clinical Thermometer Digita',
    'Clopidogrel, 075 mg',
    'Clotrimazole Cre Vaginal, 010 mg',
    'Clotrimazole Pess, 100 mg',
    'Clotrimazole Topical Cre 1%, 020 g',
    'Cloxacillin Caps, 250 mg',
    'Cloxacillin Injection, 250 mg',
    'Colchicine Tabs, 0.5 mg',
    'Combined Pill',
    'Cotrimoxazole, 480 mg',
    'Cotrimoxazole, 960 mg',
    'Cotton Buds',
    'Cotton Wool',
    'Crepe Bandage 150mm',
    'Crepe Bandage 50mm',
    'Crepe Bandage 75mm',
    'CTX 960mg',
    'Cyproheptadine, 004 mg',
    'Deep Freeze Spray, 050 ml',
    'Dexaethasone Eye Drops, 002 ml',
    'Dexamethasone Injection, 004 mg',
    'Dextrose Injection, 050 %',
    'Diazepam Injection, 010 mg',
    'Diazepam Tabs, 005 mg',
    'Diclofenac Gel, 050 g',
    'Diclofenac Injection, 075 mg',
    'Diclofenac Tab, 025 mg',
    'Diclofenac Tab, 050 mg',
    'Digoxin, 0.250 mg',
    'Diphenhydramine Syrup, 100 ml',
    'Dopamine Injection, 010 mg',
    'Doxycycline Tabs, 100 mg',
    'Dressing Pack',
    'DTG 50mg',
    'Dynexan Oral Gel, 010 mg',
    'Elasto-Plast',
    'Emergency Pill',
    'Emergency Pill, 002 mg',
    'Enalapril tabs, 005 mg',
    'Enalapril tabs, 010 mg',
    'Enalapril tabs, 20 mg',
    'Ergometrine Injection, 0.5 mg',
    'Erythromycin tabs, 250 mg',
    'Examination Gloves L',
    'Examination Gloves M',
    'Fentanyl, 100 mcg',
    'Ferrous Sulphate Tabs, 200 mg',
    'Fertomid, 050 mg',
    'Flagyl Injection, 400 mg',
    'Flu Stat, 200 mg',
    'Fluconazole, 200 mg',
    'FluoxetineCaps, 020 mg',
    'Fml Neo Opd, 005 ml',
    'Folic Acid Tabs, 005 mg',
    'Freestyle Optium Test Strip',
    'Furosemide Injection, 020 mg',
    'Furosemide Tabs, 040 mg',
    'Gabapentine, 100 mg',
    'Gauze Swab',
    'Gentamycin Injection, 040 mg',
    'Glibenclamide Tabs, 005 mg',
    'Gliclazide, 080 mg',
    'Glucose Powder, 500 g',
    'Glycerine Supp, 100 mg',
    'Griseofulvin Tabs, 500 mg',
    'Guafenesin Xl 60, 100 ml',
    'Gv Paint, 020 ml',
    'Haloperidol Injection, 002 mg',
    'Haloperidol Tabs, 1.5 mg',
    'Heparine, 1000 SIU',
    'Histacon Caps, 200 mg /2mg',
    'Hydralazine Hcl Tabs, 010 mg',
    'Hydralazine Hcl Tabs, 050 mg',
    'Hydrochlorothiazide Tabs, 025 mg',
    'Hydrocortisone Cream, 500 g',
    'Hydrocortisone Injection, 100 mg',
    'Hyoscine Injection, 020 mg',
    'Hyoscine Tabs, 010 mg',
    'Ibuprofen Tab, 200 mg',
    'Ibuprofen Tab, 400 mg',
    'Ichthammol Ointment, 500 g',
    'Imipramine, 010 mg',
    'Indapamide Tabs, 0.5 mg',
    'Indomethacin Caps, 025 mg',
    'Insulin Hm Injection, 100 U 10 Ml',
    'Isosorbide Trinitrate, 005 mg',
    'Ketamine Injection, 050 mg (Ml)',
    'Keteconazole, 200 mg Tabs',
    'Labetolol, 5mg',
    'Lactulose, 150 ml',
    'Lancets',
    'Levonorgestrel 0.03mg',
    'Levonorgestrel 75mg Implants',
    'Lignocaine Injection, 002 %',
    'Lignocaine Spray, 050 ml',
    'Linen Saver',
    'Liquid Paraffin, 100 ml',
    'Loperamide Tabs, 002 mg',
    'Loratadine, 010 mg',
    'Losartan, 050 mg',
    'Losartan, 100 mg',
    'LPVr 200/50mg',
    'Lubrucating Gel, 050 g',
    'Magnesium Suphate injection, 010 mg',
    'Male Condoms',
    'Mannitol, 020 %',
    'Mayogel Suspension',
    'Mebendazole, 100 mg Tabs',
    'Medicine Bottles',
    'Medroxyprogesterone 150mg',
    'Mefenamic Acid, 250 mg',
    'Mepyramine Cream, 025 g',
    'Mercurochrome Paint, 020 ml',
    'Metformin Tabs, 500 mg',
    'Metformin Tabs, 850 mg',
    'Methotrexate, 005 mg',
    'Methylcellulose Eye Drops, 010 ml',
    'Methylprednisone Injection, 040 mg',
    'Methylsal Ointment, 500 mg',
    'Metoclopramide Injection, 010 mg',
    'Metoclopramide Tabs, 010 mg',
    'Metronidazole tabs, 400 mg',
    'Miconazol Oral Gel, 030 g',
    'Miconazole Cream, 002 %',
    'Midazolam, 010 mg',
    'Migril, 002 mg',
    'Mist Alba Susp, 100 ml',
    'Mmt, 250 mg',
    'Morphine Injection, 010 mg',
    'Morphine tabs, 10mg',
    'Multivitamin Tabs, 0.25 mg',
    'Mybulen, 200 mg',
    'N95 Masks',
    'Naloxone, 0.4 mg',
    'Nasal Drops- Oxymetazoline, 005 ml',
    'Needles 21g',
    'Neurobion Tabs, 200 mg',
    'Nifedipine, 005 mg',
    'Nifedipine, 010 mg',
    'Nitrofurantoin, 100 mg',
    'Nitrofurazone Ointment, 500 g',
    'Nitrolingual Spray, 020 ml',
    'Norflex Co Tabs, 375 mg',
    'Normal Saline 100ml',
    'Normal Saline 1L',
    'Nystatin Ointment, 020 g',
    'Nystatin Oral Susp, 1000 u',
    'Nystatin Vaginal Pess, 100 mg',
    'Ointment Jars',
    'Omeprazole Tabs, 020 mg',
    'Oral Rehydration Salts, 002 g',
    'Osteoeze Gold, 200 mg',
    'Oxytocin Injection, 010 mg',
    'Pain Relief Gel, 020 g',
    'PanaCod Tab, 500 mg',
    'Paper Bags L',
    'Paracetamol tabs, 500 mg',
    'Pen Light Torch',
    'Pen Vk Tab, 250 mg',
    'Pentaprazole Injection, 040 mg',
    'Perfulgan, 001 g',
    'Pethedine Injection, 050 mg',
    'Pethedine Injection, 100 mg',
    'Phenytoin Injection, 200 mg',
    'Phernobabitol tabs, 020 mg',
    'Plasma Expander',
    'Podophylline Paint, 020 ml',
    'POP 75mm',
    'Potassium Chloride Injection',
    'Potassium Chloride tabs, 600 mg',
    'Potassium Citrate, 100 ml',
    'Povidone Iodine Solution',
    'Povidone Ointment, 500 mg',
    'Pravastatin tabs, 020 mg',
    'Prednisone Tab, 005 mg',
    'Probanthine Tabs, 015 mg',
    'Prochlorperazine Injection, 005 mg',
    'Prochlorperazine Tabs, 005 mg',
    'Promethazine Injection, 050 mg',
    'Promethazine Tabs, 025 mg',
    'Propranolol Tabs, 010 mg',
    'Propranolol Tabs, 040 mg',
    'Pyridoxine, 025 mg',
    'Pyridoxine, 25 mg Donor-Funded',
    'Ranitidine, 150 mg',
    'RH 150/75mg',
    'RHE 150/75/275mg',
    'RHZE 150/75/400/275mg',
    'Rocuronium injection, 010 mg',
    'Salbutamol Inhaler, 200 MID',
    'Salbutamol, 004 mg Tablets',
    'Sanitary Pads',
    'Savlon',
    'Selenium Tab, 100 mg',
    'Sharps Container',
    'Sildenafil, 050 mg',
    'Simvastatin, 020 mg',
    'Sinucon Tab, 200 mg',
    'Sodium Bicarbonate, 050 ml',
    'Sodium Valproate, 200 mg',
    'Spersallerg Opd, 010 ml',
    'Spironolactone tabs, 025 mg',
    'Sterile Trocar',
    'Suppositories Indocid (Arthrexin), 100 mg',
    'Surgical Masks',
    'Suxamethonium injection, 010 mg',
    'Syringes 3ml',
    'TDF/3TC 300/300mg',
    "TDF/3TC/DTG 300/300/50mg 30's",
    "TDF/3TC/DTG 300/300/50mg 90's",
    'Tetanus Toxoid Vaccine, 010 mg',
    'Tetracycline Ointment, 003 % 25G',
    'Tetracycline Opthal Ointment, 020 g',
    'Thiamine Injection',
    'Throat Lozenges, 250 mg',
    'Thymol Glycerine, 100 ml',
    'Tongue Depressors',
    'Tramadol Injection, 100 mg',
    'Tramadol Tabs, 050 mg',
    'Tranexamic Acid Injection, 500 mg',
    'Tranexamic Acid tabs, 500 mg',
    'Trifen Adult, 100 ml',
    'Tumsulosin, 0.5 mg',
    'Urirex K, 050 mg',
    'Venteze Resp.Sol, 005 mg 20ml',
    'Vitamin B Co Injection, 001 mg',
    'Vitamin B Co Tablets, 001 mg',
    'Vitamin B12, 002 mg',
    'Vitamin E Cream, 500 g',
    'Vitamin K Injection (Konakion), 001 mg',
    'Warfarin Tabs, 005 mg',
    'Water For Injection, 010 ml',
    'Zinc Oxide Ointment, 030 mg',
    'Zinc Tablets, 020 mg',
    'ZnO Plater 100mm',
    'ZnO Plater 75mm',
    'Zuvamor, 040 mg',
]

@app.route('/api/session-ping', methods=['POST'])
@login_required
def session_ping() -> ResponseReturnValue:
    """Tell the server the user is still here.

    The timeout is meant to measure INACTIVITY, but the only activity it
    could see was a page request. Filling in a long dispensing form makes no
    requests at all, so twenty minutes of typing looked exactly like twenty
    minutes of an empty room — and the submit at the end landed on the login
    page with the work lost.

    The page calls this only when the user actually interacts with it, so an
    unattended terminal still times out as intended. The before_request hook
    has already refreshed the activity stamp by the time this runs; all this
    has to do is answer.
    """
    return jsonify({'ok': True, 'timeout_minutes': SESSION_TIMEOUT_MINUTES})


@app.route('/api/medication-defaults', methods=['GET'])
@login_required
def get_medication_defaults() -> ResponseReturnValue:
    """Pack size and schedule for each stocked item, keyed by name.

    These describe the PRODUCT, so they are the same on every delivery and
    there is no reason to retype them. Price is deliberately not included:
    that describes this particular purchase, and pre-filling it would invite
    somebody to accept last month's figure without reading the invoice.

    Leaving pack size to be retyped was not merely tedious — the field resets
    to 1, and a pack size of 1 values a pack-priced item at the pack price per
    unit, which is the inflation this app was already fixed for once.
    """
    try:
        db = get_mongo_client()['pharmacy_db']
        out = {}
        for m in db['medications'].find(
                {}, {'_id': 0, 'name': 1, 'pack_size': 1, 'schedule': 1}):
            name = m.get('name')
            if not name:
                continue
            out[name] = {'pack_size': m.get('pack_size') or '',
                         'schedule': m.get('schedule') or ''}
        return jsonify(out)
    except Exception as e:
        app.logger.warning(f'medication defaults failed: {e}')
        return jsonify({})


def _medication_names() -> list[str]:
    """Every medication on file, for the name suggestions.

    Asked of the medications collection rather than kept in a list here, so
    that adding an item under Add Medication makes it suggestable everywhere
    at once and nothing has to be edited by hand. A name that is suggested is
    therefore a name that exists, which is the whole point: the old list could
    offer "Morpine tabs, 10mg" long after the register had been corrected to
    "Morphine", and the dispense only failed at the end.

    Zero-balance items are included on purpose. Dispensing one records the
    shortfall as unmet demand, which is a figure the pharmacy orders against —
    hiding those names would quietly stop that being recorded.
    """
    try:
        db = get_mongo_client()['pharmacy_db']
        names = db['medications'].distinct('name')
        out = sorted((n.strip() for n in names if n and str(n).strip()),
                     key=lambda t: t.lower())
        # Only while nothing is on file: a brand-new pharmacy gets something
        # to type against rather than a blank box.
        return out or sorted(MEDICATION_SEED_NAMES, key=lambda t: t.lower())
    except Exception as e:
        app.logger.warning(f'medication name list unavailable: {e}')
        # Free typing still works, and the server checks the name either way.
        return []


@app.route('/api/stock', methods=['GET'])
@login_required
def api_stock() -> ResponseReturnValue:
    """One item's balance as it stands now, for the dispensing form.

    Asked for each time an item is picked rather than loaded with the page,
    so a dispense made at the next counter a minute ago is already counted.
    """
    name = (request.args.get('name') or '').strip()
    if not name:
        return jsonify({'found': False})
    try:
        med = get_mongo_client()['pharmacy_db']['medications'].find_one(
            {'name': name}, {'_id': 0, 'balance': 1, 'expiry_date': 1})
    except Exception as e:
        app.logger.warning(f'stock lookup failed: {e}')
        return jsonify({'found': False, 'error': True})
    if not med:
        return jsonify({'found': False})
    exp = _expiry_iso(med.get('expiry_date')) if med.get('expiry_date') else None
    days = None
    if exp:
        try:
            days = (datetime.strptime(exp, '%Y-%m-%d').date() - local_now().date()).days
        except ValueError:
            days = None
    return jsonify({'found': True, 'balance': _num(med.get('balance', 0) or 0),
                    'expiry': exp, 'expiry_days': days})


@app.route('/api/medications', methods=['GET'])
@login_required
def get_medication_options() -> ResponseReturnValue:
    """The medication names to suggest, as JSON.

    Templates fetch this once per page load and cache it in JS module scope,
    so a medication added in one tab appears in the next page that loads.
    """
    return jsonify(_medication_names())


# ---------------------------------------------------------------------------
# Public product feed for the Healing Hub website.
#
# The only page in this app that needs no login, so it says as little as it
# can: no stock quantities (only in stock / out of stock), no costs, and no
# price for anything sold on prescription. Only items an admin has ticked
# "Show on website" appear. The website caches it for a few minutes, so a
# busy day of browsing costs the database almost nothing.
# ---------------------------------------------------------------------------
PUBLIC_FEED_MAX_AGE = 300


@app.route('/api/public/products', methods=['GET'])
def public_products() -> ResponseReturnValue:
    try:
        meds = get_mongo_client()['pharmacy_db']['medications'].find(
            _active({'on_website': True}),
            {'_id': 0, 'name': 1, 'web_category': 1, 'description': 1,
             'selling_price': 1, 'rx_only': 1, 'schedule': 1, 'balance': 1})
        items = []
        for m in _by_name(meds):
            rx = bool(m.get('rx_only')) or m.get('schedule') == 'controlled'
            price = _money(m.get('selling_price'))
            items.append({
                'name': m['name'],
                'category': m.get('web_category') or (WEB_CATEGORIES[0] if rx else WEB_CATEGORIES[1]),
                'description': m.get('description') or '',
                'prescription_only': rx,
                'price': None if rx or not price else price,
                'in_stock': (m.get('balance', 0) or 0) > 0,
            })
        body = {'currency': 'LSL', 'updated': datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
                'categories': WEB_CATEGORIES, 'products': items}
        resp = jsonify(body)
        resp.headers['Cache-Control'] = f'public, max-age={PUBLIC_FEED_MAX_AGE}'
    except Exception as e:
        app.logger.warning(f'public product feed failed: {e}')
        resp = jsonify({'error': 'Product list unavailable'})
        resp.status_code = 503
    resp.headers['Access-Control-Allow-Origin'] = os.environ.get('WEBSITE_ORIGIN', '*')
    return resp


# FIX (Bug, regression): init_audit() must run at module level, not inside
# `if __name__ == '__main__':`. Both gunicorn (Render) and Vercel's Python
# runtime import this module and use the `app` object directly — neither
# executes app.py as a script, so anything gated behind that guard never
# runs in production. Without this, the audit decorators never get attached
# to dispense/receive/medication routes and nothing is ever written to
# audit_log, even though everything looks fine locally with `python app.py`.
try:
    from audit_logger import init_audit
    init_audit(app)
except Exception as e:
    app.logger.error(f"Failed to load audit logger: {e}")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
