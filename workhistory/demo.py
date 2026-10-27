"""Demo data: a fictional two-week developer scenario.

Only ever written to the separate demo database, which the UI always marks as
demo data. The scenario is realistic on purpose so that sessions, reviews and
AI reports can be judged on it (and used for the AI eval set):

- last week: token refresh bug GOV-1418 (analysis -> fix -> MR), a K8s PoC,
  and the start of the slow 민원 조회 issue GOV-1423
- this week: index + count-query MR for GOV-1423, K8s ingress, DBA approval,
  research on cursor-based paging (GOV-1430)

Recording mode is "업무 앱만 기록", so YouTube/shopping become 업무 외 time,
exactly as the real import would store them.
"""

import random
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import demo_files, sessionstore, snapshots, store
from .classify import OFFWORK_APP, TrackRule, is_tracked
from .db import Database
from .store import mark_days_dirty

GOV, K8S = "Gov24 민원 API", "K8s 전환 PoC"
PROJECTS = {
    GOV: ("#4f7cff", [("title", "gov24"), ("title", "GOV-1"), ("title", "minwon")],
          "커서 기반 페이징 전환 검토 (GOV-1430)"),
    K8S: ("#20a37a", [("title", "kubectl"), ("title", "helm"), ("title", "kubernetes"),
                      ("title", "ingress")], "Helm values를 dev/stage 환경별로 분리"),
}
TRACK_RULES = [("idea64", ""), ("code.exe", ""), ("windowsterminal", ""), ("postman", ""), ("dbeaver", ""), ("teams", ""),
               ("slack", ""), ("chrome", "jira"), ("chrome", "gitlab"), ("chrome", "stack overflow"),
               ("chrome", "spring"), ("chrome", "kubernetes"), ("chrome", "helm"), ("chrome", "querydsl"),
               ("chrome", "pagination")]

IDE = "idea64.exe"
CHROME = "chrome.exe"
TERM = "WindowsTerminal.exe"
W = {
    "jira1418": (CHROME, "[GOV-1418] 리프레시 토큰 재발급 시 기존 토큰 미무효화 - Jira - Chrome"),
    "jira1423": (CHROME, "[GOV-1423] 민원 조회 API 응답 지연 - Jira - Chrome"),
    "jira1430": (CHROME, "[GOV-1430] 민원 목록 커서 기반 페이징 검토 - Jira - Chrome"),
    "filter": (IDE, "gov24-minwon-api – TokenRefreshFilter.java"),
    "jwt": (IDE, "gov24-minwon-api – JwtTokenProvider.java"),
    "authtest": (IDE, "gov24-minwon-api – TokenRefreshFilterTest.java"),
    "repo": (IDE, "gov24-minwon-api – MinwonQueryRepository.java"),
    "repotest": (IDE, "gov24-minwon-api – MinwonQueryRepositoryTest.java"),
    "ctrl": (IDE, "gov24-minwon-api – MinwonController.java"),
    "so_refresh": (CHROME, "Spring Security refresh token rotation - Stack Overflow - Chrome"),
    "so_count": (CHROME, "QueryDSL fetchCount deprecated count query - Stack Overflow - Chrome"),
    "cursor": (CHROME, "Cursor based pagination with Spring Data JPA - Chrome"),
    "postman_refresh": ("Postman.exe", "POST /api/v1/auth/refresh - gov24 - Postman"),
    "postman_minwon": ("Postman.exe", "GET /api/v1/minwon?page=0&size=20 - gov24 - Postman"),
    "gradle": (TERM, "gradlew test --tests *TokenRefresh* - gov24-minwon-api"),
    "dbeaver": ("dbeaver.exe", "MINWON_REQ 실행계획 - gov24_dev - DBeaver"),
    "mr312": (CHROME, "!312 토큰 재발급 시 기존 리프레시 토큰 무효화 · gov24-minwon-api · GitLab - Chrome"),
    "mr318": (CHROME, "!318 민원 조회 복합 인덱스 및 count 쿼리 분리 · gov24-minwon-api · GitLab - Chrome"),
    "k8s_docs": (CHROME, "Deployments | Kubernetes - Chrome"),
    "ingress_docs": (CHROME, "Ingress | Kubernetes - Chrome"),
    "kubectl": (TERM, "kubectl get pods -n poc-dev"),
    "helm": (TERM, "helm upgrade --install poc-api ./poc-chart -n poc-dev"),
    "values": ("Code.exe", "values.yaml - poc-chart (helm) - Visual Studio Code"),
    "helm_docs": (CHROME, "Helm | Values Files - Chrome"),
    "scrum": ("ms-teams.exe", "일일 스크럼 | Microsoft Teams"),
    "weekly": ("ms-teams.exe", "주간 회의 | Microsoft Teams"),
    "dba": ("ms-teams.exe", "DBA 인덱스 검토 | Microsoft Teams"),
    "slack": ("slack.exe", "#gov24-dev | Slack"),
    "youtube": (CHROME, "개발자 브이로그 - YouTube - Chrome"),
    "shop": (CHROME, "키보드 할인 - 쇼핑 - Chrome"),
}

