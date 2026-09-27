"""
app.py
------
Web layer (Flask). Ties the database + algorithms modules together and
exposes them as a browser-based system, per the brief's "running as a
web-based system" requirement.

Routes:
    GET  /                 -> dashboard: visual slot display + entry form
    GET/POST /login         -> staff login (single shared password)
    POST /logout            -> staff logout
    POST /entry              -> Vehicle Entry module (park a car) [staff only]
    GET  /checkout           -> preview fee for a plate before paying
    POST /checkout/confirm   -> Vehicle Exit + Payment (cash) + Barrier [staff only]
    POST /checkout/mpesa     -> Vehicle Exit + Payment (Lipa Na M-Pesa) - sends STK push [staff only]
    GET  /checkout/status/<id> -> polled by the waiting screen for the STK push result
    POST /mpesa/callback     -> Safaricom's Daraja API posts the payment result here
    GET  /receipt/<id>       -> receipt for any completed session, cash or M-Pesa
    GET  /history            -> log of completed sessions (from the DB)
"""

try:
    from dotenv import load_dotenv
    load_dotenv()  # picks up MPESA_*/STAFF_PASSWORD variables from .env, if present
except ImportError:
    pass

import os
from functools import wraps
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session

import mpesa
from database import init_db, get_conn
from algorithms import SlotManager

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

# Single shared password for attendants - simple by design (the brief
# doesn't call for individual staff accounts). Set STAFF_PASSWORD in .env
# to something real before this goes anywhere near production.
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "masembo123")


def login_required(view):
    """Guards actions that change state (recording entries, taking
    payment). Viewing the dashboard, fee preview and history stays public -
    only the actual "do something" routes need a logged-in attendant."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_staff"):
            flash("Please log in as staff to do that.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

# One SlotManager instance shared for the app's lifetime - it is the live
# in-memory brain of the car park, backed by SQLite (see algorithms.py).
init_db()
manager = SlotManager()


@app.route("/")
def dashboard():
    slots = manager.get_slot_display()
    return render_template(
        "index.html",
        slots=slots,
        available=manager.available_count(),
        total=len(slots),
        is_staff=session.get("is_staff", False),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == STAFF_PASSWORD:
            session["is_staff"] = True
            flash("Logged in as staff.", "success")
            return redirect(next_url)
        flash("Incorrect password.", "error")
    return render_template("login.html", next=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("is_staff", None)
    flash("Logged out.", "success")
    return redirect(url_for("dashboard"))


@app.route("/entry", methods=["POST"])
@login_required
def entry():
    plate = request.form.get("plate_number", "")
    if not plate.strip():
        flash("Please enter a number plate.", "error")
        return redirect(url_for("dashboard"))

    ok, result = manager.park_vehicle(plate)
    if ok:
        flash(f"Vehicle {plate.strip().upper()} parked in slot {result}.", "success")
    else:
        flash(result, "error")
    return redirect(url_for("dashboard"))


@app.route("/checkout", methods=["GET"])
def checkout_preview():
    plate = request.args.get("plate_number", "")
    preview = manager.preview_fee(plate) if plate else None
    if plate and not preview:
        flash("No active session found for that plate.", "error")
    return render_template(
        "checkout.html", preview=preview, plate=plate, is_staff=session.get("is_staff", False)
    )


@app.route("/checkout/confirm", methods=["POST"])
@login_required
def checkout_confirm():
    plate = request.form.get("plate_number", "")
    ok, result = manager.checkout_vehicle(plate)
    if not ok:
        flash(result, "error")
        return redirect(url_for("dashboard"))
    return render_template("receipt.html", receipt=result, is_staff=True)


@app.route("/checkout/mpesa", methods=["POST"])
@login_required
def checkout_mpesa():
    """Starts the Lipa Na M-Pesa Online flow: sends an STK push to the
    driver's phone and shows a waiting screen while we wait for Safaricom
    to call back with the result."""
    plate = request.form.get("plate_number", "")
    phone = request.form.get("phone_number", "")

    if not phone.strip():
        flash("Enter the phone number to send the M-Pesa prompt to.", "error")
        return redirect(url_for("checkout_preview", plate_number=plate))

    # Short stays are free per the fee tiers - nothing to charge, so skip
    # M-Pesa entirely and just complete the checkout as normal, without
    # ever tagging the session as an M-Pesa payment attempt.
    preview = manager.preview_fee(plate)
    if not preview:
        flash("No active session found for this plate.", "error")
        return redirect(url_for("dashboard"))
    if preview["fee"] <= 0:
        ok, result = manager.checkout_vehicle(plate)
        if ok:
            return render_template("receipt.html", receipt=result, is_staff=True)
        flash(result, "error")
        return redirect(url_for("dashboard"))

    initiated = manager.initiate_mpesa_checkout(plate, phone)
    if not initiated:
        flash("No active session found for this plate.", "error")
        return redirect(url_for("dashboard"))
    session_id, fee, minutes = initiated

    ok, result = mpesa.stk_push(
        phone_number=phone,
        amount=fee,
        account_reference=plate.strip().upper(),
        transaction_desc="Masembo Parking Fee",
    )
    if not ok:
        flash(f"Could not send the M-Pesa prompt: {result}", "error")
        return redirect(url_for("checkout_preview", plate_number=plate))

    checkout_request_id = result
    manager.attach_checkout_request_id(session_id, plate.strip().upper(), checkout_request_id)

    return render_template(
        "mpesa_wait.html",
        plate=plate.strip().upper(),
        phone=phone,
        amount=fee,
        session_id=session_id,
    )


@app.route("/checkout/status/<int:session_id>")
def checkout_status(session_id):
    """Polled by the waiting screen's JavaScript every few seconds."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, payment_status FROM sessions WHERE id=?", (session_id,)
        ).fetchone()

    if not row:
        return jsonify(status="error", message="Session not found."), 404

    if row["status"] == "completed" and row["payment_status"] == "paid":
        return jsonify(status="paid", redirect=url_for("view_receipt", session_id=session_id))
    if row["payment_status"] == "failed":
        return jsonify(status="failed")
    return jsonify(status="pending")


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    """Safaricom posts the STK push result here. This URL must be a public
    HTTPS address (set as MPESA_CALLBACK_URL) - use ngrok in development."""
    data = request.get_json(silent=True) or {}
    stk = data.get("Body", {}).get("stkCallback", {})
    checkout_request_id = stk.get("CheckoutRequestID")
    result_code = stk.get("ResultCode")

    if not checkout_request_id:
        return jsonify(ResultCode=1, ResultDesc="Missing CheckoutRequestID"), 400

    if result_code == 0:
        mpesa_receipt = None
        for item in stk.get("CallbackMetadata", {}).get("Item", []):
            if item.get("Name") == "MpesaReceiptNumber":
                mpesa_receipt = item.get("Value")
        manager.complete_mpesa_checkout(checkout_request_id, mpesa_receipt)
    else:
        # ResultCode != 0 covers "cancelled by user", "insufficient funds",
        # timeouts, etc. - Safaricom's ResultDesc explains which.
        manager.mark_mpesa_failed(checkout_request_id)

    # Safaricom expects this exact acknowledgement shape, regardless of
    # whether the underlying payment succeeded or failed.
    return jsonify(ResultCode=0, ResultDesc="Accepted")


