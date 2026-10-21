"""Segro Bus: seat assignment and boarding validation (prototype).

Runs locally with SQLite (no setup) and on Vercel with Postgres (set DATABASE_URL).
Three portals share one core: college, private (organisation) and government (sandbox).
Every organisation has its own buses, riders, staff and scanner key.
"""
import csv
import hashlib
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # local SQLite use does not need it
    psycopg = None

BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, "public")
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get("SEGRO_DB", os.path.join(BASE, "segro.db"))
ON_VERCEL = bool(os.environ.get("VERCEL"))
ORG_TYPES = ("college", "private", "government")

if IS_PG and psycopg is None:
    raise RuntimeError("DATABASE_URL is set but psycopg is not installed (pip install -r requirements.txt)")
if ON_VERCEL and not (IS_PG and os.environ.get("SEGRO_SECRET_KEY") and os.environ.get("SEGRO_UID_SALT")):
    raise RuntimeError("On Vercel set DATABASE_URL, SEGRO_SECRET_KEY and SEGRO_UID_SALT (see README)")

SECRET_KEY = os.environ.get("SEGRO_SECRET_KEY", "dev-secret-change-me")
UID_SALT = os.environ.get("SEGRO_UID_SALT", "dev-salt-change-me")
TZ_NAME = os.environ.get("SEGRO_TZ", "Asia/Kolkata")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # tzdata missing: fall back to India time
    TZ = timezone(timedelta(hours=5, minutes=30))

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=ON_VERCEL, PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

INTEGRITY = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())


# ------------------------------------------------------------------ database
def schema_statements(pg: bool):
    pk = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY"
    return [
        f"""CREATE TABLE IF NOT EXISTS organisations(
            id {pk}, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            org_type TEXT NOT NULL CHECK(org_type IN ('college','private','government')),
            device_key TEXT UNIQUE NOT NULL, open_signup INTEGER NOT NULL DEFAULT 1, last_seen TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS buses(
            id {pk}, org_id INTEGER NOT NULL REFERENCES organisations(id),
            bus_number TEXT NOT NULL, capacity INTEGER NOT NULL, route TEXT, vehicle_no TEXT,
            driver_name TEXT, driver_phone TEXT, UNIQUE(org_id, bus_number))""",
        f"""CREATE TABLE IF NOT EXISTS users(
            id {pk}, org_id INTEGER NOT NULL REFERENCES organisations(id),
            login_id TEXT NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'rider' CHECK(role IN ('rider','staff','admin')),
            uid_hash TEXT UNIQUE, bus_id INTEGER REFERENCES buses(id),
            failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT, UNIQUE(org_id, login_id))""",
        f"""CREATE TABLE IF NOT EXISTS boardings(
            id {pk}, user_id INTEGER NOT NULL REFERENCES users(id),
            bus_id INTEGER NOT NULL REFERENCES buses(id), trip TEXT NOT NULL,
            boarded_at TEXT NOT NULL, UNIQUE(user_id, trip))""",
        """CREATE TABLE IF NOT EXISTS enrol_requests(
            org_id INTEGER PRIMARY KEY REFERENCES organisations(id),
            user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL)""",
    ]


class DB:
    """Tiny wrapper so the same SQL (with ? placeholders) runs on SQLite and Postgres."""

    def __init__(self):
        if IS_PG:
            self.c = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, prepare_threshold=None)
        else:
            self.c = sqlite3.connect(SQLITE_PATH, isolation_level=None)
            self.c.row_factory = sqlite3.Row

    def q(self, sql, params=()):
        return self.c.execute(sql.replace("?", "%s") if IS_PG else sql, tuple(params))

    def one(self, sql, params=()):
        r = self.q(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.q(sql, params).fetchall()]

    def begin(self):
        self.q("BEGIN" if IS_PG else "BEGIN IMMEDIATE")

    def commit(self):
        self.q("COMMIT")

    def rollback(self):
        self.q("ROLLBACK")

    def close(self):
        self.c.close()


_schema_ready = False
BUS_EXTRA = ("route", "vehicle_no", "driver_name", "driver_phone")