# Day offset from this week's Monday -> list of (start, end, [(window, weight), ...]) or (start, end, "afk").
SCRIPT = {
    -7: [("09:00", "09:15", [("scrum", 1)]),
         ("09:15", "10:40", [("jira1418", 1), ("filter", 4), ("jwt", 2), ("so_refresh", 2)]),
         ("10:40", "12:00", [("postman_refresh", 2), ("filter", 3), ("slack", 1)]),
         ("12:00", "13:00", "afk"),
         ("13:00", "15:00", [("jwt", 3), ("filter", 3), ("gradle", 2), ("so_refresh", 1)]),
         ("15:00", "15:20", [("youtube", 1)]),
         ("15:20", "17:40", [("authtest", 4), ("gradle", 2), ("filter", 2)])],
    -6: [("09:00", "09:15", [("scrum", 1)]),
         ("09:15", "11:50", [("filter", 4), ("authtest", 3), ("gradle", 2), ("slack", 1)]),
         ("11:50", "13:00", "afk"),
         ("13:00", "14:30", [("mr312", 3), ("filter", 1), ("postman_refresh", 2)]),
         ("14:30", "14:45", "afk"),
         ("14:45", "17:30", [("jira1418", 1), ("filter", 2), ("authtest", 2), ("slack", 1)])],
    -5: [("09:00", "09:15", [("scrum", 1)]),
         ("09:15", "12:00", [("k8s_docs", 3), ("kubectl", 3), ("helm", 2), ("values", 2)]),
         ("12:00", "13:00", "afk"),
         ("13:00", "16:30", [("kubectl", 4), ("helm", 3), ("values", 2), ("ingress_docs", 1)]),
         ("16:30", "16:45", [("shop", 1)]),
         ("16:45", "17:30", [("mr312", 2), ("slack", 1)])],
    -4: [("09:00", "09:15", [("scrum", 1)]),
         ("09:15", "11:00", [("jira1418", 1), ("postman_refresh", 2), ("filter", 1), ("slack", 1)]),
         ("11:00", "12:00", [("jira1423", 2), ("postman_minwon", 3)]),
         ("12:00", "13:00", "afk"),
         ("13:00", "17:30", [("dbeaver", 4), ("repo", 3), ("postman_minwon", 1), ("jira1423", 1)])],
    -3: [("09:00", "09:15", [("scrum", 1)]),
         ("09:15", "12:00", [("dbeaver", 3), ("repo", 4), ("so_count", 2)]),
         ("12:00", "13:00", "afk"),
         ("13:00", "15:00", [("repo", 3), ("repotest", 3), ("postman_minwon", 2)]),
         ("15:00", "16:00", [("weekly", 1)]),
         ("16:00", "16:10", [("youtube", 1)]),
         ("16:10", "17:30", [("repotest", 2), ("dbeaver", 2), ("slack", 1)])],
    0: [("09:00", "09:15", [("scrum", 1)]),
        ("09:15", "12:00", [("repo", 4), ("so_count", 1), ("repotest", 3), ("postman_minwon", 1)]),
        ("12:00", "13:00", "afk"),
        ("13:00", "15:30", [("repotest", 2), ("postman_minwon", 2), ("dbeaver", 2)]),
        ("15:30", "17:30", [("mr318", 3), ("repo", 1), ("slack", 1)])],
    1: [("09:00", "09:15", [("scrum", 1)]),
        ("09:15", "12:00", [("ingress_docs", 2), ("kubectl", 3), ("helm", 2), ("values", 3)]),
        ("12:00", "13:00", "afk"),
        ("13:00", "16:00", [("helm", 3), ("kubectl", 3), ("helm_docs", 1), ("values", 2)]),
        ("16:00", "16:20", [("youtube", 1)]),
        ("16:20", "17:30", [("mr318", 2), ("slack", 1)])],
    2: [("09:00", "09:15", [("scrum", 1)]),
        ("09:15", "11:00", [("mr318", 3), ("repo", 3), ("repotest", 2)]),
        ("11:00", "11:40", [("dba", 1)]),
        ("11:40", "13:00", "afk"),
        ("13:00", "17:30", [("repo", 2), ("repotest", 2), ("mr318", 2), ("jira1423", 1), ("slack", 1)])],
    3: [("09:00", "09:15", [("scrum", 1)]),
        ("09:15", "12:00", [("jira1430", 1), ("cursor", 3), ("repo", 3), ("ctrl", 1)]),
        ("12:00", "13:00", "afk"),
        ("13:00", "17:30", [("repo", 3), ("repotest", 2), ("cursor", 1), ("postman_minwon", 1)])],
    4: [("09:00", "12:00", [("repo", 2), ("repotest", 2), ("cursor", 1)])],
}

