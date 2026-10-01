"""Fictional file versions for the demo scenario (results history). See demo.py."""

import io
import zipfile

PKG = "src/main/java/kr/demo/minwon"
TEST = "src/test/java/kr/demo/minwon"


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


FILTER_V0 = """package kr.demo.minwon.auth;

public class TokenRefreshFilter extends OncePerRequestFilter {
    private final JwtTokenProvider tokenProvider;
    private final RefreshTokenRepository refreshTokens;

    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain)
            throws ServletException, IOException {
        String refresh = req.getHeader("X-Refresh-Token");
        if (refresh != null && tokenProvider.isAccessExpired(req)) {
            String access = tokenProvider.reissueAccess(refresh);
            res.setHeader("Authorization", "Bearer " + access);
        }
        chain.doFilter(req, res);
    }
}
"""

FILTER_V1 = FILTER_V0.replace(
    """            String access = tokenProvider.reissueAccess(refresh);
            res.setHeader("Authorization", "Bearer " + access);""",
    """            TokenPair pair = tokenProvider.rotate(refresh);
            res.setHeader("Authorization", "Bearer " + pair.access());
            res.setHeader("X-Refresh-Token", pair.refresh());""")

FILTER_V2 = FILTER_V1.replace(
    """            TokenPair pair = tokenProvider.rotate(refresh);""",
    """            RefreshToken stored = refreshTokens.findByToken(refresh)
                    .orElseThrow(() -> new InvalidTokenException("재사용된 리프레시 토큰"));
            refreshTokens.delete(stored); // 기존 리프레시 토큰 무효화 (GOV-1418)
            TokenPair pair = tokenProvider.rotate(refresh);
            refreshTokens.save(new RefreshToken(pair.refresh(), stored.userId()));""")

JWT_V0 = """package kr.demo.minwon.auth;

public class JwtTokenProvider {
    private final JwtProperties props;

    public String reissueAccess(String refreshToken) {
        Claims claims = parse(refreshToken);
        return createAccess(claims.getSubject());
    }

    public boolean isAccessExpired(HttpServletRequest req) {
        return parseAccess(req).getExpiration().before(new Date());
    }
}
"""

JWT_V1 = JWT_V0.replace(
    """    public boolean isAccessExpired""",
    """    public TokenPair rotate(String refreshToken) {
        Claims claims = parse(refreshToken);
        String subject = claims.getSubject();
        return new TokenPair(createAccess(subject), createRefresh(subject));
    }

    public boolean isAccessExpired""")

FILTER_TEST_V1 = """package kr.demo.minwon.auth;

class TokenRefreshFilterTest {

    @Test
    void 리프레시_토큰_재발급시_새_토큰을_돌려준다() {
        TokenPair first = login("tester");
        TokenPair second = refresh(first.refresh());
        assertThat(second.refresh()).isNotEqualTo(first.refresh());
    }

    @Test
    void 이전_리프레시_토큰으로_다시_재발급할_수_있다_버그재현() {
        TokenPair first = login("tester");
        refresh(first.refresh());
        assertThat(refresh(first.refresh())).isNotNull(); // GOV-1418: 재사용이 막히지 않음
    }
}
"""

FILTER_TEST_V2 = FILTER_TEST_V1.replace(
    """    void 이전_리프레시_토큰으로_다시_재발급할_수_있다_버그재현() {
        TokenPair first = login("tester");
        refresh(first.refresh());
        assertThat(refresh(first.refresh())).isNotNull(); // GOV-1418: 재사용이 막히지 않음
    }""",
    """    void 이전_리프레시_토큰은_재사용할_수_없다() {
        TokenPair first = login("tester");
        refresh(first.refresh());
        assertThatThrownBy(() -> refresh(first.refresh()))
                .isInstanceOf(InvalidTokenException.class);
    }""")

LEGACY_TEST = """package kr.demo.minwon.auth;

@Disabled("구 토큰 방식")
class LegacyTokenTest {
}
"""

REPO_V0 = """package kr.demo.minwon.query;

public class MinwonQueryRepository {
    private final JPAQueryFactory query;

    public Page<MinwonSummary> search(MinwonCondition cond, Pageable pageable) {
        List<MinwonSummary> content = query.select(summary())
                .from(minwonReq)
                .where(statusEq(cond.status()), receiptBetween(cond.from(), cond.to()))
                .orderBy(minwonReq.receiptDt.desc())
                .offset(pageable.getOffset())
                .limit(pageable.getPageSize())
                .fetch();
        long total = query.selectFrom(minwonReq)
                .where(statusEq(cond.status()), receiptBetween(cond.from(), cond.to()))
                .fetchCount();
        return new PageImpl<>(content, pageable, total);
    }
}
"""

