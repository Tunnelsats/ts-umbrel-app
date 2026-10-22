FROM debian:13-slim

RUN apt-get update && apt-get install -y \
    ca-certificates \
    wireguard-tools \
    iproute2 \
    iptables \
    nftables \
    conntrack \
    curl \
    jq \
    procps \
    python3 \
    python3-flask \
    python3-requests \
    python3-yaml \
    && sed -i 's|^mozilla/\(emSign_.*\.crt\)$|!mozilla/\1|' /etc/ca-certificates.conf \
    && update-ca-certificates --fresh \
    && rm -rf /var/lib/apt/lists/*

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

RUN sed -i 's/sysctl -q net.ipv4.conf.all.src_valid_mark=1/echo "Bypassing sysctl src_valid_mark"/' /usr/bin/wg-quick

COPY scripts/ /app/scripts/
COPY web/ /app/web/
COPY server/ /app/server/
COPY tunnelsats/umbrel-app.yml /app/
RUN for f in /app/scripts/*.sh; do tr -d '\r' < "$f" > "$f.tmp" && mv "$f.tmp" "$f"; done \
    && chmod +x /app/scripts/*.sh

ENV WG_CONF_PATH="/data/tunnelsats*.conf"

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
