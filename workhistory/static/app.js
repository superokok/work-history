"use strict";

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Work-History"] = "1";
    opts.body = JSON.stringify(body || {});
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `요청 실패 (${res.status})`);
    err.kind = data.kind;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3000);
}

async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message); }
}

const pad = (n) => String(n).padStart(2, "0");
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const hhmm = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const shiftDay = (day, delta) => { const d = new Date(day + "T00:00:00"); d.setDate(d.getDate() + delta); return isoDay(d); };
const appName = (app) => app.replace(/\.exe$/i, "");

function dur(s) {
  const m = Math.round(s / 60);
  if (m < 60) return `${m}분`;
  return `${Math.floor(m / 60)}시간 ${m % 60}분`;
}
function ago(ts) {
  // Calendar days, not 24-hour spans: yesterday 23:00 is "어제" even at 08:00 today.
  const days = Math.round((new Date(isoDay(new Date()) + "T00:00:00") - new Date(isoDay(new Date(ts * 1000)) + "T00:00:00")) / 86400000);
  const time = new Date(ts * 1000).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return days <= 0 ? `오늘 ${hhmm(ts)}` : days === 1 ? `어제 ${hhmm(ts)}` : `${days}일 전 (${time})`;
}
const badge = (kind, text) => h("span", { class: `badge ${kind}` }, text);

// ------------------------------------------------------------------ state
const state = {
  projects: [],
  day: isoDay(new Date()),
  reportDay: isoDay(new Date()),
  openApps: new Set(),
  openPaths: new Set(),  // "app\ntitle" of window rows showing their path
};
const projectById = (id) => state.projects.find((p) => p.id === id);
const projectColor = (id) => projectById(id)?.color || "#9aa3b5";
const projectName = (id) => projectById(id)?.name || "미분류";

function projectOptions(select, { all = false, none = "미분류", selected = "" } = {}) {
  select.replaceChildren(
    all ? h("option", { value: "" }, "전체") : null,
    h("option", { value: all ? "none" : "" }, none),
    ...state.projects.filter((p) => !p.archived || String(p.id) === String(selected))
      .map((p) => h("option", { value: p.id }, p.name)),
  );
  select.value = String(selected ?? "");
}

async function loadProjects() {
  state.projects = await api("GET", "/api/projects");
  const keep = (id) => $(id).value;
  projectOptions($("search-project"), { all: true, selected: keep("search-project") });
}

// ------------------------------------------------------------------ tabs
document.querySelectorAll("button.tab").forEach((btn) =>
  btn.addEventListener("click", () => showTab(btn.dataset.tab)));

