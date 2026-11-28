"""Shell fragments that confine one user's processes inside a shared sandbox."""

from __future__ import annotations

import shlex

from .config import Config, parse_size

PERIOD_US = 100_000


def bootstrap_check(cfg: Config) -> str:
    """Run once in each new sandbox: refuse it if the limits cannot be enforced there."""
    lim = cfg.limits
    if lim.mode != "cgroup":
        return ""
    root = shlex.quote(lim.cgroup_root)
    needed = [c for c, v in (("cpu", lim.cpu), ("memory", lim.memory), ("pids", lim.pids)) if v]
    return (
        f'[ -f {root}/cgroup.controllers ] || '
        f'{{ echo "limits.mode cgroup needs cgroup v2, which this sandbox does not expose" >&2; exit 4; }}\n'
        f'for c in {" ".join(needed)}; do grep -qw "$c" {root}/cgroup.controllers || '
        f'{{ echo "cgroup controller $c is not available" >&2; exit 4; }}; done\n'
        f'[ -w {root}/cgroup.subtree_control ] || '
        f'{{ echo "cgroup hierarchy is not writable" >&2; exit 4; }}\n'
    )


def snippet(cfg: Config, user: str) -> str:
    """Placed at the top of the turn script, before `exec runuser`. Runs as root, so the
    shell joins the user's cgroup first and the agent inherits it. Applied every turn:
    cgroups live in memory, so a disk-mode suspend and resume loses them."""
    lim = cfg.limits
    if lim.mode == "cgroup":
        return _cgroup(cfg, user)
    if lim.mode == "ulimit":
        out = ""
        if lim.pids:
            out += f"ulimit -u {int(lim.pids)}\n"
        if lim.memory:
            out += f"ulimit -v {parse_size(lim.memory) // 1024}\n"   # KiB, per process
        return out
    return ""


def _cgroup(cfg: Config, user: str) -> str:
    lim = cfg.limits
    root = shlex.quote(lim.cgroup_root)
    parent = shlex.quote(f"{lim.cgroup_root}/smux")
    leaf = shlex.quote(f"{lim.cgroup_root}/smux/{user}")
    enable = " ".join(f"+{c}" for c, v in (("cpu", lim.cpu), ("memory", lim.memory),
                                           ("pids", lim.pids)) if v)
    lines = [
        f"mkdir -p {parent}",
        f'echo "{enable}" > {root}/cgroup.subtree_control',
        f'echo "{enable}" > {parent}/cgroup.subtree_control',
        f"mkdir -p {leaf}",
    ]
    if lim.cpu:
        lines.append(f'echo "{int(lim.cpu * PERIOD_US)} {PERIOD_US}" > {leaf}/cpu.max')
    if lim.memory:
        lines.append(f"echo {parse_size(lim.memory)} > {leaf}/memory.max")
        lines.append(f"echo 0 > {leaf}/memory.swap.max")          # no escaping to swap
    if lim.pids:
        lines.append(f"echo {int(lim.pids)} > {leaf}/pids.max")
    lines.append(f"echo $$ > {leaf}/cgroup.procs")
    return "\n".join(lines) + "\n"
