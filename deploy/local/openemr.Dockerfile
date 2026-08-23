# Pinned stable release per TICK-001's endpoint matrix (evidence/TICK-001); never
# tracks an unversioned/"latest" tag (NFR-15).
FROM openemr/openemr:8.3.0

# TICK-057: the two OAuth template overrides are copied in at build time rather
# than bind-mounted over the vendor path one file at a time.
#
# Both are byte-for-byte copies of this pinned release's own templates with a
# single addition each (TICK-037's consent-screen fix, TICK-045's iframe
# breakout). Mounting them individually failed twice: once going stale, and once
# -- TICK-057 -- truncating to zero bytes when their source directory was
# deleted out from under the running container. A single-file bind mount whose
# source disappears leaves an empty file at the destination, *shadowing* the
# vendor original rather than falling back to it, so both pages rendered blank
# with nothing logged anywhere.
#
# Copying makes what runs match what is on disk by construction. The cost is
# that changing either template needs an image rebuild; both are pinned vendor
# copies that only change when OpenEMR is upgraded, which is a rebuild anyway.
COPY openemr_overrides/templates/oauth2/scope-authorize.html.twig \
     /var/www/localhost/htdocs/openemr/templates/oauth2/scope-authorize.html.twig
COPY openemr_overrides/templates/oauth2/oauth2-login.html.twig \
     /var/www/localhost/htdocs/openemr/templates/oauth2/oauth2-login.html.twig

# The vendor image runs as root and serves as apache; match the ownership and
# permissions of the files these replace.
RUN chmod 0644 /var/www/localhost/htdocs/openemr/templates/oauth2/scope-authorize.html.twig \
               /var/www/localhost/htdocs/openemr/templates/oauth2/oauth2-login.html.twig
