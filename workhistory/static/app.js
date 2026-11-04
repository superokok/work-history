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
  reviewDay: isoDay(new Date()),
  reportDay: isoDay(new Date()),
  resumeProject: null,
  openSessions: new Set(),
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
  projectOptions($("filter-project"), { all: true, selected: keep("filter-project") });
  projectOptions($("search-project"), { all: true, selected: keep("search-project") });
  projectOptions($("note-project"), { none: "프로젝트 없음", selected: keep("note-project") });
}

// ------------------------------------------------------------------ tabs
document.querySelectorAll("button.tab").forEach((btn) =>
  btn.addEventListener("click", () => showTab(btn.dataset.tab)));

function showTab(name) {
  document.querySelectorAll("button.tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
  if (name === "dashboard") loadDashboard();
  if (name === "timeline") loadTimeline();
  if (name === "review") loadReview();
  if (name === "report") loadReport();
  if (name === "resume") loadResume();
  if (name === "projects") { renderProjects(); loadFolders(); }
  if (name === "privacy") { loadLaunch(); loadAISettings(); loadTracking(); loadExclusions(); }
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
$("filter-project").addEventListener("change", () => loadTimeline());
$("filter-app").addEventListener("change", () => loadTimeline());

const WARNINGS = {
  afk_missing: "이 날짜에는 유휴(AFK) 감지 기록이 없어 모든 창 시간을 활동으로 계산했습니다. 실제보다 길 수 있습니다.",
};

let timelineSeq = 0;
async function loadTimeline() {
  const seq = ++timelineSeq;
  const params = new URLSearchParams({ date: state.day });
  if ($("filter-project").value) params.set("project", $("filter-project").value);
  if ($("filter-app").value) params.set("app", $("filter-app").value);
  const t = await guarded(() => api("GET", `/api/timeline?${params}`));
  if (!t || seq !== timelineSeq) return;

  $("warnings").replaceChildren(...t.warnings.map((w) => h("div", { class: "warning" }, WARNINGS[w] || w)));

  const appSel = $("filter-app");
  const currentApp = appSel.value;
  appSel.replaceChildren(h("option", { value: "" }, "전체"),
    ...t.by_app.map((a) => h("option", { value: a.app }, `${appName(a.app)} (${dur(a.seconds)})`)));
  appSel.value = t.by_app.some((a) => a.app === currentApp) ? currentApp : "";

  $("summary").replaceChildren(...summaryBlock(t.total_active_s, t.by_project, t.offwork_s));
  state.dayChanges = t.changes || [];  // session cards show the file changes made during them
  renderSessions(t.sessions);
  renderNotes(t.notes);
  renderChanges(state.dayChanges);
}

const OFFWORK_COLOR = "#c8ccd4";

function summaryBlock(total, byProject, offwork = 0) {
  const max = Math.max(1, offwork, ...byProject.map((p) => p.seconds));
  return [
    h("div", { class: "summary-head" },
      h("span", {}, "프로젝트별 활동 시간 ", badge("inferred", "자동 집계")),
      h("span", { class: "total" }, dur(total))),
    h("div", { class: "bars" }, byProject.flatMap((p) => [
      h("span", {}, h("span", { class: "dot", style: { background: projectColor(p.project_id) } }), projectName(p.project_id)),
      h("div", { class: "bar", style: { width: `${(p.seconds / max) * 100}%`, background: projectColor(p.project_id) } }),
      h("span", { class: "num" }, dur(p.seconds)),
    ]), offwork > 0 ? [
      h("span", { class: "muted", title: "기록 대상이 아닌 앱에서 보낸 시간 (앱 이름·창 제목은 저장하지 않음)" }, "업무 외"),
      h("div", { class: "bar offwork", style: { width: `${(offwork / max) * 100}%`, background: OFFWORK_COLOR } }),
      h("span", { class: "num muted" }, dur(offwork)),
    ] : []),
  ];
}

async function patchSession(id, body, message) {
  await guarded(async () => {
    await api("PATCH", `/api/sessions/${id}`, body);
    if (message) toast(message);
    await loadTimeline();
  });
}

function renderSessions(sessions) {
  $("rows-empty").hidden = sessions.length > 0;
  $("sessions").replaceChildren(...sessions.map((s, i) => sessionCard(s, sessions[i + 1])));
}

function sessionCard(s, next) {
  const projectSelect = h("select", {
    title: "세션 프로젝트",
    onchange: (e) => patchSession(s.id, { project_id: e.target.value },
      e.target.value === "auto" ? "자동 분류로 되돌렸습니다." : "세션 프로젝트를 지정했습니다."),
  },
  h("option", { value: "auto" }, s.project_manual ? "자동 분류로 되돌리기" : `자동: ${projectName(s.project_id)}`),
  h("option", { value: "" }, "미분류"),
  ...state.projects.filter((p) => !p.archived || p.id === s.project_id).map((p) => h("option", { value: p.id }, p.name)));
  projectSelect.value = s.project_manual ? String(s.project_id ?? "") : "auto";

  const noteBox = h("div", { class: "session-note-edit", hidden: true });
  const noteArea = h("textarea", { rows: 3, placeholder: "이 세션에서 한 일, 막힌 점, 다음에 할 일…" });
  noteArea.value = s.note;
  noteBox.append(noteArea, h("div", { class: "row" },
    h("button", { class: "small primary", onclick: () => patchSession(s.id, { note: noteArea.value }, "메모를 저장했습니다.") }, "메모 저장"),
    h("button", { class: "small", onclick: () => (noteBox.hidden = true) }, "취소")));

  const details = h("details", { open: state.openSessions.has(s.id) },
    h("summary", {}, `창 기록 ${s.rows.length}개 보기`), rowsTable(s));
  details.addEventListener("toggle", () =>
    details.open ? state.openSessions.add(s.id) : state.openSessions.delete(s.id));

  if (s.offwork && !s.title_edited && !s.note) {
    return h("div", { class: "card session offwork-session" },
      h("div", { class: "session-head" },
        h("span", { class: "time" }, `${hhmm(s.start)}–${hhmm(s.end)}`),
        h("span", { class: "session-title muted" }, "업무 외"),
        badge("plain", "기록 대상 아님"),
        h("span", { class: "num session-dur muted" }, dur(s.active_s))));
  }

  return h("div", { class: `card session${s.excluded ? " excluded" : ""}`, style: { borderLeftColor: projectColor(s.project_id) } },
    h("div", { class: "session-head" },
      h("span", { class: "time" }, `${hhmm(s.start)}–${hhmm(s.end)}`),
      h("span", { class: "session-title" }, s.title),
      s.title_edited ? badge("user", "직접 수정") : badge("inferred", "자동 추정"),
      s.locked ? badge("plain", "범위 고정") : null,
      s.excluded ? badge("plain", "제외됨") : null,
      h("span", { class: "num session-dur" }, dur(s.active_s))),
    s.note ? h("div", { class: "session-note" }, s.note) : null,
    noteBox,
    h("div", { class: "row session-actions" },
      projectSelect, s.project_manual ? badge("user", "직접 지정") : badge("inferred", "자동"),
      h("button", { class: "small", onclick: () => {
        const title = prompt("세션 제목 (비우면 자동 제목으로 돌아갑니다)", s.title_edited ? s.title : "");
        if (title !== null) patchSession(s.id, { title });
      } }, "제목 수정"),
      h("button", { class: "small", onclick: () => { noteBox.hidden = false; noteArea.focus(); } }, s.note ? "메모 수정" : "메모"),
      h("button", { class: "small", onclick: () => patchSession(s.id, { excluded: !s.excluded },
        s.excluded ? "다시 집계에 포함합니다." : "집계·리뷰에서 제외했습니다.") }, s.excluded ? "포함" : "제외"),
      next ? h("button", { class: "small", onclick: () => mergeWith(s, next) }, "다음 세션과 병합") : null),
    changesLine(s),
    details);
}

// ------------------------------------------------------------------ results (file changes)
const SNAPSHOT_INTERVAL_S = 300;
const kindLabel = { A: "추가", M: "수정", D: "삭제" };
const lineDelta = (f) => (f.is_text === 0 || f.lines_added === null ? "" : ` (+${f.lines_added} −${f.lines_removed})`);

function fileButton(f) {
  return h("button", { class: `link file-${f.kind}`, title: `${kindLabel[f.kind]} · 변경 내용 보기`, onclick: () => showDiff(f) },
    f.path.split("/").pop(), lineDelta(f));
}

function changesLine(s) {
  // Snapshots run every few minutes, so a change recorded shortly after a session still belongs to it.
  const files = (state.dayChanges || []).filter((c) => c.ts > s.start && c.ts <= s.end + SNAPSHOT_INTERVAL_S)
    .flatMap((c) => c.files);
  if (!files.length) return null;
  const shown = files.slice(0, 6);
  return h("div", { class: "session-changes" }, h("span", { class: "meta" }, "파일 변경 "),
    ...shown.flatMap((f, i) => [i ? ", " : "", fileButton(f)]),
    files.length > shown.length ? h("span", { class: "meta" }, ` 외 ${files.length - shown.length}개`) : null);
}

function renderChanges(changes) {
  $("changes-card").hidden = changes.length === 0;
  $("changes").replaceChildren(...changes.map((c) => h("li", { class: "change-item" },
    h("div", {},
      h("span", { class: "time" }, hhmm(c.ts)), " ", h("b", {}, c.folder),
      h("span", { class: "meta" }, ` · ${projectName(c.project_id)} · 파일 ${c.files_added + c.files_modified + c.files_deleted}개`,
        c.lines_added || c.lines_removed ? ` · +${c.lines_added} −${c.lines_removed}` : "")),
    h("div", { class: "change-files" }, ...c.files.flatMap((f, i) => [i ? ", " : "", fileButton(f)])))),
    changes.some((c) => c.files.some((f) => f.project_id === null)) ? h("li", { class: "meta" },
      "미분류 파일 변경이 있습니다. ", h("button", { class: "small link", onclick: () => showTab("projects") },
        "프로젝트 탭 → 작업 폴더에서 정하기")) : "");
}

function diffLines(d) {
  if (!d.text) return [h("div", { class: "muted" }, "텍스트가 아닌 파일이라 내용을 비교할 수 없습니다.")];
  return d.diff.split("\n").map((line) => h("div", {
    class: line.startsWith("+") && !line.startsWith("+++") ? "add"
      : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "",
  }, line || " "));
}

async function showDiff(f) {
  const d = await guarded(() => api("GET", `/api/change-files/${f.id}/diff`));
  if (!d) return;
  $("diff-title").textContent = `${d.path} · ${kindLabel[d.kind]}`;
  $("diff-body").replaceChildren(...diffLines(d));
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
    ...diffs.flatMap((d) => [h("div", { class: "diff-file" }, `${d.path} · ${kindLabel[d.kind]}`), ...diffLines(d)]),
    ...(total > files.length ? [h("div", { class: "muted" }, `외 ${total - files.length}개 파일은 타임라인에서 볼 수 있습니다.`)] : []));
  $("diff-overlay").hidden = false;
}
$("diff-close").addEventListener("click", () => ($("diff-overlay").hidden = true));
$("diff-overlay").addEventListener("click", (e) => { if (e.target.id === "diff-overlay") $("diff-overlay").hidden = true; });

// Projects for sub-folders of a registered folder. The app only suggests (same name as a project);
// nothing is assigned until the user presses 확인. "프로젝트 없음" is a decision too: not asked again.
const openAreas = new Map();  // folder id -> sub-folder being looked into ("" = the folder itself)

function areaSelect(suggested) {
  return h("select", {},
    h("option", { value: "" }, "프로젝트 고르기"),
    ...state.projects.filter((p) => !p.archived).map((p) =>
      h("option", { value: p.id, selected: p.id === suggested }, p.id === suggested ? `${p.name} (이름이 같음)` : p.name)),
    h("option", { value: "none" }, "프로젝트 없음 (묻지 않기)"));
}

const areaChoice = (select) => (select.value === "none" ? null : Number(select.value));

async function confirmAreas(folderId, picks) {
  for (const [subpath, projectId] of picks) {
    await api("POST", `/api/folders/${folderId}/areas`, { subpath, project_id: projectId });
  }
  toast(`하위 폴더 ${picks.length}개의 프로젝트를 정했습니다. 지난 기록에도 적용했습니다.`);
  loadFolders();
}

async function renderAreas(f, box) {
  const under = openAreas.get(f.id) || "";
  const a = await guarded(() => api("GET", `/api/folders/${f.id}/areas?under=${encodeURIComponent(under)}`));
  if (!a) return;
  const up = under.includes("/") ? under.slice(0, under.lastIndexOf("/")) : "";
  const rows = a.groups.map((g) => {
    const select = areaSelect(g.suggested_project_id);
    return { g, select, el: h("div", { class: "area-row" },
      h("div", {}, h("b", {}, g.name), h("span", { class: "meta" }, ` · 변경 ${g.changes}건 · 파일 ${g.files}개 · ${ago(g.last_ts)}`)),
      h("div", { class: "row" }, select,
        h("button", { class: "small", onclick: () => select.value
          ? guarded(() => confirmAreas(f.id, [[g.subpath, areaChoice(select)]]))
          : toast("프로젝트를 고르세요.") }, "확인"),
        h("button", { class: "small link", title: "이 폴더 안을 더 나눠서 정합니다",
          onclick: () => { openAreas.set(f.id, g.subpath); renderAreas(f, box); } }, "안쪽 폴더로 나누기"))) };
  });
  const suggested = rows.filter((r) => r.g.suggested_project_id);
  let whole = null;
  if (under && a.loose_files) {
    const select = areaSelect(a.under_suggested_project_id);
    whole = h("div", { class: "area-row" },
      h("div", {}, h("b", {}, `'${under.split("/").pop()}' 전체`),
        h("span", { class: "meta" }, ` · 이 폴더 바로 안의 파일 ${a.loose_files}개 포함`)),
      h("div", { class: "row" }, select, h("button", { class: "small", onclick: () => select.value
        ? guarded(() => confirmAreas(f.id, [[under, areaChoice(select)]])) : toast("프로젝트를 고르세요.") }, "확인")));
  }
  box.replaceChildren(
    h("div", { class: "row" },
      under ? h("button", { class: "small link", onclick: () => { openAreas.set(f.id, up); renderAreas(f, box); } }, "← 위로") : "",
      h("span", { class: "meta" }, under ? `${f.label}/${under} 안` : "앱이 추천만 하고, 확인을 눌러야 저장됩니다."),
      suggested.length > 1 ? h("button", { class: "small", onclick: () => guarded(() => confirmAreas(f.id,
        suggested.map((r) => [r.g.subpath, r.g.suggested_project_id]))) }, `추천 ${suggested.length}개 모두 확인`) : ""),
    ...rows.map((r) => r.el),
    whole || "",
    !under && a.loose_files ? h("div", { class: "meta" },
      `폴더 바로 안의 파일 ${a.loose_files}개는 위에서 고른 폴더의 프로젝트를 따릅니다.`) : "",
    !rows.length && !whole ? h("div", { class: "muted" }, "프로젝트를 정할 하위 폴더가 없습니다.") : "",
    a.areas.length ? h("details", { class: "area-saved" }, h("summary", {}, `정해 둔 하위 폴더 ${a.areas.length}개`),
      ...a.areas.map((x) => h("div", { class: "area-row" },
        h("span", {}, x.subpath, h("span", { class: "meta" }, ` → ${x.project_id ? projectName(x.project_id) : "프로젝트 없음"}`)),
        h("button", { class: "small link", onclick: () => guarded(async () => {
          await api("DELETE", `/api/folders/${f.id}/areas`, { subpath: x.subpath });
          loadFolders();
        }) }, "취소")))) : "");
}

async function loadFolders() {
  const folders = await guarded(() => api("GET", "/api/folders"));
  if (!folders) return;
  projectOptions($("folder-project"), { none: "프로젝트 없음", selected: $("folder-project").value });
  $("folder-list").replaceChildren(...(folders.length ? folders.map((f) => {
    const project = h("select", { onchange: (e) => guarded(async () => {
      await api("PATCH", `/api/folders/${f.id}`, { project_id: e.target.value ? Number(e.target.value) : null });
      toast("폴더의 프로젝트를 바꿨습니다.");
    }) });
    projectOptions(project, { none: "프로젝트 없음", selected: f.project_id ?? "" });
    const areas = h("div", { class: "areas", hidden: !openAreas.has(f.id) });
    if (openAreas.has(f.id)) renderAreas(f, areas);
    const toggleAreas = () => {
      if (openAreas.has(f.id)) { openAreas.delete(f.id); areas.hidden = true; return; }
      openAreas.set(f.id, "");
      areas.hidden = false;
      renderAreas(f, areas);
    };
    return h("li", { class: "folder-item" },
      h("div", {},
        h("b", {}, f.label), f.enabled ? null : badge("plain", "꺼짐"),
        h("div", { class: "meta" }, f.path),
        h("div", { class: "meta" }, `파일 ${f.files}개 · `,
          f.last_scan_at ? `마지막 확인 ${ago(f.last_scan_at)}` : "아직 기록 전",
          f.last_change_at ? ` · 마지막 변경 ${ago(f.last_change_at)}` : ""),
        f.last_error ? h("div", { class: "warning" }, f.last_error) : null,
        f.unassigned_changes ? h("div", { class: "unassigned" },
          `프로젝트가 정해지지 않은 파일 변경 ${f.unassigned_changes}건 `,
          h("button", { class: "small", onclick: toggleAreas }, "하위 폴더별로 정하기"))
          : f.areas ? h("button", { class: "small link", onclick: toggleAreas }, `하위 폴더 프로젝트 ${f.areas}개 보기`) : null),
      h("div", { class: "row" }, project,
        h("button", { class: "small", onclick: () => guarded(async () => {
          await api("PATCH", `/api/folders/${f.id}`, { enabled: !f.enabled });
          loadFolders();
        }) }, f.enabled ? "끄기" : "켜기"),
        h("button", { class: "small danger", onclick: () => {
          if (!confirm(`'${f.label}' 폴더 기록을 삭제할까요? 이 폴더의 변경 기록과 숨은 Git 저장소가 지워집니다. 원래 폴더의 파일은 그대로입니다.`)) return;
          guarded(async () => { await api("DELETE", `/api/folders/${f.id}`); loadFolders(); });
        } }, "삭제")),
      areas);
  }) : [h("li", { class: "muted" }, "등록한 폴더가 없습니다. 소스 코드나 문서 폴더를 추가하세요.")]));
}

$("folder-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "/api/folders", {
      path: $("folder-path").value, label: $("folder-label").value,
      project_id: $("folder-project").value ? Number($("folder-project").value) : null,
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

function mergeWith(s, next) {
  if (!confirm(`${hhmm(s.start)}–${hhmm(next.end)} 사이의 세션을 하나로 합칩니다. 계속할까요?`)) return;
  guarded(async () => {
    await api("POST", "/api/sessions/merge", { ids: [s.id, next.id] });
    toast("세션을 병합했습니다.");
    await loadTimeline();
  });
}

function rowsTable(s) {
  return h("table", { class: "rows" },
    h("thead", {}, h("tr", {}, h("th", {}, "시간"), h("th", {}, "앱"), h("th", {}, "창 제목"),
      h("th", {}, "창 프로젝트"), h("th", { class: "num" }, "활동"), h("th", {}, ""))),
    h("tbody", {}, s.rows.map((r) => {
      const select = h("select", {
        title: "이 창 기록의 프로젝트 (세션 자동 분류에 반영)",
        onchange: (e) => guarded(async () => {
          await api("POST", "/api/assign", { event_ids: r.event_ids, project_id: e.target.value || null });
          toast(e.target.value ? "창 기록에 프로젝트를 지정했습니다." : "직접 지정을 해제했습니다 (규칙 적용).");
          loadTimeline();
        }),
      });
      projectOptions(select, { none: r.project_source === "manual" ? "미분류" : "미분류 / 규칙", selected: r.project_id ?? "" });
      const offwork = r.app === "";
      return h("tr", { class: offwork ? "offwork-row" : null },
        h("td", { class: "time" }, `${hhmm(r.start)}–${hhmm(r.end)}`),
        h("td", {}, offwork ? h("span", { class: "muted" }, "업무 외") : appName(r.app)),
        h("td", { class: "title" }, offwork ? h("span", { class: "muted" }, "(기록 대상이 아닌 앱)")
          : r.title || h("span", { class: "muted" }, "(제목 없음)")),
        h("td", { class: "project" }, offwork ? null : [select, r.project_source === "manual" ? badge("user", "수동")
          : r.project_source === "rule" ? badge("inferred", "규칙") : null]),
        h("td", { class: "num" }, dur(r.active_s)),
        h("td", {}, r.start > s.start + 1 ? h("button", { class: "link", title: "이 창 기록부터 새 세션으로 나눕니다",
          onclick: () => guarded(async () => {
            await api("POST", `/api/sessions/${s.id}/split`, { at: r.start });
            toast("세션을 나눴습니다.");
            loadTimeline();
          }) }, "여기서 분할") : null));
    })));
}

function renderNotes(notes) {
  $("notes").replaceChildren(...notes.map((n) => h("li", {},
    h("div", { class: "meta" }, hhmm(n.created_at), projectById(n.project_id) ? ` · ${projectName(n.project_id)}` : "", " ",
      h("button", { class: "link", onclick: () => editNote(n) }, "수정"), " ",
      h("button", { class: "link", onclick: () => deleteNote(n) }, "삭제")),
    n.body)));
}

function editNote(n) {
  const body = prompt("메모 수정", n.body);
  if (body === null) return;
  guarded(async () => { await api("PATCH", `/api/notes/${n.id}`, { body }); loadTimeline(); });
}

function deleteNote(n) {
  if (!confirm("이 메모를 삭제할까요?")) return;
  guarded(async () => { await api("DELETE", `/api/notes/${n.id}`); loadTimeline(); });
}

$("note-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "/api/notes", { day: state.day, body: $("note-body").value, project_id: $("note-project").value || null });
    $("note-body").value = "";
    loadTimeline();
  });
});