function showTab(name) {
  document.querySelectorAll("button.tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
  if (name === "dashboard") loadDashboard();
  if (name === "timeline") loadTimeline();
  if (name === "report") loadReport();
  if (name === "settings") { loadFolders(); loadLaunch(); loadAISettings(); loadTracking(); loadNames(); loadExclusions(); }
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  const s = await api("GET", "/api/status");
  $("demo-banner").hidden = !s.demo;
  $("db-path").textContent = s.db_path;
  $("pause-btn").textContent = s.paused ? "기록 재개" : "일시정지";
  $("sync-btn").hidden = s.demo;
  const chip = $("sync-status");
  chip.className = "chip";
  chip.title = "";
  if (s.paused) {
    chip.classList.add("paused");
    chip.textContent = "일시정지됨";
  } else if (s.demo) {
    chip.textContent = "데모 모드";
  } else if (!s.sync) {
    chip.textContent = "첫 동기화 중…";
  } else if (!s.collecting) {
    chip.textContent = "창 기록 안 함 (작업 폴더만)";
  } else if (s.collector_error || !s.sync.ok) {
    chip.classList.add("err");
    chip.textContent = "창 기록 오류 (저장된 기록만 표시)";
    chip.title = s.collector_error || s.sync.error || "";
  } else {
    chip.classList.add("ok");
    chip.textContent = `창 기록 중 · ${hhmm(s.sync.finished_at)} 동기화`;
  }
  return s;
}

$("sync-btn").addEventListener("click", () => guarded(async () => {
  $("sync-btn").disabled = true;
  try {
    const r = await api("POST", "/api/sync");
    toast(r.ok ? `동기화 완료: 창 ${r.imported.window}건` : `동기화 실패: ${r.error}`);
  } finally {
    $("sync-btn").disabled = false;
  }
  await loadStatus();
  await loadTimeline();
}));

$("pause-btn").addEventListener("click", () => guarded(async () => {
  const s = await api("GET", "/api/status");
  await api("POST", s.paused ? "/api/resume" : "/api/pause");
  toast(s.paused ? "기록을 다시 가져옵니다." : "일시정지했습니다. 이 기간의 활동은 가져오지 않습니다.");
  await loadStatus();
  await loadTimeline();
}));

// ------------------------------------------------------------------ timeline
function setDay(day) {
  state.day = day;
  $("day").value = day;
  loadTimeline();
}
$("day").addEventListener("change", (e) => e.target.value && setDay(e.target.value));
$("today").addEventListener("click", () => setDay(isoDay(new Date())));
$("prev-day").addEventListener("click", () => setDay(shiftDay(state.day, -1)));
$("next-day").addEventListener("click", () => setDay(shiftDay(state.day, 1)));
$("filter-app").addEventListener("change", () => loadTimeline());

const WARNINGS = {
  afk_missing: "이 날짜에는 유휴(AFK) 감지 기록이 없어 모든 창 시간을 활동으로 계산했습니다. 실제보다 길 수 있습니다.",
};

let timelineSeq = 0;
async function loadTimeline() {
  const seq = ++timelineSeq;
  const params = new URLSearchParams({ date: state.day });
  if ($("filter-app").value) params.set("app", $("filter-app").value);
  const t = await guarded(() => api("GET", `/api/timeline?${params}`));
  if (!t || seq !== timelineSeq) return;

  $("warnings").replaceChildren(...t.warnings.map((w) => h("div", { class: "warning" }, WARNINGS[w] || w)));

  const appSel = $("filter-app");
  const currentApp = appSel.value;
  appSel.replaceChildren(h("option", { value: "" }, "전체"),
    ...t.by_app.map((a) => h("option", { value: a.app }, `${appName(a.app)} (${dur(a.seconds)})`)));
  appSel.value = t.by_app.some((a) => a.app === currentApp) ? currentApp : "";

  renderApps(t.apps);
  renderChanges(t.changes || []);
}

const APP_WINDOWS_SHOWN = 5;

function renderApps(apps) {
  $("rows-empty").hidden = apps.length > 0;
  $("app-list").replaceChildren(...apps.map(appCard));
}

function pathBox(w) {
  const input = h("input", { class: "path-text", readonly: true, value: w.path, onclick: (e) => e.target.select() });
  return h("div", { class: "row path-box" },
    h("span", { class: "meta" }, "파일"), input,
    h("button", { class: "small", onclick: async () => {
      try {
        await navigator.clipboard.writeText(w.path);
      } catch {
        input.select();
        if (!document.execCommand("copy")) return toast("복사하지 못했습니다. 선택된 경로를 Ctrl+C로 복사하세요.");
      }
      toast("경로를 복사했습니다.");
    } }, "복사"));
}

function appCard(a) {
  const head = (title, ...rest) => h("div", { class: "session-head" },
    h("span", { class: "time" }, `${hhmm(a.first)}–${hhmm(a.last)}`), title, ...rest,
    h("span", { class: "num session-dur" }, dur(a.seconds)));
  if (a.app === "") {
    return h("div", { class: "card session offwork-session" },
      head(h("span", { class: "session-title muted" }, "업무 외"), badge("plain", "기록 대상 아님")));
  }
  const windowRow = (w) => {
    const key = `${a.app}\n${w.title}`;
    const pathRow = h("tr", { class: "path-row", hidden: !state.openPaths.has(key) },
      h("td", { colspan: 3 }, w.path ? pathBox(w) : h("span", { class: "muted" },
        "경로 없음 · 작업 폴더 안의 파일만 경로가 나옵니다.")));
    const row = h("tr", { class: "clickable", title: "눌러서 경로 보기", onclick: () => {
      pathRow.hidden = !pathRow.hidden;
      pathRow.hidden ? state.openPaths.delete(key) : state.openPaths.add(key);
    } },
    h("td", { class: "title" }, w.title || h("span", { class: "muted" }, "(제목 없음)"),
      w.path ? badge("plain", "파일") : null),
    h("td", { class: "time" }, `마지막 ${hhmm(w.last)}`),
    h("td", { class: "num" }, dur(w.seconds)));
    return [row, pathRow];
  };
  const table = (windows) => h("table", { class: "rows" }, h("tbody", {}, windows.flatMap(windowRow)));
  const rest = a.windows.slice(APP_WINDOWS_SHOWN);
  const more = rest.length ? h("details", { open: state.openApps.has(a.app) },
    h("summary", {}, `창 ${rest.length}개 더 보기`), table(rest)) : null;
  more?.addEventListener("toggle", () => (more.open ? state.openApps.add(a.app) : state.openApps.delete(a.app)));
  return h("div", { class: "card session app-card" },
    head(h("span", { class: "session-title" }, a.label), h("span", { class: "meta" }, `창 ${a.windows.length}개`)),
    table(a.windows.slice(0, APP_WINDOWS_SHOWN)),
    more);
}

// ------------------------------------------------------------------ results (file changes)
const kindLabel = { A: "추가", M: "수정", D: "삭제" };
const lineDelta = (f) => (f.is_text === 0 || f.lines_added === null ? "" : ` (+${f.lines_added} −${f.lines_removed})`);

function fileButton(f) {
  return h("button", { class: `link file-${f.kind}`, title: `${kindLabel[f.kind]} · 변경 내용 보기`, onclick: () => showDiff(f) },
    f.path.split("/").pop(), lineDelta(f));
}

function renderChanges(changes) {
  $("changes-card").hidden = changes.length === 0;
  $("changes").replaceChildren(...changes.map((c) => h("li", { class: "change-item" },
    h("div", {},
      h("span", { class: "time" }, hhmm(c.ts)), " ", h("b", {}, c.folder),
      h("span", { class: "meta" }, ` · 파일 ${c.files_added + c.files_modified + c.files_deleted}개`,
        c.lines_added || c.lines_removed ? ` · +${c.lines_added} −${c.lines_removed}` : "")),
    h("div", { class: "change-files" }, ...c.files.flatMap((f, i) => [i ? ", " : "", fileButton(f)])))));
}

const mb = (bytes) => (bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))}KB` : `${(bytes / 1024 / 1024).toFixed(1)}MB`);
const NOT_TEXT = {
  large: (d) => `큰 파일이라 내용은 저장하지 않고 바뀐 것만 기록합니다 (${[d.old_size, d.new_size].filter((s) => s != null).map(mb).join(" → ")}).`,
  pdf: () => "PDF는 내용을 비교하지 않습니다. 바뀐 것만 기록합니다.",
  old_office: () => "예전 Office 형식(doc·xls·ppt)이라 내용을 비교할 수 없습니다. docx·xlsx·pptx로 저장하면 바뀐 문장을 보여 줍니다.",
  image: () => "그림입니다.",
  binary: () => "텍스트가 아닌 파일이라 내용을 비교할 수 없습니다.",
};

// An image of the change: before and after, side by side (served only from this PC).
function imagePair(d, fileId) {
  const side = (which, label) => h("figure", { class: "image-side" },
    h("figcaption", { class: "meta" }, label),
    (which === "old" ? d.kind !== "A" : d.kind !== "D")
      ? h("img", { src: `/api/change-files/${fileId}/image?side=${which}`, alt: `${label} 그림` })
      : h("div", { class: "muted" }, which === "old" ? "(새로 추가)" : "(삭제됨)"));
  return h("div", { class: "image-pair" }, side("old", "바뀌기 전"), side("new", "바뀐 뒤"));
}

function diffLines(d, fileId) {
  if (!d.text) {
    const why = h("div", { class: "muted" }, (NOT_TEXT[d.reason] || NOT_TEXT.binary)(d));
    return d.image && fileId ? [why, imagePair(d, fileId)] : [why];
  }
  return d.diff.split("\n").map((line) => h("div", {
    class: line.startsWith("+") && !line.startsWith("+++") ? "add"
      : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "",
  }, line || " "));
}

async function showDiff(f) {
  const d = await guarded(() => api("GET", `/api/change-files/${f.id}/diff`));
  if (!d) return;
  $("diff-title").textContent = `${d.path} · ${kindLabel[d.kind]}`;
  $("diff-body").replaceChildren(...diffLines(d, f.id));
  $("diff-overlay").hidden = false;
}

// A "파일 변경" citation (C ref): every file of that change, one after another, in one window.
const CHANGE_FILES_SHOWN = 10;
async function showChange(changeId) {
  const c = await guarded(() => api("GET", `/api/changes/${changeId}`));
  if (!c) return;
  const files = c.files.slice(0, CHANGE_FILES_SHOWN);
  const diffs = await guarded(() => Promise.all(files.map((f) => api("GET", `/api/change-files/${f.id}/diff`))));
  if (!diffs) return;
  const total = c.files_added + c.files_modified + c.files_deleted;
  $("diff-title").textContent = `${c.folder} · ${new Date(c.ts * 1000).toLocaleString("ko-KR")} · 파일 ${total}개`;
  $("diff-body").replaceChildren(
    ...diffs.flatMap((d, i) => [h("div", { class: "diff-file" }, `${d.path} · ${kindLabel[d.kind]}`),
      ...diffLines(d, files[i].id)]),
    ...(total > files.length ? [h("div", { class: "muted" }, `외 ${total - files.length}개 파일은 타임라인에서 볼 수 있습니다.`)] : []));
  $("diff-overlay").hidden = false;
}
$("diff-close").addEventListener("click", () => ($("diff-overlay").hidden = true));
$("diff-overlay").addEventListener("click", (e) => { if (e.target.id === "diff-overlay") $("diff-overlay").hidden = true; });

async function loadFolders() {
  const folders = await guarded(() => api("GET", "/api/folders"));
  if (!folders) return;
  $("folder-list").replaceChildren(...(folders.length ? folders.map((f) =>
    h("li", { class: "folder-item" },
      h("div", {},
        h("b", {}, f.label), f.enabled ? null : badge("plain", "꺼짐"),
        h("div", { class: "meta" }, f.path),
        h("div", { class: "meta" }, `파일 ${f.files}개 · `,
          f.last_scan_at ? `마지막 확인 ${ago(f.last_scan_at)}` : "아직 기록 전",
          f.last_change_at ? ` · 마지막 변경 ${ago(f.last_change_at)}` : ""),
        f.last_error ? h("div", { class: "warning" }, f.last_error) : null),
      h("div", { class: "row" },
        h("button", { class: "small", onclick: () => guarded(async () => {
          await api("PATCH", `/api/folders/${f.id}`, { enabled: !f.enabled });
          loadFolders();
        }) }, f.enabled ? "끄기" : "켜기"),
        h("button", { class: "small danger", onclick: () => {
          if (!confirm(`'${f.label}' 폴더 기록을 삭제할까요? 이 폴더의 변경 기록과 숨은 Git 저장소가 지워집니다. 원래 폴더의 파일은 그대로입니다.`)) return;
          guarded(async () => { await api("DELETE", `/api/folders/${f.id}`); loadFolders(); });
        } }, "삭제")))) : [h("li", { class: "muted" }, "등록한 폴더가 없습니다. 소스 코드나 문서 폴더를 추가하세요.")]));
}

$("folder-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "/api/folders", {
      path: $("folder-path").value, label: $("folder-label").value,
    });
    $("folder-path").value = "";
    $("folder-label").value = "";
    toast("폴더를 등록했습니다. 첫 기록은 기준점으로 저장되고, 이후 바뀐 것부터 결과물로 표시됩니다.");
    loadFolders();
  });
});

$("folder-scan").addEventListener("click", () => guarded(async () => {
  $("folder-scan").disabled = true;
  try {
    const results = await api("POST", "/api/folders/scan", {});
    const changed = results.filter((r) => r.change_id && !r.baseline).length;
    const errors = results.filter((r) => r.error).length;
    toast(`기록 완료: 변경 있는 폴더 ${changed}개${errors ? `, 오류 ${errors}개` : ""}`);
  } finally {
    $("folder-scan").disabled = false;
  }
  loadFolders();
}));

// ------------------------------------------------------------------ AI reports
// A report is shown read-only first; "작성" / "수정" opens the editor, and only "저장" or "취소"
// leaves it. While editing, the period cannot change, so unsaved text is never lost by a click.
const reportKind = () => document.querySelector('input[name="report-kind"]:checked').value;
let reportSaved = null;
let reportBusy = false;
let reportEditing = false;
let reportLoaded = ""; // "kind|day" shown (and being edited)

function reportPeriod(kind, day) {
  if (kind === "daily") return [day, day];
  const d = new Date(day + "T00:00:00");
  const monday = shiftDay(day, -((d.getDay() + 6) % 7));
  return [monday, shiftDay(monday, 6)];
}

const reportDirty = () => reportEditing && $("report-body").value !== (reportSaved ? reportSaved.body : "");

function setReportState() {
  const el = $("report-state");
  if (reportEditing) {
    el.className = "badge plain";
    el.textContent = reportDirty() ? "편집 중 · 저장하지 않음" : "편집 중";
  } else if (reportSaved) {
    el.className = "badge user";
    el.textContent = `저장됨 · ${new Date(reportSaved.updated_at * 1000).toLocaleString("ko-KR")}`;
  } else {
    el.className = "badge";
    el.textContent = "";
  }
}
$("report-body").addEventListener("input", () => setReportState());

// The marks the app puts on draft lines ("(AI 제안 — 확인 필요)") become badges in the view.
const REPORT_MARK = /\s*\(([^()]*?) — 확인 필요\)/g;
function reportInline(text) {
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(REPORT_MARK)) {
    parts.push(text.slice(last, m.index), badge("plain", `${m[1]} · 확인 필요`));
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}

// "(진행 중)" / "(완료)" at the end of a line of what was done: the user's own call, one click away.
// "(제안)" at the end of a plan line: the AI's suggestion until the user confirms it (one click).
const REPORT_STATUS = /\s*\((진행 중|완료|제안)\)\s*$/;
const SUGGESTED_LINE = /^\s*[-*•] .*\(제안\)\s*$/;
const STATUS_TITLES = { "제안": "AI 제안: 눌러서 계획으로 확정 (필요 없으면 수정에서 지우세요)" };
const statusChip = (status, index) => h("button", {
  class: `status-chip${status === "완료" ? " done" : status === "제안" ? " suggested" : ""}`,
  title: STATUS_TITLES[status] || "눌러서 완료 / 진행 중으로 바꾸기",
  onclick: () => guarded(() => toggleReportStatus(index)) }, status);

async function toggleReportStatus(index) {
  if (!reportSaved || reportEditing) return;
  const lines = reportSaved.body.split("\n");
  const m = lines[index].match(REPORT_STATUS);
  if (!m) return;
  const next = m[1] === "제안" ? null : m[1] === "완료" ? "진행 중" : "완료";
  lines[index] = lines[index].slice(0, m.index) + (next ? ` (${next})` : "");
  const r = await api("POST", "/api/reports", { kind: reportKind(), day: state.reportDay, body: lines.join("\n") });
  reportSaved = r.saved;
  renderReportView();
  setReportState();
  toast(!next ? "계획으로 확정했습니다." : next === "완료" ? "완료로 표시했습니다." : "진행 중으로 되돌렸습니다.");
}

// The saved text laid out: "# 제목", "## 업무", "[칸]", "- 항목", "(참고)" and plain lines.
function reportView(text) {
  const out = [];
  let list = null;
  text.split("\n").forEach((line, index) => {
    const t = line.trim();
    if (/^[-*•]$/.test(t)) return;  // an empty bullet: a plan line left for the user
    if (/^[-*•] /.test(t)) {
      if (!list) out.push(list = h("ul"));
      const m = t.match(REPORT_STATUS);
      list.append(h("li", {}, reportInline(m ? t.slice(2, m.index) : t.slice(2)), m ? statusChip(m[1], index) : null));
      return;
    }
    list = null;
    if (!t) return;
    if (t.startsWith("## ")) out.push(h("h3", { class: "rv-group" }, reportInline(t.slice(3))));
    else if (t.startsWith("# ")) out.push(h("h2", { class: "rv-title" }, t.slice(2)));
    else if (/^\[[^\]]+\]$/.test(t)) out.push(h("div", { class: "rv-label" }, t.slice(1, -1)));
    else if (/^\(.*\)$/.test(t)) out.push(h("p", { class: "muted small-text" }, t.slice(1, -1)));
    else out.push(h("p", {}, reportInline(t)));
  });
  return out;
}

function renderReportView() {
  const [start, end] = reportPeriod(reportKind(), state.reportDay);
  const what = reportKind() === "daily" ? "업무일지" : "주간보고";
  $("report-view").replaceChildren(...(reportSaved && reportSaved.body.trim()
    ? reportView(reportSaved.body)
    : [h("div", { class: "report-empty muted" },
        h("p", {}, `${start === end ? start : `${start} ~ ${end}`} ${what}가 아직 없습니다.`),
        h("button", { class: "primary", onclick: () => setReportEditing(true) }, "작성하기"))]));
}

function setReportEditing(on) {
  reportEditing = on;
  if (on) {
    $("report-body").value = reportSaved ? reportSaved.body : "";
    $("report-preview-box").hidden = true;
  } else {
    $("report-side").replaceChildren();
  }
  $("report-view").hidden = on;
  $("report-editor").hidden = !on;
  $("report-view-actions").hidden = on;
  $("report-edit-actions").hidden = !on;
  $("report-edit").textContent = reportSaved ? "수정" : "작성";
  $("report-copy").disabled = !reportSaved;
  // The period is fixed while editing.
  for (const el of [$("report-prev"), $("report-next"), $("report-day"),
                    ...document.querySelectorAll('input[name="report-kind"]')]) {
    el.disabled = on;
    el.title = on ? "편집 중에는 기간을 바꿀 수 없습니다. 저장하거나 취소하세요." : "";
  }
  if (on) $("report-body").focus();
  else renderReportView();
  setReportState();
}

async function loadReport() {
  const kind = reportKind(), day = state.reportDay;
  $("report-day").value = day;
  const [start, end] = reportPeriod(kind, day);
  $("report-period").textContent = start === end ? start : `${start} ~ ${end}`;
  const [r, ai] = await Promise.all([
    guarded(() => api("GET", `/api/reports?kind=${kind}&date=${day}`)),
    guarded(() => api("GET", "/api/ai/settings")),
  ]);
  if (!r || kind !== reportKind() || day !== state.reportDay) return;
  reportSaved = r.saved;
  renderReportNotice(ai);
  // Coming back to the report being edited (e.g. after opening a cited record) keeps the editor,
  // its text and the draft's evidence.
  if (reportEditing && reportLoaded === `${kind}|${day}`) return setReportState();
  reportLoaded = `${kind}|${day}`;
  setReportEditing(false);
}

function renderReportNotice(ai) {
  const notes = [];
  const local = ai && isLocal(ai.model);
  if (ai && !local && !ai.claude_installed && !(ai.local_models || []).length) notes.push(ai.claude_bundled
    ? "Claude Code가 설치되어 있지 않습니다. Claude Code를 설치하고 로그인하거나, 내 PC의 AI 모델을 llm 폴더에 넣으면 AI 초안을 만들 수 있습니다."
    : "내 PC의 AI 모델이 없습니다. 앱 폴더의 llm\\models 에 .gguf 모델 파일을 넣으면 AI 초안을 만들 수 있습니다.");
  else if (ai && !ai.demo && !ai.enabled) notes.push("AI가 꺼져 있습니다. 설정 탭의 AI 설정에서 모델을 고르면 실제 기록으로 초안을 만들 수 있습니다.");
  else if (local) notes.push("내 PC의 AI로 만듭니다. 기록은 이 PC 밖으로 나가지 않습니다. 그래픽카드가 없는 PC에서는 몇 분 걸릴 수 있습니다.");
  $("report-notice").replaceChildren(...notes.map((n) => h("div", { class: "warning" }, n)));
}

function setReportDay(day) {
  state.reportDay = day;
  loadReport();
}
$("report-day").addEventListener("change", (e) => e.target.value && setReportDay(e.target.value));
$("report-prev").addEventListener("click", () => setReportDay(shiftDay(state.reportDay, reportKind() === "daily" ? -1 : -7)));
$("report-next").addEventListener("click", () => setReportDay(shiftDay(state.reportDay, reportKind() === "daily" ? 1 : 7)));
document.querySelectorAll('input[name="report-kind"]').forEach((el) => el.addEventListener("change", loadReport));

$("report-edit").addEventListener("click", () => setReportEditing(true));
$("report-cancel").addEventListener("click", () => {
  if (reportBusy) return;
  if (reportDirty() && !confirm("저장하지 않은 내용을 버리고 저장된 보고서로 돌아갈까요?")) return;
  setReportEditing(false);
});
window.addEventListener("beforeunload", (e) => { if (reportDirty()) e.preventDefault(); });

$("report-preview").addEventListener("click", () => guarded(async () => {
  const p = await api("GET", `/api/ai/preview?kind=${reportKind()}&date=${state.reportDay}`);
  $("report-preview-text").textContent = `[지시문]\n${p.system}\n\n[업무 기록]\n${p.prompt}`;
  $("report-preview-box").hidden = false;
  $("report-preview-box").open = true;
}));

$("report-generate").addEventListener("click", () => {
  if (reportBusy) return;
  const body = $("report-body").value;
  if (body.trim() && !confirm("편집 중인 내용을 AI 초안으로 바꿉니다. 저장된 보고서는 '저장'을 누르기 전까지 그대로입니다.")) return;
  guarded(async () => {
    reportBusy = true;
    for (const id of ["report-generate", "report-save", "report-cancel"]) $(id).disabled = true;
    $("report-generate").textContent = "AI가 작성 중…";
    $("report-stop").disabled = false;
    $("report-stop").textContent = "멈추기";
    // A report too big for one request goes in several: show which one is being written, and let
    // the user stop it before the next one (a single request cannot be stopped midway).
    const poll = setInterval(() => api("GET", "/api/ai/report/progress").then((p) => {
      if (!reportBusy || !p.running || p.total < 2) return;
      $("report-stop").hidden = false;
      $("report-generate").textContent = p.step >= p.total
        ? `AI가 작성 중… (요약 ${p.step}/${p.total})`
        : `AI가 작성 중… (${p.step}/${p.total}, 기록이 많아 나눠 보냄)`;
    }).catch(() => {}), 1500);
    try {
      const r = await api("POST", "/api/ai/report", { kind: reportKind(), day: state.reportDay });
      $("report-body").value = r.text;
      setReportState();
      renderReportSide(r);
    } finally {
      clearInterval(poll);
      reportBusy = false;
      for (const id of ["report-generate", "report-save", "report-cancel"]) $(id).disabled = false;
      $("report-generate").textContent = "AI 초안 만들기";
      $("report-stop").hidden = true;
    }
  });
});

$("report-stop").addEventListener("click", () => guarded(async () => {
  $("report-stop").disabled = true;
  $("report-stop").textContent = "멈추는 중… (지금 보낸 요청이 끝나면)";
  await api("POST", "/api/ai/report/cancel");
}));

// ---- Citations. What the AI wrote and what is in the records must never look alike:
// AI text sits in a tinted "AI" box; records show their own content (the note, the files, the work folder).
const SOURCE_KINDS = { work: "작업 기록", change: "파일 변경", note: "메모", day: "타임라인" };

const sourceWhen = (src) => (src.type === "change" ? `${src.day.slice(5)} ${hhmm(src.start)}`
  : src.day ? src.day.slice(5) : "");

function openSource(src, ref) {
  if (src.type === "change") showChange(Number(ref.slice(1)));
  else gotoDay(src.day);
}

// Inline chip inside an AI answer.
function citationChip(src, ref = src.ref) {
  const label = src.type === "project" ? src.label : `${sourceWhen(src)} ${SOURCE_KINDS[src.type]}`;
  return h("button", { class: `ref-chip ${src.type}`, title: src.label, onclick: () => openSource(src, ref) }, label);
}

// One record, showing what it actually says.
function factRow(src, ref) {
  const text = src.type === "project" && src.next_action ? `${src.label} · 다음 할 일: ${src.next_action}` : src.label;
  return h("button", { class: `fact ${src.type}`, title: "기록 열기", onclick: () => openSource(src, ref) },
    h("span", { class: "fact-kind" }, [SOURCE_KINDS[src.type], sourceWhen(src)].filter(Boolean).join(" · ")),
    h("span", { class: "fact-text" }, text));
}

const VISIBLE_FACTS = 3;

function factList(refs, sources) {
  // Many records under one line are noise: show a few, fold the rest behind "+N".
  const rows = refs.filter((ref) => sources[ref]).map((ref) => factRow(sources[ref], ref));
  const box = h("div", { class: "facts" }, rows.slice(0, VISIBLE_FACTS));
  if (rows.length > VISIBLE_FACTS) {
    const more = h("button", { class: "link small-text" }, `근거 ${rows.length - VISIBLE_FACTS}개 더 보기`);
    more.addEventListener("click", () => { more.remove(); box.append(...rows.slice(VISIBLE_FACTS)); });
    box.append(more);
  }
  return box;
}

const factLegend = () => h("div", { class: "legend" },
  h("span", { class: "legend-ai" }, "AI가 쓴 문장"), h("span", { class: "legend-fact" }, "기록에 있는 사실 (누르면 열림)"));

function renderReportSide(r) {
  const flagBadge = { no_evidence: badge("plain", "근거 없음") };
  const aiText = (text, flag) => h("div", { class: `ai-text${flag ? " flagged" : ""}` },
    badge("ai", "AI"), " ", text, flag ? [" ", flagBadge[flag].cloneNode(true)] : null);
  const [doneLabel, nextLabel] = r.kind === "daily" ? ["오늘 한 일", "다음 할 일"] : ["금주 실적", "차주 계획"];
  const suggestion = badge("plain", "제안");
  $("report-side").replaceChildren(
    h("div", { class: "card" },
      h("h2", {}, "AI 초안과 근거"),
      ...(r.notes || []).map((n) => h("p", { class: "warning" }, n)),
      factLegend(),
      r.summary ? aiText(r.summary) : null,
      ...r.projects.map((p) => h("div", { class: "report-project" },
        h("h3", {}, p.project, p.seconds ? h("span", { class: "meta" }, ` · ${dur(p.seconds)}`) : null),
        p.missing_from_ai ? h("p", { class: "warning" }, "이 작업 폴더의 기록이 AI 초안에 빠졌습니다. 직접 작성하세요.") : null,
        p.done.length ? h("div", {}, h("div", { class: "meta" }, doneLabel),
          h("ul", { class: "report-items" }, p.done.map((it) => h("li", {},
            aiText(it.text, it.flag),
            it.refs.length ? factList(it.refs, r.sources) : null)))) : null,
        p.next && p.next.length ? h("div", {}, h("div", { class: "meta" }, `${nextLabel} (AI 제안, 이어지는 기록)`),
          h("ul", { class: "report-items" }, p.next.map((it) => h("li", {},
            h("div", { class: "ai-text" }, badge("ai", "AI"), " ", it.text, " ", suggestion.cloneNode(true)),
            factList(it.refs, r.sources))))) : null))),
    h("p", { class: "muted small-text" },
      `AI 사용: 입력 ${r.usage.input_tokens.toLocaleString()} · 출력 ${(r.usage.output_tokens + r.usage.thinking_tokens).toLocaleString()} 토큰`,
      r.dropped_refs ? ` · 확인되지 않은 근거 ${r.dropped_refs}개는 제외했습니다` : "",
      r.dropped_plans ? ` · 근거 없는 계획 제안 ${r.dropped_plans}개는 넣지 않았습니다` : ""),
  );
}

$("report-save").addEventListener("click", () => guarded(async () => {
  if (reportBusy) return;
  const r = await api("POST", "/api/reports", { kind: reportKind(), day: state.reportDay, body: $("report-body").value });
  reportSaved = r.saved;
  setReportEditing(false);
  toast(r.saved ? "보고서를 저장했습니다." : "빈 보고서라 저장된 내용을 지웠습니다.");
}));

$("report-copy").addEventListener("click", () => guarded(async () => {
  if (!reportSaved) return;
  // AI-suggested plans the user has not looked at should not slip into the company report.
  const suggested = reportSaved.body.split("\n").filter((l) => SUGGESTED_LINE.test(l)).length;
  if (suggested && !confirm(`확인하지 않은 AI 제안이 ${suggested}개 있습니다 ("(제안)" 표시).\n`
    + "그대로 복사할까요? 확인하려면 취소한 뒤 제안을 눌러 확정하거나 수정에서 지우세요.")) return;
  await navigator.clipboard.writeText(reportSaved.body);
  toast("클립보드에 복사했습니다.");
}));

// ------------------------------------------------------------------ AI settings
const MODEL_LABELS = { "": "사용 안 함", "claude-code": "Claude Code · 내 기본 모델", "claude-code:sonnet": "Claude Code · Sonnet",
  "claude-code:opus": "Claude Code · Opus", "claude-code:haiku": "Claude Code · Haiku (빠름)" };
const isLocal = (m) => typeof m === "string" && m.startsWith("local:");
const modelLabel = (m) => MODEL_LABELS[m] || (isLocal(m) ? `내 PC · ${m.slice(6)} (외부 전송 없음)` : m);
let aiModel = "";

function renderModelList(s) {
  const item = (title, meta, ...actions) => h("li", {},
    h("span", {}, h("b", {}, title), " ", h("span", { class: "meta" }, meta)), h("span", { class: "row" }, ...actions));
  $("ai-model-list").replaceChildren(
    ...s.local_models.map((m) => item(`내 PC · ${m.slice(6)}`, "자동 등록 · 외부 전송 없음 · llm\\models 폴더")),
    // Only where Claude Code is part of the install (development); the released zip has none.
    ...(s.claude_bundled ? [item("Claude Code", s.claude_installed ? "이 PC에 설치됨 · Anthropic으로 전송" : "설치되어 있지 않음")] : []),
    ...(s.local_models.length ? [] : [h("li", { class: "muted" },
      "내 PC 모델이 없습니다. 앱 폴더의 llm\\models 에 .gguf 모델 파일을 넣으면 자동으로 등록됩니다.")]));
}

async function loadAISettings() {
  const s = await guarded(() => api("GET", "/api/ai/settings"));
  if (!s) return;
  renderModelList(s);
  $("ai-model").replaceChildren(...["", ...s.models].map((m) => h("option", { value: m },
    modelLabel(m) + (m === s.default_model ? " · 추천" : ""))));
  $("ai-model").value = aiModel = s.model;
  $("ai-claude-note").hidden = !s.claude_bundled;
  const ok = (cond, yes, no) => h("span", { class: `chip ${cond ? "ok" : "err"}` }, cond ? yes : no);
  $("ai-status").replaceChildren(
    s.claude_bundled ? ok(s.claude_installed, "Claude Code 설치됨", "Claude Code 없음") : "",
    h("span", { class: `chip ${s.local_models.length ? "ok" : ""}` },
      s.local_models.length ? `내 PC AI 모델 ${s.local_models.length}개` : "내 PC AI 모델 없음"),
    s.demo ? h("span", { class: "chip" }, "데모 DB: 전송 허용")
      : ok(s.enabled, isLocal(s.model) ? "AI 켜짐: 내 PC에서 처리" : "AI 켜짐: 실제 기록 전송 허용", "AI 꺼짐"));
  $("ai-usage").textContent = s.stats.runs
    ? `지금까지 ${s.stats.runs}회 호출 (성공 ${s.stats.ok}) · 입력 ${s.stats.input_tokens.toLocaleString()} / 출력 ${(s.stats.output_tokens + s.stats.thinking_tokens).toLocaleString()} 토큰 · 평균 ${(s.stats.avg_ms / 1000).toFixed(1)}초`
    : "아직 AI를 호출한 적이 없습니다.";
}

// ------------------------------------------------------------------ launch (autostart)
function renderLaunch(l) {
  $("launch-autostart").checked = l.autostart.enabled;
  $("launch-autostart").disabled = l.demo || !l.autostart.supported;
  $("launch-status").hidden = !l.demo;
  $("launch-status").textContent = l.demo ? "데모 모드에서는 바꿀 수 없습니다." : "";
  $("quit-row").hidden = !l.can_quit;
  $("launch-url").hidden = !l.url;
  $("launch-url").textContent = l.url ? `이 앱의 주소: ${l.url} (다른 프로그램이 포트를 쓰고 있으면 번호가 바뀔 수 있으니, 작업 기록.cmd로 여는 것이 가장 확실합니다)` : "";
}

async function loadLaunch() {
  const l = await guarded(() => api("GET", "/api/launch"));
  if (l) renderLaunch(l);
}

$("launch-autostart").addEventListener("change", (e) => guarded(async () => {
  try {
    const l = await api("POST", "/api/launch", { autostart: e.target.checked });
    renderLaunch(l);
    toast(l.autostart.enabled ? "컴퓨터를 켜면 작업 기록이 자동으로 시작됩니다." : "자동 실행을 껐습니다.");
  } catch (err) {
    loadLaunch();
    throw err;
  }
}));

$("app-quit").addEventListener("click", () => guarded(async () => {
  if (!confirm("작업 기록을 종료할까요? 다시 켤 때까지 창 기록과 작업 폴더 기록이 멈춥니다.")) return;
  await api("POST", "/api/quit", {});
  document.body.innerHTML = '<main style="padding:3rem;text-align:center"><h2>작업 기록을 종료했습니다</h2>'
    + '<p class="muted">다시 켜려면 작업 기록.cmd를 실행하세요. 자동 실행을 켜 두었다면 PC를 다시 켤 때 자동으로 시작됩니다.</p></main>';
}));

$("ai-check").addEventListener("click", () => guarded(async () => {
  $("ai-check").disabled = true;
  try {
    const r = await api("POST", "/api/ai/check", {});
    toast(`연결 확인 완료: ${modelLabel(r.model)} 사용 가능`);
  } finally {
    $("ai-check").disabled = false;
  }
}));

$("ai-model").addEventListener("change", (e) => {
  const model = e.target.value;
  // Ask whenever the records would go somewhere new; staying on this PC needs no warning after the first choice.
  const question = !model || model === aiModel ? null
    : isLocal(model) ? (aiModel ? null : "보고서와 'AI에게 묻기'에 내 PC의 AI를 쓸까요? 기록은 이 PC 밖으로 나가지 않습니다. 처음 쓸 때 모델을 불러오느라 시간이 걸리고, 그래픽카드가 없는 PC에서는 보고서 하나에 몇 분 걸릴 수 있습니다.")
    : (aiModel && !isLocal(aiModel)) ? null
    : "보고서와 'AI에게 묻기'에 Claude Code를 쓸까요? 실제 업무 기록이 (비밀값을 가린 뒤) Claude Code를 거쳐 Anthropic으로 전송됩니다.";
  if (question && !confirm(question)) {
    e.target.value = aiModel;
    return;
  }
  guarded(async () => {
    await api("POST", "/api/ai/settings", { model });
    toast(model ? "AI 모델을 바꿨습니다." : "AI를 껐습니다. 실제 기록은 AI에 보내지 않습니다.");
    loadAISettings();
  });
});

// ------------------------------------------------------------------ search
$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    const params = new URLSearchParams({ q: $("search-q").value });
    for (const [k, id] of [["from", "search-from"], ["to", "search-to"], ["project", "search-project"]]) {
      if ($(id).value) params.set(k, $(id).value);
    }
    renderSearch(await api("GET", `/api/search?${params}`));
  });
});

function gotoDay(day) {
  showTab("timeline");
  setDay(day);
}

// ------------------------------------------------------------------ ask the AI
const TOOL_LABELS = { search_history: "기록 검색", get_day: "하루 보기", get_change: "변경 내용 보기" };

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const question = $("ask-q").value.trim();
  if (!question) return;
  const btn = $("ask-btn");
  btn.disabled = true;
  btn.textContent = "찾는 중…";
  $("ask-result").replaceChildren(h("p", { class: "muted" }, "AI가 기록을 찾아보고 있습니다…"));
  guarded(async () => {
    try {
      const r = await api("POST", "/api/ai/ask", { question });
      const sources = Object.fromEntries(r.citations.map((c) => [c.ref, c]));
      // Show the answer with each [ref] turned into a clickable chip.
      const parts = r.answer.split(/(\[[SCNPDR][0-9A-Za-z-]+\])/g).map((part) => {
        const m = part.match(/^\[([SCNPDR][0-9A-Za-z-]+)\]$/);
        return m && sources[m[1]] ? citationChip(sources[m[1]]) : inlineMarkdown(part);
      }).flat();
      const cited = r.citations.map((c) => c.ref);
      $("ask-result").replaceChildren(
        factLegend(),
        h("div", { class: "ai-text answer" }, badge("ai", "AI 답변"), " ", ...parts),
        cited.length ? h("div", { class: "answer-facts" }, h("div", { class: "meta" }, "답변에 쓴 기록"), factList(cited, sources)) : "",
        h("p", { class: "muted small-text" },
          `확인한 기록: ${r.tool_calls.map((c) => TOOL_LABELS[c.name] || c.name).join(" → ") || "없음"}`,
          r.dropped_refs ? ` · 확인되지 않은 근거 ${r.dropped_refs}개는 지웠습니다` : "",
          ` · ${r.usage.requests}회 호출`));
    } catch (err) {
      $("ask-result").replaceChildren();
      throw err;
    } finally {
      btn.disabled = false;
      btn.textContent = "묻기";
    }
  });
});

// **bold** and `code` from the model's answer, as elements (text stays text, never HTML).
function inlineMarkdown(text) {
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).map((part) =>
    /^\*\*[^*\n]+\*\*$/.test(part) ? h("b", {}, part.slice(2, -2))
      : /^`[^`\n]+`$/.test(part) ? h("code", {}, part.slice(1, -1))
      : part);
}

