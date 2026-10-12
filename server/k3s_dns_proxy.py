#!/usr/bin/env python3
"""Split-horizon DNS forwarder for TunnelSats k3s mode (Issue #124).

Routes internal Kubernetes cluster names to kube-dns and binds external DNS
queries to the protected WireGuard tunnel interface, failing closed with
SERVFAIL if the tunnel or upstream is unavailable.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import os
import signal
import socket
import socketserver
import struct
import threading

DNS_HEADER_LEN = 12
RCODE_FORMERR = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3
DEFAULT_CLUSTER_SUFFIXES: tuple[str, ...] = ()
DEFAULT_CLUSTER_CIDRS: tuple[str, ...] = (
    "10.42.0.0/16",
    "10.43.0.0/16",
    "fd00:42::/48",
    "fd00:43::/112",
)
PRIVATE_REVERSE_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "fc00::/7",
)


class DNSFormatError(ValueError):
    """Raised when a DNS packet is truncated or violates wire-format rules."""

    def __init__(self, message: str, txid: int | None = None, flags: int = 0) -> None:
        super().__init__(message)
        self.txid = txid
        self.flags = flags


@dataclass(frozen=True)
class ParsedDNSQuery:
    txid: int
    flags: int
    qdcount: int
    qname: str
    qtype: int
    qclass: int
    question_section: bytes
    max_udp_payload: int = 512


def decode_qname(packet: bytes, offset: int = DNS_HEADER_LEN) -> tuple[str, int]:
    """Decode a DNS wire-format domain name starting at offset."""
    if offset >= len(packet):
        raise DNSFormatError("Truncated QNAME offset")

    labels: list[str] = []
    pos = offset
    end_pos: int | None = None
    jumped = False
    visited_offsets: set[int] = set()
    total_wire_len = 0
    hops = 0

    while True:
        if pos >= len(packet):
            raise DNSFormatError("Truncated QNAME label length")
        length = packet[pos]

        if length == 0:
            if not jumped:
                end_pos = pos + 1
            break

        top_bits = length & 0xC0
        if top_bits == 0xC0:
            if pos + 1 >= len(packet):
                raise DNSFormatError("Truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | packet[pos + 1]
            if (
                pointer < DNS_HEADER_LEN
                or pointer >= len(packet)
                or pointer >= pos
                or pointer in visited_offsets
            ):
                raise DNSFormatError("Invalid or cyclic DNS compression pointer")
            visited_offsets.add(pos)
            visited_offsets.add(pointer)
            if not jumped:
                end_pos = pos + 2
                jumped = True
            pos = pointer
            hops += 1
            if hops > 64:
                raise DNSFormatError("Exceeded compression pointer hop limit")
            continue

        if top_bits != 0:
            raise DNSFormatError("Unsupported DNS label type bits")

        pos += 1
        if pos + length > len(packet):
            raise DNSFormatError("Truncated QNAME label bytes")
        total_wire_len += length + 1
        if total_wire_len > 255:
            raise DNSFormatError("QNAME exceeds maximum wire length of 255 octets")

        raw_label = packet[pos : pos + length]
        if any(b <= 0x20 or b == 0x2E or b >= 0x7F for b in raw_label):
            raise DNSFormatError("Invalid byte or embedded dot in QNAME label")
        label = raw_label.decode("ascii")
        labels.append(label)
        pos += length

    assert end_pos is not None
    return ".".join(labels), end_pos


extract_qname = decode_qname


def _extract_edns_udp_payload_size(packet: bytes, offset: int, arcount: int) -> int:
    max_payload = 512
    pos = offset
    for _ in range(arcount):
        if pos >= len(packet):
            break
        try:
            _, pos = decode_qname(packet, pos)
        except DNSFormatError:
            break
        if pos + 10 > len(packet):
            break
        rr_type, rr_class, _rr_ttl, rdlength = struct.unpack("!HHIH", packet[pos : pos + 10])
        pos += 10
        if pos + rdlength > len(packet):
            break
        if rr_type == 41:
            max_payload = max(512, min(int(rr_class), 65535))
        pos += rdlength
    return max_payload


def parse_dns_query(packet: bytes) -> ParsedDNSQuery:
    """Parse a raw DNS query packet and extract header + first question."""
    if len(packet) < DNS_HEADER_LEN:
        raise DNSFormatError("Packet shorter than 12-byte DNS header", txid=None)

    txid, flags, qdcount, ancount, nscount, arcount = struct.unpack(
        "!HHHHHH", packet[:DNS_HEADER_LEN]
    )
    qr = (flags >> 15) & 0x1
    if qr != 0:
        raise DNSFormatError("Packet is a DNS response, not a query", txid=txid, flags=flags)
    if qdcount != 1 or ancount != 0 or nscount != 0:
        raise DNSFormatError(
            "DNS query must have exactly one question and empty answer/authority sections",
            txid=txid,
            flags=flags,
        )

    try:
        qname, qname_end = decode_qname(packet, DNS_HEADER_LEN)
    except DNSFormatError as exc:
        raise DNSFormatError(str(exc), txid=txid, flags=flags) from exc

    if qname_end + 4 > len(packet):
        raise DNSFormatError("Truncated QTYPE/QCLASS in question section", txid=txid, flags=flags)

    qtype, qclass = struct.unpack("!HH", packet[qname_end : qname_end + 4])
    question_section = packet[DNS_HEADER_LEN : qname_end + 4]
    max_udp_payload = (
        _extract_edns_udp_payload_size(packet, qname_end + 4, arcount) if arcount > 0 else 512
    )
    return ParsedDNSQuery(
        txid=txid,
        flags=flags,
        qdcount=qdcount,
        qname=qname,
        qtype=qtype,
        qclass=qclass,
        question_section=question_section,
        max_udp_payload=max_udp_payload,
    )


def _parse_cidr_networks(
    cidrs: tuple[str, ...] | list[str],
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        for item in raw.split(","):
            token = item.strip()
            if not token:
                continue
            try:
                nets.append(ipaddress.ip_network(token, strict=False))
            except ValueError:
                continue
    return nets


def _extract_reverse_dns_ip(
    normalized_qname: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if normalized_qname.endswith(".in-addr.arpa"):
        prefix = normalized_qname[: -len(".in-addr.arpa")]
        octets = prefix.split(".")
        if len(octets) == 4 and all(o.isdigit() for o in octets):
            try:
                return ipaddress.IPv4Address(".".join(reversed(octets)))
            except ValueError:
                return None
        return None
    if normalized_qname.endswith(".ip6.arpa"):
        prefix = normalized_qname[: -len(".ip6.arpa")]
        nibbles = prefix.split(".")
        if len(nibbles) == 32 and all(len(n) == 1 and n in "0123456789abcdef" for n in nibbles):
            rev_hex = "".join(reversed(nibbles))
            hextets = [rev_hex[i : i + 4] for i in range(0, 32, 4)]
            try:
                return ipaddress.IPv6Address(":".join(hextets))
            except ValueError:
                return None
        return None
    return None


def _is_valid_k8s_subpath(prefix_labels: list[str], zone_kind: str) -> bool:
    if not prefix_labels:
        return True
    non_srv = [lbl for lbl in prefix_labels if not lbl.startswith("_")]
    if zone_kind == "svc":
        return 2 <= len(non_srv) <= 3
    if zone_kind == "pod":
        return 1 <= len(non_srv) <= 2
    return False


def classify_qname(
    qname: str,
    cluster_suffixes: tuple[str, ...] = DEFAULT_CLUSTER_SUFFIXES,
    cluster_cidrs: tuple[str, ...] = DEFAULT_CLUSTER_CIDRS,
    cluster_domain: str = "cluster.local",
) -> str:
    """Classify a DNS query domain as 'cluster', 'external', or 'nxdomain'."""
    normalized = qname.strip().rstrip(".").lower()
    if not normalized:
        return "external"
    if normalized == "svc" or normalized.endswith(".svc"):
        return "nxdomain"
    if "." not in normalized:
        return "external"

    # Reverse DNS (in-addr.arpa / ip6.arpa): CoreDNS enables fallthrough for
    # in-addr.arpa and ip6.arpa by default, which would forward missing PTR
    # queries to the node's upstream resolvers outside WireGuard. Answer
    # internal/cluster/private reverse lookups locally with NXDOMAIN and route
    # public reverse lookups through the WireGuard-bound external path.
    if normalized.endswith(".in-addr.arpa") or normalized.endswith(".ip6.arpa"):
        rev_ip = _extract_reverse_dns_ip(normalized)
        if rev_ip is None:
            return "nxdomain"
        if rev_ip.is_loopback or rev_ip.is_link_local or rev_ip.is_unspecified:
            return "nxdomain"
        local_nets = _parse_cidr_networks((*cluster_cidrs, *PRIVATE_REVERSE_CIDRS))
        if any(rev_ip.version == net.version and rev_ip in net for net in local_nets):
            return "nxdomain"
        return "external"

    # Only trust the configured cluster_domain (and any explicit cluster_suffixes)
    # so custom-domain clusters never forward cluster.local to CoreDNS.
    custom_domain = cluster_domain.strip().strip(".").lower() or "cluster.local"
    extra_domains = tuple(
        s.strip().strip(".").lower()
        for s in cluster_suffixes
        if s.strip().strip(".").lower()
        and s.strip().strip(".").lower() not in ("in-addr.arpa", "ip6.arpa", "svc")
    )
    managed_domains = tuple(dict.fromkeys((custom_domain, *extra_domains)))
    for dom in managed_domains:
        if normalized.endswith("." + dom) or normalized == dom:
            if normalized in (dom, f"svc.{dom}", f"pod.{dom}"):
                return "cluster"
            svc_suffix = f".svc.{dom}"
            if normalized.endswith(svc_suffix):
                prefix = normalized[: -len(svc_suffix)].split(".")
                return "cluster" if _is_valid_k8s_subpath(prefix, "svc") else "nxdomain"
            pod_suffix = f".pod.{dom}"
            if normalized.endswith(pod_suffix):
                prefix = normalized[: -len(pod_suffix)].split(".")
                return "cluster" if _is_valid_k8s_subpath(prefix, "pod") else "nxdomain"
            return "nxdomain"

    if normalized == "cluster.local" or normalized.endswith(".cluster.local"):
        return "nxdomain"

    return "external"


def build_error_response(query_packet: bytes, rcode: int = RCODE_SERVFAIL) -> bytes | None:
    """Build a wire-format DNS error response (SERVFAIL=2, FORMERR=1, NXDOMAIN=3).

    Returns None if query_packet is shorter than the 12-byte DNS header.
    """
    if len(query_packet) < DNS_HEADER_LEN:
        return None

    txid, req_flags, _qd, _an, _ns, _ar = struct.unpack(
        "!HHHHHH", query_packet[:DNS_HEADER_LEN]
    )
    opcode = (req_flags >> 11) & 0x0F
    rd = (req_flags >> 8) & 0x01
    resp_flags = (1 << 15) | (opcode << 11) | (rd << 8) | (1 << 7) | (rcode & 0x0F)

    question_section = b""
    qdcount = 0
    if rcode != RCODE_FORMERR:
        try:
            parsed = parse_dns_query(query_packet)
            question_section = parsed.question_section
            qdcount = 1
        except DNSFormatError:
            question_section = b""
            qdcount = 0

    header = struct.pack("!HHHHHH", txid, resp_flags, qdcount, 0, 0, 0)
    return header + question_section


def _parse_ipv4_endpoint(raw_token: str, default_port: int = 53) -> tuple[str, int] | None:
    token = raw_token.strip()
    if not token:
        return None
    host_part = token
    port = default_port
    if ":" in token and token.count(":") == 1:
        candidate_host, candidate_port = token.rsplit(":", 1)
        if candidate_port.isdigit():
            parsed_port = int(candidate_port)
            if 1 <= parsed_port <= 65535:
                host_part = candidate_host.strip()
                port = parsed_port
            else:
                return None
        else:
            return None
    try:
        addr = ipaddress.ip_address(host_part)
    except ValueError:
        return None
    if addr.version != 4:
        return None
    return (str(addr), port)


def parse_endpoint_list(
    raw_csv: str,
    default_port: int = 53,
) -> list[tuple[str, int]]:
    """Parse comma-separated IPv4[:port] endpoints, preserving order and deduplicating."""
    endpoints: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for part in raw_csv.split(","):
        parsed = _parse_ipv4_endpoint(part, default_port=default_port)
        if parsed is not None and parsed not in seen:
            seen.add(parsed)
            endpoints.append(parsed)
    return endpoints


def filter_usable_vpn_dns(
    endpoints: list[tuple[str, int]],
    cluster_cidrs: tuple[str, ...] = DEFAULT_CLUSTER_CIDRS,
) -> list[tuple[str, int]]:
    """Filter out loopback, unspecified, link-local, multicast, reserved, and cluster-CIDR IPv4 endpoints."""
    cluster_nets = [
        net
        for net in _parse_cidr_networks(cluster_cidrs)
        if net.version == 4 and net.prefixlen < 32
    ]
    usable: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in endpoints:
        try:
            addr = ipaddress.IPv4Address(item[0])
        except ValueError:
            continue
        if (
            addr.is_loopback
            or addr.is_unspecified
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
        ):
            continue
        if any(addr in net for net in cluster_nets):
            continue
        if item not in seen:
            seen.add(item)
            usable.append(item)
    return usable


def parse_wg_dns_servers(
    wg_conf_path: str | None,
    fallback: str = "1.1.1.1,9.9.9.9",
    cluster_cidrs: tuple[str, ...] = DEFAULT_CLUSTER_CIDRS,
) -> list[tuple[str, int]]:
    """Extract valid non-local/non-cluster IPv4 DNS servers from a WireGuard config or fallback."""
    candidates: list[tuple[str, int]] = []
    if wg_conf_path and os.path.isfile(wg_conf_path):
        try:
            with open(wg_conf_path, encoding="utf-8") as handle:
                in_interface = False
                for raw_line in handle:
                    line = raw_line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        in_interface = line.lower() == "[interface]"
                        continue
                    if in_interface and "=" in line:
                        key, value = line.split("=", 1)
                        if key.strip().lower() == "dns":
                            candidates.extend(parse_endpoint_list(value))
        except OSError:
            candidates = []

    discovered = filter_usable_vpn_dns(candidates, cluster_cidrs=cluster_cidrs)
    if discovered:
        return discovered
    return filter_usable_vpn_dns(parse_endpoint_list(fallback), cluster_cidrs=cluster_cidrs)



def parse_resolv_conf_nameserver(
    resolv_conf_path: str = "/etc/resolv.conf",
    default_host: str = "10.43.0.10",
    default_port: int = 53,
    cluster_cidrs: tuple[str, ...] = DEFAULT_CLUSTER_CIDRS,
    pod_cidrs: tuple[str, ...] = (),
) -> tuple[str, int]:
    """Discover the first cluster-internal IPv4 nameserver in resolv.conf."""
    cluster_nets = [
        net
        for net in _parse_cidr_networks(cluster_cidrs)
        if net.version == 4 and net.prefixlen < 32
    ]
    if resolv_conf_path and os.path.isfile(resolv_conf_path):
        try:
            with open(resolv_conf_path, encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].lower() == "nameserver":
                        parsed = _parse_ipv4_endpoint(parts[1], default_port=default_port)
                        if parsed is None:
                            continue
                        addr = ipaddress.IPv4Address(parsed[0])
                        if addr.is_loopback or addr.is_unspecified:
                            continue
                        if cluster_nets and not any(addr in net for net in cluster_nets):
                            continue
                        return parsed
        except OSError:
            pass

    fallback = _parse_ipv4_endpoint(default_host, default_port=default_port)
    if fallback is not None and cluster_nets:
        fallback_addr = ipaddress.IPv4Address(fallback[0])
        if not any(fallback_addr in net for net in cluster_nets):
            k8s_svc_host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
            if k8s_svc_host:
                try:
                    svc_host_addr = ipaddress.IPv4Address(k8s_svc_host)
                    for net in cluster_nets:
                        if svc_host_addr in net and net.num_addresses > 10:
                            return (str(net.network_address + 10), default_port)
                except ValueError:
                    pass

            raw_pod_tokens = list(pod_cidrs) + [
                tok.strip()
                for tok in os.environ.get("K3S_POD_CIDRS", "").split(",")
                if tok.strip()
            ]
            explicit_pod_nets = {
                net
                for net in _parse_cidr_networks(tuple(raw_pod_tokens))
                if net.version == 4 and net.prefixlen < 32
            }
            target_ip = os.environ.get("DOCKER_TARGET_IP", "").strip()
            if target_ip:
                try:
                    target_addr = ipaddress.IPv4Address(target_ip)
                    for net in cluster_nets:
                        if target_addr in net:
                            explicit_pod_nets.add(net)
                except ValueError:
                    pass

            known_pod_defaults = {
                ipaddress.IPv4Network("10.42.0.0/16"),
                ipaddress.IPv4Network("10.244.0.0/16"),
                ipaddress.IPv4Network("192.168.0.0/16"),
            }
            known_svc_defaults = {
                ipaddress.IPv4Network("10.43.0.0/16"),
                ipaddress.IPv4Network("10.96.0.0/12"),
            }
            service_candidates = (
                [
                    net
                    for net in cluster_nets
                    if net in known_svc_defaults and net not in explicit_pod_nets
                ]
                + [
                    net
                    for net in cluster_nets
                    if net not in explicit_pod_nets and net not in known_pod_defaults
                ]
                + [net for net in cluster_nets if net not in explicit_pod_nets]
                + list(cluster_nets)
            )
            for net in service_candidates:
                if net.num_addresses > 10:
                    return (str(net.network_address + 10), default_port)
    return fallback if fallback is not None else ("10.43.0.10", default_port)



def parse_resolv_conf_cluster_domain(
    resolv_conf_path: str = "/etc/resolv.conf",
    default_domain: str = "cluster.local",
) -> str:
    """Discover the Kubernetes cluster domain from search/domain directives in resolv.conf."""
    fallback = default_domain.strip().strip(".").lower() or "cluster.local"
    if resolv_conf_path and os.path.isfile(resolv_conf_path):
        try:
            with open(resolv_conf_path, encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].lower() in ("search", "domain"):
                        for token in parts[1:]:
                            clean = token.strip().strip(".").lower()
                            if clean.startswith("svc.") and len(clean) > 4:
                                return clean[4:]
                            idx = clean.find(".svc.")
                            if idx > 0 and idx + 5 < len(clean):
                                return clean[idx + 5 :]
        except OSError:
            pass
    return fallback


def bind_socket_to_interface(
    sock: object,
    vpn_interface: str,
    sys_net_root: str = "/sys/class/net",
) -> None:
    """Verify vpn_interface is present and IFF_UP, then bind sock via SO_BINDTODEVICE."""
    iface = vpn_interface.strip()
    if not iface or "/" in iface or len(iface.encode("utf-8")) >= 16:
        raise OSError(f"Invalid VPN interface name: {vpn_interface!r}")

    iface_dir = os.path.join(sys_net_root, iface)
    if not os.path.isdir(iface_dir):
        raise OSError(f"VPN interface {iface} is missing")

    flags_path = os.path.join(iface_dir, "flags")
    if os.path.isfile(flags_path):
        try:
            with open(flags_path, encoding="utf-8") as handle:
                raw_flags = handle.read().strip()
            flags_val = int(raw_flags, 16)
        except (OSError, ValueError) as exc:
            raise OSError(f"Cannot read interface flags for {iface}") from exc
        if (flags_val & 0x1) == 0:
            raise OSError(f"VPN interface {iface} is down")

    so_bindtodevice = getattr(socket, "SO_BINDTODEVICE", 25)
    sock.setsockopt(socket.SOL_SOCKET, so_bindtodevice, iface.encode("utf-8") + b"\x00")


def _recv_exact(sock: object, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise OSError("Unexpected EOF reading TCP DNS frame")
        buf.extend(chunk)
    return bytes(buf)


def _response_matches_query(response: bytes, query: ParsedDNSQuery) -> bool:
    if len(response) < DNS_HEADER_LEN:
        return False
    resp_txid, resp_flags, resp_qdcount, _, _, _ = struct.unpack(
        "!HHHHHH", response[:DNS_HEADER_LEN]
    )
    qr = (resp_flags >> 15) & 0x1
    if resp_txid != query.txid or qr != 1:
        return False
    if resp_qdcount >= 1:
        try:
            resp_qname, qname_end = decode_qname(response, DNS_HEADER_LEN)
            if qname_end + 4 > len(response):
                return False
            resp_qtype, resp_qclass = struct.unpack("!HH", response[qname_end : qname_end + 4])
        except DNSFormatError:
            return False
        if (
            resp_qname.rstrip(".").lower() != query.qname.rstrip(".").lower()
            or resp_qtype != query.qtype
            or resp_qclass != query.qclass
        ):
            return False
    return True


def _response_is_truncated(response: bytes) -> bool:
    if len(response) < DNS_HEADER_LEN:
        return False
    _, resp_flags, _, _, _, _ = struct.unpack("!HHHHHH", response[:DNS_HEADER_LEN])
    return bool((resp_flags >> 9) & 0x1)


def build_truncated_response(parsed: ParsedDNSQuery, upstream_response: bytes) -> bytes:
    """Build a size-limited TC=1 DNS response prompting the UDP client to retry over TCP."""
    if len(upstream_response) >= DNS_HEADER_LEN:
        _, up_flags, _, _, _, _ = struct.unpack("!HHHHHH", upstream_response[:DNS_HEADER_LEN])
        resp_flags = (up_flags | (1 << 15) | (1 << 9)) & 0xFFFF
    else:
        opcode = (parsed.flags >> 11) & 0x0F
        rd = (parsed.flags >> 8) & 0x01
        resp_flags = (1 << 15) | (opcode << 11) | (1 << 9) | (rd << 8) | (1 << 7)
    header = struct.pack("!HHHHHH", parsed.txid, resp_flags, 1, 0, 0, 0)
    candidate = header + parsed.question_section
    if len(candidate) <= parsed.max_udp_payload:
        return candidate
    return struct.pack("!HHHHHH", parsed.txid, resp_flags, 0, 0, 0, 0)


class SplitHorizonResolver:
    """Routes cluster queries to cluster_dns and external queries over vpn_interface to vpn_dns."""

    def __init__(
        self,
        cluster_dns: tuple[str, int],
        vpn_dns: list[tuple[str, int]],
        vpn_interface: str = "tunnelsatsv2",
        timeout: float = 3.0,
        cluster_suffixes: tuple[str, ...] = DEFAULT_CLUSTER_SUFFIXES,
        cluster_cidrs: tuple[str, ...] = DEFAULT_CLUSTER_CIDRS,
        cluster_domain: str = "cluster.local",
        socket_binder: Callable[[object, str], None] | None = None,
        wg_conf_path: str | None = None,
    ) -> None:
        self.cluster_dns = cluster_dns
        self.vpn_dns = list(vpn_dns)
        self.vpn_interface = vpn_interface
        self.timeout = float(timeout)
        self.cluster_suffixes = tuple(cluster_suffixes)
        self.cluster_cidrs = tuple(cluster_cidrs)
        self.cluster_domain = cluster_domain.strip().strip(".").lower() or "cluster.local"
        self.socket_binder = socket_binder or bind_socket_to_interface
        self.wg_conf_path = wg_conf_path
        self._wg_conf_mtime_ns: int | None = None
        self._lock = threading.Lock()

    def _get_vpn_dns_upstreams(self) -> list[tuple[str, int]]:
        if not self.wg_conf_path:
            return list(self.vpn_dns)
        try:
            stat_res = os.stat(self.wg_conf_path)
            mtime_ns = stat_res.st_mtime_ns
        except OSError:
            mtime_ns = None
        with self._lock:
            if mtime_ns != self._wg_conf_mtime_ns:
                self._wg_conf_mtime_ns = mtime_ns
                self.vpn_dns = parse_wg_dns_servers(
                    self.wg_conf_path,
                    cluster_cidrs=self.cluster_cidrs,
                )
            return list(self.vpn_dns)

    def _forward_udp_single(
        self,
        query_packet: bytes,
        parsed_query: ParsedDNSQuery,
        upstream: tuple[str, int],
        bind_vpn: bool,
    ) -> bytes:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            if bind_vpn:
                self.socket_binder(sock, self.vpn_interface)
            sock.sendto(query_packet, upstream)
            while True:
                response, addr = sock.recvfrom(65535)
                if addr[0] != upstream[0] or addr[1] != upstream[1]:
                    continue
                if _response_matches_query(response, parsed_query):
                    return response
                raise OSError("Upstream UDP DNS response failed RFC 5452 validation")

    def _forward_tcp_single(
        self,
        query_packet: bytes,
        parsed_query: ParsedDNSQuery,
        upstream: tuple[str, int],
        bind_vpn: bool,
    ) -> bytes:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            if bind_vpn:
                self.socket_binder(sock, self.vpn_interface)
            sock.connect(upstream)
            framed = struct.pack("!H", len(query_packet)) + query_packet
            sock.sendall(framed)
            raw_len = _recv_exact(sock, 2)
            (resp_len,) = struct.unpack("!H", raw_len)
            if resp_len < DNS_HEADER_LEN:
                raise OSError("Upstream TCP DNS response shorter than 12-byte header")
            response = _recv_exact(sock, resp_len)
            if not _response_matches_query(response, parsed_query):
                raise OSError("Upstream TCP DNS response failed RFC 5452 validation")
            return response

    def _forward_to_upstreams(
        self,
        query_packet: bytes,
        parsed_query: ParsedDNSQuery,
        upstreams: list[tuple[str, int]],
        transport: str,
        bind_vpn: bool,
    ) -> bytes | None:
        if bind_vpn and not self.vpn_interface.strip():
            return None
        if not upstreams:
            return None

        for upstream in upstreams:
            try:
                if transport == "tcp":
                    return self._forward_tcp_single(query_packet, parsed_query, upstream, bind_vpn)
                response = self._forward_udp_single(query_packet, parsed_query, upstream, bind_vpn)
                if _response_is_truncated(response):
                    try:
                        response = self._forward_tcp_single(
                            query_packet, parsed_query, upstream, bind_vpn
                        )
                    except OSError:
                        pass
                if len(response) > parsed_query.max_udp_payload:
                    return build_truncated_response(parsed_query, response)
                return response
            except OSError:
                continue
        return None

    def resolve(self, query_packet: bytes, transport: str = "udp") -> bytes | None:
        """Resolve a raw DNS query packet and return a wire-format DNS response."""
        if len(query_packet) < DNS_HEADER_LEN:
            return None

        try:
            parsed = parse_dns_query(query_packet)
        except DNSFormatError:
            return build_error_response(query_packet, rcode=RCODE_FORMERR)

        route_target = classify_qname(
            parsed.qname,
            cluster_suffixes=self.cluster_suffixes,
            cluster_cidrs=self.cluster_cidrs,
            cluster_domain=self.cluster_domain,
        )
        if route_target == "nxdomain":
            return build_error_response(query_packet, rcode=RCODE_NXDOMAIN)
        if route_target == "cluster":
            upstream_resp = self._forward_to_upstreams(
                query_packet,
                parsed,
                [self.cluster_dns],
                transport=transport,
                bind_vpn=False,
            )
        else:
            # Strict fail-closed external resolution: bind to VPN interface and
            # query only VPN DNS upstreams. Never fall back to cluster_dns.
            upstream_resp = self._forward_to_upstreams(
                query_packet,
                parsed,
                self._get_vpn_dns_upstreams(),
                transport=transport,
                bind_vpn=True,
            )

        if upstream_resp is not None:
            return upstream_resp
        return build_error_response(query_packet, rcode=RCODE_SERVFAIL)


class SplitHorizonDNSServer:
    """Concurrent UDP and TCP split-horizon DNS forwarder server."""

    def __init__(
        self,
        resolver: SplitHorizonResolver,
        listen_host: str = "0.0.0.0",
        listen_port: int = 15353,
        pid_file: str | None = None,
        allowed_sources: list[str] | tuple[str, ...] | None = None,
        max_concurrent_queries: int = 64,
    ) -> None:
        self.resolver = resolver
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.pid_file = pid_file
        self.max_concurrent_queries = max(1, int(max_concurrent_queries))
        self._query_semaphore = threading.BoundedSemaphore(self.max_concurrent_queries)
        self._tcp_conn_semaphore = threading.BoundedSemaphore(self.max_concurrent_queries)
        self._restrict_sources = allowed_sources is not None
        self.allowed_networks: list[ipaddress.IPv4Network] = []
        if allowed_sources is not None:
            for item in allowed_sources:
                token = str(item).strip()
                if not token:
                    continue
                try:
                    net = ipaddress.ip_network(token, strict=False)
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid entry in allowed_sources: {item!r}"
                    ) from exc
                if net.version != 4:
                    raise ValueError(
                        f"Only IPv4 addresses/CIDRs are supported in allowed_sources: {item!r}"
                    )
                self.allowed_networks.append(net)
            if not self.allowed_networks:
                raise ValueError(
                    "Explicit allowed_sources must contain at least one valid IPv4 address or CIDR"
                )
        self._udp_server: socketserver.ThreadingUDPServer | None = None
        self._tcp_server: socketserver.ThreadingTCPServer | None = None
        self._udp_thread: threading.Thread | None = None
        self._tcp_thread: threading.Thread | None = None

    def _source_is_allowed(self, client_ip: str) -> bool:
        if not self._restrict_sources:
            return True
        try:
            addr = ipaddress.IPv4Address(client_ip)
        except ValueError:
            return False
        if addr.is_loopback:
            return True
        return any(addr in net for net in self.allowed_networks)

    @property
    def udp_address(self) -> tuple[str, int]:
        if self._udp_server is None:
            raise RuntimeError("UDP server is not started")
        return self._udp_server.server_address  # type: ignore[return-value]

    @property
    def tcp_address(self) -> tuple[str, int]:
        if self._tcp_server is None:
            raise RuntimeError("TCP server is not started")
        return self._tcp_server.server_address  # type: ignore[return-value]

    def start(self) -> None:
        resolver = self.resolver
        is_allowed = self._source_is_allowed
        query_semaphore = self._query_semaphore
        tcp_conn_semaphore = self._tcp_conn_semaphore

        class UDPServer(socketserver.ThreadingUDPServer):
            allow_reuse_address = True
            daemon_threads = True

            def process_request(self, request: object, client_address: object) -> None:
                client_ip = (
                    client_address[0]
                    if isinstance(client_address, tuple) and client_address
                    else ""
                )
                if not is_allowed(client_ip):
                    return
                if not query_semaphore.acquire(blocking=False):
                    data, sock = request  # type: ignore[misc]
                    err_resp = build_error_response(data, rcode=RCODE_SERVFAIL)
                    if err_resp is not None:
                        try:
                            sock.sendto(err_resp, client_address)
                        except OSError:
                            pass
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    query_semaphore.release()
                    raise

        class TCPServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def process_request(self, request: object, client_address: object) -> None:
                client_ip = (
                    client_address[0]
                    if isinstance(client_address, tuple) and client_address
                    else ""
                )
                if not is_allowed(client_ip):
                    self.shutdown_request(request)
                    return
                if not tcp_conn_semaphore.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    tcp_conn_semaphore.release()
                    raise

            def process_request_thread(self, request: object, client_address: object) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    tcp_conn_semaphore.release()

        class UDPHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                data, sock = self.request
                try:
                    response = resolver.resolve(data, transport="udp")
                finally:
                    query_semaphore.release()
                if response is not None:
                    try:
                        sock.sendto(response, self.client_address)
                    except OSError:
                        pass

        class TCPHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                conn = self.request
                conn.settimeout(resolver.timeout)
                try:
                    for _ in range(64):
                        raw_len = _recv_exact(conn, 2)
                        (msg_len,) = struct.unpack("!H", raw_len)
                        if msg_len == 0 or msg_len > 4096:
                            return
                        query_data = _recv_exact(conn, msg_len)
                        if not query_semaphore.acquire(blocking=False):
                            err_resp = build_error_response(query_data, rcode=RCODE_SERVFAIL)
                            if err_resp is not None:
                                conn.sendall(struct.pack("!H", len(err_resp)) + err_resp)
                            continue
                        try:
                            response = resolver.resolve(query_data, transport="tcp")
                        finally:
                            query_semaphore.release()
                        if response is not None:
                            conn.sendall(struct.pack("!H", len(response)) + response)
                except OSError:
                    pass

        self._udp_server = UDPServer((self.listen_host, self.listen_port), UDPHandler)
        preferred_tcp_port = (
            self._udp_server.server_address[1]
            if self.listen_port == 0
            else self.listen_port
        )
        try:
            self._tcp_server = TCPServer((self.listen_host, preferred_tcp_port), TCPHandler)
        except OSError:
            if self.listen_port != 0:
                self._udp_server.server_close()
                self._udp_server = None
                raise
            self._tcp_server = TCPServer((self.listen_host, 0), TCPHandler)

        self._udp_thread = threading.Thread(
            target=self._udp_server.serve_forever,
            name="k3s-dns-udp",
            daemon=True,
        )
        self._tcp_thread = threading.Thread(
            target=self._tcp_server.serve_forever,
            name="k3s-dns-tcp",
            daemon=True,
        )
        self._udp_thread.start()
        self._tcp_thread.start()

        if self.pid_file:
            pid_dir = os.path.dirname(self.pid_file)
            if pid_dir:
                os.makedirs(pid_dir, exist_ok=True)
            tmp_pid = f"{self.pid_file}.tmp.{os.getpid()}"
            with open(tmp_pid, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            os.replace(tmp_pid, self.pid_file)

    def stop(self) -> None:
        if self._udp_server is not None:
            try:
                self._udp_server.shutdown()
                self._udp_server.server_close()
            except OSError:
                pass
            self._udp_server = None

        if self._tcp_server is not None:
            try:
                self._tcp_server.shutdown()
                self._tcp_server.server_close()
            except OSError:
                pass
            self._tcp_server = None

        if self.pid_file:
            try:
                os.remove(self.pid_file)
            except OSError:
                pass


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split-horizon UDP/TCP DNS forwarder for TunnelSats k3s mode."
    )
    parser.add_argument(
        "--listen-host",
        default="0.0.0.0",
        help="Host IP to bind UDP and TCP DNS listeners (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=15353,
        help="Port to bind UDP and TCP DNS listeners (default: 15353)",
    )
    parser.add_argument(
        "--cluster-dns",
        default="",
        help="Cluster DNS upstream IP[:port] (default: discovered from /etc/resolv.conf or 10.43.0.10:53)",
    )
    parser.add_argument(
        "--cluster-cidrs",
        default=",".join(DEFAULT_CLUSTER_CIDRS),
        help="Comma-separated cluster pod/service CIDRs for PTR and resolv.conf validation",
    )
    parser.add_argument(
        "--cluster-domain",
        default="",
        help="Kubernetes cluster DNS domain (default: discovered from /etc/resolv.conf or cluster.local)",
    )
    parser.add_argument(
        "--resolv-conf",
        default="/etc/resolv.conf",
        help="Path to resolv.conf for cluster DNS discovery (default: /etc/resolv.conf)",
    )
    parser.add_argument(
        "--vpn-dns",
        default="",
        help="Comma-separated VPN DNS upstream IP[:port] list (overrides --wg-conf)",
    )
    parser.add_argument(
        "--wg-conf",
        default="/etc/wireguard/tunnelsatsv2.conf",
        help="Path to WireGuard config for DNS = ... extraction",
    )
    parser.add_argument(
        "--vpn-interface",
        default="tunnelsatsv2",
        help="WireGuard interface name for SO_BINDTODEVICE (default: tunnelsatsv2)",
    )
    parser.add_argument(
        "--allowed-sources",
        default=None,
        help="Optional comma-separated allowed client IPv4 addresses/CIDRs",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Upstream query timeout in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--max-concurrent-queries",
        type=int,
        default=64,
        help="Maximum concurrent DNS queries before returning SERVFAIL (default: 64)",
    )
    parser.add_argument(
        "--pid-file",
        default="",
        help="Optional PID file written once UDP and TCP listeners are ready",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cluster_cidrs = tuple(
        c.strip() for c in args.cluster_cidrs.split(",") if c.strip()
    ) or DEFAULT_CLUSTER_CIDRS

    if args.cluster_dns.strip():
        cluster_list = parse_endpoint_list(args.cluster_dns.strip())
        cluster_dns = cluster_list[0] if cluster_list else ("10.43.0.10", 53)
    else:
        cluster_dns = parse_resolv_conf_nameserver(
            args.resolv_conf,
            cluster_cidrs=cluster_cidrs,
        )

    cluster_domain = (
        args.cluster_domain.strip().strip(".").lower()
        or parse_resolv_conf_cluster_domain(args.resolv_conf)
    )

    wg_conf_for_reload: str | None = args.wg_conf if args.wg_conf else None
    vpn_dns: list[tuple[str, int]] = []
    if args.vpn_dns.strip():
        vpn_dns = filter_usable_vpn_dns(
            parse_endpoint_list(args.vpn_dns.strip()),
            cluster_cidrs=cluster_cidrs,
        )
        if vpn_dns:
            wg_conf_for_reload = None
    if not vpn_dns:
        vpn_dns = parse_wg_dns_servers(args.wg_conf, cluster_cidrs=cluster_cidrs)


    allowed_sources = (
        [s.strip() for s in args.allowed_sources.split(",") if s.strip()]
        if args.allowed_sources is not None
        else None
    )

    resolver = SplitHorizonResolver(
        cluster_dns=cluster_dns,
        vpn_dns=vpn_dns,
        vpn_interface=args.vpn_interface,
        timeout=args.timeout,
        cluster_cidrs=cluster_cidrs,
        cluster_domain=cluster_domain,
        wg_conf_path=wg_conf_for_reload,
    )
    server = SplitHorizonDNSServer(
        resolver=resolver,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        pid_file=args.pid_file.strip() or None,
        allowed_sources=allowed_sources,
        max_concurrent_queries=args.max_concurrent_queries,
    )

    stop_event = threading.Event()

    def _handle_signal(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    server.start()
    try:
        stop_event.wait()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
