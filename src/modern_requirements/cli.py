"""Submit and manage structured feature requests for Modern.js."""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlsplit

REPOSITORY = "web-infra-dev/modern.js"
FEATURE_LABEL = "✨ feature request"
FEATURE_TITLE_PREFIX = "[Feature]:"
PROBLEM_HEADING = "## What problem does this feature solve?"
OUTCOME_HEADING = "## What does the proposed API look like?"
POOL_LABEL = "requirement"
REGISTRATION_SOURCES = ("human", "agent")
DECISIONS = ("pending", "approved")
STATUSES = ("todo", "doing", "blocked", "done", "cancelled")
LABELS = {
    POOL_LABEL: ("5319e7", "Agent requirement pool membership"),
    "registration:human": ("0075ca", "Registration explicitly requested by a human"),
    "registration:agent": (
        "d4c5f9",
        "Registration initiated by an agent",
    ),
    "decision:pending": ("fbca04", "Idea collected; no decision to proceed yet"),
    "decision:approved": ("0e8a16", "Human has decided to proceed"),
    **{
        f"status:{status}": (color, f"Requirement status: {status}")
        for status, color in zip(
            STATUSES, ("c5def5", "fbca04", "d93f0b", "0e8a16", "bfc4c9"), strict=True
        )
    },
}
TRANSITIONS = {
    "todo": {"doing", "cancelled"},
    "doing": {"blocked", "todo", "done", "cancelled"},
    "blocked": {"doing", "todo", "cancelled"},
    "done": {"todo"},
    "cancelled": {"todo"},
}


class RequirementError(Exception):
    """A public error with stable code, exit status, and reconciliation details."""

    def __init__(self, code: str, message: str, *, exit_code: int = 1, **details: Any):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details

    def payload(self) -> dict:
        return {"ok": False, "code": self.code, "message": str(self), **self.details}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, field: str, code: str = "INVALID_REQUIREMENT") -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequirementError(code, f"{field} must be a nonempty string", exit_code=2)
    return value.strip()


def validate_requirement(value: Any, *, decision: str = "approved") -> dict:
    """Check structure; the submitting agent checks meaning and English prose."""
    if decision not in DECISIONS:
        raise RequirementError("USAGE_ERROR", "Unknown decision", exit_code=2)
    required = {"title", "problem", "expected_outcome"}
    if decision == "approved":
        required.add("acceptance")
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - {"source", "acceptance"}
    ):
        raise RequirementError(
            "INVALID_REQUIREMENT",
            "Expected title, problem, expected_outcome, optional source, and acceptance (required when approved)",
            exit_code=2,
        )
    result = {
        name: _text(value[name], name)
        for name in ("title", "problem", "expected_outcome")
    }
    if "\n" in result["title"] or "\r" in result["title"]:
        raise RequirementError(
            "INVALID_REQUIREMENT", "title must occupy one line", exit_code=2
        )
    acceptance = value.get("acceptance", [])
    if not isinstance(acceptance, list) or (decision == "approved" and not acceptance):
        raise RequirementError(
            "INVALID_REQUIREMENT",
            "acceptance must be a list, nonempty when approved",
            exit_code=2,
        )
    result["acceptance"] = []
    for item in acceptance:
        if not isinstance(item, dict) or set(item) != {
            "criterion",
            "verification",
            "pass_condition",
        }:
            raise RequirementError(
                "INVALID_REQUIREMENT",
                "Each acceptance item requires criterion, verification, and pass_condition only",
                exit_code=2,
            )
        result["acceptance"].append(
            {
                key: _text(item[key], key)
                for key in ("criterion", "verification", "pass_condition")
            }
        )
    if len({_json(item) for item in result["acceptance"]}) != len(result["acceptance"]):
        raise RequirementError(
            "INVALID_REQUIREMENT",
            "Duplicate acceptance items are ambiguous",
            exit_code=2,
        )
    if "source" in value:
        result["source"] = _text(value["source"], "source")
    return result


def _quote(value: str) -> str:
    return "\n".join("> " + line for line in value.split("\n"))


def _feature_title(title: str) -> str:
    title = _text(title.removeprefix(FEATURE_TITLE_PREFIX), "title")
    return f"{FEATURE_TITLE_PREFIX} {title}"


def render_body(requirement: dict) -> str:
    """Render human-readable Markdown without a hidden copy of business fields."""
    body = (
        f"{PROBLEM_HEADING}\n\n{_quote(requirement['problem'])}\n\n"
        f"{OUTCOME_HEADING}\n\n{_quote(requirement['expected_outcome'])}\n\n"
        "## Acceptance\n"
    )
    for item in requirement["acceptance"]:
        body += "\n### Condition\n"
        for key, heading in (
            ("criterion", "Criterion"),
            ("verification", "Verification"),
            ("pass_condition", "Pass condition"),
        ):
            body += f"\n#### {heading}\n\n{_quote(item[key])}\n"
    if "source" in requirement:
        body += f"\n## Source\n\n{_quote(requirement['source'])}\n"
    return body


def parse_body(title: str, body: str, *, decision: str = "approved") -> dict:
    content = body
    # Quoted fields cannot introduce section delimiters. Exact round-trip checking
    # ensures edits never discard unsupported manual text.
    blocks = re.findall(
        r"^(#{2,4} [^\n]+)\n\n((?:> [^\n]*\n?)+)", content, re.MULTILINE
    )
    fields = [
        (heading, "\n".join(line[2:] for line in text.rstrip("\n").split("\n")))
        for heading, text in blocks
    ]
    try:
        if [heading for heading, _ in fields[:2]] != [
            PROBLEM_HEADING,
            OUTCOME_HEADING,
        ]:
            raise ValueError("missing sections")
        value = {
            "title": title,
            "problem": fields[0][1],
            "expected_outcome": fields[1][1],
            "acceptance": [],
        }
        remainder = fields[2:]
        if remainder and remainder[-1][0] == "## Source":
            value["source"] = remainder.pop()[1]
        if len(remainder) % 3:
            raise ValueError("missing acceptance")
        for start in range(0, len(remainder), 3):
            group = remainder[start : start + 3]
            if [heading for heading, _ in group] != [
                "#### Criterion",
                "#### Verification",
                "#### Pass condition",
            ]:
                raise ValueError("invalid acceptance")
            value["acceptance"].append(
                dict(
                    zip(
                        ("criterion", "verification", "pass_condition"),
                        (text for _, text in group),
                        strict=True,
                    )
                )
            )
        value = validate_requirement(value, decision=decision)
        if render_body(value) != content:
            raise ValueError("unsupported text or formatting")
    except (ValueError, RequirementError) as error:
        raise RequirementError(
            "INVALID_FORMAT",
            "Body does not match the supported Markdown format; original body is preserved",
        ) from error
    return value


def issue_decision(issue: dict) -> str:
    """Unlabelled legacy requirements were approved under the original contract."""
    decisions = [
        label["name"].removeprefix("decision:")
        for label in issue.get("labels", [])
        if label["name"].startswith("decision:")
    ]
    if not decisions:
        return "approved"
    if len(decisions) != 1 or decisions[0] not in DECISIONS:
        raise RequirementError(
            "STATE_CONFLICT", "Requirement needs one supported decision label"
        )
    return decisions[0]


