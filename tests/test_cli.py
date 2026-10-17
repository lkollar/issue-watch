import sqlite3
import subprocess
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from issue_watch.cli import Config, Issue, Watch, digest, load_config, poll, search, send


def issue(number, title="Fix a bug"):
    return Issue(
        number,
        "owner/project",
        number,
        title,
        f"https://github.com/owner/project/issues/{number}",
    )


class PollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.sqlite3"
        self.config = Config(
            "reader@example.com",
            "watch@example.com",
            "/fake/sendmail",
            (Watch("Bugs", "repo:owner/project is:issue label:bug"),),
        )
        self.matches = [issue(1)]
        self.messages = []

    def run_poll(self, *, dry_run=False):
        return poll(
            self.config,
            self.state,
            dry_run=dry_run,
            fetch=lambda query: self.matches,
            deliver=lambda config, message: self.messages.append(message),
        )

    def test_baseline_then_bundle_and_deduplicate(self):
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.messages, [])
        self.matches.extend([issue(2, "Fix café"), issue(3)])
        self.assertEqual(self.run_poll(), 2)
        self.assertEqual(len(self.messages), 1)
        message = self.messages[0]
        self.assertEqual(str(message["Subject"]), "[owner/project] Fix café (+1 more)")
        self.assertIn(issue(2).url, message.get_content())
        self.assertIn(issue(3).url, message.get_content())
        self.assertEqual(self.run_poll(), 0)

    def test_removed_then_reappearing_issue_not_sent_again(self):
        self.run_poll()
        self.matches = []
        self.run_poll()
        self.matches = [issue(1), issue(2)]
        self.assertEqual(self.run_poll(), 1)
        self.assertNotIn(issue(1).url, self.messages[0].get_content())

    def test_send_failure_retries_next_run(self):
        self.run_poll()
        self.matches.append(issue(2))

        def fail(*args):
            raise RuntimeError("mail failed")

        with self.assertRaisesRegex(RuntimeError, "mail failed"):
            poll(self.config, self.state, fetch=lambda query: self.matches, deliver=fail)
        self.assertEqual(self.run_poll(), 1)

    def test_dry_run_has_no_state_or_mail_effects(self):
        self.run_poll(dry_run=True)
        self.assertFalse(self.state.exists())
        self.run_poll()
        self.matches.append(issue(2))
        before = self.state.read_bytes()
        with patch("builtins.print") as output:
            self.assertEqual(self.run_poll(dry_run=True), 1)
        self.assertIn(issue(2).url, output.call_args.args[0])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.run_poll(), 1)

    def test_overlapping_watches_send_issue_once(self):
        self.config = Config(
            self.config.recipient,
            self.config.sender,
            self.config.sendmail,
            self.config.watches + (Watch("Other", "other query"),),
        )
        self.run_poll()
        self.matches.append(issue(2))
        self.assertEqual(self.run_poll(), 1)
        body = self.messages[0].get_content()
        self.assertEqual(body.count(issue(2).url), 1)
        self.assertIn("Bugs, Other", body)

    def test_later_query_failure_does_not_advance_other_watch(self):
        self.run_poll()
        self.config = Config(
            self.config.recipient,
            self.config.sender,
            self.config.sendmail,
            self.config.watches + (Watch("Other", "other query"),),
        )
        self.matches.append(issue(2))

        def fetch(query):
            if query == "other query":
                raise RuntimeError("API failed")
            return self.matches

        with self.assertRaisesRegex(RuntimeError, "API failed"):
            poll(
                self.config,
                self.state,
                fetch=fetch,
                deliver=lambda *args: self.fail("must not send partial digest"),
            )
        self.assertEqual(self.run_poll(), 1)

    def test_concurrent_run_cannot_send(self):
        self.run_poll()
        self.matches.append(issue(2))
        with sqlite3.connect(self.state) as connection:
            connection.execute("BEGIN IMMEDIATE")
            real_connect = sqlite3.connect
            with patch("issue_watch.cli.sqlite3.connect", wraps=sqlite3.connect) as connect:
                connect.side_effect = lambda *args, **kwargs: real_connect(args[0], timeout=0)
                with self.assertRaises(sqlite3.OperationalError):
                    self.run_poll()
        self.assertEqual(self.messages, [])


class SearchTests(unittest.TestCase):
    def response(self, total, numbers, **extra):
        import json
        from io import BytesIO

        return BytesIO(
            json.dumps(
                {
                    "total_count": total,
                    "incomplete_results": False,
                    "items": [
                        {
                            "id": n,
                            "number": n,
                            "title": f"Issue {n}",
                            "html_url": issue(n).url,
                            "repository_url": "https://api.github.com/repos/owner/project",
                        }
                        for n in numbers
                    ],
                    **extra,
                }
            ).encode()
        )

    def test_pagination(self):
        with patch(
            "issue_watch.cli.urlopen",
            side_effect=[
                self.response(101, range(1, 101)),
                self.response(101, [101]),
            ],
        ) as fetch:
            self.assertEqual(len(search("example")), 101)
            self.assertIn("page=2", fetch.call_args.args[0].full_url)

    def test_refuses_incomplete_capped_and_changing_results(self):
        for responses in (
            [self.response(1, [1], incomplete_results=True)],
            [self.response(1001, [])],
            [self.response(101, range(1, 101)), self.response(102, [101, 102])],
            [self.response(2, [1]), self.response(2, [])],
        ):
            with self.subTest(), patch("issue_watch.cli.urlopen", side_effect=responses):
                with self.assertRaises(RuntimeError):
                    search("example")


class MailAndConfigTests(unittest.TestCase):
    def test_sendmail_receives_valid_unicode_message(self):
        config = Config("reader@example.com", "watch@example.com", "/sendmail", ())
        message = digest(config, {1: (issue(1, "Café\nInjected: header"), ["Bugs"])})
        with patch("issue_watch.cli.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            send(config, message)
            self.assertEqual(run.call_args.args[0], ["/sendmail", "-oi", "-t"])
            parsed = BytesParser(policy=policy.default).parsebytes(run.call_args.kwargs["input"])
            self.assertIsNone(parsed["Injected"])
            self.assertIn("Café", str(parsed["Subject"]))
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"delivery failed")
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                send(config, message)

    def test_example_config(self):
        config = load_config(Path(__file__).parents[1] / "config.example.toml")
        self.assertEqual(config.recipient, "you@example.com")
        self.assertEqual(len(config.watches), 1)


if __name__ == "__main__":
    unittest.main()
