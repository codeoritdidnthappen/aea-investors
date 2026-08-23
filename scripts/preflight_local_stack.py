"""Refuse to bring up a local stack that would come up degraded.

TICK-057. The AI Chat panel vanished from the portal for an entire session and
nothing reported a fault: `docker ps` said healthy, no log line was written, and
the failure was only visible under `docker inspect`. The cause was that the stack
had been started from inside a build worktree, so Compose resolved its relative
bind mounts against that worktree; when the worktree was deleted the mount
sources went with it. A directory mount whose source is gone presents as an empty
directory, and a single-file mount presents as an empty file that *shadows* the
vendor original rather than falling back to it.

Every check here is for a fault that is silent at runtime. Anything Compose
already refuses on its own -- a missing `${VAR:?}` -- is deliberately not
duplicated.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Short-syntax bind mounts only: `- <source>:<target>[:<mode>]` where the source is
# a relative path. Named volumes have no path separator and are skipped.
_BIND = re.compile(r"^\s*-\s+(?P<source>\.{1,2}/[^:\s]+):(?P<target>/[^:\s]+)")
_ENV_KEY = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=")
# How docker-compose.yml references a variable decides whether an absent .env entry
# matters. `${KEY:?msg}` makes Compose refuse to start, so it needs no help here.
# `${KEY:-default}` is optional by construction. A bare `${KEY}` silently expands to
# an empty string, which is the only one that degrades quietly.
_COMPOSE_REQUIRED = re.compile(r"\$\{(?P<key>[A-Z][A-Z0-9_]*):\?")
_COMPOSE_DEFAULTED = re.compile(r"\$\{(?P<key>[A-Z][A-Z0-9_]*):-")
_COMPOSE_BARE = re.compile(r"\$\{(?P<key>[A-Z][A-Z0-9_]*)\}")
_WORKTREE_MARKER = "/.builder/worktrees/"


class PreflightError(Exception):
    """A fault that would leave the stack running but degraded."""


def find_worktree_start(compose_path: Path) -> str | None:
    """Reject a compose file living inside a disposable build worktree.

    Compose resolves relative bind mounts against the compose file's own
    directory, so starting the stack from a worktree pins every mount to a path
    that is deleted when the build finishes. The containers keep running and keep
    serving from the corpse.
    """
    if _WORKTREE_MARKER in f"{compose_path.resolve().as_posix()}/":
        return (
            f"{compose_path} is inside a build worktree.\n"
            "  Compose resolves relative bind mounts against this file's directory, so "
            "every mount would\n  point at a path that is deleted when the build "
            "finishes -- leaving containers serving\n  empty directories. Start the "
            "stack from the main checkout instead."
        )
    return None


def find_broken_mounts(compose_path: Path) -> list[str]:
    """Every relative bind mount whose source is missing, or is an empty directory."""
    root = compose_path.parent
    problems: list[str] = []
    for line in compose_path.read_text(encoding="utf-8").splitlines():
        match = _BIND.match(line)
        if match is None:
            continue
        source = (root / match.group("source")).resolve()
        target = match.group("target")
        if not source.exists():
            problems.append(f"mount source does not exist: {source}\n  (mounted at {target})")
        elif source.is_dir() and not any(source.iterdir()):
            # The TICK-057 failure mode: present, but empty, so the container mounts
            # nothing over the destination and the feature silently disappears.
            problems.append(
                f"mount source is an empty directory: {source}\n  (mounted at {target})"
            )
    return problems


def find_missing_env_keys(env_path: Path, example_path: Path, compose_path: Path) -> list[str]:
    """Keys whose absence from `.env` would degrade the stack quietly.

    `.env` is gitignored, so a rename landing in `.env.example` reaches every
    checkout except the one actually running -- which is how TICK-051's rename left
    the only stack that mattered unable to start.

    Only keys Compose references *bare* are reported. A `${KEY:?}` reference already
    stops Compose with a clear message, and a `${KEY:-default}` reference is optional
    by design; failing on either would make this check noise, and a check people learn
    to ignore protects nothing.
    """
    if not example_path.is_file():
        raise PreflightError(f"missing {example_path}")
    if not env_path.is_file():
        raise PreflightError(f"missing {env_path} -- copy it from {example_path.name}")

    def keys(path: Path) -> set[str]:
        found = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _ENV_KEY.match(line.strip())
            if match is not None:
                found.add(match.group("key"))
        return found

    compose = compose_path.read_text(encoding="utf-8")
    defaulted = set(_COMPOSE_DEFAULTED.findall(compose)) | set(_COMPOSE_REQUIRED.findall(compose))
    bare = set(_COMPOSE_BARE.findall(compose)) - defaulted
    absent = keys(example_path) - keys(env_path)
    return sorted(absent & bare)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deploy-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "deploy" / "local",
        help="Directory holding docker-compose.yml, .env and .env.example",
    )
    parser.add_argument(
        "--skip-env",
        action="store_true",
        help="Skip the .env comparison (for CI, which has no .env)",
    )
    args = parser.parse_args()

    compose_path = args.deploy_root / "docker-compose.yml"
    if not compose_path.is_file():
        print(f"PREFLIGHT_FAILED: missing {compose_path}", file=sys.stderr)
        return 1

    failures: list[str] = []
    worktree = find_worktree_start(compose_path)
    if worktree is not None:
        failures.append(worktree)
    failures.extend(find_broken_mounts(compose_path))
    if not args.skip_env:
        try:
            missing = find_missing_env_keys(
                args.deploy_root / ".env", args.deploy_root / ".env.example", compose_path
            )
        except PreflightError as error:
            failures.append(str(error))
        else:
            for key in missing:
                failures.append(
                    f"{key} is declared in .env.example and referenced by "
                    "docker-compose.yml with no default,\n  but is not set in .env -- it "
                    "would expand to an empty string.\n  (.env is gitignored, so a rename "
                    "reaches every checkout except the one actually running)"
                )

    if failures:
        print("PREFLIGHT_FAILED", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("PREFLIGHT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
