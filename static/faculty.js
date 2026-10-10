const view = document.getElementById("view");
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const tm = t => { const [h, m] = t.split(":").map(Number); return (h % 12 || 12) + ":" + String(m).padStart(2, "0") + " " + (h >= 12 ? "PM" : "AM"); };
// Local (not UTC) dates — toISOString() gives yesterday's date in India before 5:30 AM.
const pad = n => String(n).padStart(2, "0");
const localDate = (d = new Date()) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
const localMonth = (d = new Date()) => d.getFullYear() + "-" + pad(d.getMonth() + 1);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"];

let me = null;          // {code, name}
let schedule = [];      // this faculty's timetable rows
let options = null;     // {periods, days}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  const json = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.endsWith("/login") && !path.endsWith("/password")) { me = null; renderLogin("Your session ended. Please log in again."); throw new Error("SESSION"); }
  if (res.status === 403 && json.error && json.error.includes("new password")) { renderChangePassword(true); throw new Error("SESSION"); }
  if (!res.ok) throw new Error(json.error || "Something went wrong.");
  return json;
}

// Run a screen; if loading it fails, show the error with a way back instead of a frozen page.
async function screen(fn) {
  try { await fn(); }
  catch (err) {
    if (err.message === "SESSION") return;
    view.innerHTML = topbar() + '<div class="msg err">' + esc(err.message) + '</div>' +
      '<div class="row" style="margin-top:14px"><button class="btn secondary" id="backBtn">Back to my classes</button></div>';
    bindTopbar();
    document.getElementById("backBtn").onclick = renderDashboard;
  }
}

async function boot() {
  try {
    const s = await api("/api/faculty/session");
    if (!s.logged_in) return renderLogin();
    me = { code: s.code, name: s.name };
    if (s.must_change) return renderChangePassword(true);
    await loadDashboard();
  } catch (e) { if (e.message !== "SESSION") renderLogin(); }
}

// ---------- login ----------
function renderLogin(msg) {
  view.innerHTML =
    '<p class="sub">Log in with your faculty code and the password given to you by the office. First-time passwords must be changed after you log in.</p>' +
    (msg ? '<div class="msg err">' + esc(msg) + '</div>' : '') +
    '<form class="stack" id="loginForm">' +
    '<div><label for="code">Faculty code</label><input id="code" autocomplete="username" autocapitalize="characters" required></div>' +
    '<div><label for="pw">Password</label><input id="pw" type="password" autocomplete="current-password" required></div>' +
    '<button class="btn" type="submit">Log in</button></form>';
  document.getElementById("loginForm").onsubmit = async e => {
    e.preventDefault();
    const code = document.getElementById("code").value.trim().toUpperCase();
    const password = document.getElementById("pw").value;
    try {
      const r = await api("/api/faculty/login", { method: "POST", body: JSON.stringify({ code, password }) });
      const s = await api("/api/faculty/session");
      me = { code: s.code, name: s.name };
      if (r.must_change) return renderChangePassword(true);
      await loadDashboard();
    } catch (err) { if (err.message !== "SESSION") renderLogin(err.message); }
  };
}

// ---------- dashboard ----------
async function loadDashboard() {
  const s = await api("/api/faculty/schedule");
  schedule = s.schedule;
  renderDashboard();
}

function topbar(title, forced) {
  return '<div class="topbar"><div><span class="pill">' + esc(me.code) + '</span> <span class="who">' + esc(me.name) + '</span></div>' +
    '<div class="row">' + (forced ? '' : '<button class="link" id="pwBtn">Change password</button>') + '<button class="link" id="logoutBtn">Log out</button></div></div>' +
    (title ? '<h2>' + esc(title) + '</h2>' : '');
}

function bindTopbar() {
  document.getElementById("logoutBtn").onclick = async () => { await api("/api/faculty/logout", { method: "POST" }).catch(() => {}); me = null; renderLogin(); };
  const pw = document.getElementById("pwBtn");
  if (pw) pw.onclick = () => renderChangePassword(false);
}

// Most recent date (today or earlier) on which this weekday falls.
function lastDateFor(day) {
  const d = new Date();
  const target = DAYS.indexOf(day) + 1;   // JS: Mon = 1
  while (d.getDay() !== target) d.setDate(d.getDate() - 1);
  return localDate(d);
}

