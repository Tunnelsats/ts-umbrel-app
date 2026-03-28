FROM debian:13-slim

RUN apt-get update && apt-get install -y \
    wireguard-tools \
    iproute2 \
    iptables \
    nftables \
    curl \
    jq \
    procps \
    python3 \
    python3-flask \
    python3-requests \
    python3-yaml \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i 's/sysctl -q net.ipv4.conf.all.src_valid_mark=1/echo "Bypassing sysctl src_valid_mark"/' /usr/bin/wg-quick

COPY scripts/ /app/scripts/
COPY web/ /app/web/
COPY server/ /app/server/
COPY umbrel-app.yml /app/
RUN chmod +x /app/scripts/*.sh

ENV WG_CONF_PATH="/data/tunnelsats*.conf"

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
