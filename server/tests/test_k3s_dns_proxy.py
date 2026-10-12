import socket
import socketserver
import struct
import threading
import pytest

from k3s_dns_proxy import (
    DNSFormatError,
    SplitHorizonDNSServer,
    SplitHorizonResolver,
    bind_socket_to_interface,
    build_error_response,
    classify_qname,
    decode_qname,
    parse_dns_query,
    parse_resolv_conf_nameserver,
    parse_wg_dns_servers,
)


def build_dns_query(
    qname: str,
    txid: int = 0x1234,
    qtype: int = 1,
    qclass: int = 1,
    flags: int = 0x0100,
) -> bytes:
    header = struct.pack("!HHHHHH", txid, flags, 1, 0, 0, 0)
    labels = b""
    stripped = qname.strip(".")
    if stripped:
        for part in stripped.split("."):
            encoded = part.encode("ascii")
            labels += struct.pack("!B", len(encoded)) + encoded
    labels += b"\x00"
    question = labels + struct.pack("!HH", qtype, qclass)
    return header + question


def test_parse_dns_query_and_classify_split_horizon_domains():
    pkt = build_dns_query("nodes.lightning.directory.", txid=0xABCD, qtype=1)
    parsed = parse_dns_query(pkt)

    assert parsed.txid == 0xABCD
    assert parsed.qname == "nodes.lightning.directory"
    assert parsed.qtype == 1
    assert parsed.qclass == 1

    # Internal cluster domains
    assert classify_qname("lnd.default.svc.cluster.local") == "cluster"
    assert classify_qname("bitcoind.bitcoin.svc.cluster.local.") == "cluster"
    assert classify_qname("10-42-1-7.default.pod.cluster.local") == "cluster"
    assert classify_qname("kubernetes.default.svc") == "nxdomain"
    assert classify_qname("svc") == "nxdomain"
    assert classify_qname("com.") == "external"
    assert classify_qname("bitcoind") == "external"

    # Reverse DNS: cluster-internal/private CIDRs -> nxdomain locally (never CoreDNS,
    # since CoreDNS enables fallthrough on in-addr.arpa/ip6.arpa); public IPs -> external
    assert classify_qname("10.0.43.10.in-addr.arpa") == "nxdomain"
    assert classify_qname("7.1.42.10.in-addr.arpa") == "nxdomain"
    assert classify_qname("25.1.168.192.in-addr.arpa") == "nxdomain"
    assert classify_qname("2.100.51.198.in-addr.arpa") == "external"
    assert (
        classify_qname(
            "7.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.2.4.0.0.8.b.d.0.1.0.0.2.ip6.arpa",
            cluster_cidrs=("10.42.0.0/16", "10.43.0.0/16", "2001:db8:42::/64"),
        )
        == "nxdomain"
    )
    assert (
        classify_qname(
            "2.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa",
            cluster_cidrs=("10.42.0.0/16", "10.43.0.0/16", "2001:db8:42::/64"),
        )
        == "external"
    )

    # Search-path expansion directly under .cluster.local or with invalid label depth -> nxdomain
    assert classify_qname("nodes.lightning.directory.cluster.local") == "nxdomain"
    assert classify_qname("nodes.lightning.directory.default.svc.cluster.local") == "nxdomain"
    assert classify_qname("orphan.svc.cluster.local") == "nxdomain"
    assert classify_qname("_grpc._tcp.lnd.default.svc.cluster.local") == "cluster"

    # External clearnet/peer domains
    assert classify_qname("nodes.lightning.directory") == "external"
    assert classify_qname("lseed.bitcoinstats.com") == "external"
    assert classify_qname("example.com.") == "external"
    assert classify_qname("cluster.local.evil.example.com") == "external"