// ------------------------------------------------------------------ daily review
let reviewData = null;

function setReviewDay(day) {
  state.reviewDay = day;
  $("review-day").value = day;
  loadReview();
}
$("review-day").addEventListener("change", (e) => e.target.value && setReviewDay(e.target.value));
$("review-today").addEventListener("click", () => setReviewDay(isoDay(new Date())));
$("review-prev").addEventListener("click", () => setReviewDay(shiftDay(state.reviewDay, -1)));
$("review-next").addEventListener("click", () => setReviewDay(shiftDay(state.reviewDay, 1)));

function setReviewState() {
  const el = $("review-state");
  const saved = reviewData?.saved;
  const dirty = saved ? $("review-body").value !== saved.body : $("review-body").value !== reviewData?.generated;
  if (saved && !dirty) {
    el.className = "badge user";
    el.textContent = `저장됨 · ${new Date(saved.updated_at * 1000).toLocaleString("ko-KR")}`;
  } else if (saved) {
    el.className = "badge plain";
    el.textContent = "저장하지 않은 변경 있음";
  } else {
    el.className = "badge inferred";
    el.textContent = dirty ? "초안 수정 중 · 아직 저장하지 않음" : "자동 초안 · 아직 저장하지 않음";
  }
}
$("review-body").addEventListener("input", setReviewState);

