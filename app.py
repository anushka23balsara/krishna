import csv
import io
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import date as date_cls, datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from data_seed import BRANCHES, FACULTY, PERIODS, STUDENTS, SUBJECTS, TIMETABLE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
DAY_ORDER = "CASE t.day WHEN 'Mon' THEN 1 WHEN 'Tue' THEN 2 WHEN 'Wed' THEN 3 WHEN 'Thu' THEN 4 WHEN 'Fri' THEN 5 END"
MIN_ATTENDANCE = 75  # percent required by the university

# Vercel's project folder is read-only, so data has to live in /tmp there (it resets on cold starts).
DATA_DIR = os.environ.get("DATA_DIR") or ("/tmp" if os.environ.get("VERCEL") else BASE_DIR)


def _load_secret_key():
    """SECRET_KEY env var in production; otherwise a random key saved next to the database."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, ".secret_key")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        return key


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config.update(
    # Neon / any Postgres: set DATABASE_URL. Without it, a local SQLite file is used.
    DATABASE_URL=os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL"),
    DATABASE=os.environ.get("DATABASE_PATH") or os.path.join(DATA_DIR, "college.db"),
    PASSWORD_FILE=os.environ.get("PASSWORD_FILE") or os.path.join(DATA_DIR, "faculty_passwords.txt"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
)


def today_ist():
    return datetime.now(IST).date()


def now_ist_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------- database: Neon / Postgres when DATABASE_URL is set, otherwise a local SQLite file ----------

class DB:
    """Tiny wrapper so the same SQL (written with ? placeholders) runs on SQLite and on Postgres (Neon)."""

    def __init__(self):
        url = app.config.get("DATABASE_URL")
        self.pg = bool(url)
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            # prepare_threshold=None keeps it working through Neon's connection pooler (PgBouncer).
            self.conn = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None, connect_timeout=10)
        else:
            self.conn = sqlite3.connect(app.config["DATABASE"])
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql, params=()):
        return self.conn.execute(self._sql(sql), params)

    def executemany(self, sql, rows):
        if self.pg:
            with self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), rows)
        else:
            self.conn.executemany(sql, rows)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_conn():
    return DB()


SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    default_room TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    roll_no TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    batch TEXT NOT NULL,
    sr_no INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS faculty (
    code TEXT PRIMARY KEY,
    full_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS periods (
    no INTEGER PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timetable (
    id {AUTO_ID},
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    day TEXT NOT NULL,
    start_period INTEGER NOT NULL,
    end_period INTEGER NOT NULL,
    batch TEXT,
    subject_code TEXT NOT NULL REFERENCES subjects(code),
    faculty_code TEXT REFERENCES faculty(code),
    room TEXT
);
CREATE TABLE IF NOT EXISTS teacher_auth (
    code TEXT PRIMARY KEY REFERENCES faculty(code),
    password_hash TEXT NOT NULL,
    must_change INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attendance (
    id {AUTO_ID},
    timetable_id INTEGER NOT NULL REFERENCES timetable(id),
    date TEXT NOT NULL,
    roll_no TEXT NOT NULL REFERENCES students(roll_no),
    status TEXT NOT NULL CHECK(status IN ('Present','Absent')),
    marked_by TEXT NOT NULL REFERENCES faculty(code),
    marked_at TEXT NOT NULL,
    UNIQUE(timetable_id, date, roll_no)
);
CREATE INDEX IF NOT EXISTS idx_att_slot_date ON attendance(timetable_id, date);
CREATE INDEX IF NOT EXISTS idx_tt_faculty ON timetable(faculty_code);
CREATE INDEX IF NOT EXISTS idx_tt_branch ON timetable(branch_id);
"""