function renderSearch(r) {
  const groups = [];
  const section = (title, count, body) => h("div", { class: "result-group" }, h("h3", {}, `${title} (${count})`), body);
  if (r.projects.length) {
    groups.push(section("프로젝트", r.projects.length, h("ul", { class: "plain" }, r.projects.map((p) => h("li", {},
      h("span", {}, h("span", { class: "dot", style: { background: p.color } }), p.name,
        p.next_action ? h("span", { class: "meta" }, ` · 다음 할 일: ${p.next_action}`) : null))))));
  }
  if (r.notes.length) {
    groups.push(section("메모", r.notes.length, h("ul", { class: "notes" }, r.notes.map((n) => h("li", {},
      h("div", { class: "meta" }, h("button", { class: "link", onclick: () => gotoDay(n.day) }, n.day),
        projectById(n.project_id) ? ` · ${projectName(n.project_id)}` : ""),
      n.body)))));
  }
  if (r.files && r.files.length) {
    groups.push(section("변경 파일", r.files.length, h("ul", { class: "plain" }, r.files.map((f) => h("li", {},
      h("span", {}, h("button", { class: "link", onclick: () => gotoDay(f.day) }, `${f.day} ${hhmm(f.ts)}`), " ",
        fileButton(f), h("span", { class: "meta" }, ` · ${f.folder} · ${kindLabel[f.kind]}`)),
      h("span", { class: "meta" }, projectName(f.project_id)))))));
  }
  groups.push(section("창 기록", r.events.length, r.events.length ? h("table", { class: "rows" },
    h("thead", {}, h("tr", {}, h("th", {}, "날짜"), h("th", {}, "처음–마지막"), h("th", {}, "앱"), h("th", {}, "창 제목"), h("th", {}, "프로젝트"))),
    h("tbody", {}, r.events.map((ev) => h("tr", {},
      h("td", { class: "time" }, h("button", { class: "link", onclick: () => gotoDay(ev.day) }, ev.day)),
      h("td", { class: "time" }, `${hhmm(ev.first_ts)}–${hhmm(ev.last_ts)}`),
      h("td", {}, appName(ev.app)),
      h("td", { class: "title" }, ev.title),
      h("td", {}, projectById(ev.project_id) ? projectName(ev.project_id) : h("span", { class: "muted" }, "미분류"))))))
    : h("p", { class: "muted" }, "일치하는 창 기록이 없습니다.")));
  $("search-results").replaceChildren(...groups);
}

