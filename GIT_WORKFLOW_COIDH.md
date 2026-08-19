# Git workflow

Follow these rules for all work in this repo.

- **Never commit directly to `main`.** All changes land on a dedicated branch and reach `main`
  only via a pull request.
- **One branch per logical unit of work.** Multiple commits per branch are fine and encouraged
  (small, focused commits). Use [Conventional Commits](https://www.conventionalcommits.org)
  for messages (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `perf:`).
- **Branch naming** — Conventional prefix + kebab-case description:
  - `feat/<description>` — new feature
  - `fix/<description>` — bug fix
  - `chore/<description>` — tooling, config, deps, housekeeping
  - `docs/<description>` — docs only
  - `refactor/<description>` — behavior-preserving code change
  - `test/<description>` — tests only
  - `perf/<description>` — performance work
- **Do NOT reuse an existing branch** (e.g. a prior feature branch) for new or unrelated work
  **without asking first.** Default to cutting a fresh branch off up-to-date `main`.
- **Merge style:** rebase merge, delete the branch on merge
  (`gh pr merge --rebase --delete-branch`). Individual conventional commits land on `main`;
  history stays linear.

## Required local git config (per repo — part of project setup)

```bash
make git-setup
```

## GitHub CLI quirk

Remotes use an SSH host alias (e.g. `git@github-coidh:...`), which `gh` does not recognize as
GitHub — a bare `gh pr list` fails with "none of the git remotes configured for this repository
point to a known GitHub host." **Every `gh` command must name the repo explicitly**, but *how*
depends on the subcommand:

- Commands acting on things **inside** a repo (`gh pr`, `gh issue`, `gh run`) take `-R`:

  ```
  gh pr create -R codeoritdidnthappen/[PROJECT_NAME] ...
  gh pr merge  -R codeoritdidnthappen/[PROJECT_NAME] ...
  ```

- `gh repo` commands take the repo **positionally** — they have no `-R` flag, and passing one
  fails with `unknown shorthand flag: 'R'`:

  ```
  gh repo view codeoritdidnthappen/[PROJECT_NAME]
  ```

## Autonomous delivery loop

Claude runs the full delivery cycle unattended. Report progress at each step as usual, but do
not stop to ask for approval between steps (see "When to ask" for the exceptions).

1. Sync: `git checkout main && git fetch --prune && git pull`.
2. Cut a fresh branch off `main` (naming rules above).
3. Implement in small, focused conventional commits.
4. Run the **merge gate** (below). Do not proceed until it passes.
5. Push and open a PR: `gh pr create -R <owner>/<repo>` with a summary of changes and gate
   results in the body.
6. Merge: `gh pr merge --rebase --delete-branch -R <owner>/<repo>`.
7. Sync `main` again (step 1), delete the local branch, and begin the next unit of work.

## Merge gate (mandatory before every merge)

- **No test suite — by decision, for now.** This project has no tests and none are required.
  Do not write tests to satisfy this gate, and do not treat their absence as a gate that
  passed on its own. When a suite is added, a "tests green" requirement returns here.
- **Code review clean.** Run `/code-review`; the review must come back clean or every finding
  must be addressed (fixed, or explicitly rejected with a stated reason in the PR body).
- **On failure: fix and retry, bounded.** Fix the failures/findings and re-run the gate. After
  **2 failed fix rounds**, STOP — do not merge; leave the PR open, report what's failing and
  why, and wait for the user.

## Verification first

Never claim work is done without demonstrating it. With no test suite, this rule carries the
whole weight: exercise the change directly — run it, read the file back, observe the behavior —
and report what you actually saw, not what you expect. "Done" means the merge gate passed and
the change was seen working, not "the code looks right." Report failures faithfully, with
output.

## When to ask

Autonomy does not cover these — always ask first:

- Adding new dependencies.
- Deleting files or data; destructive git operations (`reset --hard`, `push --force`, ...).
- Changing the scope of the task, or reworking behavior that already merged.
- Anything touching credentials, secrets, API keys, or money: broker/exchange config,
  live-trading switches, risk limits, order sizing.
- Reusing an existing branch for new work.
