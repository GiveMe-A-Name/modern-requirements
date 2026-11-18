"""Plain Issue content and agent-verifiable completion contracts."""

from copy import deepcopy
import unittest

from modern_requirements.cli import (
    RequirementError,
    issue_decision,
    parse_body,
    render_body,
    validate_report,
    validate_requirement,
)
from tests.requirements_fixtures import completion, requirement


class RequirementContractTests(unittest.TestCase):
    def test_plain_markdown_roundtrip_keeps_multiline_and_headings(self):
        value = requirement()
        value["problem"] = (
            "First line\n\n## This is content\n`$(command)` 中文 source name"
        )
        body = render_body(value)
        self.assertEqual(parse_body(value["title"], body), value)
        self.assertNotIn("<!--", body)
        # The hidden metadata marker appended by 631c5a5 is no longer a format.
        for suffix in (
            "\nManual note\n",
            "\n<!-- requirement-pool:v1 eyJzY2hlbWEiOjF9 -->\n",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(RequirementError):
                parse_body(value["title"], body + suffix)

    def test_required_fields_and_no_extra_identifiers_or_questions(self):
        for change in (
            {"problem": ""},
            {"acceptance": []},
            {"questions": []},
            {"request_id": "extra"},
            {"acceptance": [{**requirement()["acceptance"][0], "id": "AC-1"}]},
        ):
            with self.subTest(change=change), self.assertRaises(RequirementError):
                validate_requirement({**requirement(), **change})

    def test_pending_ideas_roundtrip_without_acceptance_but_approved_work_requires_it(
        self,
    ):
        # Capture a real idea without fabricating acceptance or losing its source.
        for source in (None, "Current conversation"):
            for acceptance in (None, []):
                with self.subTest(source=source, acceptance=acceptance):
                    value = requirement()
                    value.pop("source")
                    value.pop("acceptance")
                    if source is not None:
                        value["source"] = source
                    if acceptance is not None:
                        value["acceptance"] = acceptance
                    pending = validate_requirement(value, decision="pending")
                    self.assertEqual(pending, {**value, "acceptance": []})
                    self.assertEqual(
                        parse_body(
                            pending["title"], render_body(pending), decision="pending"
                        ),
                        pending,
                    )
                    with self.assertRaises(RequirementError):
                        validate_requirement(value, decision="approved")
                    with self.assertRaises(RequirementError):
                        parse_body(
                            pending["title"], render_body(pending), decision="approved"
                        )

    def test_pending_acceptance_when_supplied_must_still_be_well_formed(self):
        # Pending exempts absent criteria, not malformed criteria or missing core content.
        for change in (
            {"acceptance": None},
            {"acceptance": [{"criterion": "Incomplete criterion"}]},
            {"problem": ""},
        ):
            with self.subTest(change=change), self.assertRaises(RequirementError):
                validate_requirement({**requirement(), **change}, decision="pending")
        self.assertEqual(
            validate_requirement(requirement(), decision="pending"), requirement()
        )

    def test_decision_labels_do_not_guess_between_conflicting_or_unknown_values(self):
        self.assertEqual(issue_decision({"labels": []}), "approved")
        for names in (
            ["decision:pending", "decision:approved"],
            ["decision:unrecognized"],
        ):
            with (
                self.subTest(names=names),
                self.assertRaises(RequirementError) as caught,
            ):
                issue_decision({"labels": [{"name": name} for name in names]})
            self.assertEqual(caught.exception.code, "STATE_CONFLICT")

    def test_completion_requires_current_full_coverage_and_durable_evidence(self):
        detail = {"number": 4, "requirement": requirement()}
        report = completion(detail)
        self.assertEqual(validate_report(report, 4, requirement()), report)
        for mutate in (
            lambda x: x.update(issue_number=5),
            # 05e8f76 required a content hash; reports now bind to the Issue only.
            lambda x: x.update(content_version="0" * 64),
            lambda x: x.update(results=[]),
            lambda x: x["results"][0].update(result="unknown"),
            lambda x: x["results"][0].update(criterion="Different condition"),
            lambda x: x["results"][0].update(actual_result=""),
            lambda x: x["results"][0].update(
                evidence=[
                    {"kind": "url", "url": "/tmp/results", "description": "Local path"}
                ]
            ),
            lambda x: x.update(repository_changes=True),
        ):
            broken = deepcopy(report)
            mutate(broken)
            with (
                self.subTest(broken=broken),
                self.assertRaises(RequirementError) as caught,
            ):
                validate_report(broken, 4, requirement())
            self.assertEqual(caught.exception.code, "INVALID_COMPLETION_REPORT")
            self.assertEqual(caught.exception.exit_code, 1)

    def test_report_cannot_duplicate_one_condition_and_omit_another(self):
        value = requirement()
        value["acceptance"].append(
            {
                "criterion": "Optional columns are identified.",
                "verification": "Compare optional columns with the schema.",
                "pass_condition": "Every optional column is listed.",
            }
        )
        detail = {"number": 1, "requirement": value}
        report = completion(detail)
        report["results"][1] = deepcopy(report["results"][0])
        with self.assertRaises(RequirementError):
            validate_report(report, 1, value)
