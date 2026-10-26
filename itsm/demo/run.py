#!/usr/bin/env python3
"""Run the local before/after demo without moving projects or changing keys."""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
HTTP = build_opener(ProxyHandler({}))


def arguments():
    parser = argparse.ArgumentParser(description="ITSM + 하네스 적용 전/후 데모")
    parser.add_argument("--baseline-dir", type=Path, default=ROOT.parent / "itsm_dlc")
    parser.add_argument("--harness-dir", type=Path, default=ROOT.parent / "harness-1")
    parser.add_argument("--env-file", type=Path, default=ROOT.parent / ".env", help="공통 키 파일 (기본: 저장소 루트 .env)")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "demo" / ".runtime")
    parser.add_argument("--web-port", type=int, default=8000)
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--baseline-port", type=int, default=8100)
    parser.add_argument("--harness-port", type=int, default=8101)
    parser.add_argument("--trace", action="store_true", help="itsm_dlc의 기존 trace 뷰어도 실행")
    parser.add_argument("--check", action="store_true", help="경로·Python·포트만 확인하고 종료 (키 검사 없음)")
    parser.add_argument("--startup-timeout", type=float, default=60)
    return parser.parse_args()


def services(args):
    runtime = args.runtime_dir.resolve()
    shared_env = getattr(args, "env_file", None)
    shared_env = shared_env.resolve() if shared_env and shared_env.is_file() else None
    return [
        ("api", ROOT, args.api_port, "itsm_api.main:create_app", "/api/v2/health",
         {"ITSM_API_DATA_DIR": str(runtime / "api")}),
        ("baseline", args.baseline_dir.resolve(), args.baseline_port, "app.main:create_app", "/api/health",
         {"AGENT_DATA_DIR": str(runtime / "baseline"),
          "AGENT_ENV_FILE": str(shared_env or args.baseline_dir.resolve() / ".env.local"),
          "AGENT_TRACE_FILE": str(runtime / "baseline" / "agent_trace.log"),
          **({"AGENT_TRACE": "1"} if args.trace else {})}),
        ("harness", args.harness_dir.resolve(), args.harness_port, "app.main:create_app", "/api/health",
         {"AGENT_DATA_DIR": str(runtime / "harness"),
          "AGENT_ENV_FILE": str(shared_env or args.harness_dir.resolve() / ".env.local")}),
        ("web", ROOT, args.web_port, "itsm_web.main:create_app", "/api/health",
         {"ITSM_WEB_DATA_DIR": str(runtime / "web"),
          "ITSM_AGENT_BASE_URL": f"http://127.0.0.1:{args.baseline_port}",
          "ITSM_HARNESS_AGENT_BASE_URL": f"http://127.0.0.1:{args.harness_port}"}),
    ]


def preflight(specs, args):
    ports = [spec[2] for spec in specs]
    if any(not 1 <= port <= 65535 for port in ports):
        raise RuntimeError("1~65535 범위의 포트를 지정하세요.")
    if args.startup_timeout <= 0:
        raise RuntimeError("startup-timeout은 0보다 커야 합니다.")
    for name, directory, port, factory, _, _ in specs:
        python = directory / ".venv" / "bin" / "python"
        module = directory / (factory.split(":")[0].replace(".", "/") + ".py")
        if not module.is_file() or not os.access(python, os.X_OK):
            raise RuntimeError(f"{name}: {directory} 경로와 .venv 설치를 확인하세요.")
    if args.trace and not (args.baseline_dir / "scripts" / "watch_trace.py").is_file():
        raise RuntimeError("itsm_dlc/scripts/watch_trace.py를 찾을 수 없습니다.")


def reserve_ports(args):
    """Hold sockets until uvicorn inherits them, avoiding check-then-bind races."""
    sockets = {}
    requested = {name: getattr(args, f"{name}_port") for name in ("web", "api", "baseline", "harness")}

    def reserve(name, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
        sockets[name] = sock
        setattr(args, f"{name}_port", sock.getsockname()[1])
        return True

    try:
        # Reserve all preferred ports before looking for alternatives, so a
        # busy web port does not steal the API's otherwise available port.
        for name, port in requested.items():
            reserve(name, port)
        for name, port in requested.items():
            if name in sockets:
                continue
            for offset in range(1, 65536):
                candidate = (port - 1 + offset) % 65535 + 1
                if reserve(name, candidate):
                    print(f"포트 자동 변경: {name} {port} → {candidate}", flush=True)
                    break
            else:
                raise RuntimeError(f"{name}: 사용 가능한 로컬 포트를 찾지 못했습니다.")
        return sockets
    except BaseException:
        for sock in sockets.values():
            sock.close()
        raise


def check_children(children):
    for name, process in children:
        if process.poll() is not None:
            raise RuntimeError(f"{name} 프로세스가 종료되었습니다 (exit={process.returncode}). runtime/logs/{name}.log를 확인하세요.")


def wait_ready(url, children, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_children(children)
        try:
            with HTTP.open(url, timeout=1) as response:
                health = json.load(response)
            if health.get("status") == "ok" and health.get("itsm", {}).get("reachable", True):
                return
        except (URLError, OSError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"시작 시간 초과: {url}. runtime/logs를 확인하세요.")


def stop(children):
    # Only process groups created by this launcher; never stop pre-existing servers.
    for _, process in reversed(children):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 8
    for _, process in reversed(children):
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def main():
    args = arguments()
    specs = services(args)
    children = []
    logs = []
    sockets = {}

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        preflight(specs, args)
        sockets = reserve_ports(args)
        specs = services(args)  # Rebuild every URL using the actual reserved ports.
        if args.check:
            for name, directory, port, *_ in specs:
                print(f"OK {name}: {directory} (:{port})")
            print("키·모델 연결은 검사하지 않았습니다.")
            return 0
        runtime = args.runtime_dir.resolve()
        (runtime / "logs").mkdir(parents=True, exist_ok=True)
        for name, directory, port, factory, health_path, overrides in specs:
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "ITSM_BASE_URL": f"http://127.0.0.1:{args.api_port}", **overrides}
            log = (runtime / "logs" / f"{name}.log").open("a")
            logs.append(log)
            command = [str(directory / ".venv" / "bin" / "python"), "-m", "uvicorn", factory,
                       "--factory", "--fd", str(sockets[name].fileno())]
            process = subprocess.Popen(command, cwd=directory, env=env, stdout=log, stderr=log,
                                       start_new_session=True, pass_fds=(sockets[name].fileno(),))
            children.append((name, process))
            sockets[name].close()
            wait_ready(f"http://127.0.0.1:{port}{health_path}", children, args.startup_timeout)
            print(f"준비됨: {name} (:{port})", flush=True)
        if args.trace:
            directory = args.baseline_dir.resolve()
            command = [str(directory / ".venv" / "bin" / "python"), "-u", "scripts/watch_trace.py",
                       "--file", str(runtime / "baseline" / "agent_trace.log")]
            children.append(("trace", subprocess.Popen(command, cwd=directory, start_new_session=True)))
        print(f"\n데모: http://127.0.0.1:{args.web_port}\n로그·데이터: {runtime}\nCtrl+C: 전체 종료", flush=True)
        while True:
            check_children(children)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n데모를 종료합니다.", flush=True)
        return 0
    except (RuntimeError, OSError) as exc:
        print(f"실행 실패: {exc}", flush=True)
        return 1
    finally:
        # Do not let a second Ctrl+C interrupt cleanup.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop(children)
        for sock in sockets.values():
            sock.close()
        for log in logs:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
