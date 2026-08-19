---
id: TICK-022
title: "chore(deploy): create reproducible local OCI-equivalent topology"
type: chore
epic: EPIC-08
priority: P1
estimate: L
depends_on: [TICK-005, TICK-011]
labels: [deployment, oci]
source: [NFR-9, NFR-15, NFR-16, NFR-20, NFR-31]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/23
---

## Context

The demo uses separate OpenEMR/MariaDB and AI-server VMs; local deployment must reproduce their boundary and persistent state before OCI rollout.

## Acceptance Criteria

- [ ] Local configuration uses the pinned OpenEMR release and separate OpenEMR/MariaDB and AI-server services.
- [ ] The AI session volume persists SQLite WAL state across restart.
- [ ] Secrets are supplied outside source control and no paid service is required for the default path.

## Testing

Start from a clean checkout, seed synthetic data, restart the AI service, and run health checks. CI must be green.

## Out of Scope

Provisioning a production OCI account.
