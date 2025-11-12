# error_logger.py
"""
Separate error-logging module.
Place this file in the project root and add the following line at the very top of app.py
(after the imports but before any routes are defined):

    from error_logger import init_error_logging
    init_error_logging(app)

That's it – every crash will now be recorded in errors.log **and** in MongoDB.
"""

import logging
import traceback
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from flask import request, jsonify, render_template_string
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

# --------------------------------------------------------------------------- #
# Configuration (adjust if you keep the file elsewhere)
# --------------------------------------------------------------------------- #
LOG_FILE = "errors.log"                     # will be created in the root folder
MONGO_URI = None                            # will be read from env, fallback to app config
ERROR_COLLECTION = "error_logs"

# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _get_mongo_client(app):
    """Lazy MongoDB client – re-uses the same connection logic as the main app."""
    uri = MONGO_URI or app.config.get("MONGODB_URI") or "mongodb://localhost:27017/"
    return MongoClient(uri, serverSelectionTimeoutMS=36000)

def _log_to_file(logger, exc_info):
    """Write a nicely formatted traceback to the rotating log file."""
    logger.error(
        "=== UNHANDLED EXCEPTION ===\n"
        "Timestamp: %s\n"
        "URL: %s %s\n"
        "Remote: %s\n"
        "User-Agent: %s\n"
        "Form: %s\n"
        "JSON: %s\n"
        "Traceback:\n%s",
        datetime.utcnow().isoformat(),
        request.method,
        request.url,
        request.remote_addr,
        request.headers.get("User-Agent", ""),
        request.form.to_dict(),
        request.get_json(silent=True) or {},
        "".join(traceback.format_exception(*exc_info)),
    )

def _log_to_mongo(db, exc_info):
    """Persist the same data in MongoDB for later analysis / UI."""
    try:
        error_doc = {
            "timestamp": datetime.utcnow(),
            "method": request.method,
            "url": request.url,
            "remote_addr": request.remote_addr,
            "user_agent": request.headers.get("User-Agent"),
            "form": request.form.to_dict(),
            "json": request.get_json(silent=True) or {},
            "traceback": traceback.format_exception(*exc_info),
            "path": request.path,
            "endpoint": request.endpoint,
        }
        db[ERROR_COLLECTION].insert_one(error_doc)
    except Exception as mongo_err:   # never let a logging error crash the app
        app.logger.warning(f"Failed to write error to MongoDB: {mongo_err}")

# --------------------------------------------------------------------------- #
# Flask error-handler registration
# --------------------------------------------------------------------------- #
def init_error_logging(flask_app):
    """Call this once with your Flask `app` object."""
    global app
    app = flask_app

    # ---- 1. File logger (daily rotation, keep 30 days) ----
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=30)
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger = logging.getLogger("error_logger")
    logger.setLevel(logging.ERROR)
    logger.addHandler(file_handler)

    # ---- 2. Flask 500 handler ----
    @flask_app.errorhandler(Exception)
    def handle_uncaught_exception(error):
        # `error` may be the original exception or a wrapped one – get the real exc_info
        exc_type, exc_value, exc_tb = (
            type(error),
            error,
            error.__traceback__,
        ) if hasattr(error, "__traceback__") else (None, None, None)

        # Log to file
        _log_to_file(logger, (exc_type, exc_value, exc_tb))

        # Log to MongoDB (fire-and-forget)
        try:
            client = _get_mongo_client(flask_app)
            db = client["pharmacy_db"]
            _log_to_mongo(db, (exc_type, exc_value, exc_tb))
        except ServerSelectionTimeoutError:
            logger.warning("MongoDB unavailable while logging error.")
        finally:
            try:
                client.close()
            except Exception:
                pass

        # ---- 3. User-friendly response ----
        if request.path.startswith("/api/") or request.headers.get("Accept") == "application/json":
            return (
                jsonify(
                    {
                        "error": "Internal Server Error",
                        "message": "An unexpected error occurred. It has been logged.",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                ),
                500,
            )
        else:
            # Simple HTML fallback (you can replace with a custom template)
            html = f"""
            <h1>500 – Internal Server Error</h1>
            <p>Something went wrong. The incident has been recorded (ID: {datetime.utcnow().strftime('%Y%m%d%H%M%S')}).</p>
            <p><a href="javascript:window.history.back()">Go back</a> or <a href="/">return home</a>.</p>
            """
            return render_template_string(html), 500

    # ---- 4. Optional: also capture Flask's built-in 500 page (debug=False) ----
    @flask_app.errorhandler(500)
    def internal_server_error(e):
        # Flask already logged the error; we just forward to our handler
        return handle_uncaught_exception(e)

    # ---- 5. Explicit 404 (nice JSON for APIs) ----
    @flask_app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not Found"}), 404
        return e.get_response(), 404

    # Success – the app now has robust error capture
    flask_app.logger.info("Error-logging middleware initialised (file + MongoDB).")