def ensure_schema(db):
    for st in schema_statements(IS_PG):
        db.q(st)
    for col in BUS_EXTRA:  # older databases get the new bus columns
        try:
            db.q(f"ALTER TABLE buses ADD COLUMN {'IF NOT EXISTS ' if IS_PG else ''}{col} TEXT")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise


def get_db():
    global _schema_ready
    if "db" not in g:
        g.db = DB()
        if not _schema_ready:  # tables are created automatically on the first request
            ensure_schema(g.db)
            _schema_ready = True
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = DB()
    ensure_schema(db)
    db.close()


# ------------------------------------------------------------------ helpers
def now():
    return datetime.now(TZ)


def hash_uid(uid: str) -> str:
    return hashlib.sha256((UID_SALT + uid.strip().upper()).encode()).hexdigest()


def current_trip(shift=None) -> str:
    n = now()
    return f"{n:%Y-%m-%d}-{shift if shift else ('AM' if n.hour < 12 else 'PM')}"


def is_future(iso):
    return bool(iso) and datetime.fromisoformat(iso) > now()


def reply(result, status, message, **extra):
    return jsonify({"result": result, "message": message, **extra}), status


def roles_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session:
                return reply("AUTH_REQUIRED", 401, "Sign in first")
            if roles and session.get("role") not in roles:
                return reply("FORBIDDEN", 403, "Not allowed for your role")
            return f(*a, **k)
        return w
    return deco


def session_org():
    return get_db().one("SELECT * FROM organisations WHERE id=?", (session["org_id"],))


def device_org():
    key = request.headers.get("X-Device-Key", "")
    if not key:
        return None
    org = get_db().one("SELECT * FROM organisations WHERE device_key=?", (key,))
    if org:
        get_db().q("UPDATE organisations SET last_seen=? WHERE id=?", (now().isoformat(timespec="seconds"), org["id"]))
    return org


def find_org(portal, code):
    if portal not in ORG_TYPES:
        return None
    o = get_db().one("SELECT * FROM organisations WHERE code=?", ((code or "").strip().upper(),))
    return o if o and o["org_type"] == portal else None


def upsert_buses(db, org_id, csv_text):
    """CSV header: route_no (or bus_number), capacity, and optionally vehicle_no, route, driver_name, driver_phone.
    Existing buses are updated; columns missing from the CSV are left as they were."""
    added = updated = 0
    errors = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for n, row in enumerate(reader, start=2):
        if n > 501:
            errors.append({"line": n, "error": "limit is 500 rows per import"})
            break
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        num = row.get("route_no") or row.get("bus_number") or ""
        try:
            cap = int(row.get("capacity") or 0)
        except ValueError:
            cap = 0
        if not num or len(num) > 20 or not 1 <= cap <= 200:
            errors.append({"line": n, "error": "route_no and a capacity of 1-200 are required"})
            continue
        vals = {c: (row[c][:400] if row.get(c) else None) for c in BUS_EXTRA if c in row}
        if row.get("phone"):
            vals["driver_phone"] = row["phone"][:20]
        if db.one("SELECT 1 AS x FROM buses WHERE org_id=? AND bus_number=?", (org_id, num)):
            sets = ", ".join(f"{c}=?" for c in vals)
            db.q(f"UPDATE buses SET capacity=?{', ' + sets if sets else ''} WHERE org_id=? AND bus_number=?",
                 (cap, *vals.values(), org_id, num))
            updated += 1
        else:
            cols = ["org_id", "bus_number", "capacity", *vals]
            db.q(f"INSERT INTO buses({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                 (org_id, num, cap, *vals.values()))
            added += 1
    return added, updated, errors


# ------------------------------------------------------------------ boarding rules
def try_board(db, org, user, bus_number, shift=None):
    """valid rider -> bus exists -> assigned bus (not government) -> duplicate -> capacity -> record."""
    if user is None or user["org_id"] != org["id"] or user["role"] != "rider":
        return reply("INVALID_ID", 404, "ID not found")
    if shift is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,10}", shift):
        return reply("BAD_REQUEST", 400, "shift must be 1-10 letters, digits, - or _")
    bus = db.one("SELECT * FROM buses WHERE org_id=? AND bus_number=?", (org["id"], bus_number))
    if bus is None:
        return reply("UNKNOWN_BUS", 400, "This bus is not registered")
    if org["org_type"] != "government" and user["bus_id"] != bus["id"]:
        assigned = db.one("SELECT bus_number FROM buses WHERE id=?", (user["bus_id"],)) if user["bus_id"] else None
        return reply("WRONG_BUS", 409, "Rider is assigned to another bus",
                     assigned_bus=assigned["bus_number"] if assigned else None)
    trip = current_trip(shift)
    db.begin()
    try:
        if IS_PG:  # serialise scans for this bus so two taps cannot both take the last seat
            db.q("SELECT id FROM buses WHERE id=? FOR UPDATE", (bus["id"],))
        if db.one("SELECT 1 AS x FROM boardings WHERE user_id=? AND trip=?", (user["id"], trip)):
            db.rollback()
            return reply("DUPLICATE", 409, "Already boarded for this trip")
        used = db.one("SELECT COUNT(*) AS c FROM boardings WHERE bus_id=? AND trip=?", (bus["id"], trip))["c"]
        if used >= bus["capacity"]:
            db.rollback()
            return reply("FULL", 409, "Bus is full", seats_left=0)
        db.q("INSERT INTO boardings(user_id,bus_id,trip,boarded_at) VALUES(?,?,?,?)",
             (user["id"], bus["id"], trip, now().isoformat(timespec="seconds")))
        db.commit()
    except INTEGRITY:
        db.rollback()
        return reply("DUPLICATE", 409, "Already boarded for this trip")
    except Exception:
        db.rollback()
        raise
    return reply("OK", 200, f"Welcome, {user['name']}", seats_left=bus["capacity"] - used - 1)


