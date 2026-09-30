"""Kanban Status policy: columns only, never labels."""

from unittest import TestCase

from scripts.kanban_status import (
    decide_status,
    is_soak_issue,
    is_spec_event_text,
    linked_issue_numbers,
    ready_chain_green,
)


READY = ["omnigent:ready", "review:lena", "review:viktor", "bug", "area:risk"]


class ReadyChainTests(TestCase):
    def test_green_requires_kay_ready_and_both_reviews(self):
        self.assertTrue(ready_chain_green(set(READY)))

    def test_missing_omnigent_ready_is_not_green(self):
        labs = set(READY) - {"omnigent:ready"}
        self.assertFalse(ready_chain_green(labs))

    def test_veto_blocks_green(self):
        self.assertFalse(ready_chain_green(set(READY) | {"veto:lena"}))
        self.assertFalse(ready_chain_green(set(READY) | {"veto:viktor"}))


class DecideStatusTests(TestCase):
    def test_new_issue_without_status_goes_backlog(self):
        d = decide_status(
            issue_state="OPEN",
            labels=["bug", "area:telegram", "priority:p2"],
            current=None,
            new_issue=True,
        )
        self.assertEqual(d.want, "backlog")

    def test_epic_goes_backlog_epics(self):
        d = decide_status(
            issue_state="OPEN",
            labels=["epic", "enhancement"],
            current=None,
            new_issue=True,
        )
        self.assertEqual(d.want, "backlog-epics")

    def test_epic_already_on_epics_is_left_alone(self):
        d = decide_status(
            issue_state="OPEN",
            labels=["epic"],
            current="Backlog: Epics",
        )
        self.assertIsNone(d.want)

    def test_spec_freeze_comment_promotes_backlog(self):
        d = decide_status(
            issue_state="OPEN",
            labels=["enhancement"],
            current="Backlog",
            spec_event=True,
        )
        self.assertEqual(d.want, "spec-acceptance")

    def test_template_acceptance_heading_is_not_a_spec_event(self):
        self.assertFalse(
            is_spec_event_text("## Acceptance criteria\n- [ ] foo")
        )
        d = decide_status(
            issue_state="OPEN",
            labels=["enhancement"],
            current="Backlog",
            new_issue=True,
            spec_event=is_spec_event_text("## Acceptance criteria\n- [ ] foo"),
        )
        self.assertIsNone(d.want)

    def test_ready_chain_moves_to_ready_to_work(self):
        d = decide_status(
            issue_state="OPEN",
            labels=READY,
            current="Spec/Acceptance",
        )
        self.assertEqual(d.want, "ready-to-work")

    def test_reviews_without_omnigent_ready_do_not_move(self):
        labs = [x for x in READY if x != "omnigent:ready"]
        d = decide_status(
            issue_state="OPEN",
            labels=labs,
            current="Spec/Acceptance",
        )
        self.assertIsNone(d.want)

    def test_veto_blocks_ready_to_work(self):
        d = decide_status(
            issue_state="OPEN",
            labels=READY + ["veto:viktor"],
            current="Spec/Acceptance",
        )
        self.assertIsNone(d.want)

    def test_veto_on_ready_to_work_returns_to_spec(self):
        d = decide_status(
            issue_state="OPEN",
            labels=READY + ["veto:lena"],
            current="Ready to Work",
        )
        self.assertEqual(d.want, "spec-acceptance")

    def test_removing_omnigent_ready_returns_to_spec(self):
        labs = [x for x in READY if x != "omnigent:ready"]
        d = decide_status(
            issue_state="OPEN",
            labels=labs,
            current="Ready to Work",
        )
        self.assertEqual(d.want, "spec-acceptance")

    def test_soak_merge_moves_to_shadow_and_does_not_close(self):
        d = decide_status(
            issue_state="OPEN",
            labels=["enhancement", "area:risk"],
            current="Ready to Work",
            soak_merged_staging=True,
        )
        self.assertEqual(d.want, "shadow")

    def test_shadow_is_not_demoted(self):
        d = decide_status(
            issue_state="OPEN",
            labels=READY,
            current="Shadow",
            spec_event=True,
        )
        self.assertIsNone(d.want)

    def test_done_is_retained(self):
        d = decide_status(
            issue_state="CLOSED",
            labels=["bug"],
            current="Done",
            new_issue=True,
        )
        self.assertIsNone(d.want)

    def test_closed_issue_is_not_moved(self):
        d = decide_status(
            issue_state="CLOSED",
            labels=READY,
            current="Backlog",
        )
        self.assertIsNone(d.want)

    def test_parked_stays_backlog(self):
        d = decide_status(
            issue_state="OPEN",
            labels=["parked", "enhancement"],
            current="Backlog",
            spec_event=True,
        )
        self.assertIsNone(d.want)

    def test_parked_blocks_ready_to_work(self):
        d = decide_status(
            issue_state="OPEN",
            labels=READY + ["parked"],
            current="Spec/Acceptance",
        )
        self.assertIsNone(d.want)


class SoakAndSpecTextTests(TestCase):
    def test_spec_freeze_language(self):
        self.assertTrue(is_spec_event_text("Spec freeze started."))
        self.assertTrue(is_spec_event_text("Marcus spec posted"))
        self.assertTrue(is_spec_event_text("fix-round for the wizard"))

    def test_soak_label_or_text(self):
        self.assertTrue(is_soak_issue({"shadow-modus"}, "feat: foo"))
        self.assertTrue(is_soak_issue(set(), "28-day paper soak, leave issue open"))
        self.assertFalse(is_soak_issue(set(), "universe observe-sensor shadow"))


class LinkedIssueTests(TestCase):
    def test_extracts_hash_refs(self):
        self.assertEqual(linked_issue_numbers("Fixes #614", "see #615"), [614, 615])
