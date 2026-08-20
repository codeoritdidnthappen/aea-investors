---
id: TICK-022
title: "chore(local): create reproducible local demo topology"
type: chore
epic: EPIC-08
priority: P1
estimate: L
depends_on: [TICK-005, TICK-011]
labels: [local, docker]
source: [NFR-9, NFR-15, NFR-16, NFR-20, NFR-31]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/23
builder_commit: aca74df
---
## Context

The demo runs only as a local, reproducible Docker topology with separate OpenEMR/MariaDB and AI-server services and persistent local state.

## Acceptance Criteria

- [ ] Local configuration uses the pinned OpenEMR release and separate OpenEMR/MariaDB and AI-server services.
- [ ] The AI session volume persists SQLite WAL state across restart.
- [ ] Secrets are supplied outside source control and no paid service is required for the default path.

## Testing

Start from a clean checkout, seed synthetic data, restart the AI service, and run health checks. CI must be green.

## Out of Scope

OCI, public ingress, cloud provisioning, and production deployment.