REPO_V1 = REPO_V0.replace(
    """                .where(statusEq(cond.status()), receiptBetween(cond.from(), cond.to()))
                .orderBy(minwonReq.receiptDt.desc())""",
    """                // 복합 인덱스(RECEIPT_DT, STATUS_CD)를 타도록 조건 순서 정리 (GOV-1423)
                .where(receiptBetween(cond.from(), cond.to()), statusEq(cond.status()))
                .orderBy(minwonReq.receiptDt.desc())""")

REPO_V2 = REPO_V1.replace(
    """        long total = query.selectFrom(minwonReq)
                .where(statusEq(cond.status()), receiptBetween(cond.from(), cond.to()))
                .fetchCount();
        return new PageImpl<>(content, pageable, total);""",
    """        JPAQuery<Long> count = query.select(minwonReq.count())
                .from(minwonReq)
                .where(receiptBetween(cond.from(), cond.to()), statusEq(cond.status()));
        // count 쿼리 분리: 마지막 페이지 등 필요할 때만 실행
        return PageableExecutionUtils.getPage(content, pageable, count::fetchOne);""")

REPO_V3 = REPO_V2.replace(
    """    }
}
""",
    """    }

    // GOV-1430 검토 중: 커서 기반 페이징 초안 (마지막 접수번호 이후 조회)
    public List<MinwonSummary> searchAfter(MinwonCondition cond, Long lastReqNo, int size) {
        return query.select(summary())
                .from(minwonReq)
                .where(receiptBetween(cond.from(), cond.to()), statusEq(cond.status()),
                        lastReqNo == null ? null : minwonReq.reqNo.lt(lastReqNo))
                .orderBy(minwonReq.reqNo.desc())
                .limit(size)
                .fetch();
    }
}
""")

REPO_TEST_V1 = """package kr.demo.minwon.query;

class MinwonQueryRepositoryTest {

    @Test
    void 기간과_상태로_민원을_조회한다() {
        Page<MinwonSummary> page = repository.search(cond("2026-09-01", "2026-09-30", "RECEIVED"), PageRequest.of(0, 20));
        assertThat(page.getContent()).hasSize(20);
    }
}
"""

REPO_TEST_V2 = REPO_TEST_V1.replace(
    """        assertThat(page.getContent()).hasSize(20);
    }""",
    """        assertThat(page.getContent()).hasSize(20);
    }

    @Test
    void 첫_페이지에서는_count_쿼리를_실행하지_않는다() {
        repository.search(cond("2026-09-01", "2026-09-30", "RECEIVED"), PageRequest.of(0, 20));
        assertThat(queryCounter.count("select count")).isZero();
    }""")

REPO_TEST_V3 = REPO_TEST_V2.replace(
    """        assertThat(queryCounter.count("select count")).isZero();
    }""",
    """        assertThat(queryCounter.count("select count")).isZero();
    }

    @Test
    void 운영_데이터_규모에서_응답시간_500ms_이내() {
        assertTimeout(Duration.ofMillis(500),
                () -> repository.search(cond("2026-01-01", "2026-09-30", null), PageRequest.of(0, 20)));
    }""")

INDEX_SQL = """-- GOV-1423 민원 조회 성능 개선: 접수일자 + 처리상태 복합 인덱스
CREATE INDEX IX_MINWON_REQ_01 ON MINWON_REQ (RECEIPT_DT, STATUS_CD);
"""

VALUES_V0 = """replicaCount: 1
image:
  repository: registry.local/poc-api
  tag: latest
service:
  port: 8080
"""
VALUES_V1 = VALUES_V0.replace("replicaCount: 1", "replicaCount: 2").replace("tag: latest", 'tag: "0.3.1"') + """resources:
  limits:
    memory: 512Mi
"""
VALUES_V2 = VALUES_V1 + """ingress:
  enabled: true
  host: poc-api.dev.local
"""
DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
spec:
  replicas: {{ .Values.replicaCount }}
  template:
    spec:
      containers:
        - name: api
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          ports:
            - containerPort: {{ .Values.service.port }}
"""
INGRESS = """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ .Release.Name }}
spec:
  ingressClassName: nginx
  rules:
    - host: {{ .Values.ingress.host }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ .Release.Name }}
                port:
                  number: {{ .Values.service.port }}