// ------------------------------------------------------------------ what to record
async function loadTracking() {
  const t = await guarded(() => api("GET", "/api/tracking"));
  if (!t) return;
  document.querySelectorAll('input[name="track-mode"]').forEach((el) => (el.checked = el.value === t.mode));
  const describe = (r) => [r.app_pattern ? `앱: ${r.app_pattern}` : "모든 앱", r.title_pattern ? ` · 제목에 "${r.title_pattern}"` : ""].join("");
  $("track-rules").replaceChildren(...(t.rules.length ? t.rules.map((r) => h("li", {},
    h("span", {}, describe(r)),
    h("button", { class: "small", onclick: () => guarded(async () => {
      await api("DELETE", `/api/tracking/rules/${r.id}`);
      loadTracking();
    }) }, "삭제")))
    : [h("li", { class: "muted" }, "규칙 없음 — 아래에서 업무에 쓰는 앱을 추가하세요.")]));
  if ($("track-apps-box").open) loadTrackApps();
}

async function loadTrackApps() {
  $("track-apps").replaceChildren(h("li", { class: "muted" }, "불러오는 중…"));
  const apps = await guarded(() => api("GET", "/api/tracking/apps?days=7"));
  if (!apps) { $("track-apps").replaceChildren(h("li", { class: "muted" }, "불러오지 못했습니다.")); return; }
  $("track-apps").replaceChildren(...(apps.length ? apps.map((a) => h("li", {},
    h("span", {}, appName(a.app), h("span", { class: "meta" }, ` · ${dur(a.seconds)}`)),
    a.tracked ? badge("user", "기록 중")
      : h("button", { class: "small", onclick: () => guarded(async () => {
        await api("POST", "/api/tracking/rules", { app: appName(a.app) });
        toast(`${appName(a.app)}을(를) 기록 대상에 추가했습니다.`);
        loadTracking();
      }) }, "추가")))
    : [h("li", { class: "muted" }, "최근 기록이 없습니다.")]));
}

