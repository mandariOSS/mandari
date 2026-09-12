# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# mandari-backup: restic + PostgreSQL-Client in der Hauptversion der Produktion
# =============================================================================
# Die Dumps entstehen mit derselben PostgreSQL-Hauptversion wie die Datenbank
# und lassen sich im Notfall mit deren eigenem pg_restore einspielen.
# Das Image dient zugleich als Wegwerf-Datenbank für den Wiederherstellungstest.
# =============================================================================
ARG POSTGRES_IMAGE=postgres:16-alpine

FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic

FROM ${POSTGRES_IMAGE}

# Bewusst ohne Versions-Pin: der SSH-Client soll Sicherheitsupdates beim Neubau erhalten
# hadolint ignore=DL3018
RUN apk add --no-cache openssh-client-default

COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY scripts/backup-entrypoint.sh /usr/local/bin/backup-entrypoint
COPY scripts/mandari-backup.sh /usr/local/bin/mandari-backup
RUN chmod 755 /usr/local/bin/backup-entrypoint /usr/local/bin/mandari-backup \
    && restic version && pg_dump --version && ssh -V

ENTRYPOINT ["/usr/local/bin/backup-entrypoint"]
CMD []
