"""Admin CLI for Segro Bus. Works on SQLite locally, or on Neon/Postgres when DATABASE_URL is set.

  python manage.py init
  python manage.py add-org SATHYA "Sathyabama" college      # prints the scanner key
  python manage.py add-org ACME "Acme Shuttles" private
  python manage.py add-org DEPOT1 "Demo Depot (sample data)" government
  python manage.py show-key SATHYA
  python manage.py add-bus SATHYA 12 50
  python manage.py add-user SATHYA ADMIN01 "Transport Office" pass1234 admin
  python manage.py add-user SATHYA STAFF01 "Door Staff" pass1234 staff
  python manage.py add-user SATHYA 21CS001 "Asha" pass1234 rider
  python manage.py assign SATHYA 21CS001 12
  python manage.py card SATHYA 21CS001 04A1B2C3
  python manage.py import-routes SATHYA data/routes_full.private.csv   # bus routes, vehicles, drivers
  python manage.py demo                                      # sample data for all three portals
"""
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

import app as A


def org_row(db, code):
    r = db.one("SELECT * FROM organisations WHERE code=?", (code.upper(),))
    if not r:
        sys.exit(f"no such organisation: {code}")
    return r


def add_org(db, code, name, typ):
    if typ not in A.ORG_TYPES:
        sys.exit("type must be college, private or government")
    if db.one("SELECT 1 AS x FROM organisations WHERE code=?", (code.upper(),)):
        return db.one("SELECT device_key FROM organisations WHERE code=?", (code.upper(),))["device_key"]
    key = secrets.token_hex(16)
    db.q("INSERT INTO organisations(code,name,org_type,device_key,open_signup) VALUES(?,?,?,?,?)",
         (code.upper(), name, typ, key, 0 if typ == "government" else 1))
    return key


def add_bus(db, code, num, cap):
    o = org_row(db, code)
    if db.one("SELECT 1 AS x FROM buses WHERE org_id=? AND bus_number=?", (o["id"], num)):
        db.q("UPDATE buses SET capacity=? WHERE org_id=? AND bus_number=?", (int(cap), o["id"], num))
    else:
        db.q("INSERT INTO buses(org_id,bus_number,capacity) VALUES(?,?,?)", (o["id"], num, int(cap)))


def add_user(db, code, lid, name, pw, role="rider"):
    if role not in ("rider", "staff", "admin"):
        sys.exit("role must be rider, staff or admin")
    o = org_row(db, code)
    if db.one("SELECT 1 AS x FROM users WHERE org_id=? AND login_id=?", (o["id"], lid)):
        db.q("UPDATE users SET name=?, password_hash=?, role=? WHERE org_id=? AND login_id=?",
             (name, generate_password_hash(pw), role, o["id"], lid))
    else:
        db.q("INSERT INTO users(org_id,login_id,name,password_hash,role) VALUES(?,?,?,?,?)",
             (o["id"], lid, name, generate_password_hash(pw), role))


def assign(db, code, lid, num):
    o = org_row(db, code)
    b = db.one("SELECT id FROM buses WHERE org_id=? AND bus_number=?", (o["id"], num))
    u = db.one("SELECT id FROM users WHERE org_id=? AND login_id=?", (o["id"], lid))
    if not b or not u:
        sys.exit("no such bus or rider")
    db.q("UPDATE users SET bus_id=? WHERE id=?", (b["id"], u["id"]))


def card(db, code, lid, uid):
    o = org_row(db, code)
    u = db.one("SELECT id FROM users WHERE org_id=? AND login_id=?", (o["id"], lid))
    if not u:
        sys.exit("no such rider")
    db.q("UPDATE users SET uid_hash=? WHERE id=?", (A.hash_uid(uid), u["id"]))


def import_routes(db, code, path):
    o = org_row(db, code)
    with open(path, newline="", encoding="utf-8") as f:
        added, updated, errors = A.upsert_buses(db, o["id"], f.read())
    print(f"{added} buses added, {updated} updated")
    for e in errors:
        print("line", e["line"], e["error"])


def demo(db):
    for code, name, typ in (("SATHYA", "Sathyabama (demo)", "college"),
                            ("ACME", "Acme Shuttles (demo)", "private"),
                            ("DEPOT1", "Demo Depot (sample data)", "government")):
        print(code, "scanner key:", add_org(db, code, name, typ))
        routes = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "routes_full.private.csv")
        if typ == "college" and not os.path.exists(routes):  # fall back to the committed route list (no driver details)
            routes = os.path.join(os.path.dirname(routes), "routes_public.csv")
        if typ == "college" and os.path.exists(routes):
            import_routes(db, code, routes)
        else:
            add_bus(db, code, "12", 24)
        add_user(db, code, "ADMIN01", "Transport Office", "admin12345", "admin")
        add_user(db, code, "STAFF01", "Door Staff", "staff12345", "staff")
        add_user(db, code, "R001", "Asha", "rider12345", "rider")
        add_user(db, code, "R002", "Ravi", "rider12345", "rider")
        if typ != "government":
            bus = "9" if typ == "college" and os.path.exists(routes) else "12"
            assign(db, code, "R001", bus)
            assign(db, code, "R002", bus)
    print("demo logins: ADMIN01/admin12345, STAFF01/staff12345, R001/rider12345 (change before real use)")


def main(a):
    if not a:
        print(__doc__)
        return
    A.init_db()
    db = A.DB()
    c = a[0]
    try:
        if c == "init":
            print("database ready:", "postgres" if A.IS_PG else A.SQLITE_PATH)
        elif c == "add-org":
            print("scanner device key (put it in the ESP32 sketch):", add_org(db, a[1], a[2], a[3]))
        elif c == "show-key":
            print(org_row(db, a[1])["device_key"])
        elif c == "add-bus":
            add_bus(db, a[1], a[2], a[3])
        elif c == "add-user":
            add_user(db, a[1], a[2], a[3], a[4], a[5] if len(a) > 5 else "rider")
        elif c == "assign":
            assign(db, a[1], a[2], a[3])
        elif c == "card":
            card(db, a[1], a[2], a[3])
        elif c == "import-routes":
            import_routes(db, a[1], a[2])
        elif c == "demo":
            demo(db)
        else:
            print(__doc__)
            return
    except IndexError:
        sys.exit("missing arguments\n" + __doc__)
    db.close()
    print("ok")


if __name__ == "__main__":
    main(sys.argv[1:])