$("track-apps-box").addEventListener("toggle", () => $("track-apps-box").open && loadTrackApps());

// ---- 앱 이름과 분류: the user's name and kind for an app, for every window of it or only windows
// whose title contains some text. The app only suggests names (title endings); nothing is guessed.
async function loadNames() {
  const r = await guarded(() => api("GET", "/api/app-names"));
  if (!r) return;
  const kindLabel = Object.fromEntries(r.kinds.map((k) => [k.key, k.label]));
  const describe = (rule) => [
    h("b", {}, rule.app),
    rule.contains ? ` · 제목에 "${rule.contains}"` : " · 모든 창",
    " → ", rule.label || "(이름 그대로)", rule.category ? ` (${kindLabel[rule.category]})` : ""];
  $("name-rules").replaceChildren(...(r.rules.length ? r.rules.map((rule) => h("li", {},
    h("span", {}, ...describe(rule)),
    h("button", { class: "small", onclick: () => guarded(async () => {
      await api("DELETE", `/api/app-names/${rule.id}`);
      toast("이름 규칙을 지웠습니다. 원래 이름으로 보입니다.");
      loadNames();
    }) }, "삭제")))
    : [h("li", { class: "muted" }, "정한 이름이 없습니다. 아래 목록에서 고칠 앱을 고르세요.")]));

  const form = (app, preset) => {
    const label = h("input", { placeholder: "보일 이름 (예: Eclipse)", value: preset.label || "" });
    const kind = h("select", {}, h("option", { value: "" }, "분류 그대로"),
      ...r.kinds.map((k) => h("option", { value: k.key }, k.label)));
    kind.value = preset.category || "";
    const contains = h("input", { placeholder: "창 제목 조건 (선택)", value: preset.contains || "" });
    const box = h("div", { class: "name-form" }, label, kind, contains,
      h("button", { class: "small primary", onclick: () => guarded(async () => {
        await api("POST", "/api/app-names", { app: app.app, label: label.value, category: kind.value, contains: contains.value });
        toast(`${label.value || app.label}: 저장했습니다. 대시보드·타임라인에 바로 적용됩니다.`);
        loadNames();
      }) }, "저장"),
      h("button", { class: "small", onclick: () => box.remove() }, "취소"));
    return box;
  };

  $("name-apps").replaceChildren(...(r.apps.length ? r.apps.map((app) => {
    const li = h("li", {});
    const open = (preset) => {
      li.querySelector(".name-form")?.remove();
      li.append(form(app, preset));
      li.querySelector(".name-form input").focus();
    };
    li.append(
      h("div", {}, h("b", {}, app.label), h("span", { class: "meta" }, ` · ${app.app} · ${dur(app.seconds)} · ${kindLabel[app.category] || "기타"}`),
        app.launcher && !app.rules.length ? [" ", badge("plain", "확인 필요")] : null,
        app.rules.length ? [" ", badge("user", `이름 규칙 ${app.rules.length}개`)] : null),
      h("div", { class: "titles" }, app.titles.slice(0, 3).map((t) => h("div", { title: t.title }, `${t.title} · ${dur(t.seconds)}`))),
      h("div", { class: "row" },
        ...app.suggestions.map((s) => h("button", { class: "small", title: "창 제목 끝에 자주 나오는 이름입니다. 누르면 저장 전에 고칠 수 있습니다.",
          onclick: () => open({ label: s.text, contains: s.text, category: app.category === "other" ? "" : app.category }) },
          `"${s.text}"(으)로 · 창의 ${Math.round(s.share * 100)}%`)),
        h("button", { class: "small", onclick: () => open({ label: app.label === app.key ? "" : app.label }) }, "직접 정하기")));
    return li;
  }) : [h("li", { class: "muted" }, "최근 7일 동안 기록된 앱이 없습니다.")]));
}