def init_db():
    """Create tables and load sample data the first time (works on an empty Neon database too).
    Also upgrades a college.db made by the old version."""
    db = get_conn()
    try:
        if db.pg:
            db.execute("SELECT pg_advisory_lock(424242)")  # stop two server workers seeding at the same time
            for stmt in SCHEMA.replace("{AUTO_ID}", "SERIAL PRIMARY KEY").split(";"):
                if stmt.strip():
                    db.execute(stmt)
            db.execute("ALTER TABLE teacher_auth ADD COLUMN IF NOT EXISTS must_change INTEGER NOT NULL DEFAULT 1")
        else:
            db.conn.executescript(SCHEMA.replace("{AUTO_ID}", "INTEGER PRIMARY KEY AUTOINCREMENT"))
            cols = {r["name"] for r in db.execute("PRAGMA table_info(teacher_auth)")}
            if "must_change" not in cols:
                # Old version: no must_change column and password == code. Anyone still on that must change it.
                db.execute("ALTER TABLE teacher_auth ADD COLUMN must_change INTEGER NOT NULL DEFAULT 1")
                for r in db.execute("SELECT code, password_hash FROM teacher_auth").fetchall():
                    db.execute("UPDATE teacher_auth SET must_change=? WHERE code=?",
                               (1 if check_password_hash(r["password_hash"], r["code"]) else 0, r["code"]))

        is_new = db.execute("SELECT COUNT(*) AS n FROM branches").fetchone()["n"] == 0
        if is_new:
            db.executemany("INSERT INTO branches VALUES (?,?,?)", BRANCHES)
            db.executemany("INSERT INTO periods VALUES (?,?,?)", PERIODS)
            db.executemany("INSERT INTO subjects VALUES (?,?)", SUBJECTS)
            db.executemany("INSERT INTO faculty VALUES (?,?)", FACULTY)
            db.executemany(
                "INSERT INTO students (roll_no, name, branch_id, batch, sr_no) VALUES (?,?,?,?,?)",
                [(roll, name, branch_i + 1, batch, sr) for roll, name, batch, sr, branch_i in STUDENTS],
            )
            db.executemany(
                "INSERT INTO timetable (branch_id, day, start_period, end_period, batch, subject_code, faculty_code, room) "
                "VALUES (?,?,?,?,?,?,?,?)",
                TIMETABLE,
            )

        # Every professor without a login gets a random one-time password. They must change it on first login.
        missing = [r["code"] for r in db.execute(
            "SELECT f.code FROM faculty f LEFT JOIN teacher_auth a ON a.code = f.code WHERE a.code IS NULL ORDER BY f.code")]
        issued = []
        for code in missing:
            pw = secrets.token_urlsafe(6)
            db.execute("INSERT INTO teacher_auth (code, password_hash, must_change) VALUES (?,?,1)",
                       (code, generate_password_hash(pw)))
            issued.append((code, pw))
        db.commit()
    finally:
        db.close()

    if issued:
        try:
            with open(app.config["PASSWORD_FILE"], "a", encoding="utf-8") as f:
                f.write(f"# One-time faculty passwords issued {now_ist_str()} (hand out privately, then delete this file)\n")
                f.writelines(f"{code}\t{pw}\n" for code, pw in issued)
            print(f"Issued {len(issued)} one-time passwords -> {app.config['PASSWORD_FILE']}")
        except OSError:
            for code, pw in issued:
                print(f"one-time password  {code}: {pw}")
    if is_new:
        print("Created the tables and loaded sample data" + (" in Postgres." if app.config.get("DATABASE_URL") else f" in {app.config['DATABASE']}."))


def reset_password(code):
    """Give one faculty member a fresh one-time password (e.g. they forgot theirs)."""
    pw = secrets.token_urlsafe(6)
    db = get_conn()
    try:
        cur = db.execute("UPDATE teacher_auth SET password_hash=?, must_change=1 WHERE code=?",
                         (generate_password_hash(pw), code.upper()))
        db.commit()
        if cur.rowcount == 0:
            return None
    finally:
        db.close()
    return pw


# ---------- helpers ----------

def bad(msg, status=400):
    return jsonify(error=msg), status