def test_dns_wire_edge_cases_and_error_response_builder():
    base_pkt = build_dns_query("svc.cluster.local", txid=0x2222)
    compressed_offset = len(base_pkt)
    compressed_pkt = base_pkt + b"\x03lnd\xc0\x0c"
    decoded_name, next_offset = decode_qname(compressed_pkt, compressed_offset)
    assert decoded_name == "lnd.svc.cluster.local"
    assert next_offset == len(compressed_pkt)

    loop_pkt = struct.pack("!HHHHHH", 0x3333, 0x0100, 1, 0, 0, 0) + b"\xc0\x0c\x00\x01\x00\x01"
    with pytest.raises(DNSFormatError) as exc_info:
        parse_dns_query(loop_pkt)
    assert exc_info.value.txid == 0x3333

    forward_ptr_pkt = (
        struct.pack("!HHHHHH", 0x3334, 0x0100, 1, 0, 0, 0) + b"\xc0\x20\x00\x01\x00\x01"
    )
    with pytest.raises(DNSFormatError):
        parse_dns_query(forward_ptr_pkt)

    with pytest.raises(DNSFormatError):
        parse_dns_query(b"\x12\x34\x01")
    assert build_error_response(b"\x12\x34\x01", rcode=2) is None

    valid_query = build_dns_query("nodes.lightning.directory", txid=0x7788, flags=0x0120)
    servfail = build_error_response(valid_query, rcode=2)
    assert servfail is not None
    r_txid, r_flags, r_qd, r_an, r_ns, r_ar = struct.unpack("!HHHHHH", servfail[:12])
    assert r_txid == 0x7788
    assert (r_flags >> 15) & 0x1 == 1
    assert (r_flags >> 8) & 0x1 == 1
    assert (r_flags >> 7) & 0x1 == 1
    assert (r_flags & 0x0F) == 2
    assert (r_qd, r_an, r_ns, r_ar) == (1, 0, 0, 0)
    assert servfail[12:] == valid_query[12:]

    malformed_query = struct.pack("!HHHHHH", 0x99AA, 0x0100, 1, 0, 0, 0) + b"\x0ftruncated"
    formerr = build_error_response(malformed_query, rcode=1)
    assert formerr is not None
    f_txid, f_flags, f_qd, f_an, f_ns, f_ar = struct.unpack("!HHHHHH", formerr[:12])
    assert f_txid == 0x99AA
    assert (f_flags >> 15) & 0x1 == 1
    assert (f_flags >> 7) & 0x1 == 1
    assert (f_flags & 0x0F) == 1
    assert (f_qd, f_an, f_ns, f_ar) == (0, 0, 0, 0)
    assert len(formerr) == 12


def test_upstream_dns_discovery_from_wireguard_and_resolv_conf(tmp_path):
    wg_conf = tmp_path / "tunnelsatsv2.conf"
    wg_conf.write_text(
        "[Interface]\n"
        "PrivateKey = AAAA\n"
        "Address = 10.9.0.2/32\n"
        "DNS = 127.0.0.53, 10.43.0.10, 10.64.0.1, 1.1.1.1:5353, 2606:4700:4700::1111, 10.64.0.1\n"
        "\n"
        "[Peer]\n"
        "PublicKey = BBBB\n",
        encoding="utf-8",
    )
    servers = parse_wg_dns_servers(str(wg_conf))
    assert servers == [("10.64.0.1", 53), ("1.1.1.1", 5353)]

    wg_no_dns = tmp_path / "nodns.conf"
    wg_no_dns.write_text("[Interface]\nAddress = 10.9.0.2/32\n", encoding="utf-8")
    assert parse_wg_dns_servers(str(wg_no_dns)) == [("1.1.1.1", 53), ("9.9.9.9", 53)]
    assert parse_wg_dns_servers(str(tmp_path / "missing.conf")) == [
        ("1.1.1.1", 53),
        ("9.9.9.9", 53),
    ]

    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text(
        "# CoreDNS resolv.conf\n"
        "search default.svc.cluster.local svc.cluster.local cluster.local\n"
        "nameserver 127.0.0.53\n"
        "nameserver 192.168.1.1\n"
        "nameserver ::1\n"
        "nameserver 10.43.0.10\n",
        encoding="utf-8",
    )
    # Must ignore 192.168.1.1 (outside cluster CIDRs) and select 10.43.0.10
    assert parse_resolv_conf_nameserver(str(resolv_conf)) == ("10.43.0.10", 53)

    # If resolv.conf only lists clearnet/LAN resolvers, fall back to cluster service DNS
    host_resolv = tmp_path / "host-resolv.conf"
    host_resolv.write_text("nameserver 8.8.8.8\nnameserver 192.168.1.1\n", encoding="utf-8")
    assert parse_resolv_conf_nameserver(str(host_resolv)) == ("10.43.0.10", 53)
    # When cluster_cidrs is sorted (e.g. 10.96.0.0/12 before 10.244.0.0/16), must pick service CIDR 10.96.0.10
    assert parse_resolv_conf_nameserver(
        str(host_resolv),
        cluster_cidrs=("10.96.0.0/12", "10.244.0.0/16"),
    ) == ("10.96.0.10", 53)
    assert parse_resolv_conf_nameserver(
        str(host_resolv),
        cluster_cidrs=("172.16.0.0/16", "172.20.0.0/16"),
        pod_cidrs=("172.20.0.0/16",),
    ) == ("172.16.0.10", 53)



