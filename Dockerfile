# syntax=docker/dockerfile:1.7

ARG ALPINE_VERSION=3.24

# ---------------------------------------------------------------------------
# Fetch and verify the HKMP server release.
#
# The payload is architecture independent .NET IL, so this stage is pinned to
# the build platform: emulating it once per target platform would download the
# very same bytes twice.
# ---------------------------------------------------------------------------
FROM --platform=${BUILDPLATFORM} alpine:${ALPINE_VERSION} AS fetch

ARG HKMP_VERSION=""
ARG HKMP_SHA256=""

RUN apk add --no-cache curl unzip

RUN set -eu; \
    if [ -z "${HKMP_VERSION:-}" ]; then \
        echo "build arg HKMP_VERSION is required (e.g. v2.4.3)" >&2; exit 1; \
    fi; \
    curl -fsSL -o /tmp/HKMPServer.zip \
        "https://github.com/Extremelyd1/HKMP/releases/download/${HKMP_VERSION:-}/HKMPServer.zip"; \
    if [ -n "${HKMP_SHA256:-}" ]; then \
        echo "${HKMP_SHA256:-}  /tmp/HKMPServer.zip" | sha256sum -c -; \
    else \
        echo "WARNING: HKMP_SHA256 not supplied, skipping integrity check" >&2; \
    fi; \
    unzip -q /tmp/HKMPServer.zip -d /opt/hkmp; \
    rm /tmp/HKMPServer.zip; \
    test -f /opt/hkmp/HKMPServer.exe; \
    printf '%s\n' "${HKMP_VERSION:-}" > /opt/hkmp/version

# ---------------------------------------------------------------------------
# Runtime image.
# ---------------------------------------------------------------------------
FROM alpine:${ALPINE_VERSION}

# HKMP_VERSION is the upstream release that was fetched. IMAGE_TAG is what this
# image is published as, which carries a revision of its own when the wrapper
# changes without the server changing: v2.4.3-2 ships upstream v2.4.3.
ARG HKMP_VERSION=""
ARG IMAGE_TAG=""

LABEL org.opencontainers.image.title="HKMP dedicated server" \
      org.opencontainers.image.description="Standalone server for HKMP (Hollow Knight Multiplayer)" \
      org.opencontainers.image.source="https://github.com/Extremelyd1/HKMP" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${IMAGE_TAG:-$HKMP_VERSION}" \
      io.hkmp.server.version="${HKMP_VERSION}" \
      maintainer="Maxim Makeev <maximalmax90@mail.ru>"

# mono lives in the regular community repository since Alpine 3.20, so no
# edge/testing pin is needed. screen supplies the pty that the server's
# Console.ReadKey() console loop requires; su-exec drops privileges.
RUN apk add --no-cache \
        bash \
        curl \
        jq \
        mono \
        screen \
        su-exec \
        tini \
        tzdata \
 && addgroup -g 1000 hkmp \
 && adduser -u 1000 -G hkmp -h /data -s /sbin/nologin -D -H hkmp \
 && mkdir -p /data /run/hkmp \
 && chown hkmp:hkmp /data \
 && chmod 1777 /run/hkmp

COPY --from=fetch /opt/hkmp /opt/hkmp
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --chmod=0755 hkmp /usr/local/bin/hkmp
# python3 is already here as a dependency of mono, and its curses module
# comes with it, so the console costs no extra packages.
COPY --chmod=0755 tui.py /usr/local/lib/hkmp/tui.py

ENV HKMP_IMAGE_TAG=${IMAGE_TAG:-$HKMP_VERSION} \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HKMP_HOME=/opt/hkmp \
    HKMP_DATA=/data \
    SCREENDIR=/run/hkmp/screen \
    HOME=/data \
    PORT=2222 \
    PUID=1000 \
    PGID=1000 \
    STOP_TIMEOUT=30

# Server state lives next to the assembly: serversettings.json,
# consolesettings.json, whitelist.json, banned.json, authorized.json, the
# generated DTLS key pair and logs/. Keep it on a volume or it dies with the
# container.
VOLUME ["/data"]
WORKDIR /data

EXPOSE 2222/udp

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["hkmp", "healthcheck"]

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