async function loadReview() {
  const day = state.reviewDay;
  $("review-day").value = day;
  const r = await guarded(() => api("GET", `/api/review?date=${day}`));
  if (!r || day !== state.reviewDay) return;
  reviewData = r;
  $("review-body").value = r.saved ? r.saved.body : r.generated;
  setReviewState();
  renderReviewSide(r);
}

function renderReviewSide(r) {
  $("review-side").replaceChildren(
    h("div", { class: "card" }, ...summaryBlock(r.total_active_s, r.by_project, r.offwork_s)),
    h("div", { class: "card" },
      h("h2", {}, "작업 세션 ", badge("inferred", "자동 추정")),
      r.sessions.length ? h("ul", { class: "plain compact" }, r.sessions.map((s) => h("li", {},
        h("span", {}, h("span", { class: "dot", style: { background: projectColor(s.project_id) } }),
          `${hhmm(s.start)}–${hhmm(s.end)} ${s.title}`),
        h("span", { class: "meta" }, dur(s.active_s)))))
        : h("p", { class: "muted" }, "세션 없음"),
      h("button", { class: "link", onclick: () => { showTab("timeline"); setDay(r.day); } }, "타임라인에서 세션 수정 →")),
    h("div", { class: "card" },
      h("h2", {}, "후속 확인 후보 ", badge("inferred", "메모에서 추출")),
      r.followups.length ? h("ul", { class: "plain compact" }, r.followups.map((f) => h("li", {}, f)))
        : h("p", { class: "muted" }, "없음 — 메모에 '확인', '해야', 'TODO' 등을 적으면 여기에 모입니다.")),
    ...r.warnings.map((w) => h("div", { class: "warning" }, WARNINGS[w] || w)),
  );
}

