#MASEMBO-PARKING-SYSTEM
A smart parking system
Masembo — Premium Parking System


A functional, web-based parking management prototype built for the "modern parking system" brief: drivers see live slot availability, vehicles are recorded on arrival, and on exit the system calculates duration + fee automatically before the barrier opens.

Stack: Python 3 + Flask (web layer) + SQLite (dynamic database). Chosen because it needs no external DB server to install for a class submission, while still being a real relational database with proper tables, keys and growth — not a flat file.

1. Use cases identified
Actor	Use case
Driver	View available slots before entering
Gate attendant/system	Record vehicle arrival, assign a slot
Driver / attendant	Request exit, view fee owed
System	Calculate fee based on duration
Driver / attendant	Confirm payment
Barrier	Open on successful payment
Manager	View history/log of all sessions
2. Modules proposed
Slot Management Module — tracks every slot's state (available / occupied) and drives the visual display.
Vehicle Entry Module — validates and records an arriving vehicle, allocates it a slot.
Fee Calculation Module — pure function that turns (entry_time, exit_time) into a fee using the client's tiers.
Vehicle Exit & Payment Module — looks up the active session, shows the fee, and finalises the session on payment. Payment can be cash (attendant confirms) or Lipa Na M-Pesa Online (STK Push), handled by mpesa.py and confirmed asynchronously via Safaricom's callback (see section 7).
Barrier Control Module — simulated: opens (returns "OPEN") only after payment is confirmed. On real hardware this would send a signal to a relay/controller instead of returning a string.
Staff Login — a single shared password (STAFF_PASSWORD in .env) gates recording entries and taking payment, via a Flask session cookie. The slot dashboard and fee lookup stay public so drivers can check availability without logging in.
All of these live in algorithms.py (the algorithms) and app.py (the web routes that expose them), with clear docstrings explaining each step.

3. Algorithms (see algorithms.py for full comments)
Slot allocation: pop a free slot number from the front of a FIFO queue — O(1) — rather than scanning every slot for the first free one (O(n)). Freed slots are pushed to the back of the queue, spreading wear evenly.
Fee calculation: tiered decision logic matching the client's exact bands (free ≤30 min, Kshs 50 ≤2h, Kshs 100 ≤4h, Kshs 300 ≤6h, Kshs 500 beyond) — O(1).
Session lookup on exit: hash map keyed by plate number gives O(1) lookup instead of scanning the sessions log.
4. Data structures and why
Structure	Used for	Why this one
list (array)	Ordered slots for the visual grid	Matches the physical, numbered layout; O(1) indexed access for rendering
collections.deque (queue)	Available slot numbers	O(1) allocate (popleft) and release (append); FIFO fairness across bays
dict (hash map)	Active sessions keyed by plate	O(1) lookup on exit instead of O(n) scan of the sessions log
SQLite tables	Durable state + full history	Survives restarts; supports reporting/history; relational integrity via foreign key
5. Dynamic database design
slots
-----
id            INTEGER PRIMARY KEY
slot_number   TEXT UNIQUE     -- e.g. "S01"
status        TEXT            -- 'available' | 'occupied'

sessions
--------
id            INTEGER PRIMARY KEY
plate_number  TEXT
slot_id       INTEGER  -> FK slots.id
entry_time    TEXT (ISO datetime)
exit_time     TEXT (ISO datetime, NULL while active)
fee           REAL (NULL until paid)
status        TEXT            -- 'active' | 'completed'
phone_number       TEXT       -- set when paying by M-Pesa
payment_method     TEXT       -- 'cash' | 'mpesa'
payment_status     TEXT       -- 'pending' | 'paid' | 'failed' (M-Pesa only)
checkout_request_id TEXT      -- Safaricom's STK push ID
mpesa_receipt      TEXT       -- e.g. "SFC1A2B3C4"
Why it's "dynamic": sessions is append-only and grows with every car that passes through (this is your history/audit trail for free). slots can be scaled up at any time by changing TOTAL_SLOTS in database.py and calling ensure_slot_count() — existing data is never touched, only new rows are added.

6. How to run it
cd parking_system
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
Then open http://localhost:5000 in your browser.

The dashboard shows the live slot grid (green = available, red = occupied) and a form to record an arrival.
"Exit / Pay" looks up a plate and shows the fee owed. For a non-zero fee, choose Pay with Cash (attendant confirms, barrier opens immediately) or Lipa na M-Pesa (sends an STK push, see below).
"History" lists the last 100 sessions from the database, including how each one was paid.
The database file parking.db is created automatically on first run in the project folder — nothing else to configure.

7. Setting up Lipa Na M-Pesa Online (STK Push)
The M-Pesa flow needs a Daraja app and a public HTTPS callback URL Safaricom can reach, so it's a little more setup than the rest:

Create a free account and app at developer.safaricom.co.ke → "My Apps" → add the Lipa Na M-Pesa Online product. This gives you a Consumer Key and Consumer Secret.
Copy .env.example to .env and fill in your key/secret. The sandbox MPESA_SHORTCODE (174379) and MPESA_PASSKEY already in the example file are Safaricom's public test values — fine for development as-is.
Expose your local server so Safaricom can reach it, e.g.:
ngrok http 5000
Copy the https://...ngrok-free.app URL it gives you into MPESA_CALLBACK_URL in .env, with /mpesa/callback appended.
Install the new dependencies and run as usual:
pip install -r requirements.txt
python3 app.py
On the "Exit / Pay" screen, enter an M-Pesa test number (Safaricom's sandbox accepts any number in the format 2547XXXXXXXX) and confirm the prompt using Safaricom's sandbox PIN simulator, which you'll find in the Daraja docs for STK Push testing.
How it works under the hood: POST /checkout/mpesa calls mpesa.stk_push(), which asks Daraja to prompt the driver's phone and returns a CheckoutRequestID. The driver sees a waiting screen (mpesa_wait.html) that polls GET /checkout/status/<id> every few seconds. Once the driver enters their PIN, Safaricom POSTs the result to POST /mpesa/callback, which frees the slot and marks the session paid — the waiting screen then redirects to the receipt. Free exits (fee = Kshs. 0 on short stays) skip M-Pesa entirely.

In production, swap MPESA_ENV=sandbox for MPESA_ENV=production and use your live Paybill/Till, passkey and a permanent HTTPS domain instead of ngrok.

8. Pushing to GitHub
git init
git add .
git commit -m "Modern Parking System prototype"
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
(.gitignore already excludes parking.db, venv/, __pycache__/ and .env — your M-Pesa credentials are never committed.)

9. What to extend next
Authentication for attendants/managers before they can view history. ✅ Done for entries/payments (single shared password). History and the dashboard are still public — add @login_required to those routes too if you want them locked down as well, and consider per-attendant accounts (a staff table + hashed passwords) instead of one shared password once there's more than one person on shift.
Number-plate recognition (ANPR) camera integration instead of manual plate entry, feeding straight into park_vehicle().
Reserved/VIP slots: add a slot_type column and a second queue so reserved bays aren't handed out by the general FIFO.
Real barrier hardware: replace the "barrier": "OPEN" string in checkout_vehicle() with a GPIO/serial signal to an actual relay.
M-Pesa reconciliation: pending_mpesa (mapping CheckoutRequestID → plate) lives in memory, so an in-flight STK push is forgotten if the server restarts mid-payment. For production, persist it (a DB table) or add a periodic "query STK status" job as a fallback for missed callbacks.
Multi-level/multi-branch support: add a level or branch_id column to slots and partition the queue per level.
