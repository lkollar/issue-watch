# issue-watch

Poll GitHub issue searches and email one digest of new matches per run.
Python 3.15, uv, SQLite, systemd, and the machine's existing sendmail setup.
No runtime dependencies beyond Python's standard library.

## Development

```sh
nix develop
uv sync
uv run issue-watch --help
uv run python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

The shell selects Nix's Python 3.15 and disables uv Python downloads.
`uv.lock` records the project's dependency resolution. Nix packages build
using the setuptools version pinned by `flake.lock`.

## Configure and run

```sh
cp config.example.toml config.toml
uv run issue-watch --config config.toml --dry-run
uv run issue-watch --config config.toml
```

Replace the example recipient, sender, and `owner/project` repository with
your own values. Use a sender accepted by your mail server. Edit the TOML
to add more `[[watches]]`.
Keep credentials out of it: optionally set `GH_TOKEN` or `GITHUB_TOKEN` for
authenticated GitHub requests. Public repositories work without a token.

The first successful run baselines each query's existing matches without
email. Subsequent runs notify on first observation of an unseen issue ID,
including issues labelled after creation. Edits and reopening do not repeat
notifications. Renaming a watch preserves its history; changing its exact
query creates a fresh baseline.

All new matches across all watches share one email. An issue matching several
watches appears once. Subject: `[owner/project] Issue title (+2 more)`.
The body lists each project's name, issue number, title, link, and watch names.

State defaults to `$XDG_STATE_HOME/issue-watch/state.sqlite3`, or
`~/.local/state/issue-watch/state.sqlite3`. Override with `--state PATH`.
`--dry-run` previews pending email without sending or changing state; a fresh
database reports baseline counts only. Run once normally to establish it.

## Nix package and NixOS timer

```sh
nix build
nix run . -- --config ./config.example.toml --dry-run
nix flake check
```

Add this flake as an input and import `inputs.issue-watch.nixosModules.default`:

```nix
services.issue-watch = {
  enable = true;
  configFile = ./issue-watch.toml;
  # Optional: root-readable file containing GH_TOKEN=...
  # environmentFile = "/run/secrets/issue-watch";
  # Default: hourly, plus up to 5m jitter.
  # onCalendar = "hourly";
};
```

The service uses a dedicated `issue-watch` user and
`/var/lib/issue-watch/state.sqlite3`. It needs the host's working sendmail
wrapper; the package does not configure an MTA. Persistent scheduling catches
up after downtime. The module is provided here; importing and enabling it in
the host configuration is a separate deployment step.

Set `services.issue-watch.stateDirectory` to use another directory. The host
must provision custom directories with ownership `issue-watch:issue-watch`
and mode 0700 before the service starts.

```sh
sudo systemctl start issue-watch.service
systemctl list-timers issue-watch.timer
journalctl -u issue-watch.service
```

## Delivery and polling limits

- SQLite serializes overlapping runs. API or mail failures exit nonzero and
  roll back all watch updates. Failed runs retry on the next scheduled poll.
- State advances after sendmail accepts the digest into its queue. This does
  not guarantee final inbox delivery; the host MTA handles transport retries.
- A crash after mail acceptance but before SQLite commit can repeat a digest.
- Open issues closed between polls won't match `state:open`. Search indexing
  delays can postpone discovery. Same-count changes during pagination can
  also postpone discovery until a later run; search is not a snapshot API.
- Queries exceeding GitHub's 1,000-result limit, incomplete responses, or
  detected pagination inconsistencies fail without advancing state. Narrow
  broad queries. Include `is:issue`; pull request matches are rejected.
- Back up the database to preserve deduplication history. Deleting state
  causes a new silent baseline, including currently pending matches.