def test_bind_socket_to_interface_and_fail_closed_servfail_without_cluster_fallback(tmp_path):
    sys_net = tmp_path / "sys_class_net"
    iface_dir = sys_net / "tunnelsatsv2"
    iface_dir.mkdir(parents=True)
    flags_file = iface_dir / "flags"
    flags_file.write_text("0x1091\n", encoding="utf-8")

    recorded_opts: list[tuple[int, int, bytes]] = []

    class DummySock:
        def setsockopt(self, level: int, optname: int, value: bytes) -> None:
            recorded_opts.append((level, optname, value))

    bind_socket_to_interface(DummySock(), "tunnelsatsv2", sys_net_root=str(sys_net))
    assert len(recorded_opts) == 1
    assert recorded_opts[0][0] == socket.SOL_SOCKET
    assert recorded_opts[0][1] == getattr(socket, "SO_BINDTODEVICE", 25)
    assert recorded_opts[0][2] == b"tunnelsatsv2\x00"

    flags_file.write_text("0x1090\n", encoding="utf-8")
    with pytest.raises(OSError, match="down"):
        bind_socket_to_interface(DummySock(), "tunnelsatsv2", sys_net_root=str(sys_net))

    with pytest.raises(OSError, match="missing"):
        bind_socket_to_interface(DummySock(), "wg-missing", sys_net_root=str(sys_net))

    cluster_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cluster_sock.bind(("127.0.0.1", 0))
    cluster_addr = cluster_sock.getsockname()
    cluster_sock.settimeout(0.05)

    try:
        def failing_binder(_sock, _iface: str) -> None:
            raise OSError("tunnelsatsv2 is down")

        resolver = SplitHorizonResolver(
            cluster_dns=cluster_addr,
            vpn_dns=[("127.0.0.1", 59999)],
            vpn_interface="tunnelsatsv2",
            timeout=0.1,
            socket_binder=failing_binder,
        )
        ext_query = build_dns_query("nodes.lightning.directory", txid=0x4455)
        resp = resolver.resolve(ext_query, transport="udp")
        assert resp is not None
        r_txid, r_flags, r_qd, _, _, _ = struct.unpack("!HHHHHH", resp[:12])
        assert r_txid == 0x4455
        assert (r_flags >> 15) & 0x1 == 1
        assert (r_flags & 0x0F) == 2
        assert r_qd == 1

        with pytest.raises(socket.timeout):
            cluster_sock.recvfrom(512)
    finally:
        cluster_sock.close()


def _find_free_dual_port() -> int:
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_sock:
            tcp_sock.bind(("127.0.0.1", 0))
            port = tcp_sock.getsockname()[1]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_sock:
                udp_sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError("Could not find free dual TCP+UDP port")


def _build_a_record_response(query_packet: bytes, ipv4_str: str) -> bytes:
    parsed = parse_dns_query(query_packet)
    resp_flags = (1 << 15) | (1 << 8) | (1 << 7)
    header = struct.pack("!HHHHHH", parsed.txid, resp_flags, 1, 1, 0, 0)
    rr = struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(ipv4_str)
    return header + parsed.question_section + rr