# ------------------------------------------------------------------ pages and health
@app.get("/")
def home():
    return send_from_directory(PUBLIC, "index.html")  # on Vercel public/ is served by the CDN


@app.get("/<path:name>")
def public_file(name):  # local dev only; on Vercel public/ is served by the CDN
    if name.startswith("api/"):
        return reply("NOT_FOUND", 404, "Not found")
    return send_from_directory(PUBLIC, name)


@app.get("/api/health")
def health():
    get_db().one("SELECT 1 AS ok")
    return jsonify({"ok": True, "database": "postgres" if IS_PG else "sqlite", "timezone": TZ_NAME})


@app.get("/api/orgs")
def list_orgs():
    return jsonify(get_db().all("SELECT code,name,org_type FROM organisations ORDER BY name"))


# ------------------------------------------------------------------ first-run setup (no CLI needed)
@app.post("/api/setup")
def setup():
    """Creates an organisation and its first admin. Needs the SETUP_TOKEN env var (header X-Setup-Token).
    Returns the scanner device key once. Remove SETUP_TOKEN from Vercel when finished."""
    token = os.environ.get("SETUP_TOKEN", "")
    if not token or not secrets.compare_digest(request.headers.get("X-Setup-Token", ""), token):
        return reply("FORBIDDEN", 403, "Setup is disabled or the token is wrong")
    d = request.get_json(silent=True) or {}
    code, name, typ = (d.get("code") or "").strip().upper(), (d.get("name") or "").strip(), d.get("org_type")
    lid, pw = (d.get("admin_id") or "").strip(), d.get("admin_password") or ""
    if not code or not name or typ not in ORG_TYPES or not lid or len(pw) < 8:
        return reply("BAD_REQUEST", 400, "code, name, org_type, admin_id and admin_password (8+) are required")
    db = get_db()
    key = secrets.token_hex(16)
    try:
        db.q("INSERT INTO organisations(code,name,org_type,device_key,open_signup) VALUES(?,?,?,?,?)",
             (code, name, typ, key, 0 if typ == "government" else 1))
    except INTEGRITY:
        return reply("EXISTS", 409, "Organisation code already exists")
    oid = db.one("SELECT id FROM organisations WHERE code=?", (code,))["id"]
    db.q("INSERT INTO users(org_id,login_id,name,password_hash,role) VALUES(?,?,?,?,'admin')",
         (oid, lid, "Administrator", generate_password_hash(pw)))
    return reply("OK", 201, "Organisation and admin created. Save the device key now.", device_key=key)


