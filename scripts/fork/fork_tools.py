"""Track what this fork has changed relative to upstream ``the-momentum/open-wearables``.

The fork carries a large, long-lived delta on top of upstream and re-syncs
periodically. When something misbehaves the first question is always "is this
ours or theirs?", and commit archaeology is a slow way to answer it. These
commands make it a lookup.

Subcommands
-----------
``setup``
    Add (or re-point) the ``upstream`` git remote and fetch it. Idempotent, so
    it is safe to run on every fresh clone - git remotes live in ``.git/config``
    and are not committed, so a new checkout has no ``upstream`` until this runs.

``diff``
    Regenerate ``docs/fork/DIVERGENCE.md``: the last sync point, how far ahead
    and behind we are, and a per-file table saying whether each diverged file is
    fork-only or modified from upstream. Anything not listed is upstream-pristine.

``tag``
    Create an ``upstream-sync/<date>`` tag on the current commit, to be run as
    part of every upstream merge. The tags are the bisect anchors: a diff
    between two of them is exactly "what did we change between syncs", and a
    diff from the latest one is "what have we changed since a known-good base".

Stdlib only, no virtualenv: this spans backend, frontend, mcp and docs, so it
must run from a bare checkout with nothing installed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_URL = "https://github.com/the-momentum/open-wearables"
UPSTREAM_REMOTE = "upstream"
UPSTREAM_BRANCH = "main"
OUTPUT = REPO_ROOT / "docs" / "fork" / "DIVERGENCE.md"
SYNC_TAG_PREFIX = "upstream-sync/"

# Files whose divergence is structural rather than behavioural: the fork is a
# fork, so its own identity, agent config and CI wiring are expected to differ
# and reporting them every time buries the changes that matter.
UNINTERESTING = (
    "docs/fork/",
    "scripts/fork/",
)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def have_upstream() -> bool:
    return UPSTREAM_REMOTE in git("remote").splitlines()


def cmd_setup(_args: argparse.Namespace) -> int:
    if have_upstream():
        git("remote", "set-url", UPSTREAM_REMOTE, UPSTREAM_URL)
    else:
        git("remote", "add", UPSTREAM_REMOTE, UPSTREAM_URL)
    print(f"upstream -> {UPSTREAM_URL}")
    print(f"fetching {UPSTREAM_REMOTE}/{UPSTREAM_BRANCH} ...")
    git("fetch", "--no-tags", UPSTREAM_REMOTE, UPSTREAM_BRANCH)
    print(f"ok: {describe(f'{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}')}")
    return 0


def describe(ref: str) -> str:
    sha = git("rev-parse", "--short", ref)
    when = git("log", "-1", "--format=%ad", "--date=short", ref)
    subject = git("log", "-1", "--format=%s", ref)
    return f"{sha} ({when}) {subject}"


def latest_sync_tag() -> str | None:
    tags = git("tag", "--list", f"{SYNC_TAG_PREFIX}*", "--sort=-creatordate").splitlines()
    return tags[0] if tags else None


def area_of(path: str) -> str:
    """Top-level area a path belongs to, for grouping the report."""
    head = path.split("/", 1)[0]
    if head in ("backend", "frontend", "mcp", "docs", "contributing", "scripts"):
        return head
    if head.startswith("."):
        return "tooling"
    return "root"


def cmd_diff(args: argparse.Namespace) -> int:
    if not have_upstream():
        raise SystemExit("no 'upstream' remote - run 'make fork-setup' first")
    if not args.no_fetch:
        git("fetch", "--no-tags", UPSTREAM_REMOTE, UPSTREAM_BRANCH)

    upstream_ref = f"{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}"
    ours = args.ref
    base = git("merge-base", upstream_ref, ours)

    behind, ahead = (int(n) for n in git("rev-list", "--left-right", "--count", f"{upstream_ref}...{ours}").split())

    rows: list[tuple[str, str, int, int]] = []
    numstat = {}
    for line in git("diff", "--numstat", base, ours).splitlines():
        added, removed, path = (line.split("\t") + ["", "", ""])[:3]
        numstat[path] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
        )
    for line in git("diff", "--name-status", base, ours).splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if any(path.startswith(prefix) for prefix in UNINTERESTING):
            continue
        added, removed = numstat.get(path, (0, 0))
        rows.append((status, path, added, removed))

    rows.sort(key=lambda r: (area_of(r[1]), r[1]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(rows, base, upstream_ref, ours, ahead, behind))
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(rows)} diverged files)")
    return 0


LABELS = {"A": "fork-only", "M": "modified", "D": "deleted", "R": "renamed"}


def render(
    rows: list[tuple[str, str, int, int]],
    base: str,
    upstream_ref: str,
    ours: str,
    ahead: int,
    behind: int,
) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sync_tag = latest_sync_tag()

    out = [
        "<!-- Generated by `make fork-diff`. Do not edit by hand. -->",
        "",
        "# Fork divergence from upstream",
        "",
        f"Upstream is [`the-momentum/open-wearables`]({UPSTREAM_URL}).",
        "",
        "| | |",
        "|---|---|",
        f"| Generated | {generated} |",
        f"| Our ref | `{ours}` — {describe(ours)} |",
        f"| Upstream `{UPSTREAM_BRANCH}` | {describe(upstream_ref)} |",
        f"| Last common commit | {describe(base)} |",
        f"| Commits we are ahead | {ahead} |",
        f"| Upstream commits not merged | {behind} |",
        f"| Latest sync tag | {sync_tag or '_none yet_'} |",
        "",
        "## How to use this",
        "",
        "Any file **not listed below is identical to upstream** — a bug in one of those is",
        "upstream's, and worth reproducing against a clean upstream checkout before",
        "debugging it here.",
        "",
        "```bash",
        "# everything we changed since the last known-good sync",
        f"git diff {sync_tag or SYNC_TAG_PREFIX + '<date>'}..HEAD",
        "",
        "# upstream's version of one file, to compare against ours",
        f"git show {upstream_ref}:<path>",
        "",
        "# which of our commits touched a file",
        f"git log --oneline {base}..HEAD -- <path>",
        "```",
        "",
        "Rationale for each intentional divergence lives in [DECISIONS.md](./DECISIONS.md).",
        "",
    ]

    if behind:
        out += [
            f"> **{behind} upstream commits are not merged yet.** The table below compares against the",
            "> last common commit, so it does not include changes upstream has made since.",
            "",
        ]

    if not rows:
        out += ["## Diverged files", "", "_None — this fork is currently identical to upstream._", ""]
        return "\n".join(out)

    counts: dict[str, int] = {}
    for status, _path, _a, _r in rows:
        counts[LABELS.get(status[0], status)] = counts.get(LABELS.get(status[0], status), 0) + 1
    summary = ", ".join(f"{n} {label}" for label, n in sorted(counts.items()))
    out += ["## Diverged files", "", f"{len(rows)} files: {summary}.", ""]

    current_area = None
    for status, path, added, removed in rows:
        area = area_of(path)
        if area != current_area:
            current_area = area
            out += [f"### `{area}`", "", "| File | Status | +/- |", "|---|---|---|"]
        label = LABELS.get(status[0], status)
        churn = "—" if label == "fork-only" else f"+{added}/-{removed}"
        out.append(f"| `{path}` | {label} | {churn} |")
    out.append("")
    return "\n".join(out)


def cmd_tag(args: argparse.Namespace) -> int:
    name = f"{SYNC_TAG_PREFIX}{args.date}"
    existing = git("tag", "--list", name)
    if existing and not args.force:
        raise SystemExit(f"tag {name} already exists (pass --force to move it)")
    git("tag", "-f" if args.force else "-a", name, "-m", f"Upstream sync {args.date}")
    print(f"tagged {name} at {describe('HEAD')}")
    print(f"push it with: git push origin {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="add/re-point the upstream remote and fetch it").set_defaults(func=cmd_setup)

    p_diff = sub.add_parser("diff", help="regenerate docs/fork/DIVERGENCE.md")
    p_diff.add_argument("--ref", default="HEAD", help="our ref to compare (default: HEAD)")
    p_diff.add_argument("--no-fetch", action="store_true", help="use the already-fetched upstream ref")
    p_diff.set_defaults(func=cmd_diff)

    p_tag = sub.add_parser("tag", help="tag HEAD as an upstream sync point")
    p_tag.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="sync date (default: today, UTC)",
    )
    p_tag.add_argument("--force", action="store_true", help="move the tag if it already exists")
    p_tag.set_defaults(func=cmd_tag)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
