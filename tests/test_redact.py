import unittest

from workhistory.redact import redact


class RedactTests(unittest.TestCase):
    def assertMasked(self, text, secret):
        out = redact(text)
        self.assertNotIn(secret, out, out)
        return out

    def test_config_values(self):
        self.assertMasked("spring.datasource.password=Sup3rS3cret!", "Sup3rS3cret!")
        self.assertMasked("  password: hunter2\n", "hunter2")
        self.assertMasked("DB_PASSWORD=p@ss_w0rd", "p@ss_w0rd")
        self.assertMasked("api_key: abcd1234efgh", "abcd1234efgh")
        self.assertMasked('client_secret = "s3cr3t-value"', "s3cr3t-value")
        self.assertMasked('{"token": "tok_live_123456"}', "tok_live_123456")

    def test_tokens_and_keys(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0ZXIifQ.c2lnbmF0dXJlLXZhbHVl"
        self.assertMasked(f"String t = \"{jwt}\";", jwt)
        self.assertMasked("Authorization: Bearer abcdef0123456789abcdef", "abcdef0123456789abcdef")
        self.assertMasked("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE")
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        self.assertMasked(pem, "MIIEpAIBAAKCAQEA")
        long_key = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0uv"
        self.assertMasked(f"key {long_key}", long_key)

    def test_inside_diff_lines_and_sentences(self):
        self.assertMasked("+    password: Dev!Pass123", "Dev!Pass123")
        self.assertMasked("-spring.datasource.password=old-secret-1", "old-secret-1")
        self.assertMasked("QA 계정 token=qa_tok_998877 확인", "qa_tok_998877")
        self.assertMasked("secret: abc, user: kim", "abc,")  # value ends at the comma

    def test_urls_netrc_and_known_token_formats(self):
        self.assertMasked("GET https://api.example.com/v1?access_token=abc123def456&x=1", "abc123def456")
        self.assertMasked("url=https://x.io/cb#token=abc123def456", "abc123def456")
        self.assertMasked("DATABASE_URL postgres://admin:S3cr3tPass@db.internal:5432/app", "S3cr3tPass")
        self.assertMasked("machine git.example.com login kim password hunter2", "hunter2")
        self.assertMasked("password hunter2\n", "hunter2")
        self.assertMasked("key AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q", "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")
        self.assertMasked("SLACK=xoxb-123456789012-abcdefABCDEF", "xoxb-123456789012-abcdefABCDEF")
        self.assertMasked("export KEY sk-proj-abcdefghijklmnopqrstuv", "sk-proj-abcdefghijklmnopqrstuv")
        self.assertMasked("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        self.assertEqual(redact("https://gitlab.example.com/gov/minwon-api"), "https://gitlab.example.com/gov/minwon-api")
        self.assertEqual(redact("비밀번호 변경 화면에서 password 확인 필요"), "비밀번호 변경 화면에서 password 확인 필요")

    def test_personal_identifiers(self):
        self.assertMasked("주민번호 900101-1234567", "900101-1234567")
        self.assertMasked("카드 1234-5678-9012-3456", "1234-5678-9012-3456")

    def test_ordinary_code_is_kept(self):
        for code in ('String password = req.getParameter("password");',
                     "if (token == null) return;",
                     "TokenPair pair = tokenProvider.rotate(refresh);",
                     "refreshTokens.delete(stored); // 기존 리프레시 토큰 무효화 (GOV-1418)",
                     "CREATE INDEX IX_MINWON_REQ_01 ON MINWON_REQ (RECEIPT_DT, STATUS_CD);",
                     "kr.demo.minwon.query.MinwonQueryRepositoryTest",
                     "GOV-1423 민원 조회 API 응답 3.2초",
                     "src/main/resources/db/migration/V42__minwon_req_index_for_receipt_and_status.sql",
                     "void 운영_데이터_규모에서_응답시간_500ms_이내_검증_케이스_long_name_2026_test() {}",
                     "this_is_a_very_long_snake_case_identifier_with_number_42_inside"):
            self.assertEqual(redact(code), code)


class DiffExcerptTests(unittest.TestCase):
    def test_private_key_added_in_a_diff_is_masked_across_lines(self):
        from types import SimpleNamespace
        from workhistory.ai_context import _excerpt
        key = ("-----BEGIN PRIVATE KEY-----\n"
               "MIIEvQIBADANBgkqhkiG9w0B/AQEFAASCBKcwggSjAgEAAoIBAQC7\n"
               "q+IzW7yWR7abc/VDL5Aa0k1L2m3N4o5P6q7R8s9\n"
               "-----END PRIVATE KEY-----\n")
        repo = {b"old": SimpleNamespace(data=b"class A {}\n"),
                b"new": SimpleNamespace(data=('class A {}\nString k = """\n' + key + '""";\n').encode())}
        text = "\n".join(_excerpt(repo, "src/A.java", "old", "new", 50))
        self.assertIn("BEGIN", text)
        for fragment in ("MIIEvQIBADANBgkqhkiG9w0B", "IzW7yWR7", "VDL5Aa0k1L2m3N4o5P6q7R8s9"):
            self.assertNotIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