@app.route("/receipt/<int:session_id>")
def view_receipt(session_id):
    """Renders the receipt for any completed session - reached once an
    M-Pesa payment is confirmed via the waiting screen's polling."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.plate_number, sl.slot_number, s.entry_time, s.exit_time,
                   s.fee, s.payment_method, s.mpesa_receipt
            FROM sessions s
            JOIN slots sl ON sl.id = s.slot_id
            WHERE s.id = ? AND s.status = 'completed'
            """,
            (session_id,),
        ).fetchone()

    if not row:
        flash("Receipt not found, or payment not completed yet.", "error")
        return redirect(url_for("dashboard"))

    entry_time = datetime.fromisoformat(row["entry_time"])
    exit_time = datetime.fromisoformat(row["exit_time"])
    receipt = {
        "plate_number": row["plate_number"],
        "slot_number": row["slot_number"],
        "entry_time": entry_time,
        "exit_time": exit_time,
        "minutes": round((exit_time - entry_time).total_seconds() / 60, 1),
        "fee": row["fee"],
        "payment_method": row["payment_method"],
        "mpesa_receipt": row["mpesa_receipt"],
        "barrier": "OPEN",
    }
    return render_template("receipt.html", receipt=receipt, is_staff=session.get("is_staff", False))


@app.route("/history")
def history():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.plate_number, sl.slot_number, s.entry_time, s.exit_time, s.fee,
                   s.status, s.payment_method
            FROM sessions s
            JOIN slots sl ON sl.id = s.slot_id
            ORDER BY s.id DESC
            LIMIT 100
            """
        ).fetchall()
    return render_template("history.html", rows=rows, is_staff=session.get("is_staff", False))


if __name__ == "__main__":
    # host=0.0.0.0 so it's reachable on a local network (e.g. a barrier
    # kiosk / display screen at the gate), debug=True for development only.
    app.run(host="0.0.0.0", port=5000, debug=True)
