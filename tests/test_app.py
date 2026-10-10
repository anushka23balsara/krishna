"""Run with:  python -m pytest -q
Also against Postgres/Neon:  TEST_DATABASE_URL=postgresql://... python -m pytest -q
(WARNING: the Postgres tests wipe every table in that database — use an empty test database / Neon branch.)"""
import importlib
import os
import sqlite3
import sys
from datetime import timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


PG_URL = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


def wipe_postgres():
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS attendance, teacher_auth, timetable, students, periods, faculty, subjects, branches CASCADE")


@pytest.fixture(params=BACKENDS)
def app_mod(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for var in ("DATABASE_PATH", "PASSWORD_FILE", "DATABASE_URL", "POSTGRES_URL"):
        monkeypatch.delenv(var, raising=False)
    if request.param == "postgres":
        wipe_postgres()
        monkeypatch.setenv("DATABASE_URL", PG_URL)
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    mod._failed_logins.clear()
    mod.app.config["TESTING"] = True
    return mod


def passwords(mod):
    out = {}
    with open(mod.app.config["PASSWORD_FILE"], encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                code, pw = line.strip().split("\t")
                out[code] = pw
    return out


def login(client, mod, code="DAP", change=True):
    pw = passwords(mod)[code]
    r = client.post("/api/faculty/login", json={"code": code, "password": pw})
    assert r.status_code == 200
    if change:
        r = client.post("/api/faculty/password", json={"current": pw, "new": "NewPass123"})
        assert r.status_code == 200, r.get_json()
    return client


def last_date_for(mod, day):
    d = mod.today_ist()
    while d.weekday() >= 5 or DAYS[d.weekday()] != day:
        d -= timedelta(days=1)
    return d


def first_slot(client):
    return client.get("/api/faculty/schedule").get_json()["schedule"][0]


# ---------- security ----------

def test_default_password_no_longer_works(app_mod):
    c = app_mod.app.test_client()
    r = c.post("/api/faculty/login", json={"code": "DAP", "password": "DAP"})
    assert r.status_code == 401


def test_one_time_passwords_are_unique_and_written(app_mod):
    pws = passwords(app_mod)
    assert len(pws) == 24
    assert all(pw != code for code, pw in pws.items())


def test_must_change_password_before_using_portal(app_mod):
    c = app_mod.app.test_client()
    login(c, app_mod, change=False)
    assert c.get("/api/faculty/schedule").status_code == 403
    assert c.get("/api/faculty/session").get_json()["must_change"] is True
    pw = passwords(app_mod)["DAP"]
    assert c.post("/api/faculty/password", json={"current": pw, "new": "short"}).status_code == 400
    assert c.post("/api/faculty/password", json={"current": pw, "new": "NewPass123"}).status_code == 200
    assert c.get("/api/faculty/schedule").status_code == 200
    # And the new password works on a fresh login, without the forced change.
    c2 = app_mod.app.test_client()
    r = c2.post("/api/faculty/login", json={"code": "DAP", "password": "NewPass123"})
    assert r.get_json()["must_change"] is False


def test_login_is_rate_limited(app_mod):
    c = app_mod.app.test_client()
    for _ in range(8):
        assert c.post("/api/faculty/login", json={"code": "DAP", "password": "nope"}).status_code == 401
    assert c.post("/api/faculty/login", json={"code": "DAP", "password": "nope"}).status_code == 429


def test_secret_key_not_hardcoded(app_mod):
    assert app_mod.app.secret_key != "gecp-timetable-secret-change-me"
    assert len(app_mod.app.secret_key) >= 32


def test_options_needs_login(app_mod):
    assert app_mod.app.test_client().get("/api/faculty/options").status_code == 401


def test_old_database_is_upgraded(tmp_path, monkeypatch):
    for var in ("DATABASE_PATH", "PASSWORD_FILE", "DATABASE_URL", "POSTGRES_URL"):
        monkeypatch.delenv(var, raising=False)
    """A college.db from the old version (password == code, no must_change column) keeps working but forces a change."""
    from werkzeug.security import generate_password_hash
    db = tmp_path / "college.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE branches (id INTEGER PRIMARY KEY, name TEXT NOT NULL, default_room TEXT NOT NULL);
        INSERT INTO branches VALUES (1, 'Civil Engineering', '7104');
        CREATE TABLE faculty (code TEXT PRIMARY KEY, full_name TEXT NOT NULL);
        CREATE TABLE teacher_auth (code TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
    """)
    conn.execute("INSERT INTO faculty VALUES ('DAP','DAP')")
    conn.execute("INSERT INTO teacher_auth VALUES ('DAP', ?)", (generate_password_hash("DAP"),))
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    c = mod.app.test_client()
    r = c.post("/api/faculty/login", json={"code": "DAP", "password": "DAP"})
    assert r.get_json()["must_change"] is True


# ---------- static files / pages ----------

def test_pages_and_logo_load(app_mod):
    c = app_mod.app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/faculty").status_code == 200
    assert c.get("/static/logo.svg").status_code == 200
    assert b"password <b>DAP</b>" not in c.get("/static/faculty.js").data


# ---------- timetable edit ----------

def test_edit_slot_rejects_bad_input(app_mod):
    c = login(app_mod.app.test_client(), app_mod)
    sid = first_slot(c)["id"]
    url = f"/api/faculty/timetable/{sid}"
    assert c.patch(url, json={"start_period": "x"}).status_code == 400          # was a 500 crash
    assert c.patch(url, json={"start_period": 9, "end_period": 12}).status_code == 400   # used to make the class vanish
    assert c.patch(url, json={"start_period": 0, "end_period": 1}).status_code == 400
    assert c.patch(url, json={"day": "Sun"}).status_code == 400
    assert c.patch(url, json={"subject_code": "PHY"}).status_code == 403
    assert c.patch(url, json={"room": "<script>"}).status_code == 400
    assert len(c.get("/api/faculty/schedule").get_json()["schedule"]) == 12


def test_edit_slot_detects_clashes(app_mod):
    c = login(app_mod.app.test_client(), app_mod)
    sched = c.get("/api/faculty/schedule").get_json()["schedule"]
    a = sched[0]
    # Move it onto another class of the same students -> clash
    conn = app_mod.get_conn()
    other = conn.execute(
        "SELECT * FROM timetable WHERE branch_id=? AND batch IS NULL AND id<>? AND day<>?",
        (a["branch_id"], a["id"], a["day"])).fetchone()
    conn.close()
    r = c.patch(f"/api/faculty/timetable/{a['id']}",
                json={"day": other["day"], "start_period": other["start_period"], "end_period": other["end_period"]})
    assert r.status_code == 409
    assert "already have" in r.get_json()["error"]


def test_edit_slot_valid_change_saves(app_mod):
    c = login(app_mod.app.test_client(), app_mod)
    s = first_slot(c)
    r = c.patch(f"/api/faculty/timetable/{s['id']}",
                json={"day": s["day"], "start_period": s["start_period"], "end_period": s["end_period"], "room": "LAB-2"})
    assert r.status_code == 200, r.get_json()
    assert first_slot(c)["room"] == "LAB-2"


# ---------- attendance ----------

def test_attendance_rejects_future_and_wrong_day(app_mod):
    c = login(app_mod.app.test_client(), app_mod)
    s = first_slot(c)
    roster = c.get(f"/api/faculty/class/{s['id']}?date={last_date_for(app_mod, s['day'])}").get_json()["roster"]
    recs = [{"roll_no": x["roll_no"], "status": "Present"} for x in roster]

    future = (app_mod.today_ist() + timedelta(days=7)).isoformat()
    r = c.post("/api/faculty/attendance", json={"timetable_id": s["id"], "date": future, "records": recs})
    assert r.status_code == 400 and "future" in r.get_json()["error"]
    r = c.post("/api/faculty/attendance", json={"timetable_id": s["id"], "date": "2099-01-01", "records": recs})
    assert r.status_code == 400

    wrong = last_date_for(app_mod, s["day"]) - timedelta(days=1)
    r = c.post("/api/faculty/attendance", json={"timetable_id": s["id"], "date": wrong.isoformat(), "records": recs})
    assert r.status_code == 400 and "runs on" in r.get_json()["error"]

    r = c.post("/api/faculty/attendance", json={"timetable_id": s["id"], "date": "not-a-date", "records": recs})
    assert r.status_code == 400


def test_attendance_requires_every_student_marked(app_mod):
    c = login(app_mod.app.test_client(), app_mod)
    s = first_slot(c)
    d = last_date_for(app_mod, s["day"]).isoformat()
    roster = c.get(f"/api/faculty/class/{s['id']}?date={d}").get_json()["roster"]
    assert all(x["status"] is None for x in roster)   # no longer defaults to Present
    half = [{"roll_no": x["roll_no"], "status": "Present"} for x in roster[:3]]
    r = c.post("/api/faculty/attendance", json={"timetable_id": s["id"], "date": d, "records": half})
    assert r.status_code == 400 and "not marked" in r.get_json()["error"]


def test_attendance_save_report_and_csv(app_mod):
    c = login(app_mod.app.test_client(), app_mod)
    s = first_slot(c)
    d = last_date_for(app_mod, s["day"])
    roster = c.get(f"/api/faculty/class/{s['id']}?date={d}").get_json()["roster"]
    # First student absent, everyone else present
    recs = [{"roll_no": x["roll_no"], "status": "Absent" if i == 0 else "Present"} for i, x in enumerate(roster)]
    r = c.post("/api/faculty/attendance", json={"timetable_id": s["id"], "date": d.isoformat(), "records": recs})
    assert r.status_code == 200
    assert r.get_json()["present"] == len(roster) - 1

    month = d.strftime("%Y-%m")
    rep = c.get(f"/api/faculty/class/{s['id']}/monthly?month={month}").get_json()
    assert rep["held"] == 1
    assert rep["roster"][0]["percent"] == 0 and rep["roster"][0]["low"] is True
    assert rep["roster"][1]["percent"] == 100

    csv_resp = c.get(f"/api/faculty/class/{s['id']}/monthly.csv?month={month}")
    assert csv_resp.status_code == 200
    assert csv_resp.mimetype == "text/csv"
    body = csv_resp.data.decode("utf-8-sig")
    assert roster[0]["roll_no"] in body and "YES" in body

    # Student side shows the 75% calculator
    st = app_mod.app.test_client().get(f"/api/student/{roster[0]['roll_no']}/attendance?month={month}").get_json()
    assert st["totals"]["low"] is True and st["totals"]["need"] == 3   # 0/1 -> needs 3 in a row (3/4 = 75%)
    st2 = app_mod.app.test_client().get(f"/api/student/{roster[1]['roll_no']}/attendance?month={month}").get_json()
    assert st2["totals"]["low"] is False and st2["totals"]["can_skip"] == 0   # 1/1 -> missing 1 gives 50%


def test_cannot_touch_another_teachers_class(app_mod):
    c = login(app_mod.app.test_client(), app_mod, "DAP")
    conn = app_mod.get_conn()
    other_id = conn.execute("SELECT id FROM timetable WHERE faculty_code <> 'DAP' LIMIT 1").fetchone()["id"]
    conn.close()
    assert c.get(f"/api/faculty/class/{other_id}").status_code == 404
    assert c.patch(f"/api/faculty/timetable/{other_id}", json={"room": "X"}).status_code == 404
    assert c.get(f"/api/faculty/class/{other_id}/monthly.csv").status_code == 404


# ---------- 75% maths ----------

@pytest.mark.parametrize("attended,held,need,skip", [
    (0, 0, 0, 0), (3, 4, 0, 0), (6, 8, 0, 0), (2, 4, 4, 0), (10, 10, 0, 3), (7, 10, 2, 0), (9, 12, 0, 0),
])
def test_75_percent_calculator(app_mod, attended, held, need, skip):
    assert app_mod.classes_to_reach(attended, held) == need
    assert app_mod.classes_can_skip(attended, held) == skip
    # Sanity: following the advice really lands at >= 75%
    if need:
        assert (attended + need) / (held + need) >= 0.75 > (attended + need - 1) / (held + need - 1)
    if skip:
        assert attended / (held + skip) >= 0.75 > attended / (held + skip + 1)


def test_init_is_safe_to_run_twice(app_mod):
    """Restarting the server (or a second worker) must not duplicate data or re-issue passwords."""
    before = len(passwords(app_mod))
    app_mod.init_db()
    conn = app_mod.get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
    conn.close()
    assert n == 264 and len(passwords(app_mod)) == before


def test_reset_password(app_mod):
    pw = app_mod.reset_password("dap")
    c = app_mod.app.test_client()
    r = c.post("/api/faculty/login", json={"code": "DAP", "password": pw})
    assert r.status_code == 200 and r.get_json()["must_change"] is True
    assert app_mod.reset_password("NOPE") is None


def test_student_lookup_still_works(app_mod):
    c = app_mod.app.test_client()
    r = c.get("/api/student/C01")
    assert r.status_code == 200 and r.get_json()["student"]["batch"] == "C1"
    assert c.get("/api/student/ZZ99").status_code == 404
    assert c.get("/api/student/C01/attendance?month=2026-13").status_code == 400