document.querySelectorAll('input[name="track-mode"]').forEach((el) => el.addEventListener("change", () => guarded(async () => {
  try {
    await api("POST", "/api/tracking/mode", { mode: el.value });
    toast(el.value === "selected" ? "이제 업무 앱만 기록합니다. 지난 기록에도 적용하려면 '다시 적용'을 누르세요."
      : "이제 모든 앱을 기록합니다.");
  } finally {
    loadTracking();
  }
})));

$("track-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "/api/tracking/rules", { app: $("track-app").value, title: $("track-title").value });
    $("track-app").value = "";
    $("track-title").value = "";
    loadTracking();
  });
});

$("track-reapply").addEventListener("click", () => {
  if (!confirm("최근 30일 기록을 원본에서 다시 가져와 지금 규칙대로 고칩니다. 기록 대상이 아닌 창은 이름·제목이 지워집니다. 계속할까요?")) return;
  guarded(async () => {
    $("track-reapply").disabled = true;
    try {
      const r = await api("POST", "/api/tracking/reapply", { days: 30 });
      toast(r.ok ? `다시 적용했습니다 (창 기록 ${r.imported.window}건).` : `실패: ${r.error}`);
    } finally {
      $("track-reapply").disabled = false;
    }
  });
});

// ------------------------------------------------------------------ privacy (설정)
async function loadExclusions() {
  const list = await guarded(() => api("GET", "/api/exclusions"));
  if (!list) return;
  $("exclusions").replaceChildren(...(list.length ? list.map((x) => h("li", {},
    h("span", {}, h("span", { class: "meta" }, x.field === "app" ? "앱: " : "제목: "), x.pattern),
    h("button", { class: "small", onclick: () => guarded(async () => { await api("DELETE", `/api/exclusions/${x.id}`); loadExclusions(); }) }, "삭제")))
    : [h("li", { class: "muted" }, "제외 항목 없음")]));
}

