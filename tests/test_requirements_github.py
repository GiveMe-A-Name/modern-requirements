"""Repository binding, REST paging, pool boundaries, and transport failures."""

from copy import deepcopy
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from modern_requirements.cli import (
    GhGitHub,
    LABELS,
    REPOSITORY,
    RequirementError,
    Requirements,
)
from tests.requirements_fixtures import FakeGitHub, requirement


class RequirementsGitHubTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.service = Requirements(self.github)

    def test_transport_fixes_repository_host_and_preserves_literal_payload(self):
        value = {"body": "$(secret) `command`\nNew line"}
        with (
            patch.dict(
                os.environ, {"GH_REPO": "other/repo", "GH_HOST": "other.example"}
            ),
            patch("modern_requirements.cli.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, '{"number":1}', "")
            self.assertEqual(
                GhGitHub().request("POST", "/issues", value), {"number": 1}
            )
        args, kwargs = run.call_args
        self.assertIn(f"repos/{REPOSITORY}/issues", args[0])
        self.assertEqual(args[0][args[0].index("--hostname") + 1], "github.com")
        self.assertEqual(json.loads(kwargs["input"]), value)
        self.assertNotIn("GH_REPO", kwargs["env"])
        self.assertNotIn("GH_HOST", kwargs["env"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertNotIn("shell", kwargs)

    def test_uncertain_creation_never_automatically_repeats_the_write(self):
        self.github.fail = lambda method, route: method == "POST" and route == "/issues"
        with self.assertRaises(RequirementError) as caught:
            self.service.create(
                requirement(), registration_source="human", decision="approved"
            )
        self.assertEqual(caught.exception.code, "WRITE_OUTCOME_UNKNOWN")
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(
            sum(
                method == "POST" and route == "/issues"
                for method, route, _ in self.github.calls
            ),
            1,
        )
        self.assertEqual(self.service.list()["items"][0]["number"], 1)

    def test_only_fixed_labels_are_provisioned_and_permission_is_required(self):
        self.github.permissions = {"push": False}
        with self.assertRaises(RequirementError) as caught:
            self.service.create(
                requirement(), registration_source="human", decision="approved"
            )
        self.assertEqual(caught.exception.code, "GITHUB_PERMISSION")
        self.assertEqual(set(self.github.labels), {"✨ feature request"})
        self.assertFalse(self.github.issues)
        self.github.permissions["push"] = True
        self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        self.assertEqual(set(self.github.labels), {*LABELS, "✨ feature request"})

    def test_creation_does_not_report_success_when_a_classification_label_is_missing(
        self,
    ):
        # Readback must confirm both labels, including explicit approval on new Issues.
        original = self.github.request

        def request(method, path, payload=None):
            result = original(method, path, payload)
            if method == "POST" and path == "/issues":
                issue = self.github.issues[result["number"]]
                issue["labels"] = [
                    label for label in issue["labels"] if label["name"] != missing_label
                ]
            return result

        self.github.request = request
        for number, missing_label in enumerate(
            ("registration:agent", "decision:approved", "✨ feature request"), 1
        ):
            with (
                self.subTest(missing_label=missing_label),
                self.assertRaises(RequirementError) as caught,
            ):
                self.service.create(
                    requirement(), registration_source="agent", decision="approved"
                )
            self.assertEqual(caught.exception.code, "WRITE_OUTCOME_UNKNOWN")
            self.assertEqual(caught.exception.details["number"], number)
            self.assertEqual(len(self.github.issues), number)

    def test_create_keeps_existing_repository_label_descriptions(self):
        label = {
            "name": "registration:agent",
            "color": "123456",
            "description": "A repository maintainer's description",
        }
        self.github.labels["registration:agent"] = deepcopy(label)
        self.service.create(
            requirement(), registration_source="agent", decision="pending"
        )
        self.assertEqual(self.github.labels["registration:agent"], label)

    def test_missing_feature_label_is_reported_before_any_remote_write(self):
        self.github.labels.clear()
        with self.assertRaises(RequirementError) as caught:
            self.service.create(
                requirement(), registration_source="human", decision="approved"
            )
        self.assertEqual(caught.exception.code, "GITHUB_CONFIGURATION")
        self.assertTrue(all(method == "GET" for method, _, _ in self.github.calls))

    def test_edit_without_write_access_does_not_post_a_comment(self):
        created = self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        self.github.permissions["push"] = False
        self.github.calls.clear()
        with self.assertRaises(RequirementError) as caught:
            self.service.edit(created["number"], requirement())
        self.assertEqual(caught.exception.code, "GITHUB_PERMISSION")
        self.assertTrue(all(method == "GET" for method, _, _ in self.github.calls))

    def test_decision_filter_keeps_paging_and_legacy_approved_work_visible(self):
        self.service.create(
            requirement(), registration_source="agent", decision="pending"
        )
        self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        self.github.issues[2]["labels"] = [
            label
            for label in self.github.issues[2]["labels"]
            if not label["name"].startswith("decision:")
        ]
        first = self.service.list(decision="approved", limit=1)
        self.assertEqual(first["items"], [])
        second = self.service.list(decision="approved", limit=1, cursor=first["cursor"])
        self.assertEqual(
            [(item["number"], item["decision"]) for item in second["items"]],
            [(2, "approved")],
        )
        self.assertEqual(
            [item["number"] for item in self.service.list(decision="pending")["items"]],
            [1],
        )
        with self.assertRaises(RequirementError) as caught:
            self.service.list(decision="pending", limit=1, cursor=first["cursor"])
        self.assertEqual(caught.exception.code, "INVALID_CURSOR")

    def test_bounded_list_filters_membership_search_and_cursor(self):
        self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        self.service.create(
            {**requirement(), "title": "Another requirement"},
            registration_source="agent",
            decision="approved",
        )
        ordinary = deepcopy(self.github.issues[1])
        ordinary.update(
            number=3, title="Ordinary Issue", labels=[{"name": "status:todo"}]
        )
        self.github.issues[3] = ordinary
        first = self.service.list(query="Another", limit=1)
        self.assertEqual(first["items"], [])
        second = self.service.list(query="Another", limit=1, cursor=first["cursor"])
        self.assertEqual([item["number"] for item in second["items"]], [2])
        with self.assertRaises(RequirementError):
            self.service.list(query="Changed", limit=1, cursor=first["cursor"])
        self.assertEqual(
            [item["number"] for item in self.service.list()["items"]], [1, 2]
        )
        with self.assertRaises(RequirementError) as caught:
            self.service.get(3)
        self.assertEqual(caught.exception.code, "NOT_A_REQUIREMENT")
        self.github.issues[1]["pull_request"] = {}
        with self.assertRaises(RequirementError) as caught:
            self.service.get(1)
        self.assertEqual(caught.exception.code, "NOT_A_REQUIREMENT")
        self.assertEqual(len(self.service.list()["items"]), 1)

    def test_get_returns_original_malformed_body_and_paged_comments(self):
        self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        self.github.issues[1]["body"] += "\nManual text must survive\n"
        for text in ("First comment", "Second comment"):
            self.github.request("POST", "/issues/1/comments", {"body": text})
        detail = self.service.get(1, comment_limit=1)
        self.assertEqual(detail["body"], self.github.issues[1]["body"])
        self.assertIsNone(detail["requirement"])
        self.assertEqual(detail["format_error"]["code"], "INVALID_FORMAT")
        self.assertEqual(detail["comments"][0]["author"], "agent")
        second = self.service.get(
            1, comment_limit=1, comment_cursor=detail["comment_cursor"]
        )
        self.assertEqual(second["comments"][0]["body"], "Second comment")
        with self.assertRaises(RequirementError):
            self.service.get(
                1, comment_limit=2, comment_cursor=detail["comment_cursor"]
            )

    def test_status_mismatch_is_explicit(self):
        self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        # Auto-close permits doing/completed only, not unclaimed or cancelled work.
        for status, reason in (("todo", None), ("todo", "completed"), ("doing", "not_planned")):
            with self.subTest(status=status, reason=reason):
                issue = self.github.issues[1]
                issue.update(state="closed", state_reason=reason)
                issue["labels"] = [label for label in issue["labels"]
                                   if not label["name"].startswith("status:")]
                issue["labels"].append({"name": f"status:{status}"})
                with self.assertRaises(RequirementError) as caught:
                    self.service.get(1)
                self.assertEqual(caught.exception.code, "STATE_CONFLICT")
                item = self.service.list()["items"][0]
                self.assertEqual(item["status_error"]["code"], "STATE_CONFLICT")
                self.assertIn("registration:human", item["labels"])
