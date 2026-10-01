import unittest

from workhistory.normalize import Activity
from workhistory.sessions import (app_label, auto_title, clean_title, dominant_project,
                                  group_activities, smooth_runs, split_by_gap)

M = 60


def A(start_min, end_min, app="idea64.exe", title="gov24-api – A.java", project=1, eid=None):
    return Activity(eid or int(start_min), start_min * M, end_min * M, app, title, project, "rule")


def spans(groups):
    return [(g[0].start / M, g[-1].end / M) for g in groups]


class GapTests(unittest.TestCase):
    def test_gap_over_threshold_splits(self):
        acts = [A(0, 30), A(35, 50), A(61, 90)]  # 5 min gap keeps, 11 min gap splits
        self.assertEqual(spans(split_by_gap(acts, 10 * M)), [(0, 50), (61, 90)])

    def test_exactly_threshold_does_not_split(self):
        self.assertEqual(len(split_by_gap([A(0, 10), A(20, 30)], 10 * M)), 1)


class SmoothingTests(unittest.TestCase):
    def test_short_detour_absorbed(self):
        acts = [A(0, 20), A(20, 22, app="slack.exe", project=None), A(22, 40)]
        runs = smooth_runs(acts, lambda a: a.project_id, 3 * M)
        self.assertEqual(spans(runs), [(0, 40)])

    def test_long_switch_kept(self):
        acts = [A(0, 20), A(20, 30, project=2), A(30, 40)]
        self.assertEqual(spans(smooth_runs(acts, lambda a: a.project_id, 3 * M)), [(0, 20), (20, 30), (30, 40)])

    def test_alternating_short_runs_collapse(self):
        acts = [A(i, i + 1, project=1 + i % 2) for i in range(10)]
        self.assertEqual(len(smooth_runs(acts, lambda a: a.project_id, 3 * M)), 1)

    def test_deterministic(self):
        acts = [A(0, 2, project=1), A(2, 4, project=2), A(4, 6, project=3)]
        first = spans(smooth_runs(acts, lambda a: a.project_id, 3 * M))
        self.assertEqual(first, spans(smooth_runs(acts, lambda a: a.project_id, 3 * M)))


class GroupTests(unittest.TestCase):
    def test_reading_then_coding(self):
        acts = [
            A(0, 30, app="chrome.exe", title="Spring Boot API 문서 - Google Chrome"),
            A(30, 31, app="slack.exe", title="Slack", project=None),
            A(31, 65, app="idea64.exe"),
            A(65, 80, app="Postman.exe", title="Postman - gov24 API 테스트"),
        ]
        groups = group_activities(acts)
        # 30 min of reading is its own session; IDE + Postman are one coding session,
        # and the 1-minute Slack detour stays inside it.
        self.assertEqual(spans(groups), [(0, 30), (30, 80)])
        self.assertEqual([auto_title(g) for g in groups],
                         ["Chrome · Spring Boot API 문서", "IntelliJ IDEA · gov24-api – A.java"])

    def test_tools_of_one_kind_stay_together(self):
        acts = [A(0, 20, app="idea64.exe"), A(20, 40, app="dbeaver.exe", title="실행계획"),
                A(40, 60, app="WindowsTerminal.exe", title="gradlew test")]
        self.assertEqual(spans(group_activities(acts)), [(0, 60)])

    def test_meeting_is_its_own_session_even_when_short(self):
        acts = [A(0, 30), A(30, 36, app="ms-teams.exe", title="일일 스크럼 | Microsoft Teams"), A(36, 60)]
        self.assertEqual(spans(group_activities(acts)), [(0, 30), (30, 36), (36, 60)])

    def test_teams_chat_is_not_a_meeting(self):
        acts = [A(0, 30), A(30, 36, app="ms-teams.exe", title="김철수 | Microsoft Teams"), A(36, 60)]
        self.assertEqual(spans(group_activities(acts)), [(0, 60)])

    def test_unclassified_lookup_stays_in_project_session(self):
        acts = [A(0, 20), A(20, 32, app="chrome.exe", title="cursor pagination", project=None), A(32, 50)]
        groups = group_activities(acts)
        self.assertEqual(spans(groups), [(0, 50)])
        self.assertEqual(dominant_project(groups[0]), 1)

    def test_long_unclassified_stretch_stands_alone(self):
        acts = [A(0, 20), A(20, 40, app="chrome.exe", title="news", project=None), A(40, 60)]
        self.assertEqual(len(group_activities(acts)), 3)

    def test_short_offwork_break_does_not_split(self):
        acts = [A(0, 20), A(20, 28, app="", title="", project=None), A(28, 50)]
        groups = group_activities(acts)
        self.assertEqual(spans(groups), [(0, 50)])
        self.assertEqual(auto_title(groups[0]), "IntelliJ IDEA · gov24-api – A.java")

    def test_window_switching_is_not_a_session_each(self):
        acts = []
        t = 0
        for i in range(30):  # 30 quick IDE/browser switches inside one project
            app = "idea64.exe" if i % 2 == 0 else "chrome.exe"
            acts.append(A(t, t + 2, app=app))
            t += 2
        self.assertEqual(len(group_activities(acts)), 1)

    def test_project_change_splits(self):
        acts = [A(0, 20, project=1), A(20, 40, project=2, title="kubectl")]
        self.assertEqual([dominant_project(g) for g in group_activities(acts)], [1, 2])

    def test_empty(self):
        self.assertEqual(group_activities([]), [])


class TitleTests(unittest.TestCase):
    def test_clean_title(self):
        self.assertEqual(clean_title("GitHub - Google Chrome", "chrome.exe"), "GitHub")
        self.assertEqual(clean_title("main.py - proj - Visual Studio Code", "Code.exe"), "main.py - proj")
        self.assertEqual(clean_title("gov24-api – UserController.java", "idea64.exe"), "gov24-api – UserController.java")
        self.assertEqual(clean_title("", "x.exe"), "")

    def test_app_label(self):
        self.assertEqual(app_label("idea64.exe"), "IntelliJ IDEA")
        self.assertEqual(app_label("SomeTool.EXE"), "SomeTool")

    def test_auto_title_uses_dominant_app_and_title(self):
        acts = [A(0, 5, app="chrome.exe", title="a - Google Chrome"), A(5, 20, title="Main.java"),
                A(20, 22, title="Other.java")]
        self.assertEqual(auto_title(acts), "IntelliJ IDEA · Main.java")

    def test_long_titles_truncated(self):
        self.assertLessEqual(len(auto_title([A(0, 5, title="x" * 300)])), 90)

    def test_empty_title_falls_back_to_app(self):
        self.assertEqual(auto_title([A(0, 5, title="")]), "IntelliJ IDEA")


if __name__ == "__main__":
    unittest.main()