# (day offset, "HH:MM", project or None, text)
NOTES = [
    (-7, "17:35", GOV, "GOV-1418 재현: 리프레시 토큰 재발급 후에도 이전 토큰으로 재발급이 됨. 토큰 회전 시 기존 토큰 무효화 로직 누락이 원인으로 보임"),
    (-6, "17:25", GOV, "TokenRefreshFilter 수정 후 MR !312 올림. QA 서버 배포 후 재확인 필요"),
    (-5, "17:20", K8S, "dev 클러스터에 민원 API 배포 성공. ingress 설정은 아직 안 됨"),
    (-4, "17:25", GOV, "GOV-1418 QA 확인 완료로 종료. GOV-1423: 민원 조회 API 응답 3.2초, MINWON_REQ 테이블 인덱스를 안 탐"),
    (-3, "17:25", GOV, "복합 인덱스(접수일자, 처리상태) 추가 시 0.4초까지 단축. DBA 검토 요청 필요"),
    (0, "17:25", GOV, "MR !318 올림 (복합 인덱스 + count 쿼리 분리)"),
    (1, "17:25", K8S, "ingress 설정 완료. values.yaml이 환경별로 섞여 있어 분리 필요"),
    (2, "17:25", GOV, "DBA 인덱스 승인. 운영 반영은 다음 주 화요일 배포 창에서 진행"),
]

# (day offset, "HH:MM" inside the session, title the user typed)
SESSION_TITLES = [
    (-7, "09:30", "GOV-1418 토큰 재발급 버그 원인 분석"),
    (-3, "09:30", "민원 조회 실행계획 분석"),
]


def _ts(day: date, hhmm: str) -> float:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m).timestamp()


def _chunks(rng: random.Random, start: float, end: float, mix: list[tuple[str, int]]):
    """Split [start, end) into 2-9 minute focus periods, switching windows like a person does."""
    names, weights = zip(*mix)
    t, previous = start, None
    while t < end:
        choices = [(n, w) for n, w in zip(names, weights) if n != previous] or list(zip(names, weights))
        name = rng.choices([n for n, _ in choices], [w for _, w in choices])[0]
        length = min(rng.uniform(2, 9) * 60, end - t)
        yield name, t, t + length
        t, previous = t + length, name


