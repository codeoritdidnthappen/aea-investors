---
id: TICK-023
title: "chore(local): configure local Caddy ingress"
type: chore
epic: EPIC-08
priority: P1
estimate: M
depends_on: [TICK-022]
labels: [local, caddy]
source: [NFR-9, NFR-16, NFR-17, NFR-34]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/24
builder_commit: 44cccc3
---
## Context

Caddy is the local ingress for OpenEMR and chat hostnames in the disposable Docker topology; it has no public or cloud exposure.

## Acceptance Criteria

- [ ] Caddy redirects local HTTP to HTTPS using local development certificates and persists local certificate state.
- [ ] The local emr hostname reaches OpenEMR and the chat hostname proxies only to the local AI service.
- [ ] Docker network configuration prevents direct host exposure of MariaDB and internal AI ports.

## Testing

Verify local TLS, routing, restart persistence, redirect, and blocked host origin-port access. CI must be green.

## Out of Scope

Cloud services, public DNS, and paid hosting.
