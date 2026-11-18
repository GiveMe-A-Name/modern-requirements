"""English requirement examples and stateful, offline GitHub responses."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from modern_requirements.cli import RequirementError


def requirement():
    return {
        "title": "[Feature]: Document the research input contract",
        "problem": "Agents cannot find the required input columns.",
        "expected_outcome": "Document the columns used by this repository's research tools in an Issue comment. Use the existing schema and reference; no repository files need changing.",
        "acceptance": [
            {
                "criterion": "The input columns are documented.",
                "verification": "Read the input reference and compare its columns with the schema.",
                "pass_condition": "Every required column appears with its type and meaning.",
            }
        ],
        "source": "Repository research workflow",
    }


def completion(detail, *, repository_changes=False):
    result = {
        "issue_number": detail["number"],
        "repository_changes": repository_changes,
        "results": [
            {
                **item,
                "result": "passed",
                "actual_result": "The documented columns match the required schema.",
                "evidence": [
                    {
                        "kind": "inline",
                        "content": "Compared the reference and schema: symbol is text, trade_date is a date, and pnl is numeric.",
                    }
                ],
            }
            for item in detail["requirement"]["acceptance"]
        ],
    }
    if repository_changes:
        result.update(
            checks=[
                {
                    "command": "pnpm test:ut",
                    "result": "passed",
                    "actual_result": "All tests passed.",
                    "evidence": [
                        {
                            "kind": "url",
                            "url": "https://github.com/web-infra-dev/modern.js/actions/runs/123",
                            "description": "Passing regression run",
                        }
                    ],
                }
            ],
            pull_requests=[10],
        )
    return result


class FakeGitHub:
    """Imitate supported REST endpoints; never run gh or contact GitHub."""

    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.labels = {"✨ feature request": {"name": "✨ feature request"}}
        self.calls = []
        self.permissions = {"push": True}
        self.pulls = {}
        self.fail = None

    def request(self, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        parsed = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        route = parsed.path
        result = self._request(method, route, query, payload)
        if self.fail and self.fail(method, route):
            raise RequirementError(
                "GITHUB_ERROR", "Accepted, then disconnected", uncertain=True
            )
        return deepcopy(result)

    def _request(self, method, route, query, payload):
        if route == "":
            return {"permissions": self.permissions}
        if route == "/labels":
            if method == "POST":
                self.labels[payload["name"]] = payload
                return payload
            return self._page(list(self.labels.values()), query)
        if route == "/issues":
            if method == "POST":
                number = max(self.issues, default=0) + 1
                issue = {
                    "number": number,
                    "html_url": f"https://github.com/web-infra-dev/modern.js/issues/{number}",
                    "title": payload["title"],
                    "body": payload["body"],
                    "state": "open",
                    "state_reason": None,
                    "labels": [{"name": name} for name in payload["labels"]],
                }
                self.issues[number] = issue
                return issue
            values = sorted(
                self.issues.values(),
                key=lambda value: value["number"],
                reverse=query.get("direction") != "asc",
            )
            if query.get("labels"):
                values = [
                    v
                    for v in values
                    if query["labels"] in [label["name"] for label in v["labels"]]
                ]
            if query.get("state", "all") != "all":
                values = [v for v in values if v["state"] == query["state"]]
            return self._page(values, query)
        parts = route.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "pulls":
            return self.pulls[int(parts[1])]
        if len(parts) == 3 and parts[0] == "issues" and parts[2] == "comments":
            number = int(parts[1])
            comments = self.comments.setdefault(number, [])
            if method == "POST":
                comment = {
                    "id": len(comments) + 1,
                    "body": payload["body"],
                    "user": {"login": "agent"},
                    "created_at": "2026-11-15T00:00:00Z",
                    "updated_at": "2026-11-15T00:00:00Z",
                    "html_url": f"https://github.com/web-infra-dev/modern.js/issues/{number}#issuecomment-{len(comments) + 1}",
                }
                comments.append(comment)
                return comment
            return self._page(comments, query)
        if len(parts) == 2 and parts[0] == "issues":
            number = int(parts[1])
            if number not in self.issues:
                raise RequirementError("NOT_FOUND", "Issue not found", http_status=404)
            issue = self.issues[number]
            if method == "PATCH":
                issue.update({k: v for k, v in payload.items() if k != "labels"})
                if "labels" in payload:
                    issue["labels"] = [{"name": name} for name in payload["labels"]]
            return issue
        raise AssertionError((method, route, payload))

    @staticmethod
    def _page(values, query):
        size = int(query.get("per_page", 100))
        start = (int(query.get("page", 1)) - 1) * size
        return values[start : start + size]
