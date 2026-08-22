# Backlog conventions

The local Markdown files are the reviewable source of truth. Titles use an imperative
`type(scope): action` form. `P1`, `P2`, and `P3` mean must, should, and could; estimates
are XS, S, M, or L. All items begin in `todo` and use Definition of Done testing in the
same ticket.

Each acceptance criterion is independently observable. Dependencies name only local
ticket IDs and are intentionally shallow. Labels are applied on publication if they
exist; missing GitHub labels will not be invented without confirmation.

Issues live in GitHub and are managed with `gh` (see
[GIT_WORKFLOW_COIDH.md](../GIT_WORKFLOW_COIDH.md), which documents the SSH host alias
that makes `-R owner/repo` mandatory on every `gh` call). The publication plan creates
the dependency roots first and reconciles each returned issue URL into its local file's
`remote_url`. A ticket and its issue are kept aligned in both directions: title, body,
and closure state. Sources: [GitHub Issues](https://docs.github.com/en/issues),
[GitHub CLI](https://cli.github.com/manual/gh_issue).

An earlier revision of this file described GitLab; the project has only ever published
to GitHub.