def issue_status(issue: dict) -> str:
    if "pull_request" in issue:
        raise RequirementError(
            "NOT_A_REQUIREMENT", "Pull requests are not requirements"
        )
    names = [label["name"] for label in issue.get("labels", [])]
    if POOL_LABEL not in names:
        raise RequirementError(
            "NOT_A_REQUIREMENT", "Issue lacks the requirement membership label"
        )
    statuses = [
        name.removeprefix("status:") for name in names if name.startswith("status:")
    ]
    if len(statuses) != 1 or statuses[0] not in STATUSES:
        raise RequirementError(
            "STATE_CONFLICT", "Requirement needs exactly one supported status label"
        )
    status = statuses[0]
    if issue_decision(issue) == "pending" and status not in ("todo", "cancelled"):
        raise RequirementError(
            "STATE_CONFLICT", "Pending ideas cannot be claimed, blocked, or completed"
        )
    expected_state = "closed" if status in ("done", "cancelled") else "open"
    expected_reason = {"done": "completed", "cancelled": "not_planned"}.get(status)
    # A merged closing PR may close the Issue before the agent records acceptance.
    # Keep it in doing until the ordinary completion-report gate succeeds.
    if (
        status == "doing"
        and issue["state"] == "closed"
        and issue.get("state_reason") == "completed"
    ):
        return status
    if issue["state"] != expected_state or (
        expected_reason and issue.get("state_reason") != expected_reason
    ):
        raise RequirementError(
            "STATE_CONFLICT", "Status label and native GitHub state disagree"
        )
    return status


def _cursor(context: dict, position: int) -> str:
    return base64.urlsafe_b64encode(
        _json({"context": context, "position": position}).encode()
    ).decode()


def _position(cursor: str | None, context: dict) -> int:
    if cursor is None:
        return 0
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        position = value["position"]
        if value["context"] != context or type(position) is not int or position < 0:
            raise ValueError("cursor context differs")
        return position
    except (ValueError, TypeError, KeyError) as error:
        raise RequirementError(
            "INVALID_CURSOR",
            "Cursor must belong to the same command, Issue, filters, and limit",
            exit_code=2,
        ) from error


def _limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise RequirementError(
            "USAGE_ERROR", "limit must be between 1 and 100", exit_code=2
        )


