---
id: TICK-023
title: "chore(ingress): configure Caddy and OCI private routing"
type: chore
epic: EPIC-08
priority: P1
estimate: M
depends_on: [TICK-022]
labels: [deployment, caddy, oci]
source: [NFR-9, NFR-16, NFR-17, NFR-34]
status: todo
remote_url: null
---

## Context

Caddy on the OpenEMR VM is the only public ingress for two sslip.io hostnames on one reserved OCI IP.

## Acceptance Criteria

- [ ] Caddy redirects HTTP to HTTPS, persists ACME state, and serves valid certificates for both hostnames.
- [ ] The emr hostname reaches OpenEMR and the chat hostname proxies only over the private OCI network to the AI VM.
- [ ] OCI rules restrict MariaDB and FastAPI origin ports to private-network callers.

## Testing

Verify TLS, routing, renewal persistence, redirect, and blocked public origin-port access. CI must be green.

## Out of Scope

Cloudflare or paid hosting.