def seed(db: Database, repos_dir: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """Fill an empty demo database. Returns False if it already has data.

    With `repos_dir`, the fictional file history (demo_files.EDITS) is written
    as snapshot commits, exactly like the real folder scanner would.
    """
    rng = random.Random(1418)
    now = time.time() if now is None else now  # a fixed clock makes the scenario reproducible (evals)
    today = date.fromtimestamp(now)
    monday = today - timedelta(days=today.weekday())
    track = [TrackRule(a, t) for a, t in TRACK_RULES]
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM sources").fetchone():
            return False
        project_ids = {}
        for name, (color, rules, next_action) in PROJECTS.items():
            pid = conn.execute("INSERT INTO projects (name, color, next_action, created_at) VALUES (?, ?, ?, ?)",
                               (name, color, next_action, now)).lastrowid
            project_ids[name] = pid
            for field, pattern in rules:
                conn.execute("INSERT INTO project_rules (project_id, field, pattern, created_at) "
                             "VALUES (?, ?, ?, ?)", (pid, field, pattern, now))
        for app, title in TRACK_RULES:
            conn.execute("INSERT INTO track_rules (app_pattern, title_pattern, created_at) VALUES (?, ?, ?)",
                         (app, title, now))
        conn.execute("INSERT INTO meta (key, value) VALUES ('track_mode', 'selected')")
        win = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                           "VALUES ('demo-window', 'demo', 'window', 'demo')").lastrowid
        afk = conn.execute("INSERT INTO sources (bucket_id, bucket_created, kind, hostname) "
                           "VALUES ('demo-afk', 'demo', 'afk', 'demo')").lastrowid

        aw_id = 0
        days = []
        for offset in sorted(SCRIPT):
            day = monday + timedelta(days=offset)
            if day > today:
                continue
            days.append((offset, day))
            for start_s, end_s, mix in SCRIPT[offset]:
                start, end = _ts(day, start_s), min(_ts(day, end_s), now)
                if start >= end:
                    continue
                aw_id += 1
                status = "afk" if mix == "afk" else "not-afk"
                conn.execute("INSERT INTO afk_events (source_id, aw_id, start_ts, end_ts, status) "
                             "VALUES (?, ?, ?, ?, ?)", (afk, aw_id, start, end, status))
                if mix == "afk":
                    continue
                for name, s, e in _chunks(rng, start, end, mix):
                    app, title = W[name]
                    if not is_tracked(track, app, title):
                        app, title = OFFWORK_APP, ""  # what the real import stores
                    aw_id += 1
                    conn.execute("INSERT INTO window_events (source_id, aw_id, start_ts, end_ts, app, title) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (win, aw_id, s, e, app, title))
        if days:
            mark_days_dirty(conn, _ts(days[0][1], "00:00"), now)

        offsets = dict(days)
        for offset, hhmm, project, text in NOTES:
            if offset in offsets and _ts(offsets[offset], hhmm) <= now:
                ts = _ts(offsets[offset], hhmm)
                conn.execute("INSERT INTO notes (day, project_id, body, created_at, updated_at) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (offsets[offset].isoformat(), project_ids.get(project), text, ts, ts))

        for offset, day in days:
            activities, _ = store.day_activities(conn, day, now)
            sessionstore.refresh_day(conn, day, now, activities)
        if repos_dir is not None:
            _seed_files(conn, Path(repos_dir), project_ids, monday, today, now)

        for offset, hhmm, title in SESSION_TITLES:
            if offset not in offsets:
                continue
            at = _ts(offsets[offset], hhmm)
            row = conn.execute("SELECT id FROM sessions WHERE start_ts <= ? AND end_ts > ?", (at, at)).fetchone()
            if row:
                sessionstore.update_session(conn, row["id"], {"title": title})
    return True


def _seed_files(conn, repos_dir: Path, project_ids: dict, monday: date, today: date, now: float) -> None:
    shutil.rmtree(repos_dir, ignore_errors=True)  # a fresh demo DB gets fresh snapshots
    folder_ids, trees, repos = {}, {}, {}
    try:
        for key, (label, name, project) in demo_files.FOLDERS.items():
            path = str(Path.home() / "demo" / name)  # fictional; shown in the OS's own path style
            folder_ids[key] = conn.execute(
                "INSERT INTO watched_folders (path, label, project_id, created_at, last_scan_at) "
                "VALUES (?, ?, ?, ?, ?)", (path, label, project_ids.get(project), now, now)).lastrowid
            repos[key] = snapshots._open_repo(snapshots.repo_path(repos_dir, folder_ids[key]))
            trees[key] = {}
        for offset, hhmm, key, files in demo_files.EDITS:
            day = monday + timedelta(days=offset)
            ts = _ts(day, hhmm)
            if day > today or ts > now:
                continue
            repo, tree = repos[key], trees[key]
            for path, content in files.items():
                if content is None:
                    tree.pop(path, None)
                else:
                    data = content if isinstance(content, bytes) else content.encode("utf-8")
                    tree[path] = snapshots.add_blob(repo, data)
            snapshots.commit_snapshot(conn, repo, folder_ids[key], dict(tree), ts)
        # What a real scan would leave behind, so the folder list shows its file count.
        for key, tree in trees.items():
            conn.executemany(
                "INSERT INTO file_state (folder_id, path, size, mtime_ns, blob_id) VALUES (?, ?, ?, 0, ?)",
                [(folder_ids[key], path, len(repos[key][blob].data), blob.decode()) for path, blob in tree.items()])
    finally:
        for repo in repos.values():
            repo.close()
