"""Public JSON commands, plain Issue storage, and executable built-in help."""

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modern_requirements.cli import (
    main,
    parse_body,
    render_body,
    Requirements,
    validate_requirement,
)
from tests.requirements_fixtures import FakeGitHub, completion, requirement


class RequirementsCliTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.service = Requirements(self.github)

    def cli(self, args, value=None):
        out, err = StringIO(), StringIO()
        input_bytes = json.dumps(value, ensure_ascii=False).encode("utf-8-sig")
        with (
            TextIOWrapper(BytesIO(input_bytes)) as stdin,
            patch("modern_requirements.cli.Requirements", return_value=self.service),
            patch("sys.stdin", stdin),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_create_and_get_use_only_native_issue_identity(self):
        # Either registration source must survive storage and be visible to readers.
        for number, source in enumerate(("human", "agent"), 1):
            with self.subTest(source=source):
                code, out, err = self.cli(
                    [
                        "create",
                        "--input",
                        "-",
                        "--registration-source",
                        source,
                        "--decision",
                        "approved",
                    ],
                    requirement(),
                )
                self.assertEqual((code, err), (0, ""))
                created = json.loads(out)
                self.assertEqual(created["number"], number)
                self.assertEqual(created["requirement"], requirement())
                self.assertNotIn("<!--", created["body"])
                self.assertFalse(
                    {"request_id", "operation_id", "content_version", "metadata"}
                    & created.keys()
                )
                expected_labels = {
                    "✨ feature request",
                    "requirement",
                    "status:todo",
                    "decision:approved",
                    f"registration:{source}",
                }
                self.assertEqual(set(created["labels"]), expected_labels)
                self.assertEqual(
                    {label["name"] for label in self.github.issues[number]["labels"]},
                    expected_labels,
                )
                code, out, err = self.cli(["get", str(number)])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out)["body"], created["body"])
                self.assertEqual(set(json.loads(out)["labels"]), expected_labels)
        code, out, err = self.cli(["list"])
        self.assertEqual((code, err), (0, ""))
        items = json.loads(out)["items"]
        self.assertIn("registration:human", items[0]["labels"])
        self.assertIn("registration:agent", items[1]["labels"])

    def test_invalid_input_and_usage_never_write(self):
        for args, value in (
            (
                [
                    "create",
                    "--input",
                    "-",
                    "--registration-source",
                    "human",
                    "--decision",
                    "approved",
                ],
                {},
            ),
            # Missing provenance must not silently classify an Issue as human or agent.
            (["create", "--input", "-", "--decision", "approved"], requirement()),
            (
                ["create", "--input", "-", "--registration-source", "human"],
                requirement(),
            ),
            (
                [
                    "create",
                    "--input",
                    "-",
                    "--registration-source",
                    "human",
                    "--decision",
                    "unknown",
                ],
                requirement(),
            ),
            (
                [
                    "create",
                    "--input",
                    "-",
                    "--registration-source",
                    "unknown",
                    "--decision",
                    "approved",
                ],
                requirement(),
            ),
            (["edit", "1", "--reason", "Revise."], None),
            (["get", "0"], None),
            (["create"], None),
            (["list", "--limit", "0"], None),
        ):
            code, out, err = self.cli(args, value)
            self.assertEqual((code, out), (2, ""))
            self.assertFalse(json.loads(err)["ok"])
        self.assertEqual(self.github.calls, [])

    def test_rejected_extra_identity_and_version_flags(self):
        for args in (
            [
                "create",
                "--input",
                "-",
                "--registration-source",
                "human",
                "--decision",
                "approved",
                "--request-id",
                "extra",
            ],
            ["edit", "1", "--input", "-", "--reason", "Revise.", "--if-version", "old"],
            [
                "transition",
                "1",
                "--from",
                "todo",
                "--to",
                "doing",
                "--reason",
                "Start.",
                "--session",
                "extra",
            ],
            [
                "transition",
                "1",
                "--from",
                "todo",
                "--to",
                "doing",
                "--reason",
                "Start.",
                "--operation-id",
                "extra",
            ],
        ):
            code, out, err = self.cli(args, requirement())
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(json.loads(err)["code"], "USAGE_ERROR")
        self.assertEqual(self.github.calls, [])

    def test_domain_error_is_json(self):
        code, out, err = self.cli(["get", "99"])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["code"], "NOT_FOUND")

    def test_transition_report_stdin_and_confirmation_error(self):
        self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        code, out, err = self.cli(
            [
                "transition",
                "1",
                "--from",
                "todo",
                "--to",
                "doing",
                "--reason",
                "Start verification.",
            ]
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["status"], "doing")
        code, out, err = self.cli(
            [
                "transition",
                "1",
                "--from",
                "doing",
                "--to",
                "done",
                "--reason",
                "Every condition passed.",
                "--report",
                "-",
            ],
            completion(self.service.get(1)),
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["status"], "done")
        self.service.transition(
            1, from_status="done", to_status="todo", reason="Recheck the report."
        )
        code, out, err = self.cli(
            [
                "transition",
                "1",
                "--from",
                "todo",
                "--to",
                "cancelled",
                "--reason",
                "Cancel requested.",
            ]
        )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["code"], "HUMAN_CONFIRMATION_REQUIRED")

    def test_file_create_list_and_complete_replacement_edit(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "requirement.json"
            source.write_text(json.dumps(requirement()))
            code, out, err = self.cli(
                [
                    "create",
                    "--input",
                    str(source),
                    "--registration-source",
                    "agent",
                    "--decision",
                    "approved",
                ]
            )
            self.assertEqual((code, err), (0, ""))
            code, out, err = self.cli(
                ["list", "--status", "todo", "--query", "Document", "--limit", "10"]
            )
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["items"][0]["number"], 1)
            source.write_text(
                json.dumps({**requirement(), "title": "Document every input column"})
            )
            code, out, err = self.cli(
                ["edit", "1", "--input", str(source), "--reason", "Clarify the title."]
            )
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(
                json.loads(out)["title"], "[Feature]: Document every input column"
            )
            self.assertIn("registration:agent", json.loads(out)["labels"])

    def test_create_matches_modern_js_feature_template_and_preserves_unicode(self):
        value = {
            **requirement(),
            "title": "Include both routes in conflict diagnostics",
            "problem": "路径冲突时没有显示文件名。",
            "expected_outcome": "List both files.\n\n```ts\nconst route = '/café'\n```",
        }
        code, out, err = self.cli(
            [
                "create", "--input", "-", "--registration-source", "human",
                "--decision", "approved",
            ],
            value,
        )
        self.assertEqual((code, err), (0, ""))
        out.encode("ascii")
        created = json.loads(out)
        self.assertEqual(created["title"], "[Feature]: " + value["title"])
        self.assertEqual(created["requirement"]["problem"], value["problem"])
        self.assertIn("## What problem does this feature solve?\n", created["body"])
        self.assertIn("## What does the proposed API look like?\n", created["body"])
        self.assertIn("> const route = '/café'\n", created["body"])
        self.assertIn("✨ feature request", created["labels"])

    def command_help(self, command=None):
        out, err = StringIO(), StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            main([*([command] if command else []), "--help"])
        self.assertEqual((caught.exception.code, err.getvalue()), (0, ""))
        return out.getvalue()

    def test_root_routes_to_independently_usable_operation_help(self):
        # Users choosing an operation should not need to read every other
        # operation's formats; each leaf must contain its own necessary contract.
        root = self.command_help()
        operations = ("create", "get", "list", "edit", "transition")
        for detail in (
            '"expected_outcome":',
            "--registration-source",
            "Completion reports",
        ):
            self.assertNotIn(detail, root)
        concepts = {
            "create": (
                "--input",
                "--registration-source",
                "--decision",
                "expected_outcome",
                "acceptance",
                "WRITE_OUTCOME_UNKNOWN",
            ),
            "get": (
                "--comment-limit",
                "--comment-cursor",
                "comment_cursor",
                "format_error",
                "state_reason",
                "NOT_FOUND",
            ),
            "list": (
                "--query",
                "--cursor",
                "--limit",
                "items",
                "status_error",
                "case-insensitive",
            ),
            "edit": (
                "--approve",
                "--input",
                "complete replacement",
                "expected_outcome",
                "CONTENT_CONFLICT",
                "without rewriting content",
            ),
            "transition": (
                "--from",
                "--to",
                "--report",
                "--confirmation",
                "repository_changes",
                "INVALID_COMPLETION_REPORT",
                "HUMAN_CONFIRMATION_REQUIRED",
            ),
        }
        for operation in operations:
            route = f"modern-requirements {operation} --help"
            self.assertIn(route, root)
            text = self.command_help(operation)
            for concept in (
                *concepts[operation],
                "Prerequisites:",
                "github.com",
                "stdout JSON",
                "stderr JSON",
                "exit 2",
            ):
                self.assertIn(concept, text, operation)
            self.assertNotIn("modern-requirements --help for", text)
            if operation != "transition":
                self.assertNotIn("Completion reports", text)
                self.assertNotIn("Confirmation JSON example", text)
            if operation in ("get", "list", "transition"):
                self.assertNotIn("Requirement JSON example", text)
            if operation != "create":
                self.assertNotIn("--registration-source", text)
        self.assertEqual(self.github.calls, [], "Help must not contact GitHub")

    def test_help_workflow_commands_and_json_examples(self):
        # Execute examples at their new leaf locations, with each documented
        # initial state supplied by an offline fixture, not by unrelated help.
        help_text = "\n\n".join(
            self.command_help(command)
            for command in ("create", "get", "list", "edit", "transition")
        )
        values = []
        end = 0
        decoder = json.JSONDecoder()
        for match in re.finditer(r"(?m)^[ \t]*\{", help_text):
            start = match.end() - 1
            if start < end:
                continue
            try:
                value, length = decoder.raw_decode(help_text[start:])
            except json.JSONDecodeError:
                continue
            values.append(value)
            end = start + length
        examples = {}
        for value in values:
            if {"title", "problem", "expected_outcome", "acceptance"} <= value.keys():
                examples.setdefault("requirement", value)
            elif {"title", "problem", "expected_outcome"} <= value.keys():
                examples.setdefault("idea", value)
            if {"issue_number", "repository_changes", "results"} <= value.keys():
                examples.setdefault("report", value)
            if {"instruction", "source"} <= value.keys():
                examples.setdefault("confirmation", value)
        self.assertEqual(
            set(examples), {"requirement", "idea", "report", "confirmation"}
        )
        for name in ("requirement", "idea"):
            decision = "pending" if name == "idea" else "approved"
            value = validate_requirement(examples[name], decision=decision)
            self.assertEqual(
                parse_body(value["title"], render_body(value), decision=decision), value
            )
        commands = [
            shlex.split(line.strip())[1:]
            for line in help_text.splitlines()
            if line.strip().startswith("modern-requirements ")
        ]
        self.assertTrue(commands)
        with TemporaryDirectory() as tmp:
            files = {
                name: Path(tmp) / name
                for name in (
                    "requirement.json",
                    "idea.json",
                    "completion.json",
                    "confirmation.json",
                )
            }
            files["requirement.json"].write_text(json.dumps(examples["requirement"]))
            files["idea.json"].write_text(json.dumps(examples["idea"]))
            for args in commands:
                if "CURSOR" in args:
                    # Continuation placeholders are tested with actual returned
                    # cursors below, never with an invented token.
                    continue
                number = 1
                if args[0] not in ("create", "list"):
                    pending = args[0] == "edit"
                    value = (
                        examples["idea"]
                        if pending and "--input" in args
                        else examples["requirement"]
                    )
                    number = self.service.create(
                        value,
                        registration_source="human",
                        decision="pending" if pending else "approved",
                    )["number"]
                if args[0] == "transition":
                    source = args[args.index("--from") + 1]
                    if source in ("doing", "blocked"):
                        self.service.transition(
                            number,
                            from_status="todo",
                            to_status="doing",
                            reason="Synthetic approved example setup.",
                        )
                    if source == "blocked":
                        self.service.transition(
                            number,
                            from_status="doing",
                            to_status="blocked",
                            reason="Synthetic missing schema. Restore it, then compare columns.",
                        )
                    if source == "cancelled":
                        self.service.transition(
                            number,
                            from_status="todo",
                            to_status="cancelled",
                            reason="Synthetic human cancellation.",
                            confirmation=examples["confirmation"],
                        )
                files["completion.json"].write_text(
                    json.dumps({**examples["report"], "issue_number": number})
                )
                confirmation = examples["confirmation"]
                if args[0] == "transition" and source == "cancelled":
                    confirmation = {
                        "instruction": "Restore the synthetic requirement for reconsideration.",
                        "source": "The controlled fixture's human restoration instruction.",
                    }
                files["confirmation.json"].write_text(json.dumps(confirmation))
                argv = [
                    str(files[arg])
                    if arg in files
                    else str(number)
                    if arg == "123"
                    else arg
                    for arg in args
                ]
                code, out, err = self.cli(argv)
                self.assertEqual((code, err), (0, ""), argv)
                self.assertTrue(json.loads(out)["ok"])

    def test_documented_cursor_examples_continue_the_same_read(self):
        # A copied continuation command must preserve Issue/filter/limit context
        # and use the cursor the reader actually received, rather than restart.
        number = self.service.create(
            requirement(), registration_source="human", decision="approved"
        )["number"]
        for index in range(31):
            self.github.request(
                "POST", f"/issues/{number}/comments", {"body": f"Discussion {index}"}
            )
        for _ in range(31):
            self.service.create(
                requirement(), registration_source="human", decision="approved"
            )
        for operation, cursor_key in (("get", "comment_cursor"), ("list", "cursor")):
            commands = [
                shlex.split(line.strip())[1:]
                for line in self.command_help(operation).splitlines()
                if line.strip().startswith("modern-requirements ")
            ]
            first, continuation = (
                commands[0],
                next(args for args in commands if "CURSOR" in args),
            )
            code, out, err = self.cli(
                [str(number) if arg == "123" else arg for arg in first]
            )
            self.assertEqual((code, err), (0, ""))
            cursor = json.loads(out)[cursor_key]
            self.assertIsNotNone(cursor)
            argv = [
                cursor if arg == "CURSOR" else str(number) if arg == "123" else arg
                for arg in continuation
            ]
            code, out, err = self.cli(argv)
            self.assertEqual((code, err), (0, ""))
            result = json.loads(out)
            self.assertIsNone(result[cursor_key])
            self.assertEqual(
                len(result["comments"] if operation == "get" else result["items"]),
                1 if operation == "get" else 2,
            )

    def test_cli_collects_and_approves_an_idea_without_changing_its_identity_or_source(
        self,
    ):
        # Public flags must reach storage and the execution gate, not just parse.
        self.service.create(
            requirement(), registration_source="human", decision="approved"
        )
        idea = requirement()
        idea.pop("acceptance")
        code, out, err = self.cli(
            [
                "create",
                "--input",
                "-",
                "--registration-source",
                "agent",
                "--decision",
                "pending",
            ],
            idea,
        )
        self.assertEqual((code, err), (0, ""))
        created = json.loads(out)
        self.assertEqual((created["number"], created["decision"]), (2, "pending"))
        self.assertEqual(created["requirement"]["acceptance"], [])
        code, out, err = self.cli(["list", "--decision", "pending"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([item["number"] for item in json.loads(out)["items"]], [2])
        code, out, err = self.cli(
            [
                "transition",
                "2",
                "--from",
                "todo",
                "--to",
                "doing",
                "--reason",
                "Start work.",
            ],
        )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["code"], "STATE_CONFLICT")
        self.github.calls.clear()
        code, out, err = self.cli(
            [
                "edit",
                "2",
                "--input",
                "-",
                "--approve",
                "--reason",
                "The user decided to proceed.",
            ],
            requirement(),
        )
        self.assertEqual((code, err), (0, ""))
        approved = json.loads(out)
        self.assertEqual(
            (approved["number"], approved["decision"], approved["status"]),
            (2, "approved", "todo"),
        )
        self.assertIn("registration:agent", approved["labels"])
        self.assertNotIn("decision:pending", approved["labels"])
        self.assertEqual(approved["comments"], [])
        self.assertNotIn("audit_url", approved)
        self.assertFalse(any(method == "POST" for method, _, _ in self.github.calls))

    def test_iterative_design_edits_then_approval_only_preserve_current_specification(
        self,
    ):
        idea = {**requirement(), "acceptance": []}
        self.service.create(idea, registration_source="agent", decision="pending")
        self.github.issues[1]["labels"].append({"name": "discussion"})
        self.github.request(
            "POST", "/issues/1/comments", {"body": "Existing discussion from a maintainer."}
        )
        discussion = self.service.get(1)["comments"]
        refined = requirement()
        for include_optional_columns in (False, True):
            refined["expected_outcome"] = (
                "Document research columns in an Issue comment.\n\n"
                "## Confirmed basic design\n"
                + (
                    "Include required and optional columns."
                    if include_optional_columns
                    else "Include required columns."
                )
            )
            refined["acceptance"][0]["pass_condition"] = (
                "Every required and optional column has a name, type, and meaning."
                if include_optional_columns
                else "Every required column has a name, type, and meaning."
            )
            self.github.calls.clear()
            code, out, err = self.cli(["edit", "1", "--input", "-"], refined)
            self.assertEqual((code, err), (0, ""))
            edited = json.loads(out)
            self.assertEqual(edited["decision"], "pending")
            self.assertEqual(edited["requirement"], refined)
            self.assertEqual(edited["comments"], discussion)
            self.assertNotIn("audit_url", edited)
            self.assertFalse(any(method == "POST" for method, _, _ in self.github.calls))
            code, out, err = self.cli(
                [
                    "transition",
                    "1",
                    "--from",
                    "todo",
                    "--to",
                    "doing",
                    "--reason",
                    "Start.",
                ]
            )
            self.assertEqual(
                (code, out, json.loads(err)["code"]), (1, "", "STATE_CONFLICT")
            )
        saved = self.service.get(1)
        self.github.calls.clear()
        code, out, err = self.cli(["edit", "1", "--approve"])
        self.assertEqual((code, err), (0, ""))
        approved = json.loads(out)
        self.assertEqual(
            (approved["number"], approved["decision"], approved["status"]),
            (1, "approved", "todo"),
        )
        self.assertEqual(approved["requirement"], refined)
        self.assertEqual(
            (approved["title"], approved["body"]), (saved["title"], saved["body"])
        )
        self.assertEqual(approved["requirement"]["source"], idea["source"])
        payloads = [
            payload
            for method, path, payload in self.github.calls
            if method == "PATCH" and path == "/issues/1"
        ]
        self.assertEqual(len(payloads), 1)
        # Payload shape is an explicit requirement: approval must not incidentally
        # replace title/body or send a lifecycle operation.
        self.assertEqual(set(payloads[0]), {"labels"})
        self.assertEqual(
            set(payloads[0]["labels"]),
            {
                "✨ feature request",
                "requirement",
                "registration:agent",
                "decision:approved",
                "status:todo",
                "discussion",
            },
        )
        self.assertEqual(approved["comments"], discussion)
        self.assertNotIn("audit_url", approved)
        self.assertFalse(any(method == "POST" for method, _, _ in self.github.calls))
        code, out, err = self.cli(["get", "1"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            (json.loads(out)["decision"], json.loads(out)["status"]),
            ("approved", "todo"),
        )

    def test_explicit_null_or_partial_replacement_is_not_approval_only(self):
        # Omitted input selects saved content; a supplied invalid JSON value
        # must not silently approve something different from the user's input.
        self.service.create(
            requirement(), registration_source="human", decision="pending"
        )
        before = self.service.get(1)
        for approve in ([], ["--approve"]):
            for value in (None, {"title": "Partial replacement"}):
                with self.subTest(approve=approve, value=value):
                    self.github.calls.clear()
                    code, out, err = self.cli(
                        ["edit", "1", "--input", "-", "--reason", "Revise.", *approve],
                        value,
                    )
                    self.assertEqual(
                        (code, out, json.loads(err)["code"]),
                        (2, "", "INVALID_REQUIREMENT"),
                    )
                    self.assertFalse(
                        any(method != "GET" for method, _, _ in self.github.calls)
                    )
                    self.assertEqual(self.service.get(1), before)

    def test_all_help_works_in_real_process_without_gh_or_database(self):
        env = {**os.environ, "PATH": "", "DB_PATH": "/missing"}
        for command in ("", "create", "list", "get", "edit", "transition"):
            args = [
                sys.executable,
                "-m",
                "modern_requirements.cli",
                *([command] if command else []),
                "--help",
            ]
            result = subprocess.run(
                args,
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout)
            self.assertEqual(result.stderr, "")