def _evidence(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise RequirementError(
            "INVALID_COMPLETION_REPORT", "Each result needs durable evidence"
        )
    for item in value:
        if not isinstance(item, dict):
            raise RequirementError(
                "INVALID_COMPLETION_REPORT",
                "Evidence must contain inline content or an HTTPS URL",
            )
        if item.get("kind") == "inline" and set(item) == {"kind", "content"}:
            content = _text(
                item["content"], "evidence.content", "INVALID_COMPLETION_REPORT"
            )
            quoted = (
                len(content) >= 2 and content[0] in "\"'" and content[-1] == content[0]
            )
            candidate = content[1:-1] if quoted else content
            if re.fullmatch(
                r"(?:file://|/|~/|\./|\.\./|[A-Za-z]:[\\/])"
                + (r"[^\n]*" if quoted else r"\S*"),
                candidate,
            ):
                raise RequirementError(
                    "INVALID_COMPLETION_REPORT",
                    "A local path alone is not inline evidence; include actual observations or a durable HTTPS reference",
                )
        elif item.get("kind") == "url" and set(item) == {"kind", "url", "description"}:
            try:
                parsed = urlsplit(
                    _text(item["url"], "evidence.url", "INVALID_COMPLETION_REPORT")
                )
            except ValueError as error:
                raise RequirementError(
                    "INVALID_COMPLETION_REPORT", "Invalid evidence URL"
                ) from error
            if parsed.scheme != "https" or not parsed.netloc:
                raise RequirementError(
                    "INVALID_COMPLETION_REPORT",
                    "Evidence links must be durable HTTPS URLs",
                )
            _text(
                item["description"], "evidence.description", "INVALID_COMPLETION_REPORT"
            )
        else:
            raise RequirementError(
                "INVALID_COMPLETION_REPORT",
                "Use inline content or a described HTTPS evidence URL",
            )


def validate_report(report: Any, number: int, requirement: dict) -> dict:
    """Require complete current coverage; the agent remains responsible for truth."""
    try:
        required = {"issue_number", "results", "repository_changes"}
        if (
            not isinstance(report, dict)
            or not required <= report.keys()
            or report.keys() - required - {"checks", "pull_requests"}
        ):
            raise RequirementError(
                "INVALID_COMPLETION_REPORT",
                "Report requires issue_number, results, and repository_changes",
            )
        if type(report["issue_number"]) is not int or report["issue_number"] != number:
            raise RequirementError(
                "INVALID_COMPLETION_REPORT",
                "Report references a different Issue",
            )
        results = report["results"]
        if not isinstance(results, list) or len(results) != len(
            requirement["acceptance"]
        ):
            raise RequirementError(
                "INVALID_COMPLETION_REPORT",
                "Report must cover every acceptance item exactly once",
            )
        covered = []
        for item in results:
            if not isinstance(item, dict) or set(item) != {
                "criterion",
                "verification",
                "pass_condition",
                "result",
                "actual_result",
                "evidence",
            }:
                raise RequirementError(
                    "INVALID_COMPLETION_REPORT",
                    "Each result must repeat the acceptance item and include result, actual_result, and evidence",
                )
            covered.append(
                {
                    key: item[key]
                    for key in ("criterion", "verification", "pass_condition")
                }
            )
            if item["result"] != "passed":
                raise RequirementError(
                    "INVALID_COMPLETION_REPORT", "All acceptance results must be passed"
                )
            _text(item["actual_result"], "actual_result", "INVALID_COMPLETION_REPORT")
            _evidence(item["evidence"])
        if sorted(map(_json, covered)) != sorted(map(_json, requirement["acceptance"])):
            raise RequirementError(
                "INVALID_COMPLETION_REPORT",
                "Report acceptance content differs or contains duplicates",
            )
        if type(report["repository_changes"]) is not bool:
            raise RequirementError(
                "INVALID_COMPLETION_REPORT", "repository_changes must be a boolean"
            )
        if report["repository_changes"]:
            checks = report.get("checks")
            pulls = report.get("pull_requests")
            if (
                not isinstance(checks, list)
                or not checks
                or not isinstance(pulls, list)
                or not pulls
            ):
                raise RequirementError(
                    "INVALID_COMPLETION_REPORT",
                    "Repository changes require passing checks and merged pull_requests",
                )
            for check in checks:
                if (
                    not isinstance(check, dict)
                    or set(check) != {"command", "result", "actual_result", "evidence"}
                    or check["result"] != "passed"
                ):
                    raise RequirementError(
                        "INVALID_COMPLETION_REPORT",
                        "Every applicable check must pass and include its result and evidence",
                    )
                _text(check["command"], "checks.command", "INVALID_COMPLETION_REPORT")
                _text(
                    check["actual_result"],
                    "checks.actual_result",
                    "INVALID_COMPLETION_REPORT",
                )
                _evidence(check["evidence"])
            if any(type(number) is not int or number < 1 for number in pulls):
                raise RequirementError(
                    "INVALID_COMPLETION_REPORT",
                    "pull_requests must contain positive PR numbers from this repository",
                )
        elif "checks" in report or "pull_requests" in report:
            raise RequirementError(
                "INVALID_COMPLETION_REPORT",
                "Set repository_changes to true when reporting repository checks or pull requests",
            )
    except RequirementError as error:
        error.exit_code = 1
        raise
    return deepcopy(report)


class GhGitHub:
    """A direct gh api boundary with fixed repository and hostname."""

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        command = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            method,
            f"repos/{REPOSITORY}{path}",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        if payload is not None:
            command += ["--input", "-"]
        env = dict(os.environ)
        env.pop("GH_REPO", None)
        env.pop("GH_HOST", None)
        env.pop("GH_DEBUG", None)
        env["GH_PROMPT_DISABLED"] = "1"
        try:
            result = subprocess.run(
                command,
                input=_json(payload) if payload is not None else None,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            raise RequirementError(
                "GITHUB_ERROR", "GitHub request timed out", uncertain=method != "GET"
            ) from error
        except OSError as error:
            raise RequirementError("GITHUB_ERROR", f"Cannot run gh: {error}") from error
        if result.returncode:
            match = re.search(r"\(HTTP (\d+)\)", result.stderr)
            http_status = int(match[1]) if match else None
            raise RequirementError(
                "NOT_FOUND" if http_status == 404 else "GITHUB_ERROR",
                result.stderr.strip() or "GitHub request failed",
                http_status=http_status,
                uncertain=method != "GET"
                and (http_status is None or http_status >= 500),
            )
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise RequirementError(
                "GITHUB_ERROR",
                "GitHub returned invalid JSON",
                uncertain=method != "GET",
            ) from error


class Requirements:
    """Requirement operations with an injectable GitHub request boundary."""

    def __init__(self, github: Any = None):
        self.github = github if github is not None else GhGitHub()

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        return self.github.request(method, path, payload)

    def _all(self, path: str, **params: Any):
        page = 1
        while True:
            values = self._request(
                "GET", path + "?" + urlencode({**params, "per_page": 100, "page": page})
            )
            yield from values
            if len(values) < 100:
                break
            page += 1

    def _require_write_access(self) -> None:
        repo = self._request("GET", "")
        permissions = repo.get("permissions", {})
        if not any(permissions.get(name) for name in ("push", "maintain", "admin")):
            raise RequirementError(
                "GITHUB_PERMISSION",
                "Push permission is required to create or update managed requirements",
            )
    def ensure_labels(self) -> None:
        """Check the template label and add any missing requirement labels."""
        self._require_write_access()
        existing = {label["name"] for label in self._all("/labels")}
        if FEATURE_LABEL not in existing:
            raise RequirementError(
                "GITHUB_CONFIGURATION", f"Modern.js is missing its {FEATURE_LABEL!r} label"
            )
        for name, (color, description) in LABELS.items():
            if name not in existing:
                self._request(
                    "POST",
                    "/labels",
                    {"name": name, "color": color, "description": description},
                )

    def _issue(self, number: int) -> dict:
        issue = self._request("GET", f"/issues/{number}")
        issue_status(issue)
        return issue

    def get(
        self, number: int, *, comment_limit: int = 30, comment_cursor: str | None = None
    ) -> dict:
        _limit(comment_limit)
        context = {"command": "comments", "number": number, "limit": comment_limit}
        position = _position(comment_cursor, context)
        issue = self._issue(number)
        result = {
            "number": number,
            "url": issue["html_url"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "status": issue_status(issue),
            "state": issue["state"],
            "state_reason": issue.get("state_reason"),
            "decision": issue_decision(issue),
            "labels": [label["name"] for label in issue["labels"]],
            "requirement": None,
        }
        try:
            result["requirement"] = parse_body(
                issue["title"], result["body"], decision=result["decision"]
            )
        except RequirementError as error:
            result["format_error"] = error.payload()
        comments = self._request(
            "GET",
            f"/issues/{number}/comments?"
            + urlencode({"per_page": comment_limit, "page": position + 1}),
        )
        result["comments"] = [
            {
                "id": comment["id"],
                "body": comment["body"],
                "author": comment["user"]["login"],
                "created_at": comment["created_at"],
                "updated_at": comment["updated_at"],
                "url": comment["html_url"],
            }
            for comment in comments
        ]
        result["comment_cursor"] = (
            _cursor(context, position + 1) if len(comments) == comment_limit else None
        )
        return result

    def list(
        self,
        *,
        status: str | None = None,
        decision: str | None = None,
        query: str = "",
        limit: int = 30,
        cursor: str | None = None,
    ) -> dict:
        """Scan one bounded REST page; cursors remain bound to the original filters."""
        _limit(limit)
        if status is not None and status not in STATUSES:
            raise RequirementError("USAGE_ERROR", "Unknown status", exit_code=2)
        if decision is not None and decision not in DECISIONS:
            raise RequirementError("USAGE_ERROR", "Unknown decision", exit_code=2)
        context = {
            "command": "list",
            "status": status,
            "decision": decision,
            "query": query,
            "limit": limit,
        }
        position = _position(cursor, context)
        values = self._request(
            "GET",
            "/issues?"
            + urlencode(
                {
                    "state": "all",
                    "labels": POOL_LABEL,
                    "sort": "created",
                    "direction": "asc",
                    "per_page": limit,
                    "page": position + 1,
                }
            ),
        )
        items = []
        for issue in values:
            if "pull_request" in issue or POOL_LABEL not in [
                label["name"] for label in issue.get("labels", [])
            ]:
                continue
            try:
                current = issue_status(issue)
                current_decision = issue_decision(issue)
            except RequirementError as error:
                items.append(
                    {
                        "number": issue["number"],
                        "title": issue["title"],
                        "url": issue["html_url"],
                        "labels": [label["name"] for label in issue["labels"]],
                        "status_error": error.payload(),
                    }
                )
                continue
            if decision is not None and current_decision != decision:
                continue
            if status is not None and current != status:
                continue
            if status is None and current not in ("todo", "doing", "blocked"):
                continue
            if (
                query.casefold()
                not in (issue["title"] + "\n" + (issue.get("body") or "")).casefold()
            ):
                continue
            items.append(
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "url": issue["html_url"],
                    "status": current,
                    "state": issue["state"],
                    "state_reason": issue.get("state_reason"),
                    "decision": current_decision,
                    "labels": [label["name"] for label in issue["labels"]],
                }
            )
        return {
            "items": items,
            "cursor": _cursor(context, position + 1) if len(values) == limit else None,
        }

    def create(self, value: Any, *, registration_source: str, decision: str) -> dict:
        if registration_source not in REGISTRATION_SOURCES:
            raise RequirementError(
                "USAGE_ERROR", "registration_source must be human or agent", exit_code=2
            )
        requirement = validate_requirement(value, decision=decision)
        requirement["title"] = _feature_title(requirement["title"])
        self.ensure_labels()
        body = render_body(requirement)
        registration_label = f"registration:{registration_source}"
        decision_label = f"decision:{decision}"
        try:
            issue = self._request(
                "POST",
                "/issues",
                {
                    "title": requirement["title"],
                    "body": body,
                    "labels": [
                        FEATURE_LABEL,
                        POOL_LABEL,
                        "status:todo",
                        registration_label,
                        decision_label,
                    ],
                },
            )
        except RequirementError as error:
            if not error.details.get("uncertain"):
                raise
            raise RequirementError(
                "WRITE_OUTCOME_UNKNOWN",
                "Creation may have succeeded. List and inspect the pool before creating again; creation is not automatically retried.",
            ) from error
        try:
            result = self.get(issue["number"])
            if (
                result["status"] != "todo"
                or result["decision"] != decision
                or decision_label not in result["labels"]
                or FEATURE_LABEL not in result["labels"]
                or result["body"] != body
                or result["title"] != requirement["title"]
                or {
                    label
                    for label in result["labels"]
                    if label.startswith("registration:")
                }
                != {registration_label}
            ):
                raise RequirementError("STATE_CONFLICT", "Creation readback differs")
        except RequirementError as error:
            raise RequirementError(
                "WRITE_OUTCOME_UNKNOWN",
                "Issue was created but readback did not confirm it; inspect this Issue before retrying",
                number=issue.get("number"),
                url=issue.get("html_url"),
            ) from error
        return result

    def _post_transition_comment(self, number: int, event: dict) -> str:
        title = "需求完成报告" if event["to"] == "done" else "需求状态更新"
        body = (
            f"## {title}\n\n"
            f"状态流转：{event['from']} → {event['to']}\n\n"
            f"原因：{event['reason']}\n"
        )
        try:
            comment = self._request(
                "POST", f"/issues/{number}/comments", {"body": body}
            )
        except RequirementError as error:
            if not error.details.get("uncertain"):
                raise
            raise RequirementError(
                "WRITE_OUTCOME_UNKNOWN",
                "The comment may have been saved. The Issue update was not attempted; inspect its comments before retrying",
                number=number,
            ) from error
        return comment["html_url"]

    def _apply(
        self,
        issue: dict,
        requirement: dict | None,
        target: str,
        *,
        approve: bool = False,
        transition_event: dict | None = None,
    ) -> dict:
        """Apply the Issue update and confirm it by reading the Issue back."""
        self._require_write_access()
        number = issue["number"]
        audit = {}
        if transition_event is not None:
            audit["audit_url"] = self._post_transition_comment(number, transition_event)
        try:
            latest = self._issue(number)
        except RequirementError as error:
            raise RequirementError(
                "WRITE_FAILED",
                "Pre-update check failed before sending the Issue update",
                number=number,
                cause=error.payload(),
                **audit,
            ) from error
        if any(
            latest.get(key) != issue.get(key)
            for key in ("title", "body", "labels", "state", "state_reason")
        ):
            raise RequirementError(
                "CONTENT_CONFLICT",
                "Issue changed before the update. Inspect it before retrying",
                number=number,
                **audit,
            )
        labels = [
            label["name"]
            for label in issue["labels"]
            if not label["name"].startswith("status:")
        ]
        if approve:
            labels = [label for label in labels if not label.startswith("decision:")]
            labels.append("decision:approved")
        # Approval of saved content only changes labels; do not incidentally
        # replace its title/body or send a lifecycle operation.
        payload = {"labels": [*labels, f"status:{target}"]}
        if requirement is not None:
            payload.update(
                title=requirement["title"],
                body=render_body(requirement),
                state="closed" if target in ("done", "cancelled") else "open",
                state_reason={"done": "completed", "cancelled": "not_planned"}.get(
                    target, "reopened"
                ),
            )
        try:
            self._request("PATCH", f"/issues/{number}", payload)
        except RequirementError as error:
            if not error.details.get("uncertain"):
                raise RequirementError(
                    "WRITE_FAILED",
                    "Issue update failed",
                    number=number,
                    cause=error.payload(),
                    **audit,
                ) from error
        try:
            observed = self._issue(number)
            if (
                observed["title"] != payload.get("title", issue["title"])
                or observed["body"] != payload.get("body", issue["body"])
                or issue_status(observed) != target
                or {label["name"] for label in observed["labels"]}
                != set(payload["labels"])
            ):
                raise RequirementError(
                    "STATE_CONFLICT", "Readback differs from the requested update"
                )
            result = self.get(number)
        except RequirementError as error:
            raise RequirementError(
                "WRITE_OUTCOME_UNKNOWN",
                "Could not confirm the Issue update. Inspect its current state before retrying",
                number=number,
                **audit,
            ) from error
        return {**result, **audit}

    def transition(
        self,
        number: int,
        *,
        from_status: str,
        to_status: str,
        reason: str,
        report: Any = None,
        confirmation: Any = None,
    ) -> dict:
        reason = _text(reason, "reason")
        issue = self._issue(number)
        decision = issue_decision(issue)
        requirement = parse_body(
            issue["title"], issue.get("body") or "", decision=decision
        )
        current = issue_status(issue)
        if current != from_status:
            code = (
                "ALREADY_CLAIMED"
                if from_status == "todo"
                and to_status == "doing"
                and current in ("doing", "blocked")
                else "STATE_CONFLICT"
            )
            raise RequirementError(code, f"Expected {from_status}, found {current}")
        if to_status not in TRANSITIONS[current]:
            raise RequirementError(
                "STATE_CONFLICT", f"Cannot transition {current} to {to_status}"
            )
        if decision == "pending" and to_status not in ("todo", "cancelled"):
            raise RequirementError(
                "STATE_CONFLICT",
                "Approve the idea and supply acceptance before claiming work",
            )
        needs_confirmation = to_status == "cancelled" or current == "cancelled"
        if needs_confirmation:
            if (
                not isinstance(confirmation, dict)
                or set(confirmation) != {"instruction", "source"}
                or any(
                    not isinstance(v, str) or not v.strip()
                    for v in confirmation.values()
                )
            ):
                raise RequirementError(
                    "HUMAN_CONFIRMATION_REQUIRED",
                    "Record the human's explicit instruction and its source",
                )
        elif confirmation is not None:
            raise RequirementError(
                "INVALID_INPUT",
                "confirmation is only used for cancellation or restoration",
                exit_code=2,
            )
        if to_status == "done":
            report = validate_report(report, number, requirement)
            if report["repository_changes"]:
                for pull_number in report["pull_requests"]:
                    pull = self._request("GET", f"/pulls/{pull_number}")
                    if (
                        not pull.get("merged")
                        or not pull.get("merged_at")
                        or pull.get("base", {}).get("ref") != "main"
                        or pull.get("base", {})
                        .get("repo", {})
                        .get("full_name", "")
                        .casefold()
                        != REPOSITORY.casefold()
                    ):
                        raise RequirementError(
                            "INVALID_COMPLETION_REPORT",
                            f"PR #{pull_number} must be merged into this repository's main",
                        )
        elif report is not None:
            raise RequirementError(
                "INVALID_INPUT", "report is only used when finishing", exit_code=2
            )
        event = {
            "from": current,
            "to": to_status,
            "reason": reason,
        }
        return self._apply(issue, requirement, to_status, transition_event=event)

    def edit(self, number: int, value: Any = ..., *, approve: bool = False) -> dict:
        # Ellipsis distinguishes omitted input from explicit JSON null, which
        # must still fail the complete-replacement input contract.
        if value is ... and not approve:
            raise RequirementError(
                "USAGE_ERROR",
                "edit requires --input unless --approve is used",
                exit_code=2,
            )
        issue = self._issue(number)
        decision = issue_decision(issue)
        current = issue_status(issue)
        if approve and (decision != "pending" or current != "todo"):
            raise RequirementError(
                "STATE_CONFLICT",
                "Approval requires a pending idea in todo; restore cancelled ideas first",
            )
        if value is ...:
            previous = parse_body(
                issue["title"], issue.get("body") or "", decision=decision
            )
            validate_requirement(previous, decision="approved")
            return self._apply(issue, None, current, approve=True)

        replacement = validate_requirement(
            value, decision="approved" if approve else decision
        )
        replacement["title"] = _feature_title(replacement["title"])
        previous = parse_body(
            issue["title"], issue.get("body") or "", decision=decision
        )
        substantive = any(
            previous[key] != replacement[key]
            for key in ("problem", "expected_outcome", "acceptance")
        )
        target = "todo" if current == "done" and substantive else current
        return self._apply(issue, replacement, target, approve=approve)


class JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RequirementError("USAGE_ERROR", message, exit_code=2)


_PREREQUISITES_HELP = """Prerequisites:
  Install GitHub CLI and run gh auth login --hostname github.com.
  Requests target web-infra-dev/modern.js on github.com. Create, edit, and
  transition require repository push access. Help is available offline.
"""

_REQUIREMENT_FIELDS_HELP = """Requirement fields:
  title: concise, nonempty English title on one line. The CLI adds [Feature]:
    to the Issue title. You can also include the prefix in your input.
  problem: current background, problem, or need.
  expected_outcome: observable result, applicable scenarios, boundaries, and
    confirmed design, including the proposed API or configuration when relevant. Readable
    design paragraphs or embedded headings belong in this existing text field;
    canonical Markdown quotes field lines so headings remain content.
    No separate design field, mandatory implementation plan, or design state.
  acceptance: list; each item has criterion, verification, and pass_condition.
    It must be nonempty for approved work. For pending ideas it may be omitted
    or empty; omission is normalized to an empty list in stored/read content.
    Every supplied item must be complete, verifiable, and judgeable.
  source: optional nonempty, concise origin description or reference, not a
    transcript appended after each discussion round.
  title, problem, and expected_outcome are always required. Extra fields are
  rejected. Decision and registration source are labels, not JSON fields.
  No acceptance IDs, separate requirement IDs, scope, or questions fields.
  The GitHub Issue number is the requirement identifier.
  Clarify missing required information with the human before creation;
  pending ideas do not need invented acceptance conditions.
  English content may retain original code names, paths, and source names.
  The CLI checks structure; the agent checks meaning, completeness, English,
  actual evidence, applicable checks, and real human authorization.
"""

_REGISTRATION_HELP = """Registration source:
  create requires --registration-source human or --registration-source agent.
  human: the human explicitly asked to register the requirement, even when
    an agent runs the command on their behalf.
  agent: the agent initiates registration without a separate human instruction
    to register it. This can be a pending idea or already approved work.
  This records who initiated registration, not who runs the CLI or owns the
  GitHub account. The caller supplies the value; the CLI cannot verify intent.
  The corresponding registration:human or registration:agent label is the
  only stored classification. The requirement JSON format stays unchanged;
  source remains an optional description or link, not this classification.
  No default is inferred: missing or invalid selection is a usage error.
  Older requirements without a registration label remain unclassified;
  reading, editing, and status transitions do not assign one retroactively.
  Edits and transitions preserve registration labels.
"""

_DECISION_HELP = """Decision and progress:
  decision:pending means the human has not decided to pursue an idea; acceptance
  may be absent. Pending ideas can only be todo or cancelled and cannot be claimed.
  decision:approved means the human decided to proceed and acceptance is complete.
  status:* records progress independently; approved todo work is claimable, but
  approval alone never claims or starts work. Ordinary edits preserve decision,
  even when adding complete acceptance. Only start work after doing is confirmed.
  Older Issues without decision labels are read as approved under the original
  contract and still require complete acceptance. Reads, edits and transitions
  do not retroactively add decision or registration labels.
  Unknown/conflicting decision labels or pending doing/blocked/done states fail
  with STATE_CONFLICT (list exposes them as status_error entries).
"""

_CREATE_HELP = """Creating:
  --decision pending or approved and --registration-source human or agent are
  both required, independent choices with no inferred defaults. Supply the
  actual human decision for approved work; pending ideas need no invented
  acceptance. Creation starts at todo and reads the Issue back.
  The Issue uses the Modern.js feature request template headings and label.
  Creation adds any missing requirement, status, registration and decision
  labels to the repository. A readback verifies the Issue and its labels.
  --input is a UTF-8 JSON file or - for stdin, following the fields below.
  Bad JSON/files fail with INVALID_INPUT; invalid fields with INVALID_REQUIREMENT.
  Missing label permissions fail with GITHUB_PERMISSION. WRITE_OUTCOME_UNKNOWN
  means creation may have succeeded: list/search and inspect matching Issues
  before creating again. The CLI never automatically repeats uncertain creation.
"""

_APPROVAL_HELP = """Approval:
  To approve the latest saved pending todo idea with complete acceptance, use
  edit NUMBER --approve without --input. It changes the decision
  label without replacing the saved title, body, or source. Use --input as well
  only when the human approves actual content changes as a complete replacement.
  Both paths record approval on the decision label. Claim work after readback
  confirms approval. Obtain the human decision before using --approve.
  Restore cancelled ideas before approval.
  Non-pending/non-todo approval is STATE_CONFLICT. Incomplete saved acceptance
  is INVALID_REQUIREMENT; unsupported canonical body text is INVALID_FORMAT.
"""

_STORAGE_HELP = """Storage and pool membership:
  The requirement label identifies pool membership; status:* records progress.
  Existing registration and unrelated labels are preserved by mutations.
  Direct-number commands reject ordinary Issues and pull requests.
  The readable Markdown body is canonical. Quoted field lines support embedded
  headings. Requirements use only the Issue number, body, labels, and comments.
  Unsupported manual text remains readable in get.body with format_error;
  mutations fail without replacing it.
"""

_GET_HELP = """Reading and comment paging:
  NUMBER is the positive GitHub Issue number of a requirement, not a PR or an
  ordinary Issue. Read the full current requirement and relevant discussion
  before claiming, editing or finishing work.
  Unsupported manual body text is preserved in body, with requirement=null and
  format_error. Non-pool Issues/PRs fail with NOT_A_REQUIREMENT; missing Issues
  are NOT_FOUND. Invalid cursors fail with INVALID_CURSOR.
  --comment-limit is 1..100 (default 30). If comment_cursor is non-null, pass it
  as --comment-cursor for the same NUMBER and limit until it is null. A full last
  page may have an empty continuation page. Pages are live, not snapshots.
  status is the progress label, not a guess from native GitHub closure. A doing
  Issue may be closed/completed after a closing PR while acceptance bookkeeping
  is pending; it remains doing. Other status/state mismatches are STATE_CONFLICT.
"""

_LIST_HELP = """Filtering, output and paging:
  Defaults to todo, doing and blocked across both decisions. --status filters
  one progress label; --decision filters pending or approved. Use approved/todo
  to find claimable work. --query is a case-insensitive substring of the title or
  original body. --limit bounds Issues scanned per REST page (1..100, default 30),
  not the number of matched results. items can be empty while cursor is non-null.
  Continue with --cursor and the SAME filters and limit until cursor is null.
  Pages are live, not snapshots. Read an Issue and discussion before claiming it.
  Output: items and cursor. Each valid item has number, title, url, status, state,
  state_reason, decision and labels (GitHub label names). Inconsistent entries
  instead have number, title, url, labels and status_error; they are not claimable.
  Pull requests and non-pool Issues are skipped. Native state is not progress:
  a doing Issue closed/completed by a PR stays active until acceptance is recorded.
"""

_STATES_HELP = """States and claims:
  todo -> doing, cancelled
  doing -> blocked, todo, done, cancelled
  blocked -> doing, todo, cancelled
  done -> todo (reason explains reopening)
  cancelled -> todo (requires the human's restoration instruction)
  Creation starts at todo. Doing means claimed; blocked keeps work reserved.
  Only approved work with complete acceptance may enter doing. Other agents
  choose approved todo work. Release to todo makes approved work available again.
  Record reasons in English. For done, use one concise sentence naming the
  merged PRs (if any) and confirming all acceptance conditions passed; do not
  repeat acceptance details, evidence, or check output in the reason.
  Block/release reasons include the obstacle, progress, and next action for
  handoff. All transition comments use Markdown with only a title, status
  transition, and reason. Done uses the title "需求完成报告"; other transitions
  use "需求状态更新". Keep reasons concise and do not paste input JSON.
  Active statuses normally map to open Issues; done to closed/completed;
  cancelled to closed/not_planned. A doing Issue may also be closed/completed
  after GitHub auto-close, while acceptance bookkeeping is still pending.
  It stays doing and appears in active list results until its report passes.
  This does not prove a PR merged or acceptance passed. Other inconsistent
  combinations, including doing with closed/not_planned, still fail.
  Claims use read/check/write/readback. Start only after doing is confirmed.
  This version assumes agents do not simultaneously read the same todo and
  both write doing. There is no lock, lease, queue, or atomic CAS.
  NUMBER is a positive requirement Issue number. --from must match current
  progress and --to must be an allowed transition; conflicts are STATE_CONFLICT.
  Claiming work already doing/blocked fails with ALREADY_CLAIMED.
  --report is only for done; --confirmation only for cancel/restore. Other use
  is INVALID_INPUT. Only one file argument may use stdin (-), otherwise USAGE_ERROR.
"""

_DESIGN_RECORDING_HELP = """Discuss, record, then approve:
  Discuss the basic problem and design with the human. Record current confirmed
  conclusions in problem/expected_outcome and verifiable acceptance in the body.
  Do not present unsettled business decisions as confirmed design. Pending ideas
  may leave acceptance empty; ordinary edits adding design and complete acceptance
  stay pending until the human explicitly approves them.
  The body holds the current specification. Use source for a concise origin or
  reference. The decision:* and status:* labels show approval and progress.
"""

_EDIT_HELP = """Editing:
  NUMBER is a positive requirement Issue number. --input is a UTF-8 JSON file
  or - for stdin. Edits save directly to the Issue and return its current details.
  Read the latest content and comments and resolve changed decisions with the
  user. Ordinary edit requires --input as a complete replacement with every
  required field; omission is a usage error and partial patches are unsupported.
  Only edit --approve may omit --input: it validates the latest canonical saved
  content and complete acceptance before approval, without rewriting content.
  Explicit --input with --approve still validates a complete replacement.
  Changes to problem, expected_outcome, or acceptance reopen done as todo.
  Title/source corrections alone keep done. Cancelled edits do not restore work.
  Reload and verify the latest acceptance items before finishing. The agent
  must reconsider earlier evidence after any substantive requirement change.
  If the Issue changes before it can be saved, edit returns CONTENT_CONFLICT.
  WRITE_FAILED reports a failed update. For WRITE_OUTCOME_UNKNOWN, use get
  to inspect the current Issue before retrying.
"""

_COMPLETION_HELP = """Automatic completion (agent responsibility):
  Full-delivery PRs target main and use Closes #NUMBER (or Fixes/Resolves) in
  their description so GitHub closes the linked Issue on merge. Use Refs for
  partial delivery or references; verify the Issue and scope before submission.
  For multiple requirements, link each Issue individually and check that the PR
  fully delivers every requirement named by a closing keyword.
  Before reporting work complete, get the latest requirement and relevant
  comments and verify every current acceptance condition against actual evidence.
  Once all prerequisites below are satisfied, submit the existing report and
  transition doing -> done without waiting for a separate human bookkeeping
  request. This closes the Issue as completed and applies status:done.
  Repository changes still require passing checks and PRs merged into main;
  a created PR, code review or green CI alone is not completion. If the human
  wants PRs left for their merge, keep the requirement unfinished until merge.
  Get the requirement again after transition. Report completion with its URL
  only after readback confirms done/status:done and the matching closed/completed
  state. get validates that native state against the status label.
  A closed/completed Issue with status:doing still needs the same report and
  doing -> done transition; do not reopen it merely to submit the report.
  All report and merged-PR checks still apply. If acceptance is incomplete,
  leave it unfinished and report the gap. Ordinary edits or transitions back
  to an active state reopen the Issue under the existing workflow.
  If already done, report that saved state and URL without another transition
  or completion comment. On failure or uncertain writes, inspect state/comments
  and report what is unconfirmed; a saved comment is not proof of completion.
  No scheduled job, background service or additional storage is involved.

Completion reports (--report FILE, or - for stdin):
  issue_number must be the current Issue number.
  results repeats every full acceptance item exactly once, without IDs,
  and adds result="passed", actual_result, and a nonempty evidence list.
  Missing, duplicated, failed, unknown, or changed acceptance results are rejected.
  Evidence is either {"kind":"inline","content":"actual observations"} or
  {"kind":"url","url":"https://...","description":"what this proves"}.
  The report is validated but its JSON, acceptance details, evidence, and
  checks are not saved in the Issue. The CLI posts a minimal Markdown completion
  comment containing only a title, status transition, and the supplied reason
  before marking done (and before closing an Issue that is still open).
  GitHub may already have closed it. A temporary local path alone is
  insufficient evidence; inline evidence must include actual observations.
  repository_changes is a required boolean. If true, include nonempty checks
  and pull_requests. Each check has command, result="passed", actual_result,
  and evidence. pull_requests contains positive PR numbers; the CLI verifies
  each was merged into this repository's main. The agent runs applicable checks
  and verifies the PR actually implements the requirement. If false, omit
  checks and pull_requests. Do not finish repository work before merge.
  Invalid/missing reports or unmerged PRs fail with INVALID_COMPLETION_REPORT.
"""

_CONFIRMATION_HELP = """Human confirmation (--confirmation FILE, or - for stdin):
  Cancellation and restoration require an object with nonempty instruction
  and source. Obtain the actual human instruction first, including in the
  current conversation, then record where and when it was given. Do not invent
  confirmation or cancel because work is difficult or blocked. There is no
  force flag. The CLI cannot authenticate a conversation supplied by an agent.
  The confirmation object is validated but not saved in the Issue comment;
  summarize the human's cancellation or restoration decision in the reason.
  Missing/invalid confirmation fails with HUMAN_CONFIRMATION_REQUIRED.
"""

_OUTPUT_HELP = """Outputs and errors:
  Success: one stdout JSON with ok=true, exit 0. Errors: stdout stays empty;
  one stderr JSON with ok=false, code, message, and known Issue/comment links.
  Usage/input-shape errors exit 2. Domain/runtime failures exit 1.
  Invalid usage fails with USAGE_ERROR; read/permission failures may produce
  GITHUB_ERROR. No database or live configuration is needed for --help.
"""

_DETAIL_OUTPUT_HELP = """Issue detail output:
  number, url, title, original body, status, native state/state_reason, decision,
  labels (GitHub names), parsed requirement, comments and comment_cursor.
  requirement contains title, problem, expected_outcome, acceptance (criterion,
  verification, pass_condition items), and optional source.
  Comments include id, body, author, created_at, updated_at and url.
  Only one comment page is returned; a non-null comment_cursor means more exist.
"""

_TRANSITION_OUTPUT_HELP = """Transition output and uncertain writes:
  Also includes audit_url for the transition comment.
  A comment records the requested update; it does not prove the Issue changed.
  WRITE_FAILED includes the saved audit_url and cause. WRITE_OUTCOME_UNKNOWN
  means a write may have succeeded but readback cannot confirm it. Inspect
  current state and comments before deciding whether to retry. For uncertain
  writes, reconcile the current Issue and its comments before retrying. Never
  infer approval, completion or a claim from a comment alone. Concurrent changes can
  produce CONTENT_CONFLICT without overwriting the new content.
  Malformed canonical content is INVALID_FORMAT; invalid full JSON fields are
  INVALID_REQUIREMENT, invalid JSON/files INVALID_INPUT. Non-requirement Issues
  and PRs are NOT_A_REQUIREMENT; missing Issues are NOT_FOUND.
"""

_REQUIREMENT_EXAMPLE = """Requirement JSON example:
{
  "title": "Document route conflict diagnostics",
  "problem": "Developers need to know which files map to the same route.",
  "expected_outcome": "Publish an example that shows how two files can resolve to the same route.\\n\\n## Proposed behavior\\nUse a route pair and explain how to rename one file to resolve the conflict.",
  "acceptance": [
    {
      "criterion": "The example explains a route conflict.",
      "verification": "Compare the example route pair with the file-based routing rules.",
      "pass_condition": "The example identifies both files, the shared route, and a viable rename."
    }
  ],
  "source": "Developer feedback on file-based routing"
}
"""

_PENDING_EXAMPLE = """Pending idea JSON example (acceptance can be added later):
{
  "title": "Explore diagnostics for route conflicts",
  "problem": "A route conflict can leave the developer unsure which file to rename.",
  "expected_outcome": "Consider including both conflicting paths in the diagnostic."
}
"""

_REPORT_EXAMPLE = """Report JSON example:
{
  "issue_number": 123,
  "repository_changes": false,
  "results": [
    {
      "criterion": "The example explains a route conflict.",
      "verification": "Compare the example route pair with the file-based routing rules.",
      "pass_condition": "The example identifies both files, the shared route, and a viable rename.",
      "result": "passed",
      "actual_result": "The example shows two conflicting files and how a rename resolves the conflict.",
      "evidence": [
        {"kind": "inline", "content": "Include the actual conflicting paths, their shared route, and the verified rename."}
      ]
    }
  ]
}
"""

_CONFIRMATION_EXAMPLE = """Confirmation JSON example (only after the actual human instruction):
{
  "instruction": "Cancel requirement #123 because the routing example is no longer needed.",
  "source": "The user's explicit message in the current conversation."
}
"""

_CREATE_EXAMPLES = """Examples (use the JSON examples above as separate cases):
  Save the approved requirement JSON as requirement.json after the human approves:
  modern-requirements create --input requirement.json --registration-source human --decision approved
  Alternatively, save the pending idea JSON as idea.json to collect an idea:
  modern-requirements create --input idea.json --registration-source agent --decision pending
  The output number identifies the created Issue; neither example claims work.
"""

_GET_EXAMPLES = """Examples (replace 123 with the requirement Issue number):
  modern-requirements get 123 --comment-limit 30
  If comment_cursor is non-null, copy its exact value into CURSOR and continue:
  modern-requirements get 123 --comment-limit 30 --comment-cursor CURSOR
"""

_LIST_EXAMPLES = """Examples:
  modern-requirements list --status todo --decision approved --query document --limit 30
  modern-requirements list --decision pending
  If cursor is non-null, copy its exact value into CURSOR and keep all filters:
  modern-requirements list --status todo --decision approved --query document --limit 30 --cursor CURSOR
"""

_TRANSITION_EXAMPLES = """Workflow examples (replace 123 and use actual decisions/evidence):
  Start with an approved todo requirement read in full with its discussion:
  modern-requirements transition 123 --from todo --to doing --reason "Check how the example routes resolve."
  modern-requirements transition 123 --from doing --to blocked --reason "The sample project is unavailable. Recover it and reproduce the route conflict."
  modern-requirements transition 123 --from blocked --to doing --reason "The sample project is available. Resume the route comparison."
  Verify the latest acceptance, save actual results as completion.json, then:
  modern-requirements transition 123 --from doing --to done --reason "The route example passed each current acceptance condition." --report completion.json
  Report example above is for non-repository work. Repository work also requires
  checks and PR numbers merged into main; do not use placeholder evidence.
  Separate release case: a claimed doing requirement can return to the pool:
  modern-requirements transition 123 --from doing --to todo --reason "The draft is ready for review. Reproduce the conflict in the sample project next."
  Separate cancellation case: a todo requirement and the actual human instruction:
  modern-requirements transition 123 --from todo --to cancelled --reason "The user confirmed that the routing example is no longer needed." --confirmation confirmation.json
  After an actual human restoration instruction, update confirmation.json:
  modern-requirements transition 123 --from cancelled --to todo --reason "The human asked to restore this requirement for reconsideration." --confirmation confirmation.json
"""

_EDIT_EXAMPLES = """Iterative recording examples (replace 123 with an existing pending todo Issue):
  Save the confirmed design and acceptance as requirement.json, then record them:
  modern-requirements edit 123 --input requirement.json
  modern-requirements get 123
  After the human explicitly approves that latest saved specification:
  modern-requirements edit 123 --approve
  modern-requirements get 123
  To approve and save a complete replacement for a pending idea in one command:
  modern-requirements edit 123 --input requirement.json --approve
"""


def build_parser() -> argparse.ArgumentParser:
    parser = JsonParser(
        prog="modern-requirements",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Choose an operation, then read its help:
  modern-requirements create --help
  modern-requirements get --help
  modern-requirements list --help
  modern-requirements edit --help
  modern-requirements transition --help""",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create", help="Create an English idea or approved requirement"
    )
    create.add_argument(
        "--input", required=True, help="Requirement JSON file, or - for stdin"
    )
    create.add_argument(
        "--registration-source",
        choices=REGISTRATION_SOURCES,
        required=True,
        help="Who initiated registration: human explicitly requested it; agent initiated it",
    )
    create.add_argument(
        "--decision",
        choices=DECISIONS,
        required=True,
        help="pending collects an idea; approved requires a human decision and complete acceptance",
    )
    get = commands.add_parser("get", help="Read a requirement and its comments")
    get.add_argument("number", type=int)
    get.add_argument(
        "--comment-limit", type=int, default=30, help="Comments per page, 1..100"
    )
    get.add_argument(
        "--comment-cursor", help="Continue comments for the same Issue and limit"
    )
    listing = commands.add_parser("list", help="Find requirements in bounded pages")
    listing.add_argument(
        "--status", choices=STATUSES, help="Default: active requirements"
    )
    listing.add_argument(
        "--decision", choices=DECISIONS, help="Filter decision status; default: both"
    )
    listing.add_argument(
        "--query", default="", help="Case-insensitive substring in title or body"
    )
    listing.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Issues scanned per page, 1..100; pages can be empty",
    )
    listing.add_argument("--cursor", help="Continue the same filters and limit")
    edit = commands.add_parser(
        "edit", help="Replace requirement fields or approve saved content"
    )
    edit.add_argument("number", type=int)
    edit.add_argument(
        "--input",
        help="Complete replacement JSON file, or - for stdin; required unless --approve",
    )
    # Accept edit commands written for version 0.1.0.
    edit.add_argument("--reason", help=argparse.SUPPRESS)
    edit.add_argument(
        "--approve",
        action="store_true",
        help="Approve a pending todo idea with complete acceptance",
    )
    transition = commands.add_parser(
        "transition", help="Claim, block, release, finish, cancel, or reopen"
    )
    transition.add_argument("number", type=int)
    transition.add_argument(
        "--from", dest="from_status", choices=STATUSES, required=True
    )
    transition.add_argument("--to", dest="to_status", choices=STATUSES, required=True)
    transition.add_argument(
        "--reason",
        required=True,
        help="English reason; one concise sentence for done; include progress and next action for block/release",
    )
    transition.add_argument(
        "--report",
        help="Completion report JSON file, or - for stdin; required for done",
    )
    transition.add_argument(
        "--confirmation",
        help="Human instruction/source JSON file, or - for stdin; required for cancel/restore",
    )
    sections = {
        "create": (
            _PREREQUISITES_HELP,
            _CREATE_HELP,
            _REQUIREMENT_FIELDS_HELP,
            _REGISTRATION_HELP,
            _DECISION_HELP,
            _DESIGN_RECORDING_HELP,
            _STORAGE_HELP,
            _DETAIL_OUTPUT_HELP,
            _OUTPUT_HELP,
            _REQUIREMENT_EXAMPLE,
            _PENDING_EXAMPLE,
            _CREATE_EXAMPLES,
        ),
        "get": (
            _PREREQUISITES_HELP,
            _GET_HELP,
            _STORAGE_HELP,
            _DECISION_HELP,
            _DETAIL_OUTPUT_HELP,
            _OUTPUT_HELP,
            _GET_EXAMPLES,
        ),
        "list": (
            _PREREQUISITES_HELP,
            _LIST_HELP,
            _DECISION_HELP,
            _OUTPUT_HELP,
            _LIST_EXAMPLES,
        ),
        "edit": (
            _PREREQUISITES_HELP,
            _EDIT_HELP,
            _REQUIREMENT_FIELDS_HELP,
            _DESIGN_RECORDING_HELP,
            _APPROVAL_HELP,
            _DECISION_HELP,
            _STORAGE_HELP,
            _DETAIL_OUTPUT_HELP,
            _OUTPUT_HELP,
            _REQUIREMENT_EXAMPLE,
            _EDIT_EXAMPLES,
        ),
        "transition": (
            _PREREQUISITES_HELP,
            _STATES_HELP,
            _DECISION_HELP,
            _STORAGE_HELP,
            _COMPLETION_HELP,
            _CONFIRMATION_HELP,
            _DETAIL_OUTPUT_HELP,
            _TRANSITION_OUTPUT_HELP,
            _OUTPUT_HELP,
            _REPORT_EXAMPLE,
            _CONFIRMATION_EXAMPLE,
            _TRANSITION_EXAMPLES,
        ),
    }
    for name, command in commands.choices.items():
        command.formatter_class = argparse.RawDescriptionHelpFormatter
        command.epilog = "\n\n".join(section.strip() for section in sections[name])
    return parser


def _input(path: str):
    try:
        text = (
            sys.stdin.buffer.read().decode("utf-8-sig")
            if path == "-"
            else Path(path).read_text(encoding="utf-8-sig")
        )
        return json.loads(text)
    except (OSError, ValueError) as error:
        raise RequirementError("INVALID_INPUT", str(error), exit_code=2) from error


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        service = Requirements()
        if args.command == "create":
            result = service.create(
                _input(args.input),
                registration_source=args.registration_source,
                decision=args.decision,
            )
        elif args.command == "list":
            result = service.list(
                status=args.status,
                decision=args.decision,
                query=args.query,
                limit=args.limit,
                cursor=args.cursor,
            )
        else:
            if args.number < 1:
                raise RequirementError(
                    "USAGE_ERROR", "number must be positive", exit_code=2
                )
            if args.command == "get":
                result = service.get(
                    args.number,
                    comment_limit=args.comment_limit,
                    comment_cursor=args.comment_cursor,
                )
            elif args.command == "edit":
                result = service.edit(
                    args.number,
                    _input(args.input) if args.input is not None else ...,
                    approve=args.approve,
                )
            else:
                if args.report == "-" and args.confirmation == "-":
                    raise RequirementError(
                        "USAGE_ERROR", "Only one input can use stdin", exit_code=2
                    )
                result = service.transition(
                    args.number,
                    from_status=args.from_status,
                    to_status=args.to_status,
                    reason=args.reason,
                    report=_input(args.report) if args.report else None,
                    confirmation=_input(args.confirmation)
                    if args.confirmation
                    else None,
                )
    except RequirementError as error:
        print(json.dumps(error.payload()), file=sys.stderr)
        return error.exit_code
    except Exception as error:
        print(
            json.dumps({"ok": False, "code": "INTERNAL_ERROR", "message": str(error)}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