$("exclusion-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    const r = await api("POST", "/api/exclusions", { field: $("exclusion-field").value, pattern: $("exclusion-pattern").value });
    $("exclusion-pattern").value = "";
    toast(`제외 목록에 추가했습니다. 기존 기록 ${r.purged}건을 삭제했습니다.`);
    loadExclusions();
  });
});

const deletedSummary = (r) => `삭제 완료: 창 기록 ${r.window_events}건, 메모 ${r.notes}건`;

$("delete-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const from = $("delete-from").value, to = $("delete-to").value;
  if (!from || !to) return toast("시작일과 종료일을 선택하세요.");
  if (!confirm(`${from} ~ ${to} 기간의 활동·메모를 삭제합니다. 되돌릴 수 없습니다.`)) return;
  guarded(async () => toast(deletedSummary(await api("POST", "/api/delete-data", { from, to }))));
});

$("delete-all").addEventListener("click", () => {
  if (!confirm("가져온 모든 활동 기록과 메모를 삭제합니다. 되돌릴 수 없습니다. 계속할까요?")) return;
  guarded(async () => toast(deletedSummary(await api("POST", "/api/delete-data", { all: true }))));
});

// ------------------------------------------------------------------ dashboard
// Where the time went (by kind of activity, inferred from the app) and what came out of it.
const dash = { span: "day", day: isoDay(new Date()) };
const WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"];
const weekdayOf = (day) => WEEKDAYS[(new Date(day + "T00:00:00").getDay() + 6) % 7];
const pct = (part, whole) => (whole > 0 ? Math.round((part / whole) * 100) : 0);
const hours = (sec) => `${Math.round(sec / 360) / 10}`;
// "이번 주", "지난주" or "11/2 주" for the week shown (r.week starts on its Monday).
function weekName(r) {
  const monday = r.week[0].day, today = isoDay(new Date());
  const thisMonday = shiftDay(today, -((new Date(today + "T00:00:00").getDay() + 6) % 7));
  if (monday === thisMonday) return "이번 주";
  if (monday === shiftDay(thisMonday, -7)) return "지난주";
  return `${Number(monday.slice(5, 7))}/${Number(monday.slice(8))} 주`;
}

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c != null) el.append(typeof c === "string" ? document.createTextNode(c) : c);
  return el;
}

let dashSeq = 0;
async function loadDashboard() {
  const seq = ++dashSeq;
  $("dash-date").value = dash.day;
  $("dash-day").classList.toggle("on", dash.span === "day");
  $("dash-week").classList.toggle("on", dash.span === "week");
  const r = await guarded(() => api("GET", `/api/dashboard?date=${dash.day}&span=${dash.span}`));
  if (r && seq === dashSeq) renderDashboard(r);
}

// On a large window the dashboard takes the height left below the header (style.css); the
// week chart is drawn for the size its card ends up with, and again when the window changes.
let dashLast = null;
function fitDashboard() {
  const tab = $("tab-dashboard");
  if (tab.hidden) return;
  tab.style.setProperty("--dash-h", `${Math.floor(window.innerHeight - tab.getBoundingClientRect().top - 16)}px`);
}
window.addEventListener("resize", () => { fitDashboard(); if (dashLast) renderWeekChart(dashLast); });

function renderDashboard(r) {
  dashLast = r;
  fitDashboard();
  const isDay = r.span === "day";
  $("dash-period").textContent = isDay ? `${r.day} (${weekdayOf(r.day)})` : `${r.start} ~ ${r.end}`;
  renderKpis(r);
  renderWeekChart(r);
  renderDonut(r);
  $("dash-strip-card").hidden = !isDay;
  if (isDay) renderStrip(r);
  renderDashWork(r);
  renderDashFolders(r);
  renderWeekChart(r);  // again, now that the cards around it have their final size
}

function renderKpis(r) {
  const t = r.totals, p = r.previous, diff = t.active_s - p.active_s;
  const kpi = (label, value, sub, cls = "") => h("div", { class: "kpi" },
    h("div", { class: "label" }, label), h("div", { class: `value ${cls}`, title: value }, value),
    h("div", { class: "sub" }, ...[].concat(sub)));
  const compare = r.span === "day" ? " 전날보다" : " 지난주 같은 기간보다";
  $("dash-kpis").replaceChildren(
    kpi("활동 시간", dur(t.active_s),
      p.active_s ? [h("span", { class: diff >= 0 ? "up" : "down" }, `${diff >= 0 ? "+" : "−"}${dur(Math.abs(diff))}`), compare] : "비교할 기록 없음"),
    kpi("업무 비율", `${pct(t.work_s, t.active_s)}%`, `업무 ${dur(t.work_s)} · 업무 외 ${dur(t.offwork_s)}`),
    kpi("고친 파일", `${r.files.toLocaleString()}개`, `+${r.lines_added.toLocaleString()} / −${r.lines_removed.toLocaleString()}줄`),
    kpi("가장 많이 쓴 앱", r.top_app ? r.top_app.label : "없음", r.top_app ? dur(r.top_app.seconds) : "기록된 활동이 없습니다", "name"));
}

// Stacked bars, one per day of the week; clicking a day opens that day.
function renderWeekChart(r) {
  // Drawn in the shape of its box when the box has a height of its own (the fitted layout).
  // Exactly the box's shape, so nothing (the weekday labels at the bottom) is cut off.
  const box = $("dash-week-chart");
  const room = box.clientHeight - 2;
  const W = 640, left = 34, bottom = 34, top = 8;
  const H = room > 0 && box.clientWidth > 0 && getComputedStyle(box).overflow === "hidden"
    ? Math.round(Math.min(420, Math.max(70, (W * room) / box.clientWidth))) : 220;
  const totals = r.week.map((d) => r.categories.reduce((s, c) => s + d.by_kind[c.key], 0));
  const maxH = Math.max(1, Math.ceil(Math.max(...totals) / 3600));
  // Hour lines at least 18 units apart, however short the chart is.
  const step = [1, 2, 3, 4, 6, 12].find((st) => (H - bottom - top) / Math.ceil(maxH / st) >= 18) || 12;
  const ceil = Math.ceil(maxH / step) * step;
  const y = (sec) => H - bottom - (sec / (ceil * 3600)) * (H - bottom - top);
  const colW = (W - left) / 7, barW = Math.min(44, colW * 0.55);
  const chart = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "요일별 활동 시간을 분류별로 쌓은 막대 차트" });
  for (let hr = 0; hr <= ceil; hr += step) {
    chart.append(svg("line", { x1: left, x2: W, y1: y(hr * 3600), y2: y(hr * 3600), stroke: "var(--line)" }),
      svg("text", { x: left - 6, y: y(hr * 3600) + 4, "text-anchor": "end" }, `${hr}h`));
  }
  r.week.forEach((d, i) => {
    const x = left + colW * i + (colW - barW) / 2;
    const selected = r.span === "day" ? d.day === r.day : true;
    const g = svg("g", { class: "bar-day" });
    let acc = 0;
    const lines = [`${d.day.slice(5)} (${WEEKDAYS[i]}) ${dur(totals[i])}`];
    for (const c of r.categories) {
      const sec = d.by_kind[c.key];
      if (!sec) continue;
      const y1 = y(acc + sec), y0 = y(acc);
      g.append(svg("rect", { class: "seg-bar", x, y: y1, width: barW, height: Math.max(0, y0 - y1 - 1), rx: 2,
        fill: c.color, opacity: selected ? 1 : 0.5 }));
      acc += sec;
      lines.push(`${c.label} ${dur(sec)}`);
    }
    g.append(svg("rect", { x: left + colW * i, y: top, width: colW, height: H - top, fill: "transparent" }),
      svg("title", {}, lines.join("\n")));
    g.addEventListener("click", () => { dash.span = "day"; dash.day = d.day; loadDashboard(); });
    chart.append(g, svg("text", { x: x + barW / 2, y: H - 14, "text-anchor": "middle", class: selected && r.span === "day" ? "sel" : null },
      `${WEEKDAYS[i]} ${Number(d.day.slice(5, 7))}/${Number(d.day.slice(8))}`));
    if (totals[i]) chart.append(svg("text", { x: x + barW / 2, y: y(acc) - 5, "text-anchor": "middle" }, `${hours(totals[i])}h`));
  });
  $("dash-week-title").textContent = `${weekName(r)} 분류별 시간`;
  $("dash-week-chart").replaceChildren(chart);
  $("dash-week-legend").replaceChildren(dashLegend(r.categories.filter((c) => r.week.some((d) => d.by_kind[c.key] > 0))));
}

