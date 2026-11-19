"""Issue-only lifecycle and durable evidence under sequential writes."""

from copy import deepcopy
import unittest

from modern_requirements.cli import RequirementError, Requirements
from tests.requirements_fixtures import FakeGitHub, completion, requirement


class RequirementWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.service = Requirements(self.github)
        self.detail = self.service.create(
            requirement(), registration_source="human", decision="approved"
        )

    def transition(self, source, target, **kwargs):
        return self.service.transition(
            1,
            from_status=source,
            to_status=target,
            reason=kwargs.pop("reason", "Proceed with the documented work."),
            **kwargs,
        )

    def edit(self, value):
        return self.service.edit(1, value)

    def test_doing_means_claimed_and_block_resume_release_preserve_labels(self):
        self.github.issues[1]["labels"].append({"name": "documentation"})
        claimed = self.transition("todo", "doing")
        self.assertEqual(claimed["status"], "doing")
        self.assertIn("registration:human", claimed["labels"])
        with self.assertRaises(RequirementError) as caught:
            self.transition("todo", "doing")
        self.assertEqual(caught.exception.code, "ALREADY_CLAIMED")
        self.transition(
            "doing",
            "blocked",
            reason="Schema unavailable. Restore the reference, then compare columns.",
        )
        self.transition("blocked", "doing")
        self.assertEqual(
            self.transition(
                "doing",
                "todo",
                reason="Draft is attached. Another agent can complete the comparison.",
            )["status"],
            "todo",
        )
        self.assertIn({"name": "documentation"}, self.github.issues[1]["labels"])
        self.assertIn("registration:human", self.service.get(1)["labels"])
        self.assertNotIn("<!--", self.github.issues[1]["body"])
        self.assertEqual(len(self.github.comments[1]), 4)

    def test_github_closed_issue_stays_active_until_reported_without_reopening(self):
        """A closing PR must not hide unfinished bookkeeping or block its report."""
        self.transition("todo", "doing")
        self.github.issues[1].update(state="closed", state_reason="completed")
        self.github.calls.clear()
        detail = self.service.get(1)
        self.assertEqual((detail["status"], detail["state"], detail["state_reason"]),
                         ("doing", "closed", "completed"))
        active = self.service.list()["items"][0]
        self.assertEqual((active["status"], active["state"], active["state_reason"]),
                         ("doing", "closed", "completed"))
        self.assertEqual(self.service.list(status="done")["items"], [])
        report = completion(detail, repository_changes=True)
        self.github.pulls[10] = {
            "merged": True, "merged_at": "2026-11-16T00:00:00Z",
            "base": {"ref": "main", "repo": {"full_name": "web-infra-dev/modern.js"}},
        }
        result = self.transition("doing", "done", report=report)
        self.assertEqual((result["status"], result["state"], result["state_reason"]),
                         ("done", "closed", "completed"))
        self.assertIn("status:done", result["labels"])
        self.assertEqual(self.service.list()["items"], [])
        # Preserve GitHub's closure throughout reconciliation, not reopen/reclose.
        updates = [body for method, path, body in self.github.calls
                   if method == "PATCH" and path == "/issues/1"]
        self.assertTrue(updates)
        self.assertTrue(all(body["state"] == "closed" for body in updates))

    def test_github_closure_cannot_bypass_evidence_or_merge_gates(self):
        """A closed Issue alone must never count as accepted repository work."""
        self.transition("todo", "doing")
        self.github.issues[1].update(state="closed", state_reason="completed")
        report = completion(self.service.get(1), repository_changes=True)
        self.github.pulls[10] = {
            "merged": False, "merged_at": None,
            "base": {"ref": "main", "repo": {"full_name": "web-infra-dev/modern.js"}},
        }
        for candidate in ({**report, "results": []}, report):
            with self.subTest(candidate=candidate):
                comments_before = deepcopy(self.github.comments[1])
                with self.assertRaises(RequirementError) as caught:
                    self.transition("doing", "done", report=candidate)
                self.assertEqual(caught.exception.code, "INVALID_COMPLETION_REPORT")
                self.assertEqual(self.github.comments[1], comments_before)
                self.assertEqual(self.service.get(1)["status"], "doing")
                self.assertEqual(self.github.issues[1]["state"], "closed")

    def test_failed_bookkeeping_after_github_closure_is_not_reported_as_done(self):
        """Saving a completion comment must not disguise a failed progress update."""
        self.transition("todo", "doing")
        self.github.issues[1].update(state="closed", state_reason="completed")
        report = completion(self.service.get(1))
        original = self.github.request

        def request(method, path, payload=None):
            if method == "PATCH":
                raise RequirementError("GITHUB_ERROR", "Forbidden", http_status=403)
            return original(method, path, payload)

        self.github.request = request
        with self.assertRaises(RequirementError) as caught:
            self.transition("doing", "done", report=report)
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        self.assertTrue(caught.exception.details["audit_url"])
        self.assertEqual(self.service.get(1)["status"], "doing")
        self.assertEqual(self.service.list()["items"][0]["state"], "closed")

    def test_cancel_and_restore_require_human_instruction(self):
        for confirmation in (None, {}, {"instruction": "Cancel it."}):
            with self.assertRaises(RequirementError) as caught:
                self.transition("todo", "cancelled", confirmation=confirmation)
            self.assertEqual(caught.exception.code, "HUMAN_CONFIRMATION_REQUIRED")
        confirmation = {
            "instruction": "Cancel this requirement.",
            "source": "User message in this conversation",
        }
        self.transition("todo", "cancelled", confirmation=confirmation)
        self.assertEqual(self.github.issues[1]["state_reason"], "not_planned")
        with self.assertRaises(RequirementError):
            self.transition("cancelled", "todo")
        self.assertEqual(
            self.transition(
                "cancelled",
                "todo",
                confirmation={
                    **confirmation,
                    "instruction": "Restore this requirement.",
                },
            )["status"],
            "todo",
        )

    def test_changed_acceptance_rejects_earlier_report(self):
        self.transition("todo", "doing")
        report = completion(self.service.get(1))
        revised = requirement()
        revised["acceptance"][0]["criterion"] = (
            "Required and optional columns are documented."
        )
        edited = self.edit(revised)
        self.assertEqual(edited["status"], "doing")
        count = len(self.github.comments[1])
        with self.assertRaises(RequirementError) as caught:
            self.transition("doing", "done", report=report)
        self.assertEqual(caught.exception.code, "INVALID_COMPLETION_REPORT")
        self.assertEqual(len(self.github.comments[1]), count)
        self.assertEqual(
            self.transition("doing", "done", report=completion(edited))["status"],
            "done",
        )

    def test_path_only_inline_evidence_cannot_finish_work(self):
        self.transition("todo", "doing")
        report = completion(self.service.get(1))
        count = len(self.github.comments[1])
        for path in (
            "/tmp/report.txt",
            "file:///tmp/report.txt",
            "./report.txt",
            "~/report.txt",
            r"C:\temp\report.txt",
            '"/tmp/report with spaces.txt"',
            "'C:\\Reports\\schema report.txt'",
        ):
            report["results"][0]["evidence"] = [{"kind": "inline", "content": path}]
            with self.subTest(path=path), self.assertRaises(RequirementError) as caught:
                self.transition("doing", "done", report=report)
            self.assertEqual(caught.exception.code, "INVALID_COMPLETION_REPORT")
        self.assertEqual(self.service.get(1)["status"], "doing")
        self.assertEqual(len(self.github.comments[1]), count)
        report["results"][0]["evidence"] = [
            {
                "kind": "inline",
                "content": "/tmp/report.txt contains three columns; all types match.",
            }
        ]
        self.assertEqual(
            self.transition("doing", "done", report=report)["status"], "done"
        )

    def test_timeout_after_state_write_confirms_readback_without_replay(self):
        self.github.fail = lambda method, path: method == "PATCH"
        self.assertEqual(self.transition("todo", "doing")["status"], "doing")
        count = len(self.github.comments[1])
        with self.assertRaises(RequirementError):
            self.transition("todo", "doing")
        self.assertEqual(len(self.github.comments[1]), count)

    def test_saved_comment_does_not_mean_state_changed(self):
        original = self.github.request

        def request(method, path, payload=None):
            if method == "PATCH":
                raise RequirementError("GITHUB_ERROR", "Forbidden", http_status=403)
            return original(method, path, payload)

        self.github.request = request
        with self.assertRaises(RequirementError) as caught:
            self.transition("todo", "doing")
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        self.assertTrue(caught.exception.details["audit_url"])
        self.assertEqual(self.service.get(1)["status"], "todo")

    def test_mutations_reject_non_pool_issues(self):
        self.github.issues[1]["labels"] = [{"name": "status:todo"}]
        before = deepcopy(self.github.issues)
        for operation in (
            lambda: self.transition("todo", "doing"),
            lambda: self.edit(requirement()),
        ):
            with self.assertRaises(RequirementError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "NOT_A_REQUIREMENT")
        self.assertEqual(self.github.issues, before)

    def test_done_edits_reopen_only_for_substantive_changes(self):
        self.transition("todo", "doing")
        self.transition("doing", "done", report=completion(self.service.get(1)))
        self.github.issues[1]["labels"].append({"name": "documentation"})
        revised = {
            **requirement(),
            "title": "Corrected title",
            "source": "Corrected source",
        }
        self.assertEqual(self.edit(revised)["status"], "done")
        revised["expected_outcome"] += " Include optional columns."
        self.assertEqual(self.edit(revised)["status"], "todo")
        self.assertIn({"name": "documentation"}, self.github.issues[1]["labels"])
        self.assertIn("registration:human", self.service.get(1)["labels"])
        self.transition(
            "todo",
            "cancelled",
            confirmation={"instruction": "Cancel this work.", "source": "User message"},
        )
        revised["problem"] = "The input reference is missing."
        self.assertEqual(self.edit(revised)["status"], "cancelled")
        self.assertIn("registration:human", self.service.get(1)["labels"])

    def test_legacy_requirement_remains_usable_without_inventing_registration_source(
        self,
    ):
        # Existing Issues lack evidence of who initiated registration; never guess.
        self.github.issues[1]["labels"] = [
            {"name": "requirement"},
            {"name": "status:todo"},
        ]
        expected_labels = {"requirement", "status:todo"}
        self.assertEqual(set(self.service.get(1)["labels"]), expected_labels)
        self.assertEqual(self.service.get(1)["decision"], "approved")
        self.assertEqual(
            set(self.service.list()["items"][0]["labels"]), expected_labels
        )
        edited = self.edit({**requirement(), "title": "Clarified legacy requirement"})
        self.assertEqual(set(edited["labels"]), expected_labels)
        claimed = self.transition("todo", "doing")
        self.assertEqual(set(claimed["labels"]), {"requirement", "status:doing"})

    def test_manual_amendment_is_readable_and_never_discarded(self):
        self.github.issues[1]["body"] += "\nManual note\n"
        original = self.github.issues[1]["body"]
        self.assertEqual(self.service.get(1)["body"], original)
        with self.assertRaises(RequirementError) as caught:
            self.edit(requirement())
        self.assertEqual(caught.exception.code, "INVALID_FORMAT")
        self.assertEqual(self.github.issues[1]["body"], original)

    def test_observed_change_during_comment_write_is_not_overwritten(self):
        original = self.github.request

        def request(method, path, payload=None):
            result = original(method, path, payload)
            if method == "POST" and path.endswith("/comments"):
                self.github.issues[1]["title"] = "Changed while writing the comment"
            return result

        self.github.request = request
        with self.assertRaises(RequirementError) as caught:
            self.transition("todo", "doing")
        self.assertEqual(caught.exception.code, "CONTENT_CONFLICT")
        self.assertTrue(caught.exception.details["audit_url"])
        self.assertEqual(self.service.get(1)["status"], "todo")


class PendingIdeaWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.service = Requirements(self.github)
        self.idea = requirement()
        self.idea.pop("acceptance")
        self.service.create(self.idea, registration_source="agent", decision="pending")

    def claim(self):
        return self.service.transition(
            1, from_status="todo", to_status="doing", reason="Start the approved work."
        )

    def approve(self, value=...):
        return self.service.edit(1, value, approve=True)

    def test_adding_acceptance_does_not_approve_an_idea_or_make_it_claimable(self):
        # Capture/edit must not turn an undecided idea into an executable commitment.
        for value in (self.idea, requirement()):
            with self.subTest(acceptance=value.get("acceptance")):
                edited = self.service.edit(1, value)
                self.assertEqual(
                    (edited["decision"], edited["status"]), ("pending", "todo")
                )
                before = deepcopy((self.github.issues, self.github.comments))
                with self.assertRaises(RequirementError) as caught:
                    self.claim()
                self.assertEqual(caught.exception.code, "STATE_CONFLICT")
                self.assertEqual((self.github.issues, self.github.comments), before)

    def test_approval_requires_acceptance_and_preserves_the_issue_through_completion(
        self,
    ):
        # Approval supplies an executable contract without losing the idea's history/source.
        before = deepcopy((self.github.issues, self.github.comments))
        for value in (self.idea, {**self.idea, "acceptance": []}):
            with (
                self.subTest(value=value),
                self.assertRaises(RequirementError) as caught,
            ):
                self.approve(value)
            self.assertEqual(caught.exception.code, "INVALID_REQUIREMENT")
            self.assertEqual((self.github.issues, self.github.comments), before)
        self.github.issues[1]["labels"].append({"name": "discussion"})
        approved = self.approve(requirement())
        self.assertEqual(
            (approved["number"], approved["decision"], approved["status"]),
            (1, "approved", "todo"),
        )
        self.assertEqual(
            set(approved["labels"]),
            {
                "✨ feature request",
                "requirement",
                "status:todo",
                "decision:approved",
                "registration:agent",
                "discussion",
            },
        )
        self.assertEqual(approved["requirement"], requirement())
        self.assertEqual(approved["comments"], [])
        self.assertNotIn("audit_url", approved)
        self.assertEqual(self.claim()["status"], "doing")
        finished = self.service.transition(
            1,
            from_status="doing",
            to_status="done",
            reason="All acceptance conditions passed.",
            report=completion(self.service.get(1)),
        )
        self.assertEqual(
            (finished["decision"], finished["status"]), ("approved", "done")
        )
        self.assertEqual(len(self.github.issues), 1)

    def test_approval_only_rejects_invalid_saved_content_and_states_without_writes(
        self,
    ):
        # An incomplete/manual/inconsistent saved Issue cannot become executable
        # just because the human approval flag was supplied.
        for scenario, code in (
            ("missing_acceptance", "INVALID_REQUIREMENT"),
            ("manual_body", "INVALID_FORMAT"),
            ("cancelled", "STATE_CONFLICT"),
            ("already_approved", "STATE_CONFLICT"),
            ("conflicting_decisions", "STATE_CONFLICT"),
            ("pending_doing", "STATE_CONFLICT"),
        ):
            with self.subTest(scenario=scenario):
                github = FakeGitHub()
                service = Requirements(github)
                service.create(
                    self.idea if scenario == "missing_acceptance" else requirement(),
                    registration_source="human",
                    decision="pending",
                )
                issue = github.issues[1]
                if scenario == "manual_body":
                    issue["body"] += "\nManual note\n"
                elif scenario == "cancelled":
                    issue.update(state="closed", state_reason="not_planned")
                    issue["labels"] = [
                        {"name": "requirement"},
                        {"name": "decision:pending"},
                        {"name": "status:cancelled"},
                    ]
                elif scenario == "already_approved":
                    issue["labels"] = [
                        {"name": "requirement"},
                        {"name": "decision:approved"},
                        {"name": "status:todo"},
                    ]
                elif scenario == "conflicting_decisions":
                    issue["labels"].append({"name": "decision:approved"})
                elif scenario == "pending_doing":
                    issue["labels"] = [
                        {"name": "requirement"},
                        {"name": "decision:pending"},
                        {"name": "status:doing"},
                    ]
                before = deepcopy((github.issues, github.comments))
                github.calls.clear()
                with self.assertRaises(RequirementError) as caught:
                    service.edit(1, approve=True)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual((github.issues, github.comments), before)
                self.assertFalse(any(method != "GET" for method, _, _ in github.calls))

    def test_approval_only_conflicts_and_uncertain_writes_require_confirmed_readback(
        self,
    ):
        # Approval requires a confirmed label update on the intended design.
        for scenario, expected in (
            ("forbidden", "WRITE_FAILED"),
            ("uncertain_unchanged", "WRITE_OUTCOME_UNKNOWN"),
            ("readback_unavailable", "WRITE_OUTCOME_UNKNOWN"),
            ("changed_before_update", "CONTENT_CONFLICT"),
            ("uncertain_applied", None),
        ):
            with self.subTest(scenario=scenario):
                github = FakeGitHub()
                service = Requirements(github)
                service.create(
                    requirement(), registration_source="agent", decision="pending"
                )
                github.calls.clear()
                original = github.request
                patch_attempted = False

                def request(method, path, payload=None):
                    nonlocal patch_attempted
                    if method == "PATCH" and path == "/issues/1":
                        patch_attempted = True
                        if scenario in ("forbidden", "uncertain_unchanged"):
                            raise RequirementError(
                                "GITHUB_ERROR",
                                "Update failed",
                                uncertain=scenario == "uncertain_unchanged",
                            )
                    if (
                        scenario == "readback_unavailable"
                        and patch_attempted
                        and method == "GET"
                    ):
                        raise RequirementError("GITHUB_ERROR", "Readback unavailable")
                    result = original(method, path, payload)
                    if scenario == "changed_before_update" and (method, path) == ("GET", ""):
                        github.issues[1]["body"] += "\nHuman changed the design\n"
                    if scenario == "uncertain_applied" and method == "PATCH":
                        raise RequirementError(
                            "GITHUB_ERROR", "Disconnected after update", uncertain=True
                        )
                    return result

                github.request = request
                if expected is None:
                    result = service.edit(1, approve=True)
                    self.assertEqual(
                        (result["decision"], result["status"]), ("approved", "todo")
                    )
                    self.assertNotIn("audit_url", result)
                else:
                    with self.assertRaises(RequirementError) as caught:
                        service.edit(1, approve=True)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn("audit_url", caught.exception.details)
                    if scenario != "readback_unavailable":
                        self.assertIn(
                            {"name": "decision:pending"}, github.issues[1]["labels"]
                        )
                self.assertEqual(github.comments[1], [])
                self.assertFalse(any(method == "POST" for method, _, _ in github.calls))
                if scenario == "changed_before_update":
                    self.assertFalse(patch_attempted)
                    self.assertTrue(
                        github.issues[1]["body"].endswith("Human changed the design\n")
                    )

    def test_cancelled_idea_must_be_restored_before_approval(self):
        # Approval must not silently undo a human cancellation, even with complete content.
        self.service.transition(
            1,
            from_status="todo",
            to_status="cancelled",
            reason="The user discarded this idea.",
            confirmation={
                "instruction": "Discard the idea.",
                "source": "Current conversation",
            },
        )
        before = deepcopy((self.github.issues, self.github.comments))
        with self.assertRaises(RequirementError) as caught:
            self.approve(requirement())
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")
        self.assertEqual((self.github.issues, self.github.comments), before)
        restored = self.service.transition(
            1,
            from_status="cancelled",
            to_status="todo",
            reason="The user wants to reconsider the idea.",
            confirmation={
                "instruction": "Restore the idea for discussion.",
                "source": "Current conversation",
            },
        )
        self.assertEqual(
            (restored["decision"], restored["status"]), ("pending", "todo")
        )
        self.assertEqual(restored["requirement"]["acceptance"], [])

    def test_failed_approval_keeps_issue_pending_and_comment_free(self):
        original = self.github.request

        def request(method, path, payload=None):
            if method == "PATCH" and path == "/issues/1":
                raise RequirementError("GITHUB_ERROR", "Forbidden", http_status=403)
            return original(method, path, payload)

        self.github.request = request
        with self.assertRaises(RequirementError) as caught:
            self.approve(requirement())
        self.assertEqual(caught.exception.code, "WRITE_FAILED")
        self.assertEqual(self.github.comments[1], [])
        self.assertNotIn("audit_url", caught.exception.details)
        self.assertEqual(self.service.get(1)["decision"], "pending")
        with self.assertRaises(RequirementError):
            self.claim()