def test_live_loopback_udp_and_tcp_split_horizon_dns_server(tmp_path):
    cluster_queries: list[str] = []
    vpn_queries: list[str] = []
    bound_interfaces: list[str] = []
    vpn_enabled = True

    class MockClusterUDPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            q = parse_dns_query(data)
            cluster_queries.append(f"udp:{q.qname}")
            sock.sendto(_build_a_record_response(data, "10.43.0.99"), self.client_address)

    class MockVPNUDPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            if not vpn_enabled:
                return
            data, sock = self.request
            q = parse_dns_query(data)
            vpn_queries.append(f"udp:{q.qname}")
            sock.sendto(_build_a_record_response(data, "198.51.100.77"), self.client_address)

    class MockVPNTCPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            if not vpn_enabled:
                return
            raw_len = self.request.recv(2)
            if len(raw_len) < 2:
                return
            (msg_len,) = struct.unpack("!H", raw_len)
            data = self.request.recv(msg_len)
            q = parse_dns_query(data)
            vpn_queries.append(f"tcp:{q.qname}")
            resp = _build_a_record_response(data, "198.51.100.88")
            self.request.sendall(struct.pack("!H", len(resp)) + resp)

    cluster_udp = socketserver.ThreadingUDPServer(("127.0.0.1", 0), MockClusterUDPHandler)
    vpn_udp = socketserver.ThreadingUDPServer(("127.0.0.1", 0), MockVPNUDPHandler)
    vpn_tcp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), MockVPNTCPHandler)

    dead_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead_sock.bind(("127.0.0.1", 0))
    dead_port = dead_sock.getsockname()[1]
    dead_sock.close()

    for srv in (cluster_udp, vpn_udp, vpn_tcp):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    pid_file = tmp_path / "dns_proxy.pid"

    def record_binder(_sock, iface: str) -> None:
        if not vpn_enabled:
            raise OSError("VPN interface down")
        bound_interfaces.append(iface)

    resolver = SplitHorizonResolver(
        cluster_dns=cluster_udp.server_address,
        vpn_dns=[
            ("127.0.0.1", dead_port),
            vpn_udp.server_address,
            vpn_tcp.server_address,
        ],
        vpn_interface="tunnelsatsv2",
        timeout=0.15,
        socket_binder=record_binder,
    )
    server = SplitHorizonDNSServer(
        resolver=resolver,
        listen_host="127.0.0.1",
        listen_port=0,
        pid_file=str(pid_file),
        allowed_sources=["127.0.0.1/32"],
    )
    server.start()

    try:
        assert pid_file.is_file()

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(1.0)
            q_pkt = build_dns_query("bitcoind.default.svc.cluster.local", txid=0x1001)
            client.sendto(q_pkt, server.udp_address)
            resp, _ = client.recvfrom(4096)
            assert resp[-4:] == socket.inet_aton("10.43.0.99")
            assert cluster_queries == ["udp:bitcoind.default.svc.cluster.local"]
            assert vpn_queries == []
            assert bound_interfaces == []

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(1.0)
            q_pkt = build_dns_query("nodes.lightning.directory", txid=0x1002)
            client.sendto(q_pkt, server.udp_address)
            resp, _ = client.recvfrom(4096)
            assert resp[-4:] == socket.inet_aton("198.51.100.77")
            assert vpn_queries == ["udp:nodes.lightning.directory"]
            assert cluster_queries == ["udp:bitcoind.default.svc.cluster.local"]
            assert "tunnelsatsv2" in bound_interfaces

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(server.tcp_address)
            q_pkt = build_dns_query("lseed.bitcoinstats.com", txid=0x1003)
            client.sendall(struct.pack("!H", len(q_pkt)) + q_pkt)
            (r_len,) = struct.unpack("!H", client.recv(2))
            resp = client.recv(r_len)
            assert resp[-4:] == socket.inet_aton("198.51.100.88")
            assert "tcp:lseed.bitcoinstats.com" in vpn_queries
            assert cluster_queries == ["udp:bitcoind.default.svc.cluster.local"]

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(server.tcp_address)
            bad_pkt = struct.pack("!HHHHHH", 0x1004, 0x0100, 1, 0, 0, 0) + b"\xc0\x0c"
            client.sendall(struct.pack("!H", len(bad_pkt)) + bad_pkt)
            (r_len,) = struct.unpack("!H", client.recv(2))
            resp = client.recv(r_len)
            r_txid, r_flags, _, _, _, _ = struct.unpack("!HHHHHH", resp[:12])
            assert r_txid == 0x1004
            assert (r_flags & 0x0F) == 1

        # Search-path query under .cluster.local (not .svc or .pod) returns NXDOMAIN (RCODE=3)
        # locally without touching either cluster_udp or vpn_udp
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(1.0)
            q_search = build_dns_query("nodes.lightning.directory.cluster.local", txid=0x1007)
            client.sendto(q_search, server.udp_address)
            resp_search, _ = client.recvfrom(4096)
            _, search_flags, _, _, _, _ = struct.unpack("!HHHHHH", resp_search[:12])
            assert (search_flags & 0x0F) == 3  # NXDOMAIN

        vpn_enabled = False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(1.0)
            q_ext = build_dns_query("clearnet-peer.example.org", txid=0x1005)
            client.sendto(q_ext, server.udp_address)
            resp_ext, _ = client.recvfrom(4096)
            _, ext_flags, _, _, _, _ = struct.unpack("!HHHHHH", resp_ext[:12])
            assert (ext_flags & 0x0F) == 2

            q_int = build_dns_query("lnd.default.svc.cluster.local", txid=0x1006)
            client.sendto(q_int, server.udp_address)
            resp_int, _ = client.recvfrom(4096)
            assert resp_int[-4:] == socket.inet_aton("10.43.0.99")
            assert cluster_queries == [
                "udp:bitcoind.default.svc.cluster.local",
                "udp:lnd.default.svc.cluster.local",
            ]
    finally:
        server.stop()
        cluster_udp.shutdown()
        cluster_udp.server_close()
        vpn_udp.shutdown()
        vpn_udp.server_close()
        vpn_tcp.shutdown()
        vpn_tcp.server_close()

    assert not pid_file.exists()


