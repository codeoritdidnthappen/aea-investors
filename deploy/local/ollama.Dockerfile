# Pinned patch release, matching the pinning convention used for mariadb/caddy and
# openemr.Dockerfile above it (NFR-15); never `latest`, and never an `-rc` build.
FROM ollama/ollama:0.32.15

# TICK-059: the model this serves is pinned by digest, and the two scripts that
# enforce that pin are copied in rather than bind-mounted -- exactly the choice
# openemr.Dockerfile documents. A single-file bind mount whose source vanishes
# leaves an *empty* file at the destination, which for an entrypoint means a model
# server that starts, verifies nothing, and serves whatever the volume happens to
# hold. That is the silent degradation this ticket exists to prevent, so it must
# not be reintroduced by the mechanism meant to prevent it.
#
# The context is the repository root, matching the other two services here.
COPY deploy/local/ollama-entrypoint.sh /usr/local/bin/aeai-ollama-entrypoint
COPY deploy/local/ollama-healthcheck.sh /usr/local/bin/aeai-ollama-healthcheck
RUN chmod 0755 /usr/local/bin/aeai-ollama-entrypoint /usr/local/bin/aeai-ollama-healthcheck

# The base image sets ENTRYPOINT ["/bin/ollama"] with CMD ["serve"]; both are
# replaced so the pinned-digest check cannot be bypassed by the default command.
ENTRYPOINT ["/usr/local/bin/aeai-ollama-entrypoint"]
CMD []