$("review-save").addEventListener("click", () => guarded(async () => {
  const body = $("review-body").value;
  if (!body.trim()) return toast("빈 리뷰는 저장할 수 없습니다.");
  const r = await api("POST", "/api/review", { day: state.reviewDay, body });
  reviewData.saved = r.saved;
  setReviewState();
  toast("리뷰를 저장했습니다.");
}));

$("review-regenerate").addEventListener("click", () => guarded(async () => {
  if (reviewData?.saved && !confirm("편집 중인 내용을 최신 자동 초안으로 바꿉니다. 저장된 리뷰는 '저장'을 누르기 전까지 유지됩니다.")) return;
  await loadReview();
  $("review-body").value = reviewData.generated;
  setReviewState();
}));

$("review-copy").addEventListener("click", () => guarded(async () => {
  await navigator.clipboard.writeText($("review-body").value);
  toast("클립보드에 복사했습니다.");
}));

// ------------------------------------------------------------------ AI reports
const reportKind = () => document.querySelector('input[name="report-kind"]:checked').value;
let reportSaved = null;
let reportBusy = false;

function reportPeriod(kind, day) {
  if (kind === "daily") return [day, day];
  const d = new Date(day + "T00:00:00");
  const monday = shiftDay(day, -((d.getDay() + 6) % 7));
  return [monday, shiftDay(monday, 6)];
}

