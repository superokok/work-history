import unittest

from workhistory.classify import Keyword, Rule
from workhistory.normalize import (AfkEvent, WindowEvent, build_activities, clip, idle_periods,
                                   merge_intervals, merge_rows, subtract)


def W(i, s, e, app="Code.exe", title="t", project_id=None):
    return WindowEvent(i, s, e, app, title, project_id)


class IntervalTests(unittest.TestCase):
    def test_merge_overlapping_and_touching(self):
        self.assertEqual(merge_intervals([(5, 7), (0, 2), (2, 3), (6, 9), (4, 4)]), [(0, 3), (5, 9)])

    def test_subtract(self):
        self.assertEqual(subtract([(0, 10)], [(2, 3), (5, 6)]), [(0, 2), (3, 5), (6, 10)])
        self.assertEqual(subtract([(0, 10)], [(-5, 15)]), [])
        self.assertEqual(subtract([(0, 10), (20, 30)], [(8, 22)]), [(0, 8), (22, 30)])
        self.assertEqual(subtract([(0, 10)], []), [(0, 10)])

    def test_clip(self):
        allowed = [(0, 10), (20, 30), (40, 50)]
        self.assertEqual(clip(5, 45, allowed), [(5, 10), (20, 30), (40, 45)])
        self.assertEqual(clip(10, 20, allowed), [])
        self.assertEqual(clip(-5, 3, allowed), [(0, 3)])


class IdleTests(unittest.TestCase):
    def test_afk_time_is_removed(self):
        acts, warnings = build_activities(
            [W(1, 0, 100)], [AfkEvent(0, 40, "not-afk"), AfkEvent(40, 70, "afk"), AfkEvent(70, 100, "not-afk")],
            [], 0, 1000)
        self.assertEqual([(a.start, a.end) for a in acts], [(0, 40), (70, 100)])
        self.assertEqual(warnings, [])

    def test_not_afk_wins_on_overlap(self):
        self.assertEqual(idle_periods([AfkEvent(0, 50, "afk"), AfkEvent(30, 40, "not-afk")]), [(0, 30), (40, 50)])

    def test_time_without_afk_data_counts_as_active(self):
        acts, _ = build_activities([W(1, 0, 100)], [AfkEvent(0, 20, "afk")], [], 0, 1000)
        self.assertEqual([(a.start, a.end) for a in acts], [(20, 100)])

    def test_missing_afk_data_warns(self):
        acts, warnings = build_activities([W(1, 0, 100)], [], [], 0, 1000)
        self.assertEqual(sum(a.duration for a in acts), 100)
        self.assertEqual(warnings, ["afk_missing"])

    def test_lock_screen_is_idle(self):
        acts, _ = build_activities([W(1, 0, 50, app="LockApp.exe"), W(2, 50, 60)], [], [], 0, 1000)
        self.assertEqual([a.event_id for a in acts], [2])


class RangeTests(unittest.TestCase):
    def test_clipped_to_day_range(self):
        acts, _ = build_activities([W(1, -50, 50), W(2, 990, 1100)], [], [], 0, 1000)
        self.assertEqual([(a.start, a.end) for a in acts], [(0, 50), (990, 1000)])

    def test_blocked_ranges_removed(self):
        acts, _ = build_activities([W(1, 0, 100)], [], [(10, 20), (50, 60)], 0, 1000)
        self.assertEqual(sum(a.duration for a in acts), 80)

    def test_overlapping_window_events_not_double_counted(self):
        acts, _ = build_activities([W(1, 0, 60), W(2, 30, 90, title="b"), W(3, 40, 50, title="c")], [], [], 0, 1000)
        self.assertEqual(sum(a.duration for a in acts), 90)
        self.assertEqual([(a.event_id, a.start) for a in acts], [(1, 0), (2, 60)])

    def test_zero_length_events_ignored(self):
        acts, _ = build_activities([W(1, 10, 10)], [], [], 0, 1000)
        self.assertEqual(acts, [])


class ClassificationAndPrivacyTests(unittest.TestCase):
    def test_exclusions_hidden_even_if_stored(self):
        acts, _ = build_activities([W(1, 0, 10, title="인터넷뱅킹 - Chrome"), W(2, 10, 20)], [], [], 0, 1000,
                                   exclusions=[Keyword("title", "인터넷뱅킹")])
        self.assertEqual([a.event_id for a in acts], [2])

    def test_rules_and_manual_override(self):
        rules = [Rule(1, 10, "title", "gov24")]
        acts, _ = build_activities([W(1, 0, 10, title="gov24-api"), W(2, 10, 20, title="gov24-api", project_id=99),
                                    W(3, 20, 30, title="other")], [], [], 0, 1000, rules=rules)
        self.assertEqual([(a.project_id, a.project_source) for a in acts],
                         [(10, "rule"), (99, "manual"), (None, None)])


class RowTests(unittest.TestCase):
    def test_merges_consecutive_same_window(self):
        acts, _ = build_activities([W(1, 0, 10), W(2, 30, 40), W(3, 40, 50, title="x"), W(4, 500, 510)],
                                   [], [], 0, 1000)
        rows = merge_rows(acts, max_gap=60)
        self.assertEqual([(r.start, r.end, r.active_s, r.event_ids) for r in rows],
                         [(0, 40, 20, [1, 2]), (40, 50, 10, [3]), (500, 510, 10, [4])])

    def test_split_by_afk_rejoins_into_one_row(self):
        acts, _ = build_activities([W(1, 0, 100)], [AfkEvent(0, 40, "not-afk"), AfkEvent(40, 70, "afk"),
                                                    AfkEvent(70, 100, "not-afk")], [], 0, 1000)
        rows = merge_rows(acts, max_gap=60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].active_s, 70)
        self.assertEqual(rows[0].event_ids, [1])


if __name__ == "__main__":
    unittest.main()
