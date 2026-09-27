"""
algorithms.py
-------------
This is the "brain" of the system: the modules/algorithms the brief asks
you to design, plus the in-memory data structures that back them and the
reasons each one was chosen.

Modules implemented here:
    1. Slot Management        -> SlotManager class
    2. Vehicle Entry (arrival) -> SlotManager.park_vehicle()
    3. Fee Calculation         -> calculate_fee()
    4. Vehicle Exit / Payment  -> SlotManager.checkout_vehicle()
    5. Barrier Control         -> simulated by the return value of checkout

Data structures used, and WHY:
    - Array/List (self.slots): a fixed-order list of Slot objects mirrors
      the physical, numbered layout of the car park. Rendering the visual
      display (green/red grid) needs an ordered, indexable sequence -
      a list gives O(1) access by position and is the natural fit.

    - Queue / deque (self.available_queue): free slot numbers are held in
      a collections.deque. Allocating a slot on arrival = popleft() and
      releasing a slot on exit = append(), both O(1). A deque (rather than
      a plain list) avoids the O(n) cost of list.pop(0), and using a QUEUE
      (FIFO) instead of, say, always picking the lowest-numbered free slot
      spreads wear evenly across the physical bays - a fairer real-world
      policy for a client operating 24/7.

    - Hash map / dict (self.active_sessions): keyed by plate_number, this
      gives O(1) lookup when a car exits ("which slot is plate KAA123Z in,
      and when did it arrive?") instead of scanning every session - critical
      once the car park has been running for months and the sessions log
      is large.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from database import get_conn


@dataclass
class Slot:
    id: int
    slot_number: str
    status: str  # 'available' | 'occupied'
    plate_number: str = None  # who is parked here right now (if occupied)


# ---------------------------------------------------------------------------
# Module 3: Fee Calculation Algorithm
# ---------------------------------------------------------------------------
def calculate_fee(entry_time: datetime, exit_time: datetime) -> float:
    """
    Tiered fee algorithm, exactly as specified by the client:
        <= 30 minutes  : free        (Kshs. 0)
        <= 2 hours     : Kshs. 50
        <= 4 hours     : Kshs. 100
        <= 6 hours     : Kshs. 300
        > 6 hours      : Kshs. 500

    Using elif bands is a deliberate, simple, O(1) decision-tree rather
    than a per-minute rate: it matches the client's flat-tier pricing
    exactly and is trivial to audit/change if tiers are revised later.
    """
    duration_minutes = (exit_time - entry_time).total_seconds() / 60

    if duration_minutes <= 30:
        fee = 0
    elif duration_minutes <= 120:
        fee = 50
    elif duration_minutes <= 240:
        fee = 100
    elif duration_minutes <= 360:
        fee = 300
    else:
        fee = 500

    return fee, duration_minutes


# ---------------------------------------------------------------------------
# Modules 1, 2, 4: Slot Management / Entry / Exit
# ---------------------------------------------------------------------------
class SlotManager:
    """
    Keeps a fast in-memory view of the car park (array + queue + hash map)
    and writes every change straight through to SQLite so the two never
    drift apart and nothing is lost on restart.
    """

    def __init__(self):
        self.slots = []               # ARRAY: ordered list of Slot objects
        self.available_queue = deque()  # QUEUE: free slot_numbers, FIFO
        self.active_sessions = {}     # HASH MAP: plate_number -> session dict
        self.pending_mpesa = {}       # HASH MAP: checkout_request_id -> plate_number
        self._load_from_db()

    # -- setup -------------------------------------------------------------
    def _load_from_db(self):
        """Rebuild the in-memory structures from the database on startup."""
        self.slots.clear()
        self.available_queue.clear()
        self.active_sessions.clear()

        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM slots ORDER BY id").fetchall()
            for row in rows:
                slot = Slot(id=row["id"], slot_number=row["slot_number"], status=row["status"])
                self.slots.append(slot)
                if slot.status == "available":
                    self.available_queue.append(slot.slot_number)

            active = conn.execute(
                "SELECT * FROM sessions WHERE status = 'active'"
            ).fetchall()
            for row in active:
                self.active_sessions[row["plate_number"]] = {
                    "session_id": row["id"],
                    "slot_id": row["slot_id"],
                    "entry_time": row["entry_time"],
                }
                for slot in self.slots:
                    if slot.id == row["slot_id"]:
                        slot.plate_number = row["plate_number"]

    # -- Module 1: read-only view for the visual display --------------------
    def get_slot_display(self):
        """Returns the ordered slot list for rendering the green/red grid."""
        return self.slots

    def available_count(self):
        return len(self.available_queue)

    # -- Module 2: Vehicle Entry (arrival) -----------------------------------
    def park_vehicle(self, plate_number: str):
        """
        Algorithm:
          1. Reject if this plate is already parked (no double entry).
          2. Reject if no slot is free (queue empty -> car park full).
          3. Pop a free slot number from the FRONT of the queue - O(1).
          4. Record entry_time = now, insert a new session row.
          5. Mark the slot occupied in both memory and DB.
        """
        plate_number = plate_number.strip().upper()

        if plate_number in self.active_sessions:
            return False, "This vehicle is already parked inside."

        if not self.available_queue:
            return False, "Car park is full. No slots available."

        slot_number = self.available_queue.popleft()
        slot = next(s for s in self.slots if s.slot_number == slot_number)
        entry_time = datetime.now()

        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (plate_number, slot_id, entry_time, status) "
                "VALUES (?, ?, ?, 'active')",
                (plate_number, slot.id, entry_time.isoformat()),
            )
            session_id = cur.lastrowid
            conn.execute("UPDATE slots SET status='occupied' WHERE id=?", (slot.id,))

        slot.status = "occupied"
        slot.plate_number = plate_number
        self.active_sessions[plate_number] = {
            "session_id": session_id,
            "slot_id": slot.id,
            "entry_time": entry_time.isoformat(),
        }
        return True, slot.slot_number

    # -- preview fee without ending the session (for the checkout screen) --
    def preview_fee(self, plate_number: str):
        plate_number = plate_number.strip().upper()
        session = self.active_sessions.get(plate_number)
        if not session:
            return None
        entry_time = datetime.fromisoformat(session["entry_time"])
        fee, minutes = calculate_fee(entry_time, datetime.now())
        return {
            "plate_number": plate_number,
            "slot_id": session["slot_id"],
            "entry_time": entry_time,
            "minutes": round(minutes, 1),
            "fee": fee,
        }

    # -- Module 4: Vehicle Exit + Payment + Barrier -------------------------
    def checkout_vehicle(self, plate_number: str):
        """
        Algorithm:
          1. Look up the active session by plate - O(1) via hash map.
          2. exit_time = now; fee = calculate_fee(entry_time, exit_time).
          3. "Payment" is confirmed by the attendant/driver clicking Pay -
             on confirmation we close the session and free the slot.
          4. Push the freed slot_number to the BACK of the queue - O(1) -
             and simulate the barrier opening.
        """
        plate_number = plate_number.strip().upper()
        session = self.active_sessions.get(plate_number)
        if not session:
            return False, "No active session found for this plate."

        entry_time = datetime.fromisoformat(session["entry_time"])
        exit_time = datetime.now()
        fee, minutes = calculate_fee(entry_time, exit_time)

        with get_conn() as conn:
            conn.execute(
                # Explicitly reset payment_method/status/receipt to the cash
                # defaults: a driver may have tried and abandoned M-Pesa
                # first, which would otherwise leave a stale 'mpesa' tag on
                # a session that actually ended up paid in cash.
                "UPDATE sessions SET exit_time=?, fee=?, status='completed', "
                "payment_method='cash', payment_status=NULL, mpesa_receipt=NULL "
                "WHERE id=?",
                (exit_time.isoformat(), fee, session["session_id"]),
            )
            conn.execute(
                "UPDATE slots SET status='available' WHERE id=?", (session["slot_id"],)
            )

        slot = next(s for s in self.slots if s.id == session["slot_id"])
        slot.status = "available"
        slot.plate_number = None
        self.available_queue.append(slot.slot_number)
        del self.active_sessions[plate_number]

        return True, {
            "plate_number": plate_number,
            "slot_number": slot.slot_number,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "minutes": round(minutes, 1),
            "fee": fee,
            "barrier": "OPEN",
        }

    # -- Module 4b: Vehicle Exit + Payment via Lipa Na M-Pesa ---------------
    # Same idea as checkout_vehicle() above, but split into two steps
    # because payment now happens on the driver's own phone, asynchronously:
    #   1. initiate_mpesa_checkout() records the STK push request.
    #   2. complete_mpesa_checkout() is called from the /mpesa/callback route
    #      once Safaricom reports whether the driver actually paid.
    def initiate_mpesa_checkout(self, plate_number: str, phone_number: str):
        """Marks the active session as awaiting an M-Pesa payment. Returns
        (session_id, fee, minutes) so the caller can trigger the STK push,
        or None if there's no active session for this plate."""
        plate_number = plate_number.strip().upper()
        session = self.active_sessions.get(plate_number)
        if not session:
            return None

        entry_time = datetime.fromisoformat(session["entry_time"])
        fee, minutes = calculate_fee(entry_time, datetime.now())

        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET phone_number=?, payment_method='mpesa', "
                "payment_status='pending' WHERE id=?",
                (phone_number, session["session_id"]),
            )
        return session["session_id"], fee, minutes

    def attach_checkout_request_id(self, session_id: int, plate_number: str, checkout_request_id: str):
        """Links Safaricom's CheckoutRequestID to this session so the
        /mpesa/callback route can find its way back to the right car."""
        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET checkout_request_id=? WHERE id=?",
                (checkout_request_id, session_id),
            )
        self.pending_mpesa[checkout_request_id] = plate_number

    def mark_mpesa_failed(self, checkout_request_id: str):
        """Leaves the session active (car is still parked) but flags the
        payment attempt as failed so the driver can retry or pay cash."""
        plate_number = self.pending_mpesa.pop(checkout_request_id, None)
        if not plate_number:
            return False
        session = self.active_sessions.get(plate_number)
        if not session:
            return False
        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET payment_status='failed' WHERE id=?",
                (session["session_id"],),
            )
        return True

    def complete_mpesa_checkout(self, checkout_request_id: str, mpesa_receipt: str):
        """Called once Safaricom's callback confirms payment succeeded.
        Mirrors checkout_vehicle(): frees the slot and closes the session."""
        plate_number = self.pending_mpesa.pop(checkout_request_id, None)
        if not plate_number:
            return False
        session = self.active_sessions.get(plate_number)
        if not session:
            return False

        entry_time = datetime.fromisoformat(session["entry_time"])
        exit_time = datetime.now()
        fee, minutes = calculate_fee(entry_time, exit_time)

        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET exit_time=?, fee=?, status='completed', "
                "payment_status='paid', mpesa_receipt=? WHERE id=?",
                (exit_time.isoformat(), fee, mpesa_receipt, session["session_id"]),
            )
            conn.execute(
                "UPDATE slots SET status='available' WHERE id=?", (session["slot_id"],)
            )

        slot = next(s for s in self.slots if s.id == session["slot_id"])
        slot.status = "available"
        slot.plate_number = None
        self.available_queue.append(slot.slot_number)
        del self.active_sessions[plate_number]
        return True