function setReportState() {
  const el = $("report-state");
  const body = $("report-body").value;
  if (reportSaved && body === reportSaved.body) {
    el.className = "badge user";
    el.textContent = `저장됨 · ${new Date(reportSaved.updated_at * 1000).toLocaleString("ko-KR")}`;
  } else if (body.trim()) {
    el.className = "badge plain";
    el.textContent = "저장하지 않음";
  } else {
    el.className = "badge";
    el.textContent = "";
  }
}
$("report-body").addEventListener("input", () => setReportState());

let reportLoaded = ""; // "kind|day" shown in the editor

async function loadReport() {
  const kind = reportKind(), day = state.reportDay;
  $("report-day").value = day;
  const [start, end] = reportPeriod(kind, day);
  $("report-period").textContent = start === end ? start : `${start} ~ ${end}`;
  $("report-preview-box").hidden = true;
  const [r, ai] = await Promise.all([
    guarded(() => api("GET", `/api/reports?kind=${kind}&date=${day}`)),
    guarded(() => api("GET", "/api/ai/settings")),
  ]);
  if (!r || kind !== reportKind() || day !== state.reportDay) return;
  // Coming back to the same report (e.g. after opening a cited record) keeps unsaved edits and the draft's evidence.
  const unsaved = $("report-body").value !== (reportSaved ? reportSaved.body : "");
  reportSaved = r.saved;
  if (reportLoaded !== `${kind}|${day}` || !unsaved) {
    reportLoaded = `${kind}|${day}`;
    $("report-body").value = r.saved ? r.saved.body : "";
    $("report-side").replaceChildren();
  }
  setReportState();
  renderReportNotice(ai);
}

function renderReportNotice(ai) {
  const notes = [];
  const local = ai && isLocal(ai.model);
  if (ai && !local && !ai.claude_installed && !(ai.local_models || []).length) notes.push(ai.claude_bundled
    ? "Claude Code가 설치되어 있지 않습니다. Claude Code를 설치하고 로그인하거나, 내 PC의 AI 모델을 llm 폴더에 넣으면 AI 초안을 만들 수 있습니다."
    : "내 PC의 AI 모델이 없습니다. 앱 폴더의 llm\\models 에 .gguf 모델 파일을 넣으면 AI 초안을 만들 수 있습니다.");
  else if (ai && !ai.demo && !ai.enabled) notes.push("AI가 꺼져 있습니다. 개인정보 탭의 AI 설정에서 모델을 고르면 실제 기록으로 초안을 만들 수 있습니다.");
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
    $("report-generate").disabled = true;
    $("report-generate").textContent = "AI가 작성 중… (1~3분)";
    try {
      const r = await api("POST", "/api/ai/report", { kind: reportKind(), day: state.reportDay });
      $("report-body").value = r.text;
      setReportState();
      renderReportSide(r);
    } finally {
      reportBusy = false;
      $("report-generate").disabled = false;
      $("report-generate").textContent = "AI 초안 만들기";
    }
  });
});

// ---- Citations. What the AI wrote and what is in the records must never look alike:
// AI text sits in a tinted "AI" box; records show their own content (the note, the files, the session title).
const SOURCE_KINDS = { session: "세션", change: "파일 변경", note: "메모", project: "프로젝트", review: "리뷰", day: "타임라인" };

const sourceWhen = (src) => (src.type === "session" || src.type === "change" ? `${src.day.slice(5)} ${hhmm(src.start)}`
  : src.day ? src.day.slice(5) : "");

function openSource(src, ref) {
  if (src.type === "project") { state.resumeProject = src.project_id; showTab("resume"); }
  else if (src.type === "change") showChange(Number(ref.slice(1)));
  else if (src.type === "review") gotoReview(src.day);
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
  const flagBadge = { no_evidence: badge("plain", "근거 없음"), unconfirmed_plan: badge("plain", "AI 제안"),
    unconfirmed_done: badge("plain", "완료 확인 필요") };
  const aiText = (text, flag) => h("div", { class: `ai-text${flag ? " flagged" : ""}` },
    badge("ai", "AI"), " ", text, flag ? [" ", flagBadge[flag].cloneNode(true)] : null);
  const [doneLabel, nextLabel, issueLabel] = r.kind === "daily" ? ["오늘 한 일", "다음 할 일", "이슈"] : ["금주 실적", "차주 계획", "이슈"];
  $("report-side").replaceChildren(
    h("div", { class: "card" },
      h("h2", {}, "AI 초안과 근거"),
      factLegend(),
      r.summary ? aiText(r.summary, r.summary_flag) : null,
      ...r.projects.map((p) => h("div", { class: "report-project" },
        h("h3", {}, p.project, p.seconds ? h("span", { class: "meta" }, ` · ${dur(p.seconds)}`) : null),
        p.missing_from_ai ? h("p", { class: "warning" }, "기록된 작업이 있지만 AI 초안에 빠졌습니다. 직접 작성하세요.") : null,
        ...[[doneLabel, p.done], [nextLabel, p.next], [issueLabel, p.issues]].filter(([, items]) => items.length).map(([label, items]) =>
          h("div", {}, h("div", { class: "meta" }, label),
            h("ul", { class: "report-items" }, items.map((it) => h("li", {},
              aiText(it.text, it.flag),
              it.refs.length ? factList(it.refs, r.sources) : null)))))))),
    h("p", { class: "muted small-text" },
      `AI 사용: 입력 ${r.usage.input_tokens.toLocaleString()} · 출력 ${(r.usage.output_tokens + r.usage.thinking_tokens).toLocaleString()} 토큰`,
      r.dropped_refs ? ` · 확인되지 않은 근거 ${r.dropped_refs}개는 제외했습니다` : ""),
  );
}