const dashLegend = (cats) => h("div", { class: "dash-legend" },
  cats.map((c) => h("span", {}, h("i", { class: "swatch", style: { background: c.color } }), c.label)));

function renderDonut(r) {
  const total = r.categories.reduce((s, c) => s + r.by_kind[c.key], 0);
  $("dash-donut-title").textContent = r.span === "week" ? `${weekName(r)} 분류`
    : r.day === isoDay(new Date()) ? "오늘 분류" : `${r.day.slice(5)} 분류`;
  if (!total) return $("dash-donut").replaceChildren(h("p", { class: "muted" }, "이 기간에 기록된 활동이 없습니다."));
  const R = 40, C = 2 * Math.PI * R;
  const ring = svg("svg", { viewBox: "0 0 100 100", role: "img", "aria-label": "분류별 시간 비율 도넛 차트" },
    svg("circle", { cx: 50, cy: 50, r: R, fill: "none", stroke: "var(--line)", "stroke-width": 14 }));
  let offset = 0;
  const used = r.categories.filter((c) => r.by_kind[c.key] > 0);
  for (const c of used) {
    const len = (r.by_kind[c.key] / total) * C;
    ring.append(svg("circle", { cx: 50, cy: 50, r: R, fill: "none", stroke: c.color, "stroke-width": 14,
      "stroke-dasharray": `${Math.max(0, len - 0.6)} ${C}`, "stroke-dashoffset": -offset, transform: "rotate(-90 50 50)" },
      svg("title", {}, `${c.label} ${dur(r.by_kind[c.key])} (${pct(r.by_kind[c.key], total)}%)`)));
    offset += len;
  }
  ring.append(svg("text", { x: 50, y: 49, "text-anchor": "middle", "font-size": 13, "font-weight": 600, fill: "var(--text)" },
      `${hours(total)}시간`),
    svg("text", { x: 50, y: 62, "text-anchor": "middle", "font-size": 8, fill: "var(--muted)" }, `업무 ${pct(r.totals.work_s, total)}%`));
  $("dash-donut").replaceChildren(h("div", { class: "donut-wrap" }, ring,
    h("div", { class: "donut-list" }, used.map((c) => h("div", {},
      h("i", { class: "swatch", style: { background: c.color } }), c.label,
      h("span", { class: "t" }, `${dur(r.by_kind[c.key])} · ${pct(r.by_kind[c.key], total)}%`))))));
}

// The day as one bar from the first to the last activity (at least 9 to 18 o'clock).
function renderStrip(r) {
  const kinds = Object.fromEntries(r.categories.map((c) => [c.key, c]));
  const base = new Date(r.day + "T00:00:00").getTime() / 1000;
  const hourOf = (ts) => (ts - base) / 3600;
  const from = Math.min(9, ...r.blocks.map((b) => Math.floor(hourOf(b.start))));
  const to = Math.max(18, ...r.blocks.map((b) => Math.ceil(hourOf(b.end))));
  const pos = (ts) => ((hourOf(ts) - from) / (to - from)) * 100;
  const strip = h("div", { class: "strip" }, r.blocks.map((b) => h("div", {
    style: { left: `${pos(b.start)}%`, width: `${Math.max(0.3, pos(b.end) - pos(b.start))}%`, background: kinds[b.kind].color },
    title: `${hhmm(b.start)}–${hhmm(b.end)} ${kinds[b.kind].label} (${dur(b.end - b.start)})`,
  })));
  const every = to - from > 12 ? 2 : 1;
  const axis = h("div", { class: "strip-axis" });
  for (let hr = from; hr <= to; hr += every) axis.append(h("span", { style: { left: `${((hr - from) / (to - from)) * 100}%` } }, `${hr}시`));
  $("dash-strip").replaceChildren(r.blocks.length
    ? h("div", {}, strip, axis)  // the colors are the week chart's legend, just above
    : h("p", { class: "muted" }, "이 날짜에 기록된 활동이 없습니다."));
}

// One line: name (and what is known about it), a meter, the number.
function dashRow({ name, sub, stat, share, onclick }) {
  return h("div", { class: "dash-row", title: [name, sub].filter(Boolean).join(" · ") + "\n타임라인에서 보기", onclick },
    h("div", { class: "dash-name" }, name, sub ? h("span", { class: "dash-sub" }, sub) : null),
    h("div", { class: "dash-meter" }, h("div", { style: { width: `${share * 100}%` } })),
    h("div", { class: "dash-stat" }, stat));
}

// Time per work folder, then 웹·소통·기타 (outside the work folders); a click opens that day's timeline.
function renderDashWork(r) {
  if (!r.work.length) return $("dash-work").replaceChildren(h("p", { class: "muted" }, "이 기간에 기록된 활동이 없습니다."));
  const max = Math.max(...r.work.map((w) => w.seconds), 1);
  const when = (day) => `${day.slice(5)} (${weekdayOf(day)})`;
  $("dash-work").replaceChildren(...r.work.map((w) => dashRow({
    name: w.title, share: w.seconds / max, stat: dur(w.seconds),
    sub: [w.outside ? "작업 폴더 밖" : "", r.span === "week" ? (w.days > 1 ? `${w.days}일 · 가장 오래 한 날 ${when(w.day)}` : when(w.day)) : ""]
      .filter(Boolean).join(" · "),
    onclick: () => gotoDay(w.day),
  })));
}

function renderDashFolders(r) {
  if (!r.folders.length) {
    return $("dash-folders").replaceChildren(h("p", { class: "muted" },
      "이 기간에 기록된 파일 변경이 없습니다. 설정 → 작업 폴더에서 소스 코드나 문서 폴더를 추가하면 바뀐 파일이 기록됩니다."));
  }
  const max = Math.max(...r.folders.map((f) => f.files), 1);
  $("dash-folders").replaceChildren(...r.folders.map((f) => dashRow({
    name: f.label, share: f.files / max, stat: `파일 ${f.files.toLocaleString()}개`,
    sub: `+${f.lines_added.toLocaleString()} −${f.lines_removed.toLocaleString()}줄 · 기록 ${f.changes}번`,
    onclick: () => gotoDay(f.day),
  })));
}

$("dash-day").addEventListener("click", () => { dash.span = "day"; loadDashboard(); });
$("dash-week").addEventListener("click", () => { dash.span = "week"; loadDashboard(); });
$("dash-prev").addEventListener("click", () => { dash.day = shiftDay(dash.day, dash.span === "week" ? -7 : -1); loadDashboard(); });
$("dash-next").addEventListener("click", () => { dash.day = shiftDay(dash.day, dash.span === "week" ? 7 : 1); loadDashboard(); });
$("dash-today").addEventListener("click", () => { dash.day = isoDay(new Date()); loadDashboard(); });
$("dash-date").addEventListener("change", (e) => { if (e.target.value) { dash.day = e.target.value; loadDashboard(); } });

// ------------------------------------------------------------------ boot
(async function boot() {
  const status = await guarded(loadStatus);
  if (status?.last_day && status.last_day < state.day) state.day = state.reportDay = dash.day = status.last_day;
  $("day").value = state.day;
  $("report-day").value = state.reportDay;
  await guarded(loadProjects);
  await loadDashboard();
  setInterval(() => guarded(loadStatus), 30000);
  // Refresh today's timeline and dashboard periodically while visible and nothing is being edited.
  setInterval(() => {
    const editing = document.activeElement && ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName);
    if (!$("tab-timeline").hidden && state.day === isoDay(new Date()) && !editing) loadTimeline();
    if (!$("tab-dashboard").hidden && dash.day === isoDay(new Date()) && !editing) loadDashboard();
  }, 60000);
})();
