# Segro Bus — One student. One bus. One seat.

Seat-assignment and boarding validation prototype with three portals (college, private organisation, government sandbox) and an ESP32 + MFRC522 card scanner. Flask API + single-page UI. SQLite locally, Postgres (Neon) on Vercel.

**Prototype notes:** sample data only. The government portal is a sandbox, not an integration with any real transport agency. Do not store real student data without proper consent and security review.

## 1. Run locally (2 minutes)
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py demo        # creates 3 demo organisations, prints each scanner key
python app.py                # http://localhost:5000
```
Demo logins (pick the portal, then): `ADMIN01 / admin12345`, `STAFF01 / staff12345`, `R001 / rider12345`.
Tests: `python tests/test_app.py`

## 2. Deploy: GitHub → Vercel → Neon (no command line needed)
1. Create an empty GitHub repo and upload **the contents of this folder** (so `app.py` sits at the repo root, not inside a subfolder). With git: `git init && git add . && git commit -m "Segro Bus" && git branch -M main && git remote add origin <your-repo-url> && git push -u origin main`.
2. vercel.com → Add New → Project → import the repo. Vercel detects the Flask app from `app.py` automatically. Click Deploy (the first build may start without a database; that's fine).
3. In the project: **Storage → Create/Connect Database → Neon (Postgres)** → connect it to the project. This adds `DATABASE_URL`.
4. **Settings → Environment Variables** (Production), add:
   - `SEGRO_SECRET_KEY` = any long random string (e.g. `python -c "import secrets;print(secrets.token_hex(32))"`)
   - `SEGRO_UID_SALT` = another random string (**never change it later**, or linked cards stop working)
   - `SETUP_TOKEN` = a password you choose, used once in step 6
5. **Deployments → Redeploy.** Tables are created automatically on the first request. Check `https://<project>.vercel.app/api/health` shows `"database":"postgres"`.
6. Create your first organisation and admin by sending one request (replace the values; works from any terminal, or Postman/Hoppscotch):
```bash
curl -X POST https://<project>.vercel.app/api/setup \
  -H "Content-Type: application/json" -H "X-Setup-Token: <your SETUP_TOKEN>" \
  -d '{"code":"SATHYA","name":"Sathyabama","org_type":"college","admin_id":"ADMIN01","admin_password":"choose-a-strong-one"}'
```
   The reply contains `device_key`: **save it**, it goes into the scanner's `secrets.h`. Repeat for more organisations (`private`, `government`). Then delete `SETUP_TOKEN` from Vercel and redeploy.
7. Open the site, choose the portal, sign in as the admin, add buses, import riders, link cards.

(Optional, from a laptop: `python manage.py demo` with `DATABASE_URL` and `SEGRO_UID_SALT` exported fills sample data.)

## 3. Build the scanner
Parts: ESP32 DevKit, MFRC522 module, 2 LEDs (green, red) + 2×220 Ω, active buzzer, breadboard, jumper wires, MIFARE 13.56 MHz cards/tags (college ID cards only work if they are 13.56 MHz MIFARE/NFC; 125 kHz EM4100 cards will NOT work with the MFRC522).

| MFRC522 | ESP32 |
|---|---|
| 3.3V | 3V3 (**not 5V**) |
| GND | GND |
| SDA/SS | GPIO 5 |
| RST | GPIO 22 |
| SCK | GPIO 18 |
| MISO | GPIO 19 |
| MOSI | GPIO 23 |

Green LED: GPIO 25 → 220 Ω → LED long leg, short leg → GND. Red LED: GPIO 26 likewise. Buzzer + → GPIO 27, − → GND.

1. Arduino IDE → install ESP32 board support; Library Manager: **MFRC522** (GithubCommunity) and **ArduinoJson** v7.
2. Upload `firmware/step1_read_uid` first. Serial Monitor at 115200: the version line should say `0x91` or `0x92`; tapping a card prints its UID. Fix wiring until this works.
3. In `firmware/segro_node/` copy `secrets.h.example` → `secrets.h`; fill in Wi-Fi, your Vercel hostname, the organisation's scanner key (`python manage.py show-key SATHYA`) and bus number. Upload `segro_node`.
4. On boot it pings the server: green + 1 beep = Wi-Fi, HTTPS and key all work. Red long beep = fix Wi-Fi, hostname or key.

Signals: OK green+1 beep · ENROLLED green+2 · INVALID/UNKNOWN_BUS red+1 · WRONG_BUS/DUPLICATE red+2 · FULL red+3 · network/key problem long red.

## Bus route list (college portal)
`data/routes_public.csv` has the 78 routes from the Transport Department list of 03/09/2026 (route number, vehicle number, route text). **Seat counts are not in that list**, so every bus is set to 50 seats; change them in Admin → Buses ("Seats on every bus", or per bus).
Driver names and phone numbers are in `data/routes_full.private.csv`, which is git-ignored so it never reaches a public repo. Load the list into the live database either way:
- Admin → Buses → Import route list: paste the contents of `routes_full.private.csv` (or `routes_public.csv` for no driver details), or
- `python manage.py import-routes SATHYA data/routes_full.private.csv` with `DATABASE_URL` set.
Riders see their route and vehicle number; only staff and admins see driver details. If you upload files to GitHub by hand, do not upload the `.private.csv` file.

## 4. Connect cards to riders
Easiest, no UIDs to type: sign in as admin → enter the rider's ID → **Link a card** → tap the new card on the scanner within 2 minutes (ENROLLED beep). Or use `python manage.py card SATHYA R001 <UID>`.
Then assign buses (admin panel or CSV import), have the rider sign in, and tap the card: the rider's page and the staff dashboard update live (every 5–8 s).

## The app screens
- **Sign in:** pick College, Organisation or Government; riders can create their own account (except Government).
- **Rider:** bus number, seats left, boarded status, live seat map, recent rides.
- **Staff:** live bus cards, scanner online status, latest taps, manual boarding.
- **Admin:** the staff view plus Buses, Riders (add, import, assign, link/unlink card, reset password), Insights (daily boardings, bus fill) and Scanner (key, wiring table, build steps).

## How it works
Boarding rules, in order: valid rider of this organisation → bus exists → rider's assigned bus (skipped for government/open ridership) → not already boarded this trip → seat available → recorded. Trip = local date + AM/PM (or `SHIFT`). The last seat is protected by a row lock in Postgres and `BEGIN IMMEDIATE` in SQLite. Each organisation has its own scanner key and data; cards never work across organisations. Card UIDs are stored only as salted SHA-256 hashes. Analytics are aggregated, with no names.

## Known limits (be upfront with judges)
- Firmware uses `setInsecure()` (encrypted but server certificate not verified); pin a CA cert for production.
- A card UID is not a secret and cards can be cloned; a real deployment needs signed/secured cards or a second factor.
- Login lockout is per account; add IP rate limiting for production.
- Live updates poll every few seconds, not WebSockets.
- Not tested: a real Vercel deployment, real hardware, and your specific cards (verified locally on SQLite only; the Postgres code path is written but not run here).
