FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    wireguard-tools \
    iproute2 \
    iptables \
    nftables \
    curl \
    jq \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i 's/sysctl -q net.ipv4.conf.all.src_valid_mark=1/echo "Bypassing sysctl src_valid_mark"/' /usr/bin/wg-quick

COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh

ENV WG_CONF_PATH="/data/tunnelsats*.conf"

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