# ------------------------------------------------------------------ accounts
@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    org = find_org(d.get("portal"), d.get("org_code"))
    if org is None:
        return reply("BAD_REQUEST", 400, "Unknown organisation for this portal")
    if not org["open_signup"]:
        return reply("SIGNUP_CLOSED", 403, "This organisation adds riders itself; ask your administrator")
    name, lid, pw = (d.get("name") or "").strip(), (d.get("login_id") or "").strip(), d.get("password") or ""
    if not name or not lid or len(pw) < 8 or len(lid) > 40 or len(name) > 80:
        return reply("BAD_REQUEST", 400, "Name, ID (max 40) and a password of 8+ characters are required")
    try:
        get_db().q("INSERT INTO users(org_id,login_id,name,password_hash,role) VALUES(?,?,?,?,'rider')",
                   (org["id"], lid, name, generate_password_hash(pw)))
    except INTEGRITY:
        return reply("EXISTS", 409, "That ID is already registered")
    return reply("OK", 201, "Account created. Your administrator assigns your bus.")


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    db = get_db()
    org = find_org(d.get("portal"), d.get("org_code"))
    u = db.one("SELECT * FROM users WHERE org_id=? AND login_id=?",
               (org["id"], (d.get("login_id") or "").strip())) if org else None
    if u and is_future(u["locked_until"]):
        return reply("LOCKED", 429, "Too many failed attempts. Try again in a few minutes.")
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""):
        if u:
            n = u["failed_logins"] + 1
            if n >= 5:
                db.q("UPDATE users SET failed_logins=0, locked_until=? WHERE id=?",
                     ((now() + timedelta(minutes=5)).isoformat(), u["id"]))
            else:
                db.q("UPDATE users SET failed_logins=? WHERE id=?", (n, u["id"]))
        return reply("BAD_LOGIN", 401, "Wrong portal, organisation, ID or password")
    db.q("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (u["id"],))
    session.clear()
    session.permanent = True
    session.update(uid=u["id"], role=u["role"], org_id=org["id"], org_type=org["org_type"])
    return reply("OK", 200, "Signed in", role=u["role"], org_type=org["org_type"], org_name=org["name"])


@app.post("/api/logout")
def logout():
    session.clear()
    return reply("OK", 200, "Signed out")


@app.get("/api/me")
@roles_required()
def me():
    db, org = get_db(), session_org()
    u = db.one("SELECT * FROM users WHERE id=?", (session["uid"],))
    out = {"name": u["name"], "login_id": u["login_id"], "role": u["role"],
           "org": {"name": org["name"], "type": org["org_type"]}, "bus": None}
    if u["role"] == "rider":
        if org["org_type"] == "government":
            out["boardings_total"] = db.one("SELECT COUNT(*) AS c FROM boardings WHERE user_id=?", (u["id"],))["c"]
        elif u["bus_id"]:
            bus = db.one("SELECT * FROM buses WHERE id=?", (u["bus_id"],))
            trip = current_trip()
            used = db.one("SELECT COUNT(*) AS c FROM boardings WHERE bus_id=? AND trip=?", (bus["id"], trip))["c"]
            boarded = db.one("SELECT 1 AS x FROM boardings WHERE user_id=? AND trip=?", (u["id"], trip)) is not None
            out["bus"] = {"bus_number": bus["bus_number"], "capacity": bus["capacity"],
                          "route": bus["route"], "vehicle_no": bus["vehicle_no"],
                          "seats_left": bus["capacity"] - used, "boarded_this_trip": boarded}
    return jsonify(out)


# ------------------------------------------------------------------ scanner (ESP32)
@app.get("/api/ping")
def ping():
    org = device_org()
    if org is None:
        return reply("BAD_DEVICE_KEY", 401, "Unknown device")
    return reply("OK", 200, f"Scanner connected to {org['name']}", org_type=org["org_type"])


@app.post("/api/scan")
def scan():
    org = device_org()
    if org is None:
        return reply("BAD_DEVICE_KEY", 401, "Unknown device")
    d = request.get_json(silent=True) or {}
    uid, bus_number, shift = (d.get("uid") or "").strip(), (d.get("bus") or "").strip(), (d.get("shift") or None)
    if not uid or not bus_number:
        return reply("BAD_REQUEST", 400, "uid and bus are required")
    db = get_db()
    h = hash_uid(uid)
    user = db.one("SELECT * FROM users WHERE uid_hash=? AND org_id=?", (h, org["id"]))
    if user is None:  # an unknown card during an open enrolment window links to the chosen rider
        req = db.one("SELECT * FROM enrol_requests WHERE org_id=?", (org["id"],))
        if req and is_future(req["expires_at"]):
            target = db.one("SELECT * FROM users WHERE id=?", (req["user_id"],))
            try:
                db.q("UPDATE users SET uid_hash=? WHERE id=?", (h, target["id"]))
            except INTEGRITY:
                return reply("CARD_IN_USE", 409, "This card is already linked to someone")
            db.q("DELETE FROM enrol_requests WHERE org_id=?", (org["id"],))
            return reply("ENROLLED", 200, f"Card linked to {target['name']}")
    return try_board(db, org, user, bus_number, shift)


@app.post("/api/staff/board")  # manual fallback at the door
@roles_required("staff", "admin")
def staff_board():
    d = request.get_json(silent=True) or {}
    db, org = get_db(), session_org()
    user = db.one("SELECT * FROM users WHERE org_id=? AND login_id=?", (org["id"], (d.get("login_id") or "").strip()))
    return try_board(db, org, user, (d.get("bus") or "").strip(), d.get("shift") or None)


@app.get("/api/dashboard")
@roles_required("staff", "admin")
def dashboard():
    db, org = get_db(), session_org()
    trip = current_trip(request.args.get("shift") or None)
    rows = db.all("""SELECT b.bus_number, b.capacity, b.route, b.vehicle_no, b.driver_name, b.driver_phone,
        (SELECT COUNT(*) FROM boardings x WHERE x.bus_id=b.id AND x.trip=?) AS boarded
        FROM buses b WHERE b.org_id=? ORDER BY b.bus_number""", (trip, org["id"]))
    rows.sort(key=lambda r: [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", r["bus_number"]) if t])
    return jsonify({"trip": trip, "scanner_last_seen": org["last_seen"], "buses": [
        {"bus_number": r["bus_number"], "capacity": r["capacity"], "boarded": r["boarded"],
         "seats_left": r["capacity"] - r["boarded"], "route": r["route"], "vehicle_no": r["vehicle_no"],
         "driver_name": r["driver_name"], "driver_phone": r["driver_phone"]} for r in rows]})


# ------------------------------------------------------------------ administrator
@app.post("/api/admin/buses")
@roles_required("admin")
def add_bus():
    d = request.get_json(silent=True) or {}
    num = str(d.get("bus_number") or "").strip()
    try:
        cap = int(d.get("capacity"))
    except (TypeError, ValueError):
        cap = 0
    if not num or len(num) > 20 or not 1 <= cap <= 200:
        return reply("BAD_REQUEST", 400, "bus_number (max 20 chars) and capacity 1-200 are required")
    db = get_db()
    if db.one("SELECT 1 AS x FROM buses WHERE org_id=? AND bus_number=?", (session["org_id"], num)):
        db.q("UPDATE buses SET capacity=? WHERE org_id=? AND bus_number=?", (cap, session["org_id"], num))
        return reply("OK", 200, f"Bus {num} capacity updated")
    db.q("INSERT INTO buses(org_id,bus_number,capacity) VALUES(?,?,?)", (session["org_id"], num, cap))
    return reply("OK", 201, f"Bus {num} added")


@app.post("/api/admin/buses/capacity-all")
@roles_required("admin")
def capacity_all():
    try:
        cap = int((request.get_json(silent=True) or {}).get("capacity"))
    except (TypeError, ValueError):
        cap = 0
    if not 1 <= cap <= 200:
        return reply("BAD_REQUEST", 400, "capacity must be 1-200")
    get_db().q("UPDATE buses SET capacity=? WHERE org_id=?", (cap, session["org_id"]))
    return reply("OK", 200, f"Every bus now has {cap} seats")


@app.post("/api/admin/buses/import")
@roles_required("admin")
def import_buses():
    text = (request.get_json(silent=True) or {}).get("csv") or ""
    added, updated, errors = upsert_buses(get_db(), session["org_id"], text)
    return jsonify({"added": added, "updated": updated, "errors": errors})


@app.get("/api/admin/riders")
@roles_required("admin")
def list_riders():
    rows = get_db().all("""SELECT u.login_id, u.name, b.bus_number, u.uid_hash
        FROM users u LEFT JOIN buses b ON b.id=u.bus_id
        WHERE u.org_id=? AND u.role='rider' ORDER BY u.login_id LIMIT 500""", (session["org_id"],))
    return jsonify([{"login_id": r["login_id"], "name": r["name"], "bus": r["bus_number"],
                     "card_linked": bool(r["uid_hash"])} for r in rows])


@app.post("/api/admin/riders/assign")
@roles_required("admin")
def assign_rider():
    d = request.get_json(silent=True) or {}
    db, oid = get_db(), session["org_id"]
    u = db.one("SELECT id FROM users WHERE org_id=? AND login_id=? AND role='rider'", (oid, (d.get("login_id") or "").strip()))
    if not u:
        return reply("NOT_FOUND", 404, "No such rider")
    bus_no = (d.get("bus") or "").strip()
    bus_id = None
    if bus_no:
        b = db.one("SELECT id FROM buses WHERE org_id=? AND bus_number=?", (oid, bus_no))
        if not b:
            return reply("UNKNOWN_BUS", 400, "This bus is not registered")
        bus_id = b["id"]
    db.q("UPDATE users SET bus_id=? WHERE id=?", (bus_id, u["id"]))
    return reply("OK", 200, "Bus assigned" if bus_id else "Bus cleared")


@app.post("/api/admin/riders/enrol")
@roles_required("admin")
def start_enrol():
    """Next unknown card tapped on this organisation's scanner within 2 minutes is linked to this rider."""
    d = request.get_json(silent=True) or {}
    db, oid = get_db(), session["org_id"]
    u = db.one("SELECT id,name FROM users WHERE org_id=? AND login_id=? AND role='rider'", (oid, (d.get("login_id") or "").strip()))
    if not u:
        return reply("NOT_FOUND", 404, "No such rider")
    db.q("DELETE FROM enrol_requests WHERE org_id=?", (oid,))
    db.q("INSERT INTO enrol_requests(org_id,user_id,expires_at) VALUES(?,?,?)",
         (oid, u["id"], (now() + timedelta(minutes=2)).isoformat()))
    return reply("OK", 200, f"Tap a new card on the scanner within 2 minutes to link it to {u['name']}")


@app.post("/api/admin/riders/import")
@roles_required("admin")
def import_riders():
    """CSV header: login_id,name,bus (bus optional). Returns one-time temporary passwords."""
    text = (request.get_json(silent=True) or {}).get("csv") or ""
    db, oid = get_db(), session["org_id"]
    created, errors = [], []
    for n, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        if n > 1001:
            errors.append({"line": n, "error": "limit is 1000 rows per import"})
            break
        lid, name, bus_no = (row.get("login_id") or "").strip(), (row.get("name") or "").strip(), (row.get("bus") or "").strip()
        if not lid or not name or len(lid) > 40 or len(name) > 80:
            errors.append({"line": n, "error": "login_id and name are required"})
            continue
        bus_id = None
        if bus_no:
            b = db.one("SELECT id FROM buses WHERE org_id=? AND bus_number=?", (oid, bus_no))
            if not b:
                errors.append({"line": n, "error": f"unknown bus {bus_no}"})
                continue
            bus_id = b["id"]
        temp = secrets.token_urlsafe(8)
        try:
            db.q("INSERT INTO users(org_id,login_id,name,password_hash,role,bus_id) VALUES(?,?,?,?,'rider',?)",
                 (oid, lid, name, generate_password_hash(temp), bus_id))
        except INTEGRITY:
            errors.append({"line": n, "error": f"{lid} already exists"})
            continue
        created.append({"login_id": lid, "temp_password": temp})
    return jsonify({"created": created, "errors": errors})


@app.get("/api/admin/analytics")
@roles_required("admin")
def analytics():
    """Aggregated only: no names or IDs."""
    rows = get_db().all("""SELECT b.bus_number, b.capacity, COUNT(x.id) AS total_boardings,
        COUNT(DISTINCT x.trip) AS trips FROM buses b LEFT JOIN boardings x ON x.bus_id=b.id
        WHERE b.org_id=? GROUP BY b.id, b.bus_number, b.capacity ORDER BY b.bus_number""", (session["org_id"],))
    out = []
    for r in rows:
        avg = round(r["total_boardings"] / r["trips"], 1) if r["trips"] else 0
        out.append({"bus_number": r["bus_number"], "capacity": r["capacity"], "trips": r["trips"],
                    "total_boardings": r["total_boardings"], "avg_per_trip": avg,
                    "avg_fill_percent": round(100 * avg / r["capacity"], 1) if r["capacity"] else 0})
    return jsonify({"buses": out})



# ------------------------------------------------------------------ extra endpoints for the UI
@app.get("/api/me/history")
@roles_required("rider")
def my_history():
    rows = get_db().all("""SELECT x.trip, x.boarded_at, b.bus_number FROM boardings x
        JOIN buses b ON b.id=x.bus_id WHERE x.user_id=? ORDER BY x.id DESC LIMIT 20""", (session["uid"],))
    return jsonify(rows)


@app.get("/api/feed")
@roles_required("staff", "admin")
def feed():
    """Latest scans for this organisation (staff see names, as they do at the door)."""
    rows = get_db().all("""SELECT u.name, u.login_id, b.bus_number, x.trip, x.boarded_at
        FROM boardings x JOIN users u ON u.id=x.user_id JOIN buses b ON b.id=x.bus_id
        WHERE b.org_id=? ORDER BY x.id DESC LIMIT 15""", (session["org_id"],))
    return jsonify(rows)


@app.get("/api/admin/device")
@roles_required("admin")
def device_info():
    o = session_org()
    return jsonify({"device_key": o["device_key"], "last_seen": o["last_seen"], "org_code": o["code"]})


@app.post("/api/admin/device/rotate")
@roles_required("admin")
def rotate_key():
    key = secrets.token_hex(16)
    get_db().q("UPDATE organisations SET device_key=? WHERE id=?", (key, session["org_id"]))
    return reply("OK", 200, "New scanner key created. Put it in secrets.h and re-flash the scanner.", device_key=key)


@app.post("/api/admin/riders/unlink")
@roles_required("admin")
def unlink_card():
    d = request.get_json(silent=True) or {}
    db = get_db()
    u = db.one("SELECT id FROM users WHERE org_id=? AND login_id=? AND role='rider'", (session["org_id"], (d.get("login_id") or "").strip()))
    if not u:
        return reply("NOT_FOUND", 404, "No such rider")
    db.q("UPDATE users SET uid_hash=NULL WHERE id=?", (u["id"],))
    return reply("OK", 200, "Card unlinked")


@app.post("/api/admin/riders/reset")
@roles_required("admin")
def reset_password():
    d = request.get_json(silent=True) or {}
    db = get_db()
    u = db.one("SELECT id FROM users WHERE org_id=? AND login_id=? AND role='rider'", (session["org_id"], (d.get("login_id") or "").strip()))
    if not u:
        return reply("NOT_FOUND", 404, "No such rider")
    temp = secrets.token_urlsafe(8)
    db.q("UPDATE users SET password_hash=?, failed_logins=0, locked_until=NULL WHERE id=?", (generate_password_hash(temp), u["id"]))
    return reply("OK", 200, "Temporary password created (shown once)", temp_password=temp)


@app.get("/api/admin/daily")
@roles_required("admin")
def daily():
    rows = get_db().all("""SELECT substr(x.trip,1,10) AS day, COUNT(*) AS boardings
        FROM boardings x JOIN buses b ON b.id=x.bus_id WHERE b.org_id=?
        GROUP BY substr(x.trip,1,10) ORDER BY day DESC LIMIT 14""", (session["org_id"],))
    return jsonify(list(reversed(rows)))

# ------------------------------------------------------------------ errors
@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return reply("HTTP_ERROR", e.code, e.name)
    app.logger.exception("unhandled error")
    return reply("SERVER_ERROR", 500, "Something went wrong")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
