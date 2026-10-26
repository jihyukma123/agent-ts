"""Port collisions must not prevent the comparison demo from starting."""
import errno
import socket
from types import SimpleNamespace

import pytest

from demo.run import reserve_ports, services


def test_busy_ports_are_reserved_uniquely_and_propagated(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        args = SimpleNamespace(web_port=port, api_port=port, baseline_port=port, harness_port=port,
                               runtime_dir=tmp_path, baseline_dir=tmp_path, harness_dir=tmp_path, trace=False)
        reserved = reserve_ports(args)
        try:
            chosen = [sock.getsockname()[1] for sock in reserved.values()]
            assert len(set(chosen)) == 4
            assert port not in chosen
            for selected in chosen:
                with socket.socket() as competing:
                    with pytest.raises(OSError) as exc:
                        competing.bind(("127.0.0.1", selected))
                    assert exc.value.errno == errno.EADDRINUSE
            specs = {spec[0]: spec for spec in services(args)}
            assert specs["api"][2] == args.api_port
            assert specs["web"][5]["ITSM_AGENT_BASE_URL"] == f"http://127.0.0.1:{args.baseline_port}"
            assert specs["web"][5]["ITSM_HARNESS_AGENT_BASE_URL"] == f"http://127.0.0.1:{args.harness_port}"
            assert occupied.fileno() != -1
        finally:
            for sock in reserved.values():
                sock.close()


def test_available_preferred_port_is_not_taken_by_fallback():
    with socket.socket() as occupied, socket.socket() as preferred:
        occupied.bind(("127.0.0.1", 0))
        preferred.bind(("127.0.0.1", 0))
        busy = occupied.getsockname()[1]
        available = preferred.getsockname()[1]
        preferred.close()
        args = SimpleNamespace(web_port=busy, api_port=available, baseline_port=busy, harness_port=busy)
        reserved = reserve_ports(args)
        try:
            assert args.api_port == available
            assert args.web_port != busy
        finally:
            for sock in reserved.values():
                sock.close()