def test_security_hardening_multi_question_smuggling_txid_and_tc_fallback():
    smuggled_pkt = (
        struct.pack("!HHHHHH", 0x5566, 0x0100, 2, 0, 0, 0)
        + b"\x03lnd\x00\x00\x01\x00\x01"
        + b"\x04evil\x03com\x00\x00\x01\x00\x01"
    )
    with pytest.raises(DNSFormatError, match="exactly one question"):
        parse_dns_query(smuggled_pkt)

    class SpoofedTXIDHandler(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            spoofed = (
                struct.pack("!H", 0x9999) + _build_a_record_response(data, "198.51.100.1")[2:]
            )
            sock.sendto(spoofed, self.client_address)

    bound_calls: list[str] = []

    class TruncatedUDPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            parsed = parse_dns_query(data)
            tc_flags = (1 << 15) | (1 << 9) | (1 << 8) | (1 << 7)
            tc_resp = (
                struct.pack("!HHHHHH", parsed.txid, tc_flags, 1, 0, 0, 0)
                + parsed.question_section
            )
            sock.sendto(tc_resp, self.client_address)

    class FullTCPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            raw_len = self.request.recv(2)
            (msg_len,) = struct.unpack("!H", raw_len)
            data = self.request.recv(msg_len)
            resp = _build_a_record_response(data, "198.51.100.250")
            self.request.sendall(struct.pack("!H", len(resp)) + resp)

    spoof_udp = socketserver.ThreadingUDPServer(("127.0.0.1", 0), SpoofedTXIDHandler)
    tc_port = _find_free_dual_port()
    tc_tcp = socketserver.ThreadingTCPServer(("127.0.0.1", tc_port), FullTCPHandler)
    tc_udp = socketserver.ThreadingUDPServer(("127.0.0.1", tc_port), TruncatedUDPHandler)

    for srv in (spoof_udp, tc_tcp, tc_udp):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        spoof_resolver = SplitHorizonResolver(
            cluster_dns=("127.0.0.1", 59998),
            vpn_dns=[spoof_udp.server_address],
            vpn_interface="tunnelsatsv2",
            timeout=0.15,
            socket_binder=lambda _s, iface: bound_calls.append(iface),
        )
        smuggled_resp = spoof_resolver.resolve(smuggled_pkt, transport="udp")
        assert smuggled_resp is not None
        _, s_flags, _, _, _, _ = struct.unpack("!HHHHHH", smuggled_resp[:12])
        assert (s_flags & 0x0F) == 1

        q_pkt = build_dns_query("nodes.lightning.directory", txid=0x6677)
        spoof_resp = spoof_resolver.resolve(q_pkt, transport="udp")
        assert spoof_resp is not None
        _, sp_flags, _, _, _, _ = struct.unpack("!HHHHHH", spoof_resp[:12])
        assert (sp_flags & 0x0F) == 2

        bound_calls.clear()
        tc_resolver = SplitHorizonResolver(
            cluster_dns=("127.0.0.1", 59998),
            vpn_dns=[("127.0.0.1", tc_port)],
            vpn_interface="tunnelsatsv2",
            timeout=0.2,
            socket_binder=lambda _s, iface: bound_calls.append(iface),
        )
        tc_resp = tc_resolver.resolve(q_pkt, transport="udp")
        assert tc_resp is not None
        assert tc_resp[-4:] == socket.inet_aton("198.51.100.250")
        assert bound_calls == ["tunnelsatsv2", "tunnelsatsv2"]
    finally:
        for srv in (spoof_udp, tc_udp, tc_tcp):
            srv.shutdown()
            srv.server_close()


def test_edns0_udp_payload_truncation_and_bounded_server_concurrency():
    base_query = build_dns_query("nodes.lightning.directory", txid=0x7788)
    # Build an EDNS0 query advertising a 8192-byte UDP payload buffer (ARCOUNT=1, OPT TYPE=41)
    edns_header = struct.pack("!HHHHHH", 0x7789, 0x0100, 1, 0, 0, 1)
    edns_opt_rr = b"\x00" + struct.pack("!HHIH", 41, 8192, 0, 0)
    edns_query = edns_header + base_query[12:] + edns_opt_rr

    parsed_std = parse_dns_query(base_query)
    parsed_edns = parse_dns_query(edns_query)
    assert parsed_std.max_udp_payload == 512
    assert parsed_edns.max_udp_payload == 8192

    class LargeUDPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            parsed = parse_dns_query(data)
            # Build a ~4500-byte response (exceeds old recvfrom(4096) and 512-byte std limit,
            # but fits inside 8192-byte EDNS0 limit).
            txt_payload = b"x" * 200
            rr = (
                b"\xc0\x0c"
                + struct.pack("!HHIH", 16, 1, 60, len(txt_payload) + 1)
                + bytes([len(txt_payload)])
                + txt_payload
            )
            big_resp = (
                struct.pack("!HHHHHH", parsed.txid, 0x8180, 1, 20, 0, 0)
                + parsed.question_section
                + (rr * 20)
            )
            sock.sendto(big_resp, self.client_address)

    large_udp = socketserver.ThreadingUDPServer(("127.0.0.1", 0), LargeUDPHandler)
    threading.Thread(target=large_udp.serve_forever, daemon=True).start()

    in_flight = threading.Event()
    release_flight = threading.Event()

    class BlockingResolver(SplitHorizonResolver):
        def resolve(self, query_packet: bytes, transport: str = "udp") -> bytes | None:
            in_flight.set()
            release_flight.wait(timeout=2.0)
            return _build_a_record_response(query_packet, "198.51.100.77")

    proxy_srv = None
    try:
        resolver = SplitHorizonResolver(
            cluster_dns=("127.0.0.1", 59998),
            vpn_dns=[large_udp.server_address],
            vpn_interface="tunnelsatsv2",
            timeout=0.5,
            socket_binder=lambda _s, _iface: None,
        )
        # 1. Standard 512-byte UDP client gets a TC=1 truncated response <= 512 bytes
        std_resp = resolver.resolve(base_query, transport="udp")
        assert std_resp is not None
        assert len(std_resp) <= 512
        _, std_flags, _, std_ancount, _, _ = struct.unpack("!HHHHHH", std_resp[:12])
        assert (std_flags >> 9) & 0x1 == 1
        assert std_ancount == 0

        # 2. EDNS0 8192-byte UDP client receives the full >4096-byte response without truncation
        edns_resp = resolver.resolve(edns_query, transport="udp")
        assert edns_resp is not None
        assert len(edns_resp) > 4096
        _, edns_flags, _, edns_ancount, _, _ = struct.unpack("!HHHHHH", edns_resp[:12])
        assert (edns_flags >> 9) & 0x1 == 0
        assert edns_ancount == 20

        # 3. Bounded concurrency limit returns SERVFAIL immediately when full
        blocking_resolver = BlockingResolver(
            cluster_dns=("127.0.0.1", 59998),
            vpn_dns=[large_udp.server_address],
            vpn_interface="tunnelsatsv2",
            timeout=1.0,
            socket_binder=lambda _s, _iface: None,
        )
        proxy_srv = SplitHorizonDNSServer(
            resolver=blocking_resolver,
            listen_host="127.0.0.1",
            listen_port=0,
            max_concurrent_queries=1,
        )
        proxy_srv.start()

        def _udp_roundtrip(addr: tuple[str, int], pkt: bytes) -> bytes:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1.5)
                s.sendto(pkt, addr)
                resp, _ = s.recvfrom(4096)
                return resp

        first_holder: list[bytes] = []

        def send_first():
            first_holder.append(_udp_roundtrip(proxy_srv.udp_address, base_query))

        t1 = threading.Thread(target=send_first, daemon=True)
        t1.start()
        assert in_flight.wait(timeout=1.0)

        second_query = build_dns_query("nodes.lightning.directory", txid=0x7790)
        second_resp = _udp_roundtrip(proxy_srv.udp_address, second_query)
        _, second_flags, _, _, _, _ = struct.unpack("!HHHHHH", second_resp[:12])
        assert (second_flags & 0x0F) == 2

        release_flight.set()
        t1.join(timeout=1.0)
        assert len(first_holder) == 1
        _, first_flags, _, _, _, _ = struct.unpack("!HHHHHH", first_holder[0][:12])
        assert (first_flags & 0x0F) == 0

        # 4. An open idle TCP connection must not consume a UDP query slot, and a
        # single TCP connection can send multiple sequential queries (e.g. A + AAAA).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_conn:
            tcp_conn.settimeout(1.0)
            tcp_conn.connect(proxy_srv.tcp_address)
            udp_while_tcp_idle = _udp_roundtrip(
                proxy_srv.udp_address,
                build_dns_query("nodes.lightning.directory", txid=0x7791),
            )
            _, idle_flags, _, _, _, _ = struct.unpack("!HHHHHH", udp_while_tcp_idle[:12])
            assert (idle_flags & 0x0F) == 0

            for txid in (0x7792, 0x7793):
                q = build_dns_query("nodes.lightning.directory", txid=txid)
                tcp_conn.sendall(struct.pack("!H", len(q)) + q)
                (rlen,) = struct.unpack("!H", tcp_conn.recv(2))
                r = tcp_conn.recv(rlen)
                r_txid, r_flags, _, _, _, _ = struct.unpack("!HHHHHH", r[:12])
                assert r_txid == txid
                assert (r_flags & 0x0F) == 0

        # 5. Custom cluster domain classification (e.g. internal.example)
        assert (
            classify_qname(
                "bitcoind.default.svc.internal.example",
                cluster_domain="internal.example",
            )
            == "cluster"
        )
        assert (
            classify_qname(
                "nodes.lightning.directory.default.svc.internal.example",
                cluster_domain="internal.example",
            )
            == "nxdomain"
        )
        assert (
            classify_qname(
                "foo.default.svc.cluster.local",
                cluster_domain="internal.example",
            )
            == "nxdomain"
        )

        # 6. Dotted DNS labels (e.g. ["example.com", "svc.cluster.local"]) are rejected
        # as malformed (FORMERR) so they can never smuggle external names into cluster DNS.
        dotted_label_query = (
            struct.pack("!HHHHHH", 0x7794, 0x0100, 1, 0, 0, 0)
            + b"\x0bexample.com\x11svc.cluster.local\x00"
            + struct.pack("!HH", 1, 1)
        )
        dotted_resp = resolver.resolve(dotted_label_query, transport="udp")
        assert dotted_resp is not None
        _, dotted_flags, _, _, _, _ = struct.unpack("!HHHHHH", dotted_resp[:12])
        assert (dotted_flags & 0x0F) == 1

        # 7. Explicit allowed_sources that contain no valid IPv4 networks raise ValueError
        # instead of failing open and permitting all clients.
        with pytest.raises(ValueError, match="allowed_sources"):
            SplitHorizonDNSServer(
                resolver=resolver,
                listen_host="127.0.0.1",
                listen_port=0,
                allowed_sources=["fd00::/64"],
            )
        with pytest.raises(ValueError, match="allowed_sources"):
            SplitHorizonDNSServer(
                resolver=resolver,
                listen_host="127.0.0.1",
                listen_port=0,
                allowed_sources=[],
            )
    finally:
        release_flight.set()
        if proxy_srv is not None:
            proxy_srv.stop()
        large_udp.shutdown()
        large_udp.server_close()

