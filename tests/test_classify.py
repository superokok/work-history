import unittest

from workhistory.classify import Keyword, Rule, keyword_matches, match_rule, matches_any, resolve_project


class ClassifyTests(unittest.TestCase):
    def test_case_insensitive_substring(self):
        self.assertTrue(keyword_matches("title", "Gov24", "idea64.exe", "gov24-api – Foo.java"))
        self.assertTrue(keyword_matches("app", "IDEA", "idea64.exe", ""))
        self.assertFalse(keyword_matches("app", "gov24", "idea64.exe", "gov24-api"))

    def test_blank_pattern_never_matches(self):
        self.assertFalse(keyword_matches("title", "   ", "a", "anything"))

    def test_korean_substring(self):
        self.assertTrue(keyword_matches("title", "민원", "chrome.exe", "정부24 민원신청 - Chrome"))

    def test_longest_pattern_wins(self):
        rules = [Rule(1, 100, "title", "api"), Rule(2, 200, "title", "gov24-api")]
        self.assertEqual(match_rule(rules, "x", "gov24-api – A.java").project_id, 200)

    def test_tie_goes_to_oldest_rule(self):
        rules = [Rule(5, 500, "title", "abc"), Rule(3, 300, "app", "abc")]
        self.assertEqual(match_rule(rules, "abc.exe", "abc").id, 3)

    def test_resolve_precedence(self):
        rules = [Rule(1, 100, "title", "gov24")]
        self.assertEqual(resolve_project(7, rules, "a", "gov24"), (7, "manual"))
        self.assertEqual(resolve_project(None, rules, "a", "gov24"), (100, "rule"))
        self.assertEqual(resolve_project(None, rules, "a", "other"), (None, None))

    def test_matches_any(self):
        kws = [Keyword("app", "KeePass"), Keyword("title", "뱅킹")]
        self.assertTrue(matches_any(kws, "KeePassXC.exe", ""))
        self.assertTrue(matches_any(kws, "chrome.exe", "KB 인터넷뱅킹"))
        self.assertFalse(matches_any(kws, "chrome.exe", "GitHub"))


if __name__ == "__main__":
    unittest.main()
