"""Poll GitHub search, compare persistent IDs, and send one digest."""

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (query TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS seen (
    query TEXT NOT NULL,
    issue_id INTEGER NOT NULL,
    PRIMARY KEY (query, issue_id)
);
"""


@dataclass(frozen=True)
class Watch:
    name: str
    query: str


@dataclass(frozen=True)
class Config:
    recipient: str
    sender: str
    sendmail: str
    watches: tuple[Watch, ...]


@dataclass(frozen=True)
class Issue:
    id: int
    repository: str
    number: int
    title: str
    url: str


def load_config(path: Path) -> Config:
    with path.open("rb") as file:
        data = tomllib.load(file)

    def text(value, field):
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"{field} must be a nonempty, single-line string")
        return value.strip()

    recipient = text(data.get("recipient"), "recipient")
    sender = text(data.get("sender"), "sender")
    sendmail = text(data.get("sendmail", "/run/wrappers/bin/sendmail"), "sendmail")
    entries = data.get("watches")
    if not isinstance(entries, list) or not entries:
        raise ValueError("at least one [[watches]] entry is required")
    watches = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each watch must be a table")
        watches.append(Watch(text(entry.get("name"), "name"), text(entry.get("query"), "query")))
    if len({watch.query for watch in watches}) != len(watches):
        raise ValueError("duplicate watch queries")
    return Config(recipient, sender, sendmail, tuple(watches))


def search(query: str) -> list[Issue]:
    """Fetch all pages; refuse capped, incomplete, or inconsistent results."""
    issues = {}
    expected = None
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "issue-watch/0.1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for page in range(1, 11):
        params = urlencode(
            {"q": query, "sort": "created", "order": "asc", "per_page": 100, "page": page}
        )
        request = Request(f"https://api.github.com/search/issues?{params}", headers=headers)
        try:
            with urlopen(request, timeout=60) as response:
                data = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub search failed: HTTP {error.code}") from error
        if data.get("incomplete_results") is not False:
            raise RuntimeError("GitHub returned incomplete results; state unchanged")
        total = data["total_count"]
        if total > 1000:
            raise RuntimeError("query exceeds GitHub's 1,000-result limit; narrow it")
        if expected is not None and total != expected:
            raise RuntimeError("search results changed during pagination; retry next run")
        expected = total
        for item in data["items"]:
            if "pull_request" in item:
                raise RuntimeError("query matched pull requests; add is:issue")
            repository = item["repository_url"].removeprefix("https://api.github.com/repos/")
            issues[item["id"]] = Issue(
                item["id"], repository, item["number"], item["title"], item["html_url"]
            )
        if len(issues) == expected:
            return list(issues.values())
        if not data["items"]:
            break
    raise RuntimeError("could not fetch complete search results; state unchanged")


def digest(config: Config, pending: dict[int, tuple[Issue, list[str]]]) -> EmailMessage:
    issues = sorted(pending.values(), key=lambda entry: (entry[0].repository, entry[0].number))
    first = issues[0][0]
    title = " ".join(first.title.split())
    suffix = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
    message = EmailMessage(policy=SMTP)
    message["From"] = config.sender
    message["To"] = config.recipient
    message["Subject"] = f"[{first.repository}] {title}{suffix}"
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    lines = [f"{len(issues)} new matching issue(s):", ""]
    for issue, names in issues:
        lines.extend(
            [
                f"{issue.repository} #{issue.number} — {' '.join(issue.title.split())}",
                issue.url,
                f"Watches: {', '.join(names)}",
                "",
            ]
        )
    message.set_content("\n".join(lines))
    return message


def send(config: Config, message: EmailMessage) -> None:
    result = subprocess.run(
        [config.sendmail, "-oi", "-t"],
        input=message.as_bytes(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        error = result.stderr.decode(errors="replace").strip()[:500]
        raise RuntimeError(f"sendmail exited {result.returncode}: {error}")


def poll(config: Config, state: Path, *, dry_run=False, fetch=search, deliver=send) -> int:
    """Serialize runs; commit all watches only after the digest is accepted."""
    if dry_run:
        connection = (
            sqlite3.connect(f"{state.resolve().as_uri()}?mode=ro", uri=True)
            if (state.exists())
            else sqlite3.connect(":memory:")
        )
        if not state.exists():
            connection.executescript(SCHEMA)
    else:
        state.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(state, timeout=5)
        connection.executescript(SCHEMA)
    try:
        # Held across fetch + delivery to prevent duplicate concurrent sends.
        connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        pending = {}
        snapshots = []
        for watch in config.watches:
            issues = fetch(watch.query)
            initialized = connection.execute(
                "SELECT 1 FROM watches WHERE query = ?", (watch.query,)
            ).fetchone()
            seen = {
                row[0]
                for row in connection.execute(
                    "SELECT issue_id FROM seen WHERE query = ?", (watch.query,)
                )
            }
            if initialized:
                for issue in issues:
                    if issue.id not in seen:
                        if issue.id not in pending:
                            pending[issue.id] = (issue, [])
                        pending[issue.id][1].append(watch.name)
            else:
                LOG.info("%s: baseline %d existing matches", watch.name, len(issues))
            snapshots.append((watch, issues))
        if pending:
            message = digest(config, pending)
            if dry_run:
                print(
                    f"To: {message['To']}\nSubject: {message['Subject']}\n\n{message.get_content()}"
                )
            else:
                deliver(config, message)
            LOG.info("%s %d issue(s)", "Would notify" if dry_run else "Notified", len(pending))
        else:
            LOG.info("No new matches")
        if not dry_run:
            for watch, issues in snapshots:
                connection.execute("INSERT OR IGNORE INTO watches VALUES (?)", (watch.query,))
                connection.executemany(
                    "INSERT OR IGNORE INTO seen VALUES (?, ?)",
                    [(watch.query, issue.id) for issue in issues],
                )
            connection.commit()
        return len(pending)
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    default_state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    parser.add_argument("--state", type=Path, default=default_state / "issue-watch/state.sqlite3")
    parser.add_argument("--dry-run", action="store_true", help="preview; no email or state changes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        poll(load_config(args.config), args.state, dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        LOG.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
