"""
mpesa.py
--------
Lipa Na M-Pesa Online (STK Push) integration using Safaricom's Daraja API.

Flow:
    1. Driver reaches the exit barrier and enters their M-Pesa phone number.
    2. stk_push() asks Safaricom to prompt that phone for payment.
    3. The driver enters their M-Pesa PIN on their own phone.
    4. Safaricom calls our /mpesa/callback route with the result, and that
       route finishes the checkout (frees the slot, opens the barrier).

Configuration - set these as environment variables (see .env.example):
    MPESA_ENV               "sandbox" (default) or "production"
    MPESA_CONSUMER_KEY      from your Daraja app
    MPESA_CONSUMER_SECRET   from your Daraja app
    MPESA_SHORTCODE         Paybill/Till number (sandbox default: 174379)
    MPESA_PASSKEY           Lipa Na M-Pesa passkey
    MPESA_CALLBACK_URL      a PUBLIC https URL Safaricom can reach
                             (use `ngrok http 5000` in development)

Nothing here ever logs or stores the driver's M-Pesa PIN - Safaricom
collects that directly on the driver's own phone, never through this app.
"""

import base64
import os
from datetime import datetime

import requests

MPESA_ENV = os.environ.get("MPESA_ENV", "sandbox")
BASE_URL = (
    "https://sandbox.safaricom.co.ke"
    if MPESA_ENV == "sandbox"
    else "https://api.safaricom.co.ke"
)

CONSUMER_KEY = os.environ.get("MPESA_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "")
SHORTCODE = os.environ.get("MPESA_SHORTCODE", "174379")  # Daraja sandbox default
PASSKEY = os.environ.get("MPESA_PASSKEY", "")
CALLBACK_URL = os.environ.get("MPESA_CALLBACK_URL", "")


class MpesaError(Exception):
    """Raised when M-Pesa isn't configured or can't be reached."""


def is_configured() -> bool:
    return bool(CONSUMER_KEY and CONSUMER_SECRET and PASSKEY and CALLBACK_URL)


def _get_access_token() -> str:
    if not CONSUMER_KEY or not CONSUMER_SECRET:
        raise MpesaError(
            "M-Pesa credentials are not set. Add MPESA_CONSUMER_KEY and "
            "MPESA_CONSUMER_SECRET to your .env file."
        )
    resp = requests.get(
        f"{BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
        auth=(CONSUMER_KEY, CONSUMER_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _format_phone(phone_number: str) -> str:
    """Normalise any local format to the 2547XXXXXXXX Daraja expects."""
    digits = "".join(ch for ch in phone_number if ch.isdigit())
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif digits.startswith("7") or digits.startswith("1"):
        digits = "254" + digits
    elif digits.startswith("254"):
        pass
    return digits


def stk_push(phone_number: str, amount: float, account_reference: str,
             transaction_desc: str = "Masembo Parking Fee"):
    """
    Triggers the STK push prompt on the driver's phone.

    Returns (True, checkout_request_id) on success,
            (False, error_message) on failure.
    """
    if not is_configured():
        return False, (
            "M-Pesa is not configured on this server. Set MPESA_CONSUMER_KEY, "
            "MPESA_CONSUMER_SECRET, MPESA_PASSKEY and MPESA_CALLBACK_URL."
        )

    try:
        token = _get_access_token()
    except (requests.RequestException, MpesaError) as exc:
        return False, f"Could not reach M-Pesa: {exc}"

    phone = _format_phone(phone_number)
    if len(phone) != 12:
        return False, "Enter a valid Safaricom number, e.g. 0712345678."

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        f"{SHORTCODE}{PASSKEY}{timestamp}".encode()
    ).decode()

    # Daraja rejects an amount of 0, but the parking system's own fee
    # tiers already give free exit (Kshs. 0) for short stays - that case
    # is short-circuited in app.py before stk_push() is ever called.
    payload = {
        "BusinessShortCode": SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": CALLBACK_URL,
        "AccountReference": account_reference,
        "TransactionDesc": transaction_desc,
    }

    try:
        resp = requests.post(
            f"{BASE_URL}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as exc:
        return False, f"Could not reach M-Pesa: {exc}"

    if data.get("ResponseCode") == "0":
        return True, data["CheckoutRequestID"]
    return False, (
        data.get("errorMessage")
        or data.get("ResponseDescription")
        or "STK push failed. Please try again."
    )
