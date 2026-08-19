# Backlog conventions

The local Markdown files are the reviewable source of truth. Titles use an imperative
`type(scope): action` form. `P1`, `P2`, and `P3` mean must, should, and could; estimates
are XS, S, M, or L. All items begin in `todo` and use Definition of Done testing in the
same ticket.

Each acceptance criterion is independently observable. Dependencies name only local
ticket IDs and are intentionally shallow. Labels are applied on publication if they
exist; missing GitLab labels will not be invented without confirmation.

GitLab issues are project-scoped work items and support labels and cross-references;
the publication plan therefore creates the dependency roots first and reconciles each
returned issue URL into its local file. Sources: [GitLab Issues](https://docs.gitlab.com/user/project/issues/),
[GitLab Issues API](https://docs.gitlab.com/api/issues/).
