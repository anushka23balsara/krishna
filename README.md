# GEC Palanpur — Timetable & Attendance

Students type a roll number to see their batch, weekly timetable and monthly attendance, with a **75% calculator** that tells them how many classes to attend (or how many they can miss).
Faculty log in to take attendance, view records and monthly reports, **download reports as Excel (CSV)**, and move their slots. The system blocks times that clash with other classes.

## Run locally

```bash
python -m venv venv
venv\Scripts\activate            # Windows   (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 (students) and http://127.0.0.1:5000/faculty (faculty).

On the first run the app creates `college.db` and **`faculty_passwords.txt`**, which holds a random one-time password for every faculty code.
Log in with a code (e.g. `DAP`) and its password from that file. The portal makes you set your own password (8+ characters) before anything else.
Both files are in `.gitignore`. Never commit them.

To start over with fresh data, delete `college.db`, `faculty_passwords.txt` and `.secret_key`, then run again.

## Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests cover login security, forced password change, rate limiting, slot-edit validation and clash detection, attendance date rules, the CSV export, and the 75% maths.

## Manual test path (demo script)

| # | Do this | You should see |
|---|---------|----------------|
| 1 | Student page → tap **C01** | Name, batch C1, Monday timetable. A **Now** tag if a class is running |
| 2 | Faculty page → log in as `DAP` / `DAP` | "Code or password is incorrect" (the old default no longer works) |
| 3 | Log in as `DAP` with the password from `faculty_passwords.txt` | Forced "Change password" screen, no Cancel button |
| 4 | Set a new password | Your dashboard of classes |
| 5 | **Take attendance** → **Save** right away | "78 student(s) not marked yet" — nothing is saved |
| 6 | **Mark all present** → tap **Absent** for student 1 → **Save** | "Saved — 77 of 78 present", with a timestamp |
| 7 | Change the date to a day this class doesn't run, or to a future date | A clear error message (the server rejects it too) |
| 8 | Back → **Monthly report** | Held 1, student 1 at 0% in red. **Download Excel (CSV)** gives a file |
| 9 | Back → **Edit slot** → start period 6, end period 1 | "End period can't be before the start period" |
| 10 | Edit slot → move it onto another class's day/time for the same branch | "Those students already have … at that time" |
| 11 | Student page → **C01** | Red banner: *Below 75%. Attend the next 3 lectures in a row* |
| 12 | Student page → **C02** | Green banner: *On track* |

## Using Neon (Postgres)

The app uses **Neon whenever the `DATABASE_URL` environment variable is set**. Without it, it falls back to a local SQLite file (`college.db`).
**You don't create any tables yourself.** On the first start, the app creates every table and loads all the students and the timetable.

1. In Neon, create a project. It comes with a database called `neondb`, so you don't need to make another one.
2. Click **Connect** and copy the connection string. It looks like
   `postgresql://user:pass@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`
3. **Run it once on your own computer** so the faculty passwords file gets written there:
   ```bash
   # Windows PowerShell
   $env:DATABASE_URL="postgresql://...your string..."
   python app.py init-db
   # macOS/Linux
   DATABASE_URL="postgresql://...your string..." python app.py init-db
   ```
   You'll see `Created the tables and loaded sample data in Postgres.`, and `faculty_passwords.txt` appears in this folder.
   Look in Neon's **Tables** tab and you'll see all 8 tables.
4. Deploy (below) with the same `DATABASE_URL`. The server sees the tables already exist and leaves your data alone.

Has a teacher forgotten their password? Run `python app.py reset-password DAP` (with `DATABASE_URL` set) to print a new one-time password.

To start completely fresh, run this in Neon's SQL editor, then do step 3 again:
```sql
DROP TABLE IF EXISTS attendance, teacher_auth, timetable, students, periods, faculty, subjects, branches CASCADE;
```

## Deploy

**Render:** connect the repo. `render.yaml` sets it up. In the dashboard, paste your Neon string as `DATABASE_URL`. The free plan is fine because the data lives in Neon.

**Vercel:** import the repo and add these environment variables: `DATABASE_URL` (your Neon string) and `SECRET_KEY` (any long random text, e.g. from `python -c "import secrets;print(secrets.token_hex(32))"`).
**`SECRET_KEY` is required on Vercel.** Without it, logins randomly get logged out.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Neon / Postgres connection string. If it's not set, the app uses local SQLite |
| `SECRET_KEY` | Signs login cookies. Set a long random value in production |
| `FLASK_ENV=production` | Cookies are only sent over HTTPS |
| `DATA_DIR` | (SQLite only) folder for `college.db`, `.secret_key` and `faculty_passwords.txt` |

## Tests against Postgres

```bash
TEST_DATABASE_URL="postgresql://..." python -m pytest -q
```
This runs every test on both SQLite and Postgres. **It wipes the tables in that database**, so point it at an empty Neon *branch*, never your real one.