function renderDashboard() {
  const byDay = {};
  schedule.forEach(s => (byDay[s.day] = byDay[s.day] || []).push(s));
  const cards = DAYS.filter(d => byDay[d]).map(d =>
    '<h3 style="margin:18px 0 8px">' + d + '</h3>' + byDay[d].map(s =>
      '<div class="class-card"><h3>' + esc(s.subject) + '</h3>' +
      '<div class="meta">' + tm(s.start) + '–' + tm(s.end) + ' &middot; <b>' + esc(s.branch) + '</b>' +
      (s.batch ? ' batch <b>' + esc(s.batch) + '</b>' : '') + ' &middot; Room <b>' + esc(s.room) + '</b></div>' +
      '<div class="row"><button class="btn small" data-att="' + s.id + '">Take attendance</button>' +
      '<button class="btn small secondary" data-hist="' + s.id + '">Records</button>' +
      '<button class="btn small secondary" data-month="' + s.id + '">Monthly report</button>' +
      '<button class="btn small secondary" data-edit="' + s.id + '">Edit slot</button></div></div>'
    ).join("")
  ).join("");
  view.innerHTML = topbar() + (cards || '<p class="free">No classes assigned to your code yet.</p>');
  bindTopbar();
  view.querySelectorAll("[data-att]").forEach(b => b.onclick = () => screen(() => renderAttendance(Number(b.dataset.att))));
  view.querySelectorAll("[data-hist]").forEach(b => b.onclick = () => screen(() => renderHistory(Number(b.dataset.hist))));
  view.querySelectorAll("[data-month]").forEach(b => b.onclick = () => screen(() => renderMonthly(Number(b.dataset.month))));
  view.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => screen(() => renderEdit(Number(b.dataset.edit))));
}