$("report-save").addEventListener("click", () => guarded(async () => {
  const r = await api("POST", "/api/reports", { kind: reportKind(), day: state.reportDay, body: $("report-body").value });
  reportSaved = r.saved;
  setReportState();
  toast(r.saved ? "보고서를 저장했습니다." : "빈 보고서라 저장된 내용을 지웠습니다.");
}));

$("report-copy").addEventListener("click", () => guarded(async () => {
  await navigator.clipboard.writeText($("report-body").value);
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

// ------------------------------------------------------------------ launch (autostart, shortcut)
function renderLaunch(l) {
  $("launch-autostart").checked = l.autostart.enabled;
  $("launch-autostart").disabled = l.demo || !l.autostart.supported;
  $("launch-shortcut").hidden = !l.shortcut.supported;
  $("launch-shortcut").disabled = l.demo;
  $("launch-shortcut").textContent = `${l.shortcut.where} 바로가기 ${l.shortcut.exists ? "다시 만들기" : "만들기"}`;
  $("launch-status").textContent = l.demo ? "데모 모드에서는 바꿀 수 없습니다."
    : l.shortcut.exists ? `${l.shortcut.where}에 바로가기가 있습니다.` : "";
  $("quit-row").hidden = !l.can_quit;
  $("launch-url").hidden = !l.url;
  $("launch-url").textContent = l.url ? `이 앱의 주소: ${l.url} (다른 프로그램이 포트를 쓰고 있으면 번호가 바뀔 수 있으니, 바탕화면 바로가기로 여는 것이 가장 확실합니다)` : "";
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

$("launch-shortcut").addEventListener("click", () => guarded(async () => {
  const l = await api("POST", "/api/launch/shortcut", {});
  renderLaunch(l);
  toast(`${l.shortcut.where}에 바로가기를 만들었습니다.`);
}));

$("app-quit").addEventListener("click", () => guarded(async () => {
  if (!confirm("작업 기록을 종료할까요? 다시 켤 때까지 창 기록과 작업 폴더 기록이 멈춥니다.")) return;
  await api("POST", "/api/quit", {});
  document.body.innerHTML = '<main style="padding:3rem;text-align:center"><h2>작업 기록을 종료했습니다</h2>'
    + '<p class="muted">다시 켜려면 "작업 기록" 바로가기나 작업 기록.cmd를 실행하세요. 자동 실행을 켜 두었다면 PC를 다시 켤 때 자동으로 시작됩니다.</p></main>';
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

// ------------------------------------------------------------------ resume work
async function loadResume() {
  const list = await guarded(() => api("GET", "/api/resume"));
  if (!list) return;
  if (!list.length) {
    $("resume-list").replaceChildren(h("p", { class: "muted" }, "프로젝트가 없습니다. '프로젝트' 탭에서 만들고 규칙을 추가하세요."));
    $("resume-detail").replaceChildren();
    return;
  }
  if (!list.some((p) => p.id === state.resumeProject)) state.resumeProject = list[0].id;
  $("resume-list").replaceChildren(...list.map((p) => h("button", {
    class: `resume-item${p.id === state.resumeProject ? " active" : ""}`,
    onclick: () => { state.resumeProject = p.id; loadResume(); },
  },
  h("div", {}, h("span", { class: "dot", style: { background: p.color } }), h("b", {}, p.name)),
  h("div", { class: "meta" }, p.last_session ? `마지막 작업 ${ago(p.last_session.end)}` : "기록 없음",
    p.week_active_s ? ` · 최근 7일 ${dur(p.week_active_s)}` : ""),
  p.next_action ? h("div", { class: "meta" }, `다음: ${p.next_action}`) : null)));
  await loadResumeDetail(state.resumeProject);
}

async function loadResumeDetail(id) {
  const r = await guarded(() => api("GET", `/api/resume/${id}`));
  if (!r || id !== state.resumeProject) return;
  const next = h("textarea", { rows: 3, placeholder: "돌아왔을 때 가장 먼저 할 일을 직접 적어 두세요." });
  next.value = r.project.next_action;
  const goto = (day) => { showTab("timeline"); setDay(day); };
  const last = r.last_session;
  $("resume-detail").replaceChildren(
    h("div", { class: "card" },
      h("h2", {}, h("span", { class: "dot", style: { background: r.project.color } }), r.project.name),
      h("h3", {}, "다음 할 일 ", badge("user", "직접 작성")),
      next,
      h("div", { class: "row" }, h("button", { class: "small primary", onclick: () => guarded(async () => {
        await api("PATCH", `/api/projects/${id}`, { next_action: next.value });
        toast("다음 할 일을 저장했습니다.");
        await loadProjects();
        loadResume();
      }) }, "저장"))),
    h("div", { class: "card" },
      h("h3", {}, "마지막 작업 ", badge("inferred", "자동 추정")),
      last ? h("div", {},
        h("p", {}, h("button", { class: "link", onclick: () => goto(last.day) }, `${last.day} ${hhmm(last.start)}–${hhmm(last.end)}`),
          ` · ${dur(last.active_s)} · `, h("b", {}, last.title)),
        last.note ? h("div", { class: "session-note" }, last.note) : null,
        r.last_activity ? h("p", { class: "muted" }, `마지막으로 본 창: ${appName(r.last_activity.app)} — ${r.last_activity.title || "(제목 없음)"} (${hhmm(r.last_activity.end)})`) : null)
        : h("p", { class: "muted" }, "이 프로젝트로 분류된 세션이 아직 없습니다.")),
    h("div", { class: "card" },
      h("h3", {}, "최근 세션"),
      r.recent_sessions.length ? h("ul", { class: "plain compact" }, r.recent_sessions.map((s) => h("li", {},
        h("span", {}, h("button", { class: "link", onclick: () => goto(s.day) }, `${s.day} ${hhmm(s.start)}`), ` ${s.title}`),
        h("span", { class: "meta" }, dur(s.active_s))))) : h("p", { class: "muted" }, "없음")),
    h("div", { class: "card" },
      h("h3", {}, "이전 메모 ", badge("user", "직접 작성")),
      r.notes.length || r.session_notes.length ? h("ul", { class: "notes" },
        r.notes.map((n) => h("li", {}, h("div", { class: "meta" }, n.day), n.body)),
        r.session_notes.map((n) => h("li", {}, h("div", { class: "meta" }, `${n.day} ${hhmm(n.start)} 세션`), n.note)))
        : h("p", { class: "muted" }, "없음")),
  );
}

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

function gotoReview(day) {
  state.reviewDay = day;
  showTab("review");
}

// ------------------------------------------------------------------ ask the AI
const TOOL_LABELS = { search_history: "기록 검색", get_day: "하루 보기", list_projects: "프로젝트 목록",
  get_project: "프로젝트 보기", get_change: "변경 내용 보기" };

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
        p.next_action ? h("span", { class: "meta" }, ` · 다음 할 일: ${p.next_action}`) : null),
      h("span", { class: "meta" }, p.last_activity_ts
        ? h("button", { class: "link", onclick: () => gotoDay(isoDay(new Date(p.last_activity_ts * 1000))) }, `마지막 작업 ${ago(p.last_activity_ts)}`)
        : "기록 없음"))))));
  }
  if (r.sessions.length) {
    groups.push(section("작업 세션", r.sessions.length, h("ul", { class: "plain" }, r.sessions.map((s) => h("li", {},
      h("span", {}, h("button", { class: "link", onclick: () => gotoDay(s.day) }, `${s.day} ${hhmm(s.start)}–${hhmm(s.end)}`),
        " ", s.title, s.note ? h("span", { class: "meta" }, ` · 메모: ${s.note}`) : null),
      h("span", { class: "meta" }, `${projectName(s.project_id)} · ${dur(s.active_s)}`))))));
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
  if (r.reviews.length) {
    groups.push(section("리뷰", r.reviews.length, h("ul", { class: "plain" }, r.reviews.map((v) => h("li", {},
      h("span", {}, h("button", { class: "link", onclick: () => gotoReview(v.day) }, v.day), " ", v.excerpt))))));
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

// ------------------------------------------------------------------ projects
$("project-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("POST", "/api/projects", { name: $("project-name").value, color: $("project-color").value });
    $("project-name").value = "";
    await refreshProjects();
  });
});