"""

REVIEW_DOC_V1 = _docx([
    "GOV-1423 민원 조회 성능 개선 검토",
    "1. 현상: 민원 목록 조회 API 평균 응답 3.2초 (운영 데이터 약 480만 건)",
    "2. 원인: MINWON_REQ 조회 시 인덱스 미사용, 페이지마다 전체 count 쿼리 실행",
    "3. 개선안: 복합 인덱스 (RECEIPT_DT, STATUS_CD) 추가, count 쿼리 분리",
    "4. 결과(개발 서버): 평균 0.4초",
    "5. 요청: DBA 인덱스 검토",
])
REVIEW_DOC_V2 = _docx([
    "GOV-1423 민원 조회 성능 개선 검토",
    "1. 현상: 민원 목록 조회 API 평균 응답 3.2초 (운영 데이터 약 480만 건)",
    "2. 원인: MINWON_REQ 조회 시 인덱스 미사용, 페이지마다 전체 count 쿼리 실행",
    "3. 개선안: 복합 인덱스 (RECEIPT_DT, STATUS_CD) 추가, count 쿼리 분리",
    "4. 결과(개발 서버): 평균 0.4초",
    "5. DBA 검토 결과: 인덱스 승인 (09-30). 운영 반영은 다음 주 화요일 배포 창",
    "6. 후속: 커서 기반 페이징 전환 검토 (GOV-1430)",
])

# folder key -> (label, fictional folder name under ~/demo, project name or None)
FOLDERS = {
    "api": ("gov24-minwon-api", "gov24-minwon-api", "Gov24 민원 API"),
    "k8s": ("poc-k8s", "poc-k8s", "K8s 전환 PoC"),
    "docs": ("업무 문서", "업무문서", "Gov24 민원 API"),
}

# (day offset from this week's Monday, "HH:MM", folder key, {path: content | None to delete})
# The first entry of each folder is its baseline snapshot.
EDITS = [
    (-7, "08:50", "api", {f"{PKG}/auth/TokenRefreshFilter.java": FILTER_V0, f"{PKG}/auth/JwtTokenProvider.java": JWT_V0,
                          f"{TEST}/auth/LegacyTokenTest.java": LEGACY_TEST,
                          f"{PKG}/query/MinwonQueryRepository.java": REPO_V0}),
    (-7, "08:50", "docs", {"README.txt": "업무 문서 폴더\n"}),
    (-7, "11:55", "api", {f"{PKG}/auth/TokenRefreshFilter.java": FILTER_V1}),
    (-7, "14:55", "api", {f"{PKG}/auth/JwtTokenProvider.java": JWT_V1}),
    (-7, "17:35", "api", {f"{TEST}/auth/TokenRefreshFilterTest.java": FILTER_TEST_V1}),
    (-6, "11:45", "api", {f"{PKG}/auth/TokenRefreshFilter.java": FILTER_V2, f"{TEST}/auth/LegacyTokenTest.java": None}),
    (-6, "17:25", "api", {f"{TEST}/auth/TokenRefreshFilterTest.java": FILTER_TEST_V2}),
    (-5, "08:55", "k8s", {"poc-chart/values.yaml": VALUES_V0}),
    (-5, "11:55", "k8s", {"poc-chart/values.yaml": VALUES_V1}),
    (-5, "16:25", "k8s", {"poc-chart/templates/deployment.yaml": DEPLOYMENT}),
    (-3, "11:55", "api", {"src/main/resources/db/migration/V42__minwon_req_index.sql": INDEX_SQL,
                          f"{PKG}/query/MinwonQueryRepository.java": REPO_V1}),
    (-3, "17:25", "api", {f"{TEST}/query/MinwonQueryRepositoryTest.java": REPO_TEST_V1}),
    (-3, "17:30", "docs", {"GOV-1423_조회성능_개선_검토.docx": REVIEW_DOC_V1}),
    (0, "11:55", "api", {f"{PKG}/query/MinwonQueryRepository.java": REPO_V2}),
    (0, "15:25", "api", {f"{TEST}/query/MinwonQueryRepositoryTest.java": REPO_TEST_V2}),
    (1, "11:55", "k8s", {"poc-chart/templates/ingress.yaml": INGRESS}),
    (1, "15:55", "k8s", {"poc-chart/values.yaml": VALUES_V2}),
    (2, "12:00", "docs", {"GOV-1423_조회성능_개선_검토.docx": REVIEW_DOC_V2}),
    (2, "16:45", "api", {f"{TEST}/query/MinwonQueryRepositoryTest.java": REPO_TEST_V3}),
    (3, "11:55", "api", {f"{PKG}/query/MinwonQueryRepository.java": REPO_V3}),
]