def login_required(allow_must_change=False):
    """Decorator: needs a faculty session; blocks everything except password change until the temp password is replaced."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            code = session.get("faculty_code")
            if not code:
                return bad("Please log in again.", 401)
            if session.get("must_change") and not allow_must_change:
                return bad("Please set a new password first.", 403)
            return fn(code, *args, **kwargs)
        return wrapper
    return deco


def parse_date(value):
    """Returns (date, error). Defaults to today in IST."""
    if not value:
        return today_ist(), None
    try:
        return date_cls.fromisoformat(value), None
    except (TypeError, ValueError):
        return None, "Date should look like 2026-09-15."


def check_class_date(slot, d):
    if d > today_ist():
        return "You can't mark attendance for a future date."
    if d.weekday() >= 5 or DAYS[d.weekday()] != slot["day"]:
        return f"This class runs on {slot['day']}, but {d.isoformat()} is a {d.strftime('%a')}."
    return None


def parse_month():
    month = request.args.get("month") or today_ist().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return None
    return month


def pct(a, h):
    return round(a * 100 / h, 1) if h else None


def classes_to_reach(attended, held, target=MIN_ATTENDANCE):
    """How many more lectures in a row the student must attend to reach target%."""
    need = (target * held - 100 * attended) / (100 - target)
    return max(0, math.ceil(need))


def classes_can_skip(attended, held, target=MIN_ATTENDANCE):
    """How many lectures the student can miss and still stay at or above target%."""
    if not held:
        return 0
    spare = (100 * attended - target * held) / target
    return max(0, math.floor(spare))


def _own_slot(conn, code, timetable_id):
    return conn.execute("SELECT * FROM timetable WHERE id=? AND faculty_code=?", (timetable_id, code)).fetchone()


def _roster(conn, slot):
    return conn.execute(
        "SELECT roll_no, name, sr_no FROM students WHERE branch_id=? AND (CAST(? AS TEXT) IS NULL OR batch=?) ORDER BY sr_no",
        (slot["branch_id"], slot["batch"], slot["batch"]),
    ).fetchall()


# Simple per-IP login throttle: 8 failures in 10 minutes locks that IP out for the rest of the window.
_failed_logins = {}


def _too_many_failures(ip):
    now = time.time()
    hits = [t for t in _failed_logins.get(ip, []) if now - t < 600]
    _failed_logins[ip] = hits
    return len(hits) >= 8


# ---------- student pages ----------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/student/<roll_no>")
def get_student(roll_no):
    roll_no = roll_no.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,6}", roll_no):
        return bad("Enter a valid roll number, like CE01.")

    conn = get_conn()
    try:
        student = conn.execute(
            "SELECT s.roll_no, s.name, s.batch, s.sr_no, b.id AS branch_id, b.name AS branch, "
            "  (SELECT COUNT(*) FROM students x WHERE x.branch_id = s.branch_id AND x.batch = s.batch) AS batch_size "
            "FROM students s JOIN branches b ON b.id = s.branch_id WHERE s.roll_no = ?",
            (roll_no,),
        ).fetchone()

        if not student:
            partial = conn.execute(
                "SELECT 1 FROM students WHERE roll_no LIKE ? LIMIT 1", (roll_no + "%",)
            ).fetchone() is not None
            return jsonify(error=f"No student found with roll number {roll_no}.", partial=partial), 404

        student = dict(student)
        branch_id = student.pop("branch_id")

        rows = conn.execute(
            "SELECT t.day, t.subject_code AS code, sub.name AS subject, "
            "  (t.batch IS NOT NULL) AS batch_specific, "
            "  COALESCE(f.full_name, t.faculty_code) AS faculty, "
            "  COALESCE(t.room, CASE WHEN t.subject_code IN ('LIB','SL') THEN NULL ELSE b.default_room END) AS room, "
            "  p1.start_time AS start, p2.end_time AS end "
            "FROM timetable t "
            "JOIN branches b ON b.id = t.branch_id "
            "JOIN subjects sub ON sub.code = t.subject_code "
            "LEFT JOIN faculty f ON f.code = t.faculty_code "
            "JOIN periods p1 ON p1.no = t.start_period "
            "JOIN periods p2 ON p2.no = t.end_period "
            "WHERE t.branch_id = ? AND (t.batch IS NULL OR t.batch = ?) "
            f"ORDER BY {DAY_ORDER}, t.start_period",
            (branch_id, student["batch"]),
        ).fetchall()

        timetable = {}
        for r in rows:
            timetable.setdefault(r["day"], []).append({
                "code": r["code"], "subject": r["subject"], "faculty": r["faculty"],
                "room": r["room"], "batch_specific": bool(r["batch_specific"]),
                "start": r["start"], "end": r["end"],
            })
        return jsonify(student=student, timetable=timetable)
    finally:
        conn.close()


@app.route("/api/student/<roll_no>/attendance")
def student_attendance(roll_no):
    """Monthly attendance: per subject, lectures held vs attended, plus the 75% rule calculator."""
    roll_no = roll_no.strip().upper()
    month = parse_month()
    if not month:
        return bad("Month should look like 2026-09.")
    like = month + "-%"

    conn = get_conn()
    try:
        student = conn.execute(
            "SELECT roll_no, name, branch_id, batch FROM students WHERE roll_no=?", (roll_no,)
        ).fetchone()
        if not student:
            return bad(f"No student found with roll number {roll_no}.", 404)

        rows = conn.execute(
            "SELECT t.subject_code AS code, sub.name AS subject, "
            "  COALESCE(h.held, 0) AS held, COALESCE(a.attended, 0) AS attended "
            "FROM timetable t "
            "JOIN subjects sub ON sub.code = t.subject_code "
            "LEFT JOIN (SELECT timetable_id, COUNT(DISTINCT date) AS held FROM attendance "
            "           WHERE date LIKE ? GROUP BY timetable_id) h ON h.timetable_id = t.id "
            "LEFT JOIN (SELECT timetable_id, COUNT(*) AS attended FROM attendance "
            "           WHERE roll_no = ? AND status = 'Present' AND date LIKE ? GROUP BY timetable_id) a "
            "  ON a.timetable_id = t.id "
            "WHERE t.branch_id = ? AND (t.batch IS NULL OR t.batch = ?)",
            (like, roll_no, like, student["branch_id"], student["batch"]),
        ).fetchall()
    finally:
        conn.close()

    by_subject = {}
    for r in rows:
        e = by_subject.setdefault(r["code"], {"code": r["code"], "subject": r["subject"], "held": 0, "attended": 0})
        e["held"] += r["held"]
        e["attended"] += r["attended"]

    report = sorted((e for e in by_subject.values() if e["code"] not in ("LIB", "SL")), key=lambda e: e["subject"])
    for e in report:
        e["percent"] = pct(e["attended"], e["held"])
        e["need"] = classes_to_reach(e["attended"], e["held"])
        e["can_skip"] = classes_can_skip(e["attended"], e["held"])
        e["low"] = e["percent"] is not None and e["percent"] < MIN_ATTENDANCE

    held = sum(e["held"] for e in report)
    attended = sum(e["attended"] for e in report)
    totals = {"held": held, "attended": attended, "percent": pct(attended, held),
              "need": classes_to_reach(attended, held), "can_skip": classes_can_skip(attended, held)}
    totals["low"] = totals["percent"] is not None and totals["percent"] < MIN_ATTENDANCE

    return jsonify(month=month, roll_no=roll_no, target=MIN_ATTENDANCE, report=report, totals=totals)


# ---------- faculty portal ----------

@app.route("/faculty")
def faculty_page():
    return render_template("faculty.html")


@app.route("/api/faculty/session")
def faculty_session():
    code = session.get("faculty_code")
    if not code:
        return jsonify(logged_in=False)
    conn = get_conn()
    try:
        row = conn.execute("SELECT full_name FROM faculty WHERE code=?", (code,)).fetchone()
    finally:
        conn.close()
    return jsonify(logged_in=True, code=code, name=row["full_name"] if row else code,
                   must_change=bool(session.get("must_change")))


@app.route("/api/faculty/login", methods=["POST"])
def faculty_login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if _too_many_failures(ip):
        return bad("Too many wrong attempts. Try again in 10 minutes.", 429)
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip().upper()
    password = str(body.get("password", ""))
    conn = get_conn()
    try:
        row = conn.execute("SELECT password_hash, must_change FROM teacher_auth WHERE code=?", (code,)).fetchone()
    finally:
        conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        _failed_logins.setdefault(ip, []).append(time.time())
        return bad("Code or password is incorrect.", 401)
    session.clear()
    session["faculty_code"] = code
    session["must_change"] = bool(row["must_change"])
    return jsonify(ok=True, code=code, must_change=bool(row["must_change"]))


@app.route("/api/faculty/logout", methods=["POST"])
def faculty_logout():
    session.clear()
    return jsonify(ok=True)


@app.route("/api/faculty/password", methods=["POST"])
@login_required(allow_must_change=True)
def faculty_change_password(code):
    body = request.get_json(silent=True) or {}
    current = str(body.get("current", ""))
    new = str(body.get("new", ""))
    if len(new) < 8:
        return bad("New password should be at least 8 characters.")
    if new.upper() == code or new == current:
        return bad("Pick a password that is different from your code and your current password.")
    conn = get_conn()
    try:
        row = conn.execute("SELECT password_hash FROM teacher_auth WHERE code=?", (code,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], current):
            return bad("Current password is incorrect.", 401)
        conn.execute("UPDATE teacher_auth SET password_hash=?, must_change=0 WHERE code=?",
                     (generate_password_hash(new), code))
        conn.commit()
    finally:
        conn.close()
    session["must_change"] = False
    return jsonify(ok=True)


@app.route("/api/faculty/schedule")
@login_required()
def faculty_schedule(code):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.day, t.start_period, t.end_period, t.batch, t.subject_code AS code, "
            "  sub.name AS subject, b.name AS branch, b.id AS branch_id, "
            "  COALESCE(t.room, b.default_room) AS room, p1.start_time AS start, p2.end_time AS end "
            "FROM timetable t "
            "JOIN branches b ON b.id = t.branch_id "
            "JOIN subjects sub ON sub.code = t.subject_code "
            "JOIN periods p1 ON p1.no = t.start_period "
            "JOIN periods p2 ON p2.no = t.end_period "
            "WHERE t.faculty_code = ? "
            f"ORDER BY {DAY_ORDER}, t.start_period",
            (code,),
        ).fetchall()
    finally:
        conn.close()
    return jsonify(schedule=[dict(r) for r in rows])


@app.route("/api/faculty/options")
@login_required()
def faculty_options(code):
    conn = get_conn()
    try:
        periods = [dict(r) for r in conn.execute("SELECT no, start_time, end_time FROM periods ORDER BY no")]
    finally:
        conn.close()
    return jsonify(periods=periods, days=list(DAYS))


@app.route("/api/faculty/class/<int:timetable_id>")
@login_required()
def faculty_class(code, timetable_id):
    d, err = parse_date(request.args.get("date"))
    if err:
        return bad(err)
    conn = get_conn()
    try:
        slot = _own_slot(conn, code, timetable_id)
        if not slot:
            return bad("That class is not on your timetable.", 404)
        err = check_class_date(slot, d)
        if err:
            return bad(err)
        students = _roster(conn, slot)
        marked = {r["roll_no"]: r["status"] for r in conn.execute(
            "SELECT roll_no, status FROM attendance WHERE timetable_id=? AND date=?", (timetable_id, d.isoformat())
        )}
    finally:
        conn.close()
    # Unmarked students come back as null so the teacher must actually tick each one (or press "all present").
    roster = [{"roll_no": s["roll_no"], "name": s["name"], "sr_no": s["sr_no"],
               "status": marked.get(s["roll_no"])} for s in students]
    return jsonify(date=d.isoformat(), batch=slot["batch"], roster=roster, already_marked=bool(marked))


@app.route("/api/faculty/attendance", methods=["POST"])
@login_required()
def save_attendance(code):
    body = request.get_json(silent=True) or {}
    try:
        timetable_id = int(body.get("timetable_id"))
    except (TypeError, ValueError):
        return bad("Missing class.")
    d, err = parse_date(body.get("date"))
    if err:
        return bad(err)
    records = body.get("records")
    if not isinstance(records, list) or not records:
        return bad("No attendance to save.")

    conn = get_conn()
    try:
        slot = _own_slot(conn, code, timetable_id)
        if not slot:
            return bad("That class is not on your timetable.", 404)
        err = check_class_date(slot, d)
        if err:
            return bad(err)

        valid_rolls = {r["roll_no"] for r in _roster(conn, slot)}
        clean = {}
        for rec in records:
            if not isinstance(rec, dict):
                return bad("Bad attendance record.")
            roll_no, status = rec.get("roll_no"), rec.get("status")
            if roll_no not in valid_rolls:
                return bad(f"{roll_no} is not in this class.")
            if status not in ("Present", "Absent"):
                return bad(f"Mark {roll_no} as Present or Absent before saving.")
            clean[roll_no] = status
        missing = valid_rolls - clean.keys()
        if missing:
            return bad(f"{len(missing)} student(s) not marked yet: {', '.join(sorted(missing)[:5])}"
                       + ("…" if len(missing) > 5 else ""))

        now = now_ist_str()
        for roll_no, status in clean.items():
            conn.execute(
                "INSERT INTO attendance (timetable_id, date, roll_no, status, marked_by, marked_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(timetable_id, date, roll_no) DO UPDATE SET status=excluded.status, "
                "marked_by=excluded.marked_by, marked_at=excluded.marked_at",
                (timetable_id, d.isoformat(), roll_no, status, code, now),
            )
        conn.commit()
    finally:
        conn.close()
    present = sum(1 for s in clean.values() if s == "Present")
    return jsonify(ok=True, saved_at=now, total=len(clean), present=present)


def _monthly(conn, code, timetable_id, month):
    slot = _own_slot(conn, code, timetable_id)
    if not slot:
        return None
    like = month + "-%"
    # Lectures for this subject may run across more than one weekly slot for this branch/batch.
    sibling_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM timetable WHERE branch_id=? AND ((batch IS NULL AND CAST(? AS TEXT) IS NULL) OR batch=?) AND subject_code=?",
        (slot["branch_id"], slot["batch"], slot["batch"], slot["subject_code"]),
    )]
    ph = ",".join("?" for _ in sibling_ids)
    held = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT timetable_id, date FROM attendance "
        f"WHERE timetable_id IN ({ph}) AND date LIKE ?) AS x",
        (*sibling_ids, like),
    ).fetchone()["n"]
    attended = {r["roll_no"]: r["n"] for r in conn.execute(
        f"SELECT roll_no, COUNT(*) AS n FROM attendance "
        f"WHERE timetable_id IN ({ph}) AND date LIKE ? AND status='Present' GROUP BY roll_no",
        (*sibling_ids, like),
    )}
    subject = conn.execute("SELECT name FROM subjects WHERE code=?", (slot["subject_code"],)).fetchone()["name"]
    roster = []
    for s in _roster(conn, slot):
        a = attended.get(s["roll_no"], 0)
        p = pct(a, held)
        roster.append({"roll_no": s["roll_no"], "name": s["name"], "sr_no": s["sr_no"], "attended": a,
                       "percent": p, "low": p is not None and p < MIN_ATTENDANCE})
    return {"month": month, "subject": subject, "held": held, "target": MIN_ATTENDANCE, "roster": roster}


@app.route("/api/faculty/class/<int:timetable_id>/monthly")
@login_required()
def faculty_monthly_report(code, timetable_id):
    month = parse_month()
    if not month:
        return bad("Month should look like 2026-09.")
    conn = get_conn()
    try:
        data = _monthly(conn, code, timetable_id, month)
    finally:
        conn.close()
    if not data:
        return bad("That class is not on your timetable.", 404)
    return jsonify(data)


@app.route("/api/faculty/class/<int:timetable_id>/monthly.csv")
@login_required()
def faculty_monthly_csv(code, timetable_id):
    """Same monthly report as a spreadsheet download (opens in Excel)."""
    month = parse_month()
    if not month:
        return bad("Month should look like 2026-09.")
    conn = get_conn()
    try:
        data = _monthly(conn, code, timetable_id, month)
    finally:
        conn.close()
    if not data:
        return bad("That class is not on your timetable.", 404)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{data['subject']} - attendance {month}", f"Lectures held: {data['held']}", f"Faculty: {code}"])
    w.writerow(["Sr no", "Roll no", "Name", "Attended", "Held", "Percent", f"Below {MIN_ATTENDANCE}%"])
    for s in data["roster"]:
        w.writerow([s["sr_no"], s["roll_no"], s["name"], s["attended"], data["held"],
                    "" if s["percent"] is None else s["percent"], "YES" if s["low"] else ""])
    safe = re.sub(r"[^A-Za-z0-9]+", "_", data["subject"]).strip("_")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{safe}_{month}.csv"'})


@app.route("/api/faculty/attendance/history/<int:timetable_id>")
@login_required()
def attendance_history(code, timetable_id):
    conn = get_conn()
    try:
        if not _own_slot(conn, code, timetable_id):
            return bad("That class is not on your timetable.", 404)
        rows = conn.execute(
            "SELECT date, COUNT(*) AS total, SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) AS present, "
            "MAX(marked_at) AS marked_at "
            "FROM attendance WHERE timetable_id=? GROUP BY date ORDER BY date DESC",
            (timetable_id,),
        ).fetchall()
    finally:
        conn.close()
    return jsonify(history=[dict(r) for r in rows])


def _find_clash(conn, slot, day, start, end, room):
    """Return a message if the new time overlaps the same batch, the same teacher, or the same room."""
    others = conn.execute(
        "SELECT t.*, sub.name AS subject, b.name AS branch, COALESCE(t.room, b.default_room) AS eff_room "
        "FROM timetable t JOIN subjects sub ON sub.code=t.subject_code JOIN branches b ON b.id=t.branch_id "
        "WHERE t.day=? AND t.id<>? AND t.start_period<=? AND t.end_period>=?",
        (day, slot["id"], end, start),
    ).fetchall()
    default_room = conn.execute("SELECT default_room FROM branches WHERE id=?", (slot["branch_id"],)).fetchone()["default_room"]
    my_room = room or default_room
    for o in others:
        when = f"{o['subject']} ({o['branch']}{' ' + o['batch'] if o['batch'] else ''})"
        if o["branch_id"] == slot["branch_id"] and (o["batch"] is None or slot["batch"] is None or o["batch"] == slot["batch"]):
            return f"Those students already have {when} at that time."
        if o["faculty_code"] and o["faculty_code"] == slot["faculty_code"]:
            return f"You already teach {when} at that time."
        if o["eff_room"] and my_room and o["eff_room"].upper() == my_room.upper() and o["subject_code"] not in ("LIB", "SL"):
            return f"Room {my_room} is already booked for {when} at that time."
    return None


@app.route("/api/faculty/timetable/<int:timetable_id>", methods=["PATCH"])
@login_required()
def edit_timetable(code, timetable_id):
    body = request.get_json(silent=True) or {}
    conn = get_conn()
    try:
        slot = _own_slot(conn, code, timetable_id)
        if not slot:
            return bad("That class is not on your timetable.", 404)

        if "subject_code" in body and body["subject_code"] != slot["subject_code"]:
            return bad("Only the timetable admin can change the subject of a slot.", 403)

        day = body.get("day", slot["day"])
        try:
            start_period = int(body.get("start_period", slot["start_period"]))
            end_period = int(body.get("end_period", slot["end_period"]))
        except (TypeError, ValueError):
            return bad("Periods must be numbers.")
        room = str(body.get("room") or "").strip()[:20] or None

        valid_periods = {r["no"] for r in conn.execute("SELECT no FROM periods")}
        if day not in DAYS:
            return bad("Not a valid day.")
        if start_period not in valid_periods or end_period not in valid_periods:
            return bad(f"Periods must be between {min(valid_periods)} and {max(valid_periods)}.")
        if end_period < start_period:
            return bad("End period can't be before the start period.")
        if room and not re.fullmatch(r"[A-Za-z0-9 \-/]+", room):
            return bad("Room can only contain letters, numbers, spaces, - and /.")

        clash = _find_clash(conn, slot, day, start_period, end_period, room)
        if clash:
            return bad(clash, 409)

        conn.execute("UPDATE timetable SET day=?, start_period=?, end_period=?, room=? WHERE id=?",
                     (day, start_period, end_period, room, timetable_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True)


@app.errorhandler(500)
def server_error(e):
    return bad("Something went wrong on the server.", 500)


# Runs on import too, so gunicorn / Render / Vercel get a ready database (not only `python app.py`).
init_db()

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "reset-password":
        new_pw = reset_password(sys.argv[2])
        print(f"{sys.argv[2].upper()} one-time password: {new_pw}" if new_pw else "No such faculty code.")
    elif len(sys.argv) == 2 and sys.argv[1] == "init-db":
        print("Database is ready.")   # init_db() already ran above
    else:
        app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