async function refreshProjects() {
  await loadProjects();
  renderProjects();
}

function renderProjects() {
  if (!state.projects.length) {
    $("project-list").replaceChildren(h("p", { class: "muted" }, "아직 프로젝트가 없습니다. 위에서 추가하세요."));
    return;
  }
  $("project-list").replaceChildren(...state.projects.map((p) => {
    const field = h("select", {}, h("option", { value: "title" }, "창 제목"), h("option", { value: "app" }, "앱 이름"));
    const pattern = h("input", { placeholder: "키워드 (예: gov24)" });
    return h("div", { class: `card project-card${p.archived ? " archived" : ""}` },
      h("h2", {}, h("span", { class: "dot", style: { background: p.color } }), p.name, p.archived ? " (보관됨)" : ""),
      h("div", { class: "rules" },
        p.rules.length ? p.rules.map((r) => h("span", { class: "rule" },
          `${r.field === "app" ? "앱" : "제목"}: ${r.pattern}`,
          h("button", { title: "규칙 삭제", onclick: () => guarded(async () => { await api("DELETE", `/api/rules/${r.id}`); refreshProjects(); }) }, "×")))
          : h("span", { class: "muted" }, "규칙 없음")),
      h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); guarded(async () => {
        await api("POST", `/api/projects/${p.id}/rules`, { field: field.value, pattern: pattern.value });
        refreshProjects();
      }); } }, field, pattern, h("button", { type: "submit" }, "규칙 추가")),
      p.next_action ? h("p", { class: "meta" }, `다음 할 일: ${p.next_action}`) : null,
      h("div", { class: "row" },
        h("button", { class: "small", onclick: () => { state.resumeProject = p.id; showTab("resume"); } }, "이어서 하기"),
        h("button", { class: "small", onclick: () => { const name = prompt("프로젝트 이름", p.name); if (name) guarded(async () => { await api("PATCH", `/api/projects/${p.id}`, { name }); refreshProjects(); }); } }, "이름 변경"),
        h("button", { class: "small", onclick: () => guarded(async () => { await api("PATCH", `/api/projects/${p.id}`, { archived: !p.archived }); refreshProjects(); }) }, p.archived ? "보관 해제" : "보관"),
        h("button", { class: "small danger", onclick: () => { if (confirm(`'${p.name}' 프로젝트를 삭제할까요? 활동 기록은 남고 미분류가 됩니다.`)) guarded(async () => { await api("DELETE", `/api/projects/${p.id}`); refreshProjects(); }); } }, "삭제")));
  }));
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

// ------------------------------------------------------------------ privacy
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

const deletedSummary = (r) => `삭제 완료: 창 기록 ${r.window_events}건, 세션 ${r.sessions}개, 메모 ${r.notes}건, 리뷰 ${r.reviews}건`;

$("delete-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const from = $("delete-from").value, to = $("delete-to").value;
  if (!from || !to) return toast("시작일과 종료일을 선택하세요.");
  if (!confirm(`${from} ~ ${to} 기간의 활동·세션·메모·리뷰를 삭제합니다. 되돌릴 수 없습니다.`)) return;
  guarded(async () => toast(deletedSummary(await api("POST", "/api/delete-data", { from, to }))));
});

$("delete-all").addEventListener("click", () => {
  if (!confirm("가져온 모든 활동 기록과 세션·메모·리뷰를 삭제합니다. 되돌릴 수 없습니다. 계속할까요?")) return;
  guarded(async () => toast(deletedSummary(await api("POST", "/api/delete-data", { all: true }))));
});

// ------------------------------------------------------------------ dashboard
// Where the time went: by kind of activity (inferred from app and window title) and by project.
const dash = { span: "day", day: isoDay(new Date()) };
const WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"];
const weekdayOf = (day) => WEEKDAYS[(new Date(day + "T00:00:00").getDay() + 6) % 7];
const pct = (part, whole) => (whole > 0 ? Math.round((part / whole) * 100) : 0);

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c != null) el.append(typeof c === "string" ? document.createTextNode(c) : c);
  return el;
}

async function loadDashboard() {
  $("dash-date").value = dash.day;
  $("dash-day").classList.toggle("on", dash.span === "day");
  $("dash-week").classList.toggle("on", dash.span === "week");
  const r = await guarded(() => api("GET", `/api/dashboard?date=${dash.day}&span=${dash.span}`));
  if (r && r.day === dash.day && r.span === dash.span) renderDashboard(r);
}

function renderDashboard(r) {
  const isDay = r.span === "day";
  $("dash-period").textContent = isDay ? `${r.day} (${weekdayOf(r.day)})` : `${r.start} ~ ${r.end}`;
  renderKpis(r);
  renderWeekChart(r);
  renderDonut(r);
  $("dash-strip-card").hidden = !isDay;
  if (isDay) renderStrip(r);
  renderDashProjects(r);
}

