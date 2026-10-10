"""
fault_injector.py - our own fault injector, covering the 6 failure categories named on
the PS slide, built on the pause/kill/restore mechanics already proven live in
safety_injector.py (the cart test: caught in 5.1s, clean restore).

Each inject_* method returns an "undo" callable -- call it to reverse the fault. This is
the dead-man's-switch contract the safety watchdog depends on: it never needs to know HOW
to undo a fault, just that calling undo() does it.

Risk tiering for Day 1 (see conversation decision, 2026-10-10): CRASH and FAILED_DEPENDENCY
are the only two exercised by today's pipeline -- they're exactly what cart-pause already
validated end to end. SLOW_NETWORK, FULL_DISK, OVERLOADED_DB and TRAFFIC_SPIKE are
implemented for real here (Day-2 needs all 6), but the planner's candidate pool today is
deliberately restricted to the first two so we're not debugging a new mechanism live.
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

import docker
import requests

CRASH = "crash"
FAILED_DEPENDENCY = "failed_dependency"
SLOW_NETWORK = "slow_network"
FULL_DISK = "full_disk"
OVERLOADED_DB = "overloaded_db"
TRAFFIC_SPIKE = "traffic_spike"

# Day-1 candidate pool: proven, low-risk, instant clean rollback.
DAY1_SAFE_FAULTS = [CRASH, FAILED_DEPENDENCY]

FAULT_DESCRIPTIONS = {
    CRASH: "kill the container outright (SIGKILL) -- simulates a hard service/container crash",
    FAILED_DEPENDENCY: "pause the container (frozen, unresponsive but not dead) -- simulates a hung/unreachable dependency",
    SLOW_NETWORK: "inject latency/loss via a netem sidecar joining the target's network namespace",
    FULL_DISK: "fill the container's writable layer with a large file",
    OVERLOADED_DB: "throttle the container's CPU allotment via docker update --cpus",
    TRAFFIC_SPIKE: "crank up the load generator's concurrent user count via its own control API",
}


@dataclass
class Injection:
    fault: str
    target: str
    meta: dict
    undo: Callable[[], None]


class FaultInjector:
    def __init__(self, client: Optional[docker.DockerClient] = None):
        self.client = client or docker.from_env()

    def inject(self, fault: str, target: str, **params) -> Injection:
        method = {
            CRASH: self._crash,
            FAILED_DEPENDENCY: self._failed_dependency,
            SLOW_NETWORK: self._slow_network,
            FULL_DISK: self._full_disk,
            OVERLOADED_DB: self._overloaded_db,
            TRAFFIC_SPIKE: self._traffic_spike,
        }.get(fault)
        if not method:
            raise ValueError(f"unknown fault type: {fault}")
        return method(target, **params)

    # ---------------- proven today (Day 1 candidate pool) ----------------
    def _crash(self, target: str, **_) -> Injection:
        c = self.client.containers.get(target)
        c.kill()
        def undo():
            c.reload()
            if c.status != "running":
                c.start()
        return Injection(CRASH, target, {"mechanism": "docker kill"}, undo)

    def _failed_dependency(self, target: str, **_) -> Injection:
        c = self.client.containers.get(target)
        c.pause()
        def undo():
            c.reload()
            if c.status == "paused":
                c.unpause()
        return Injection(FAILED_DEPENDENCY, target, {"mechanism": "docker pause"}, undo)

    # ---------------- implemented, held out of today's pool ----------------
    def _slow_network(self, target: str, delay_ms: int = 500, loss_pct: int = 10, **_) -> Injection:
        """Joins the target's network namespace with a throwaway netem-capable container
        instead of modifying the target image (most target images are slim and lack
        iproute2) -- the standard chaos-engineering sidecar trick. Killing the sidecar
        removes the rule instantly."""
        helper_name = f"chaos-netem-{target}-{int(time.time())}"
        helper = self.client.containers.run(
            "gaiadocker/iproute2", network_mode=f"container:{target}",
            cap_add=["NET_ADMIN"], detach=True, name=helper_name, remove=False,
            command=f"tc qdisc add dev eth0 root netem delay {delay_ms}ms loss {loss_pct}%",
        )
        def undo():
            try:
                helper.kill()
            except docker.errors.APIError:
                pass
            helper.remove(force=True)
        return Injection(SLOW_NETWORK, target,
                          {"mechanism": "netem sidecar", "delay_ms": delay_ms, "loss_pct": loss_pct}, undo)

    def _full_disk(self, target: str, path: str = "/tmp/chaos_fill", size_mb: int = 200, **_) -> Injection:
        c = self.client.containers.get(target)
        c.exec_run(f"dd if=/dev/zero of={path} bs=1M count={size_mb}")
        def undo():
            c.exec_run(f"rm -f {path}")
        return Injection(FULL_DISK, target, {"mechanism": "dd fill", "path": path, "size_mb": size_mb}, undo)

    def _overloaded_db(self, target: str, cpu_limit: float = 0.1, **_) -> Injection:
        c = self.client.containers.get(target)
        original = c.attrs.get("HostConfig", {}).get("NanoCpus", 0) / 1e9 or None
        c.update(cpu_quota=int(cpu_limit * 100000), cpu_period=100000)
        def undo():
            if original:
                c.update(cpu_quota=int(original * 100000), cpu_period=100000)
            else:
                c.update(cpu_quota=-1)
        return Injection(OVERLOADED_DB, target,
                          {"mechanism": "docker update --cpus", "throttled_to": cpu_limit}, undo)

    def _traffic_spike(self, target: str = "load-generator", user_count: int = 500,
                        spawn_rate: int = 50, locust_url: str = "http://localhost:8089", **_) -> Injection:
        prev = requests.get(f"{locust_url}/stats/requests", timeout=5).json()
        prev_users = prev.get("user_count", 0)
        requests.post(f"{locust_url}/swarm",
                       data={"user_count": user_count, "spawn_rate": spawn_rate}, timeout=5)
        def undo():
            requests.post(f"{locust_url}/swarm",
                          data={"user_count": prev_users, "spawn_rate": spawn_rate}, timeout=5)
        return Injection(TRAFFIC_SPIKE, target,
                          {"mechanism": "locust swarm API", "user_count": user_count}, undo)