// ---------- attendance ----------
async function renderAttendance(timetableId, date) {
  const slot = schedule.find(s => s.id === timetableId);
  date = date || lastDateFor(slot.day);
  let data, loadErr = null;
  try { data = await api("/api/faculty/class/" + timetableId + "?date=" + date); }
  catch (err) { if (err.message === "SESSION") return; loadErr = err.message; }

  const rows = data ? data.roster.map(st =>
    '<tr data-roll="' + esc(st.roll_no) + '"><td>' + st.sr_no + '</td><td>' + esc(st.name) + ' <span class="pill">' + esc(st.roll_no) + '</span></td>' +
    '<td><div class="status-toggle"><button type="button" class="present' + (st.status === "Present" ? " on" : "") + '" data-s="Present">Present</button>' +
    '<button type="button" class="absent' + (st.status === "Absent" ? " on" : "") + '" data-s="Absent">Absent</button></div></td></tr>'
  ).join("") : "";

  view.innerHTML = topbar(slot.subject) +
    '<p class="meta">' + esc(slot.branch) + (slot.batch ? ' &middot; batch ' + esc(slot.batch) : '') + ' &middot; every ' + esc(slot.day) + '</p>' +
    '<div class="row"><label for="attDate" style="margin:0">Date</label><input type="date" id="attDate" value="' + esc(date) + '" max="' + localDate() + '"></div>' +
    (loadErr ? '<div class="msg err">' + esc(loadErr) + '</div>' :
      (data.already_marked ? '<p class="pill" style="margin-top:8px">Already marked for this date — showing saved attendance</p>' : '') +
      '<div class="row" style="margin-top:12px"><button class="btn small secondary" id="allPresent">Mark all present</button><button class="btn small secondary" id="allAbsent">Mark all absent</button></div>' +
      '<table class="roster"><thead><tr><th>#</th><th>Student</th><th>Status</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div class="summary"><span><b id="presentCount"></b> present</span><span><b id="absentCount"></b> absent</span><span><b id="unmarkedCount"></b> not marked</span></div>' +
      '<div class="row"><button class="btn" id="saveAtt">Save attendance</button></div>') +
    '<div class="row" style="margin-top:10px"><button class="btn secondary" id="backBtn">Back</button></div>' +
    '<div id="attMsg"></div>';
  bindTopbar();
  document.getElementById("attDate").onchange = e => screen(() => renderAttendance(timetableId, e.target.value));
  document.getElementById("backBtn").onclick = renderDashboard;
  if (loadErr) return;

  const updateCount = () => {
    const total = data.roster.length;
    const p = view.querySelectorAll(".status-toggle button.present.on").length;
    const a = view.querySelectorAll(".status-toggle button.absent.on").length;
    document.getElementById("presentCount").textContent = p;
    document.getElementById("absentCount").textContent = a;
    document.getElementById("unmarkedCount").textContent = total - p - a;
  };
  const setAll = cls => { view.querySelectorAll(".status-toggle").forEach(g => { g.querySelectorAll("button").forEach(b => b.classList.remove("on")); g.querySelector(cls).classList.add("on"); }); updateCount(); };
  view.querySelectorAll(".status-toggle button").forEach(b => b.onclick = () => {
    b.parentElement.querySelectorAll("button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    updateCount();
  });
  updateCount();
  document.getElementById("allPresent").onclick = () => setAll(".present");
  document.getElementById("allAbsent").onclick = () => setAll(".absent");
  document.getElementById("saveAtt").onclick = async () => {
    const msg = document.getElementById("attMsg");
    const trs = [...view.querySelectorAll("tr[data-roll]")];
    const unmarked = trs.filter(tr => !tr.querySelector(".status-toggle button.on"));
    if (unmarked.length) {
      msg.innerHTML = '<div class="msg err">' + unmarked.length + ' student(s) not marked yet. Tap Present or Absent for each, or use "Mark all present".</div>';
      unmarked[0].scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const records = trs.map(tr => ({ roll_no: tr.dataset.roll, status: tr.querySelector(".status-toggle button.on").dataset.s }));
    try {
      const r = await api("/api/faculty/attendance", { method: "POST", body: JSON.stringify({ timetable_id: timetableId, date, records }) });
      msg.innerHTML = '<div class="msg ok">Saved — ' + r.present + ' of ' + r.total + ' present. Recorded at ' + esc(r.saved_at) + ' as proof.</div>';
    } catch (err) { if (err.message !== "SESSION") msg.innerHTML = '<div class="msg err">' + esc(err.message) + '</div>'; }
  };
}

// ---------- history (attendance proof records) ----------
async function renderHistory(timetableId) {
  const slot = schedule.find(s => s.id === timetableId);
  const data = await api("/api/faculty/attendance/history/" + timetableId);
  const items = data.history.map(h =>
    '<li><span>' + esc(h.date) + '</span><span>' + h.present + '/' + h.total + ' present <span class="pill">' + esc(h.marked_at) + '</span></span></li>'
  ).join("");
  view.innerHTML = topbar(slot.subject + " — records") +
    (items ? '<ul class="history-list">' + items + '</ul>' : '<p class="free">No attendance recorded yet for this class.</p>') +
    '<div class="row" style="margin-top:14px"><button class="btn secondary" id="backBtn">Back</button></div>';
  bindTopbar();
  document.getElementById("backBtn").onclick = renderDashboard;
}

// ---------- monthly report (held vs. attended, per student) ----------
async function renderMonthly(timetableId, month) {
  month = month || localMonth();
  const data = await api("/api/faculty/class/" + timetableId + "/monthly?month=" + encodeURIComponent(month));
  const low = data.roster.filter(s => s.low).length;
  const rows = data.roster.map(s =>
    '<tr' + (s.low ? ' class="row-low"' : '') + '><td>' + s.sr_no + '</td><td>' + esc(s.name) + ' <span class="pill">' + esc(s.roll_no) + '</span></td>' +
    '<td>' + s.attended + '</td><td>' + (s.percent === null ? '—' : '<span class="' + (s.low ? 'pct-low' : 'pct-ok') + '">' + s.percent + '%</span>') + '</td></tr>'
  ).join("");
  view.innerHTML = topbar(data.subject + " — monthly report") +
    '<div class="row"><label for="mMonth" style="margin:0">Month</label><input type="month" id="mMonth" value="' + esc(month) + '" max="' + localMonth() + '">' +
    '<a class="btn small secondary" style="text-decoration:none" href="/api/faculty/class/' + timetableId + '/monthly.csv?month=' + encodeURIComponent(month) + '" download>Download Excel (CSV)</a></div>' +
    '<p class="meta" style="margin-top:8px"><b>' + data.held + '</b> lecture' + (data.held === 1 ? "" : "s") + ' held this month' +
    (data.held ? ' &middot; <b class="pct-low">' + low + '</b> below ' + data.target + '%' : '') + '.</p>' +
    (data.roster.length
      ? '<table class="roster"><thead><tr><th>#</th><th>Student</th><th>Attended</th><th>%</th></tr></thead><tbody>' + rows + '</tbody></table>'
      : '<p class="free">No students in this class.</p>') +
    '<div class="row" style="margin-top:14px"><button class="btn secondary" id="backBtn">Back</button></div>';
  bindTopbar();
  document.getElementById("backBtn").onclick = renderDashboard;
  document.getElementById("mMonth").onchange = e => screen(() => renderMonthly(timetableId, e.target.value));
}

// ---------- edit timetable slot (day, time, room — subject changes are admin-only) ----------
async function renderEdit(timetableId) {
  const slot = schedule.find(s => s.id === timetableId);
  if (!options) options = await api("/api/faculty/options");
  const dayOpts = options.days.map(d => '<option ' + (d === slot.day ? "selected" : "") + '>' + d + '</option>').join("");
  const perOpts = (f, key) => options.periods.map(p => '<option value="' + p.no + '" ' + (p.no === slot[f] ? "selected" : "") + '>' + p.no + ' (' + tm(p[key]) + ')</option>').join("");

  view.innerHTML = topbar("Edit slot — " + slot.subject) +
    '<p class="meta">' + esc(slot.branch) + (slot.batch ? ' &middot; batch ' + esc(slot.batch) : '') + '. The system blocks times that clash with the same students, your other classes, or the same room.</p>' +
    '<form class="stack" id="editForm">' +
    '<div class="edit-grid"><div><label for="day">Day</label><select id="day">' + dayOpts + '</select></div>' +
    '<div><label for="room">Room</label><input id="room" maxlength="20" value="' + esc(slot.room || "") + '"></div>' +
    '<div><label for="startP">Start period</label><select id="startP">' + perOpts("start_period", "start_time") + '</select></div>' +
    '<div><label for="endP">End period</label><select id="endP">' + perOpts("end_period", "end_time") + '</select></div></div>' +
    '<div class="row"><button class="btn" type="submit">Save changes</button><button class="btn secondary" type="button" id="backBtn">Cancel</button></div>' +
    '</form><div id="editMsg"></div>';
  bindTopbar();
  document.getElementById("backBtn").onclick = renderDashboard;
  document.getElementById("editForm").onsubmit = async e => {
    e.preventDefault();
    const body = {
      day: document.getElementById("day").value,
      start_period: Number(document.getElementById("startP").value),
      end_period: Number(document.getElementById("endP").value),
      room: document.getElementById("room").value,
    };
    const msg = document.getElementById("editMsg");
    try {
      await api("/api/faculty/timetable/" + timetableId, { method: "PATCH", body: JSON.stringify(body) });
      await loadDashboard();
    } catch (err) { if (err.message !== "SESSION") msg.innerHTML = '<div class="msg err">' + esc(err.message) + '</div>'; }
  };
}

// ---------- change password (forced on first login) ----------
function renderChangePassword(forced) {
  view.innerHTML = topbar("Change password", forced) +
    (forced ? '<div class="msg err">You are using a temporary password. Set your own password to continue.</div>' : '') +
    '<form class="stack" id="pwForm">' +
    '<div><label for="cur">Current password</label><input type="password" id="cur" autocomplete="current-password" required></div>' +
    '<div><label for="new">New password (at least 8 characters)</label><input type="password" id="new" autocomplete="new-password" required minlength="8"></div>' +
    '<div><label for="new2">Repeat new password</label><input type="password" id="new2" autocomplete="new-password" required minlength="8"></div>' +
    '<div class="row"><button class="btn" type="submit">Update password</button>' + (forced ? '' : '<button class="btn secondary" type="button" id="backBtn">Cancel</button>') + '</div>' +
    '</form><div id="pwMsg"></div>';
  bindTopbar();
  if (!forced) document.getElementById("backBtn").onclick = renderDashboard;
  document.getElementById("pwForm").onsubmit = async e => {
    e.preventDefault();
    const msg = document.getElementById("pwMsg");
    const nw = document.getElementById("new").value;
    if (nw !== document.getElementById("new2").value) { msg.innerHTML = '<div class="msg err">The two new passwords don\'t match.</div>'; return; }
    try {
      await api("/api/faculty/password", { method: "POST", body: JSON.stringify({ current: document.getElementById("cur").value, new: nw }) });
      if (forced) { await loadDashboard(); return; }
      msg.innerHTML = '<div class="msg ok">Password updated.</div>';
    } catch (err) { if (err.message !== "SESSION") msg.innerHTML = '<div class="msg err">' + esc(err.message) + '</div>'; }
  };
}

boot();