function renderKpis(r) {
  const t = r.totals, p = r.previous, diff = t.active_s - p.active_s;
  // The biggest project; without projects, the biggest kind of activity.
  const top = r.projects.find((x) => x.seconds > 0 && x.project_id != null)
    || r.categories.filter((c) => c.key !== "offwork").map((c) => ({ name: c.label, seconds: r.by_kind[c.key] }))
      .sort((a, b) => b.seconds - a.seconds).find((x) => x.seconds > 0);
  const kpi =(label, value, sub, cls = "") => h("div", { class: "kpi" },
    h("div", { class: "label" }, label), h("div", { class: `value ${cls}`, title: value }, value), h("div", { class: "sub" }, ...[].concat(sub)));
  const compare = r.span === "day" ? " 전날보다" : " 지난주 같은 기간보다";
  $("dash-kpis").replaceChildren(
    kpi("활동 시간", dur(t.active_s),
      p.active_s ? [h("span", { class: diff >= 0 ? "up" : "down" }, `${diff >= 0 ? "+" : "−"}${dur(Math.abs(diff))}`), compare] : "비교할 기록 없음"),
    kpi("업무 비율", `${pct(t.work_s, t.active_s)}%`,
      `업무 ${dur(t.work_s)} · 서핑 ${pct(t.surf_s, t.active_s)}% · 업무 외 ${pct(t.offwork_s, t.active_s)}%`),
    kpi("고친 파일", `${r.files.toLocaleString()}개`, `+${r.lines_added.toLocaleString()} / −${r.lines_removed.toLocaleString()}줄`),
    kpi("가장 많이 한 일", top ? top.name : "없음", top ? dur(top.seconds) : "프로젝트별 시간이 없습니다", "name"));
}

// Stacked bars, one per day of the week; clicking a day opens that day.
function renderWeekChart(r) {
  const W = 640, H = 220, left = 34, bottom = 34, top = 8;
  const totals = r.week.map((d) => r.categories.reduce((s, c) => s + d.by_kind[c.key], 0));
  const maxH = Math.max(1, Math.ceil(Math.max(...totals) / 3600));
  const step = maxH > 8 ? 2 : 1, ceil = Math.ceil(maxH / step) * step;
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
    if (totals[i]) chart.append(svg("text", { x: x + barW / 2, y: y(acc) - 5, "text-anchor": "middle" }, `${Math.round(totals[i] / 360) / 10}h`));
  });
  $("dash-week-title").textContent = "이번 주 분류별 시간";
  $("dash-week-chart").replaceChildren(chart, legend(r.categories.filter((c) => r.week.some((d) => d.by_kind[c.key] > 0))));
}

const legend = (cats) => h("div", { class: "legend" },
  cats.map((c) => h("span", {}, h("i", { style: { background: c.color } }), c.label)));

function renderDonut(r) {
  const total = r.categories.reduce((s, c) => s + r.by_kind[c.key], 0);
  $("dash-donut-title").textContent = r.span === "day" ? "오늘 분류" : "이번 주 분류";
  if (r.span === "day" && r.day !== isoDay(new Date())) $("dash-donut-title").textContent = `${r.day.slice(5)} 분류`;
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
      `${Math.round(total / 360) / 10}시간`),
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
  $("dash-strip").replaceChildren(r.blocks.length ? h("div", {}, strip, axis) : h("p", { class: "muted" }, "이 날짜에 기록된 활동이 없습니다."));
}

function renderDashProjects(r) {
  $("dash-projects-title").textContent = r.span === "day" ? "한 일 (프로젝트별)" : "이번 주 한 일 (프로젝트별)";
  const rows = r.projects.filter((p) => p.seconds > 0 || p.files > 0);
  if (!rows.length) return $("dash-projects").replaceChildren(h("p", { class: "muted" }, "이 기간에 기록된 작업이 없습니다."));
  const max = Math.max(...rows.map((p) => p.seconds), 1);
  $("dash-projects").replaceChildren(...rows.map((p) => {
    const stat = [p.seconds ? dur(p.seconds) : null,
      p.files ? `파일 ${p.files}개 (+${p.lines_added} −${p.lines_removed})` : null].filter(Boolean).join(" · ");
    const row = h("div", { class: "proj-row", title: r.span === "day" ? "타임라인에서 보기" : "이어서 하기에서 보기" },
      h("div", { class: "proj-name" }, h("i", { class: "swatch", style: { background: p.color } }), p.name),
      h("div", { class: "proj-meter" }, h("div", { style: { width: `${(p.seconds / max) * 100}%`, background: p.color } })),
      h("div", { class: "proj-titles" }, p.titles.map((s) => (s.length > 42 ? s.slice(0, 41) + "…" : s)).join("  ·  ")),
      h("div", { class: "proj-stat" }, stat));
    row.addEventListener("click", () => {
      if (r.span === "week" && p.project_id != null) { state.resumeProject = p.project_id; return showTab("resume"); }
      $("filter-project").value = p.project_id == null ? "none" : String(p.project_id);
      gotoDay(r.span === "day" ? r.day : r.end);
    });
    return row;
  }));
}

$("dash-day").addEventListener("click", () => { dash.span = "day"; loadDashboard(); });
$("dash-week").addEventListener("click", () => { dash.span = "week"; loadDashboard(); });
$("dash-prev").addEventListener("click", () => { dash.day = shiftDay(dash.day, dash.span === "week" ? -7 : -1); loadDashboard(); });
$("dash-next").addEventListener("click", () => { dash.day = shiftDay(dash.day, dash.span === "week" ? 7 : 1); loadDashboard(); });
$("dash-today").addEventListener("click", () => { dash.day = isoDay(new Date()); loadDashboard(); });
$("dash-date").addEventListener("change", (e) => { if (e.target.value) { dash.day = e.target.value; loadDashboard(); } });

// ------------------------------------------------------------------ boot
(async function boot() {
  $("day").value = state.day;
  $("review-day").value = state.reviewDay;
  $("report-day").value = state.reportDay;
  await guarded(loadStatus);
  await guarded(loadProjects);
  await loadDashboard();
  setInterval(() => guarded(loadStatus), 30000);
  // Refresh today's timeline periodically while it is visible and nothing is being edited.
  setInterval(() => {
    const editing = (document.activeElement && ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName))
      || [...document.querySelectorAll(".session-note-edit")].some((el) => !el.hidden);
    if (!$("tab-timeline").hidden && state.day === isoDay(new Date()) && !editing) loadTimeline();
    if (!$("tab-dashboard").hidden && dash.day === isoDay(new Date()) && !editing) loadDashboard();
  }, 60000);
})();
