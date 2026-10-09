#!/usr/bin/env python3
"""devclean: reclaim disk space on a macOS dev machine, without losing work.

A dry run by default: `devclean` lists what each category would free and why
anything is kept. `devclean --apply` deletes, asking once per category unless
`--yes` is given.

Nothing that holds work is touched: a git worktree with uncommitted changes,
untracked files that exist nowhere else, or commits on no branch stays. So does
anything a running process is using (its working directory is inside it), a
booted simulator, a running emulator, and a locked worktree.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = Path(
    os.environ.get("DEVCLEAN_CONFIG", HOME / ".config/devclean/config.toml")
)
LOG_PATH = Path(
    os.environ.get("DEVCLEAN_LOG", HOME / ".local/state/devclean/devclean.log")
)

DEFAULTS: dict = {
    # Folders searched for git repos, Flutter projects and node_modules.
    "roots": ["~/projects"],
    # Shut-down simulators whose name matches are deleted, not just erased.
    "sim_delete_pattern": "",
    # Shut-down simulators holding at least this much data are erased.
    "sim_erase_min_mb": 500,
    # Simulator runtimes: keep the newest N major versions per platform.
    "runtime_keep_majors": 1,
    # Xcode device support: keep the newest N OS versions per device model.
    "device_support_keep": 1,
    # node_modules: only in projects untouched for this many days.
    "node_modules_days": 30,
    # docker: also remove unused (not only dangling) images.
    "docker_all_images": False,
    # Worktrees whose git state changed this recently stay: an idle session
    # (a Claude Code or Conductor workspace) runs no process inside it.
    "worktree_min_idle_hours": 24,
    # build/ folders written to this recently stay (a build may be running).
    "build_min_idle_hours": 2,
    # Search depth below each root.
    "max_depth": 4,
}

SDK = Path(
    os.environ.get("ANDROID_HOME")
    or os.environ.get("ANDROID_SDK_ROOT")
    or HOME / "Library/Android/sdk"
)
SIM_DEVICES = HOME / "Library/Developer/CoreSimulator/Devices"
DERIVED_DATA = HOME / "Library/Developer/Xcode/DerivedData"
DEVICE_SUPPORT_DIRS = [
    HOME / "Library/Developer/Xcode" / f"{platform}DeviceSupport"
    for platform in ("iOS ", "watchOS ", "tvOS ", "visionOS ")
]

# Directories never descended into while searching the roots.
PRUNE_DIRS = {
    "node_modules", "build", "Pods", "DerivedData", "Library", "venv",
    "__pycache__", "dist", "target",
}


# ---------------------------------------------------------------- helpers


def run(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    """Runs [cmd] and captures its output. A missing tool reads as a failure."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def human(n: int) -> str:
    if n < 0:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def parse_size(text: str) -> int:
    """'11.32GB' or '194.6MB (75%)' -> bytes."""
    m = re.match(r"\s*([\d.]+)\s*([kKMGT]?i?B)", text)
    if not m:
        return 0
    mult = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
    unit = m.group(2).replace("i", "")
    return int(float(m.group(1)) * mult.get(unit, 1))


def du(path: Path) -> int:
    r = run(["du", "-sk", str(path)], timeout=900)
    try:
        return int(r.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return -1


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v))


def process_cwds() -> list[str]:
    """Working directories of every process the user can see."""
    out = run(["lsof", "-d", "cwd", "-Fn"]).stdout
    return [os.path.realpath(line[1:]) for line in out.splitlines() if line.startswith("n")]


def process_commands() -> str:
    """Every process command line, one per line (a tool can work on a path it is not in)."""
    return run(["ps", "-axww", "-o", "command="]).stdout


def newest_mtime(folder: Path) -> float:
    """Newest mtime of [folder] and its direct children."""
    try:
        times = [folder.stat().st_mtime]
        times += [e.stat(follow_symlinks=False).st_mtime for e in os.scandir(folder)]
    except OSError:
        return 0.0
    return max(times)


def walk(root: Path, max_depth: int) -> Iterator[tuple[Path, list[os.DirEntry]]]:
    """Yields each folder under [root] with its entries. Skips hidden and heavy folders."""
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        yield folder, entries
        if depth >= max_depth:
            continue
        for e in entries:
            if (
                e.is_dir(follow_symlinks=False)
                and not e.name.startswith(".")
                and e.name not in PRUNE_DIRS
            ):
                stack.append((Path(e.path), depth + 1))


# ------------------------------------------------------------------ model


@dataclass
class Item:
    label: str
    size: int = -1  # bytes reclaimed; -1 = measure [path] with du
    action: Callable[[], None] | None = None  # None = kept
    reason: str = ""  # why it is kept, or what the action does
    path: Path | None = None

    @property
    def kept(self) -> bool:
        return self.action is None


@dataclass
class Context:
    config: dict
    roots: list[Path]
    selected: set[str]
    _cwds: list[str] | None = None
    _commands: str | None = None
    _repos: list[Path] | None = None

    @property
    def cwds(self) -> list[str]:
        if self._cwds is None:
            self._cwds = process_cwds()
        return self._cwds

    def in_use(self, path: Path) -> bool:
        """A process works inside [path], or names it on its command line."""
        if self._commands is None:
            self._commands = process_commands()
        p = os.path.realpath(path)
        if any(c == p or c.startswith(p + os.sep) for c in self.cwds):
            return True
        return any(
            re.search(re.escape(q) + r"(?:/|\s|$)", self._commands)
            for q in {p, str(path)}
        )

    @property
    def repos(self) -> list[Path]:
        """Main checkouts (a .git folder, not a .git file) under the roots."""
        if self._repos is None:
            found = []
            for root in self.roots:
                for folder, entries in walk(root, self.config["max_depth"]):
                    if any(e.name == ".git" and e.is_dir() for e in entries):
                        found.append(folder)
            self._repos = sorted(set(found))
        return self._repos


@dataclass
class Category:
    name: str
    help: str
    default: bool
    scan: Callable[[Context], list[Item]]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(repo), *args])


def rmtree(path: Path) -> Callable[[], None]:
    return lambda: shutil.rmtree(path)


def cmd(*args: str) -> Callable[[], None]:
    def go() -> None:
        r = run(list(args), timeout=1800)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return go


# -------------------------------------------------------------- worktrees


@dataclass
class Worktree:
    path: Path
    head: str = ""
    branch: str = ""
    locked: bool = False
    prunable: bool = False
    main: bool = False


def list_worktrees(repo: Path) -> list[Worktree]:
    out = git(repo, "worktree", "list", "--porcelain").stdout
    trees: list[Worktree] = []
    for block in out.strip().split("\n\n"):
        wt = None
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                wt = Worktree(Path(value))
                trees.append(wt)
            elif wt is None:
                continue
            elif key == "HEAD":
                wt.head = value
            elif key == "branch":
                wt.branch = value.removeprefix("refs/heads/")
            elif key == "locked":
                wt.locked = True
            elif key == "prunable":
                wt.prunable = True
    if trees:
        trees[0].main = True
    return trees


def status_entries(path: Path) -> list[tuple[str, str]]:
    """(status, path) pairs from `git status`, untracked files listed one by one."""
    out = run(
        ["git", "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
    ).stdout
    tokens = out.split("\0")
    entries, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        status, name = tok[:2], tok[3:]
        if status[0] in "RC":
            i += 1  # the rename source follows
        entries.append((status, name))
    return entries


def last_git_activity(path: Path) -> float:
    """Newest mtime of the worktree's index, HEAD and reflog (0 if unknown)."""
    gitdir = run(["git", "-C", str(path), "rev-parse", "--absolute-git-dir"]).stdout.strip()
    if not gitdir:
        return 0.0
    times = [
        (Path(gitdir) / name).stat().st_mtime
        for name in ("index", "HEAD", "logs/HEAD")
        if (Path(gitdir) / name).exists()
    ]
    return max(times, default=0.0)


def worktree_verdict(repo: Path, main: Path, wt: Worktree, ctx: Context) -> str:
    """Why [wt] must stay, or "" when it can go."""
    if wt.locked:
        return "locked"
    if ctx.in_use(wt.path):
        return "in use by a running process"
    idle_hours = ctx.config["worktree_min_idle_hours"]
    if idle_hours and time.time() - last_git_activity(wt.path) < idle_hours * 3600:
        return f"git activity in the last {idle_hours} h (an open session may use it)"
    entries = status_entries(wt.path)
    tracked = [n for s, n in entries if s != "??"]
    if tracked:
        return f"{len(tracked)} uncommitted change(s)"
    unique = []
    for _, name in entries:
        mine, theirs = wt.path / name, main / name
        if not (theirs.is_file() and mine.is_file() and filecmp.cmp(mine, theirs, shallow=False)):
            unique.append(name)
    if unique:
        shown = ", ".join(unique[:3]) + (f" (+{len(unique) - 3})" if len(unique) > 3 else "")
        return f"untracked files found nowhere else: {shown}"
    if not wt.branch and wt.head:
        on_a_ref = git(
            repo, "for-each-ref", "--count=1", "--contains", wt.head,
            "refs/heads", "refs/remotes", "refs/tags",
        ).stdout.strip()
        if not on_a_ref:
            return "detached HEAD with commits on no branch"
    return ""


def scan_worktrees(ctx: Context) -> list[Item]:
    items: list[Item] = []
    for repo in ctx.repos:
        trees = list_worktrees(repo)
        if len(trees) < 2:
            continue
        main = trees[0].path
        stale = [wt for wt in trees[1:] if wt.prunable or not wt.path.exists()]
        if stale:
            items.append(Item(
                f"{repo}: {len(stale)} stale worktree entr{'y' if len(stale) == 1 else 'ies'}",
                size=0, action=cmd("git", "-C", str(repo), "worktree", "prune"),
                reason="git worktree prune",
            ))
        for wt in trees[1:]:
            if wt in stale:
                continue
            why = worktree_verdict(repo, main, wt, ctx)
            label = f"{wt.path}  [{wt.branch or 'detached'}]"
            if why:
                items.append(Item(label, size=0, reason=why))
            else:
                items.append(Item(
                    label, path=wt.path,
                    action=cmd("git", "-C", str(repo), "worktree", "remove", "--force", str(wt.path)),
                    reason="remove the checkout (the branch stays)",
                ))
    return items


# ----------------------------------------------------------------- builds


def project_dirs(ctx: Context) -> set[Path]:
    dirs: set[Path] = set()
    for repo in ctx.repos:
        dirs.update(wt.path for wt in list_worktrees(repo) if wt.path.exists())
    for root in ctx.roots:
        for folder, entries in walk(root, ctx.config["max_depth"]):
            if any(e.name == "pubspec.yaml" for e in entries):
                dirs.add(folder)
    return dirs


def scan_builds(ctx: Context) -> list[Item]:
    """Flutter `build/` folders. `flutter build` / `flutter run` recreates them."""
    locked = {
        os.path.realpath(wt.path)
        for repo in ctx.repos for wt in list_worktrees(repo) if wt.locked
    }
    idle_hours = ctx.config["build_min_idle_hours"]
    items = []
    for project in sorted(project_dirs(ctx)):
        build = project / "build"
        if not (build.is_dir() and (project / "pubspec.yaml").is_file()):
            continue
        if ctx.in_use(project):
            items.append(Item(str(build), size=0, reason="project in use by a running process"))
        elif os.path.realpath(project) in locked:
            items.append(Item(str(build), size=0, reason="worktree is locked"))
        elif idle_hours and time.time() - newest_mtime(build) < idle_hours * 3600:
            items.append(Item(str(build), size=0, reason=f"written in the last {idle_hours} h"))
        else:
            items.append(Item(str(build), path=build, action=rmtree(build), reason="delete"))
    return items


def scan_node_modules(ctx: Context) -> list[Item]:
    days = ctx.config["node_modules_days"]
    cutoff = time.time() - days * 86400
    items = []
    for root in ctx.roots:
        for folder, entries in walk(root, ctx.config["max_depth"] + 2):
            names = {e.name for e in entries}
            if "node_modules" not in names or "package.json" not in names:
                continue
            nm = folder / "node_modules"
            touched = max(
                (folder / f).stat().st_mtime
                for f in ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb")
                if (folder / f).exists()
            )
            if touched > cutoff:
                continue
            if ctx.in_use(folder):
                items.append(Item(str(nm), size=0, reason="project in use by a running process"))
            else:
                items.append(Item(str(nm), path=nm, action=rmtree(nm),
                                  reason=f"untouched for {days}+ days"))
    return items


# ------------------------------------------------------------- simulators


def sim_devices() -> list[tuple[str, dict]]:
    r = run(["xcrun", "simctl", "list", "devices", "-j"])
    if r.returncode != 0:
        return []
    data = json.loads(r.stdout)["devices"]
    return [(rt, d) for rt, devs in data.items() for d in devs]


def scan_simulators(ctx: Context) -> list[Item]:
    pattern = ctx.config["sim_delete_pattern"]
    min_bytes = ctx.config["sim_erase_min_mb"] * 1024 * 1024
    items = []
    for runtime, d in sim_devices():
        udid, name, state = d["udid"], d["name"], d["state"]
        label = f"{name} ({runtime.rsplit('.', 1)[-1]}, {udid[:8]})"
        folder = SIM_DEVICES / udid
        if not d.get("isAvailable", True):
            items.append(Item(label, path=folder, action=cmd("xcrun", "simctl", "delete", udid),
                              reason="delete (its runtime is gone)"))
        elif state != "Shutdown":
            items.append(Item(label, size=0, reason=state.lower()))
        elif pattern and re.search(pattern, name):
            items.append(Item(label, path=folder, action=cmd("xcrun", "simctl", "delete", udid),
                              reason="delete (name matches sim_delete_pattern)"))
        else:
            size = d.get("dataPathSize") or du(folder / "data")
            if size >= min_bytes:
                items.append(Item(label, size=size, action=cmd("xcrun", "simctl", "erase", udid),
                                  reason="erase (keeps the simulator, wipes its data)"))
    return items


def runtimes_to_delete(runtimes: dict, keep_majors: int, in_use: set[str]) -> dict[str, str]:
    """runtime id -> why it is kept ("" = delete)."""
    by_platform: dict[str, list[tuple[str, dict]]] = {}
    for rid, rt in runtimes.items():
        by_platform.setdefault(rt.get("platformIdentifier", "?"), []).append((rid, rt))
    verdicts = {}
    for entries in by_platform.values():
        majors = sorted({version_key(rt["version"])[0] for _, rt in entries}, reverse=True)
        keep = set(majors[:keep_majors])
        for rid, rt in entries:
            if version_key(rt["version"])[0] in keep:
                verdicts[rid] = f"newest {keep_majors} major version(s)"
            elif rt.get("runtimeIdentifier") in in_use:
                verdicts[rid] = "a booted simulator uses it"
            elif not rt.get("deletable", True):
                verdicts[rid] = "not deletable (bundled with Xcode)"
            else:
                verdicts[rid] = ""
    return verdicts


def scan_runtimes(ctx: Context) -> list[Item]:
    r = run(["xcrun", "simctl", "runtime", "list", "-j"])
    if r.returncode != 0:
        return []
    runtimes = json.loads(r.stdout)
    booted = {rt for rt, d in sim_devices() if d["state"] != "Shutdown"}
    verdicts = runtimes_to_delete(runtimes, ctx.config["runtime_keep_majors"], booted)
    items = []
    for rid, rt in sorted(runtimes.items(), key=lambda kv: version_key(kv[1]["version"])):
        platform = rt.get("platformIdentifier", "").rsplit(".", 1)[-1].replace("simulator", "")
        label = f"{platform} {rt['version']} ({rt.get('build', '')})"
        why = verdicts[rid]
        if why:
            items.append(Item(label, size=0, reason=why))
        else:
            items.append(Item(label, size=rt.get("sizeBytes", -1),
                              action=cmd("xcrun", "simctl", "runtime", "delete", rid),
                              reason="delete (removes its simulators too)"))
    return items


# ------------------------------------------------------------------ xcode


def xcode_running() -> bool:
    return any(run(["pgrep", "-x", n]).returncode == 0 for n in ("Xcode", "xcodebuild"))


def scan_derived_data(ctx: Context) -> list[Item]:
    if not DERIVED_DATA.is_dir():
        return []
    if xcode_running():
        return [Item(str(DERIVED_DATA), size=0, reason="Xcode or xcodebuild is running")]
    return [
        Item(e.name, path=Path(e.path), action=rmtree(Path(e.path)), reason="delete")
        for e in os.scandir(DERIVED_DATA) if e.is_dir()
    ]


def device_support_to_delete(names: list[str], keep: int) -> set[str]:
    """Entries like 'iPhone16,1 18.6 (22G86)' or '17.2 (21C62)': keep the newest per model."""
    by_model: dict[str, list[tuple[tuple[int, ...], str]]] = {}
    for name in names:
        m = re.match(r"^(?:(.*?) )?(\d+(?:\.\d+)*)(?: \(.*\))?$", name)
        if not m:
            continue
        by_model.setdefault(m.group(1) or "", []).append((version_key(m.group(2)), name))
    doomed = set()
    for entries in by_model.values():
        entries.sort(reverse=True)
        doomed.update(name for _, name in entries[keep:])
    return doomed


def scan_device_support(ctx: Context) -> list[Item]:
    items = []
    for folder in DEVICE_SUPPORT_DIRS:
        if not folder.is_dir():
            continue
        names = [e.name for e in os.scandir(folder) if e.is_dir()]
        doomed = device_support_to_delete(names, ctx.config["device_support_keep"])
        for name in sorted(doomed):
            p = folder / name
            items.append(Item(f"{folder.name}/{name}", path=p, action=rmtree(p),
                              reason="older OS for this device model"))
    return items


# ---------------------------------------------------------------- android


def running_avds() -> set[str]:
    adb = SDK / "platform-tools/adb"
    names = set()
    for line in run([str(adb), "devices"]).stdout.splitlines():
        serial = line.split("\t")[0]
        if serial.startswith("emulator-"):
            out = run([str(adb), "-s", serial, "emu", "avd", "name"]).stdout
            if out.strip():
                names.add(out.splitlines()[0].strip())
    return names


def avds() -> list[tuple[str, Path, Path]]:
    """(name, .ini, .avd folder)."""
    folder = HOME / ".android/avd"
    found = []
    for ini in sorted(folder.glob("*.ini")) if folder.is_dir() else []:
        path = folder / f"{ini.stem}.avd"
        for line in ini.read_text(errors="ignore").splitlines():
            if line.startswith("path="):
                path = Path(line.split("=", 1)[1].strip())
        found.append((ini.stem, ini, path))
    return found


def scan_avds(ctx: Context) -> list[Item]:
    running = running_avds()
    items = []
    for name, ini, folder in avds():
        if name in running:
            items.append(Item(name, size=0, reason="running"))
            continue

        def remove(ini=ini, folder=folder) -> None:
            shutil.rmtree(folder, ignore_errors=True)
            ini.unlink(missing_ok=True)

        items.append(Item(name, path=folder, action=remove,
                          reason="delete (system images stay, recreate in a minute)"))
    return items


def scan_system_images(ctx: Context) -> list[Item]:
    images = SDK / "system-images"
    if not images.is_dir():
        return []
    # AVDs removed in this same run do not count as users of an image.
    running = running_avds()
    keep_avds = [
        (n, f) for n, _, f in avds()
        if "android-avds" not in ctx.selected or n in running
    ]
    used = set()
    for _, folder in keep_avds:
        cfg = folder / "config.ini"
        if cfg.is_file():
            for line in cfg.read_text(errors="ignore").splitlines():
                if line.replace(" ", "").startswith("image.sysdir.1="):
                    used.add(os.path.realpath(SDK / line.split("=", 1)[1].strip()))
    items = []
    for abi in sorted(images.glob("*/*/*")):
        if not abi.is_dir():
            continue
        label = str(abi.relative_to(images))
        if os.path.realpath(abi) in used:
            items.append(Item(label, size=0, reason="an AVD uses it"))
        else:
            items.append(Item(label, path=abi, action=rmtree(abi), reason="no AVD uses it"))
    return items


# ----------------------------------------------------------------- docker


def docker_up() -> bool:
    return run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15).returncode == 0


def docker_df() -> dict[str, int]:
    out = run(["docker", "system", "df", "--format", "{{json .}}"]).stdout
    rows = {}
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows[row["Type"]] = parse_size(row.get("Reclaimable", "0B"))
    return rows


def scan_docker(ctx: Context) -> list[Item]:
    if not docker_up():
        return [Item("docker", size=0, reason="docker is not running")] if shutil.which("docker") else []
    df = docker_df()
    if ctx.config["docker_all_images"]:
        images = Item("unused images", size=df.get("Images", 0),
                      action=cmd("docker", "image", "prune", "-af"), reason="docker image prune -a")
    else:
        sizes = run(["docker", "images", "-f", "dangling=true", "--format", "{{.Size}}"]).stdout
        images = Item("dangling images", size=sum(parse_size(s) for s in sizes.split()),
                      action=cmd("docker", "image", "prune", "-f"), reason="docker image prune")
    return [
        Item("stopped containers", size=df.get("Containers", 0),
             action=cmd("docker", "container", "prune", "-f"), reason="docker container prune"),
        images,
        Item("build cache", size=df.get("Build Cache", 0),
             action=cmd("docker", "builder", "prune", "-af"), reason="docker builder prune -a"),
        Item("unused networks", size=0,
             action=cmd("docker", "network", "prune", "-f"), reason="docker network prune"),
    ]


def scan_docker_volumes(ctx: Context) -> list[Item]:
    if not docker_up():
        return []
    return [Item("unused anonymous volumes", size=docker_df().get("Local Volumes", 0),
                 action=cmd("docker", "volume", "prune", "-f"),
                 reason="docker volume prune (named volumes stay)")]


# ----------------------------------------------------------------- caches


def cache_item(label: str, path: Path, action: Callable[[], None] | None,
               reason: str, busy: str = "") -> list[Item]:
    if not path.exists():
        return []
    if busy:
        return [Item(label, size=0, reason=busy)]
    return [Item(label, path=path, action=action, reason=reason)]


def scan_cache_npm(ctx: Context) -> list[Item]:
    p = HOME / ".npm/_cacache"
    return cache_item("npm cache", p, cmd("npm", "cache", "clean", "--force"), "npm cache clean")


def scan_cache_gradle(ctx: Context) -> list[Item]:
    p = HOME / ".gradle/caches"
    busy = "a Gradle daemon is running" if run(["pgrep", "-f", "GradleDaemon"]).returncode == 0 else ""
    return cache_item("Gradle caches", p, rmtree(p), "delete (re-downloaded on next build)", busy)


def scan_cache_pods(ctx: Context) -> list[Item]:
    p = HOME / "Library/Caches/CocoaPods"
    return cache_item("CocoaPods cache", p, rmtree(p), "delete")


def scan_cache_pub(ctx: Context) -> list[Item]:
    p = Path(os.environ.get("PUB_CACHE", HOME / ".pub-cache"))
    return cache_item("pub cache", p, cmd("dart", "pub", "cache", "clean", "-f"),
                      "dart pub cache clean (every project needs pub get)")


def scan_cache_brew(ctx: Context) -> list[Item]:
    r = run(["brew", "--cache"])
    if r.returncode != 0:
        return []
    return cache_item("Homebrew cache", Path(r.stdout.strip()),
                      cmd("brew", "cleanup", "-s", "--prune=all"), "brew cleanup -s")


# --------------------------------------------------------------- registry

CATEGORIES = [
    Category("worktrees", "git worktrees with no work in them", True, scan_worktrees),
    Category("builds", "Flutter build/ folders", True, scan_builds),
    Category("simulators", "unavailable, pattern-named and data-heavy iOS simulators", True, scan_simulators),
    Category("runtimes", "old iOS/watchOS/tvOS simulator runtimes", True, scan_runtimes),
    Category("derived-data", "Xcode DerivedData", True, scan_derived_data),
    Category("device-support", "Xcode device support for old OS versions", True, scan_device_support),
    Category("docker", "stopped containers, dangling images, build cache, networks", True, scan_docker),
    Category("docker-volumes", "unused anonymous Docker volumes", False, scan_docker_volumes),
    Category("android-avds", "Android emulators (AVDs) that are not running", False, scan_avds),
    Category("android-images", "Android system images no AVD uses", False, scan_system_images),
    Category("node-modules", "node_modules in projects untouched for a while", False, scan_node_modules),
    Category("cache-npm", "npm cache", False, scan_cache_npm),
    Category("cache-gradle", "Gradle caches", False, scan_cache_gradle),
    Category("cache-pods", "CocoaPods cache", False, scan_cache_pods),
    Category("cache-pub", "Dart/Flutter pub cache", False, scan_cache_pub),
    Category("cache-brew", "Homebrew downloads", False, scan_cache_brew),
]
ALIASES = {
    "caches": [c.name for c in CATEGORIES if c.name.startswith("cache-")],
    "android": ["android-avds", "android-images"],
    "xcode": ["derived-data", "device-support"],
    "all": [c.name for c in CATEGORIES],
}


def load_config(path: Path) -> dict:
    config = dict(DEFAULTS)
    if path.is_file():
        with path.open("rb") as f:
            config.update(tomllib.load(f))
    return config


def resolve_categories(names: list[str] | None) -> list[Category]:
    by_name = {c.name: c for c in CATEGORIES}
    if not names:
        return [c for c in CATEGORIES if c.default]
    picked: list[str] = []
    for n in names:
        for m in ALIASES.get(n, [n]):
            if m not in by_name:
                raise SystemExit(f"devclean: unknown category '{m}'. Run `devclean --list`.")
            if m not in picked:
                picked.append(m)
    return [by_name[n] for n in picked]


def measure(items: list[Item]) -> None:
    todo = [i for i in items if i.size < 0 and i.path is not None and not i.kept]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for item, size in zip(todo, pool.map(lambda i: du(i.path), todo)):
            item.size = size


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def free_bytes() -> int:
    return shutil.disk_usage("/").free


def confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


PLAN_HEADER = """\
# devclean plan, {when}.
# Each line is one item to delete: category <TAB> size <TAB> item.
# Review it. Delete a line to keep that item. Lines starting with # are ignored.
# Apply it with: devclean --apply --plan {path}
# Every item is checked again at apply time; one that became unsafe is skipped.
"""


def write_plan(path: Path, plan: list[tuple[Category, list[Item]]]) -> int:
    lines = [PLAN_HEADER.format(when=time.strftime("%Y-%m-%d %H:%M"), path=path)]
    count = 0
    for c, items in plan:
        for i in sorted((i for i in items if not i.kept), key=lambda i: -i.size):
            lines.append(f"{c.name}\t{human(i.size)}\t{i.label}\n")
            count += 1
    path.write_text("".join(lines))
    return count


def read_plan(path: Path) -> set[tuple[str, str]]:
    keys = set()
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            raise SystemExit(f"devclean: bad plan line (want 3 tab-separated fields): {line!r}")
        keys.add((parts[0], parts[2]))
    return keys


def choose(question: str, options: str) -> str:
    """Asks until one of [options] (single letters) is typed. EOF picks the last."""
    while True:
        try:
            answer = input(f"{question} ").strip().lower()[:1]
        except EOFError:
            return options[-1]
        if answer in options:
            return answer


def review(c: Category, doomed: list[Item]) -> list[Item]:
    """Interactive review of one category: all, one by one, or none."""
    size = sum(max(i.size, 0) for i in doomed)
    answer = choose(
        f"\n{c.name}: delete {len(doomed)} item(s), {human(size)}?"
        " [a]ll / [r]eview each / [s]kip:", "ars")
    if answer == "a":
        return doomed
    if answer == "s":
        return []
    picked = []
    for i in doomed:
        if choose(f"  {human(i.size):>9}  {i.label}  ({i.reason})  delete? [y/n]", "yn") == "y":
            picked.append(i)
    return picked


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="devclean",
        description="Reclaim disk space on a macOS dev machine. Dry run unless --apply. "
        "Review first: --apply asks per category (all, item by item, or skip), "
        "or write a plan with --plan-out, edit it, and apply it with --plan.",
    )
    p.add_argument("categories", nargs="*", metavar="CATEGORY",
                   help="categories or aliases (caches, android, xcode, all); default: the default set")
    p.add_argument("--apply", action="store_true",
                   help="delete: asks per category, or follows a reviewed --plan")
    p.add_argument("--plan-out", type=Path, metavar="FILE",
                   help="write the plan to FILE for review (deletes nothing)")
    p.add_argument("--plan", type=Path, metavar="FILE",
                   help="with --apply: delete only the items left in this reviewed plan")
    p.add_argument("-y", "--yes", action="store_true",
                   help="with --apply --plan: do not ask for the final confirmation")
    p.add_argument("--root", action="append", dest="roots", metavar="DIR",
                   help="folder to search (repeatable); overrides the config roots")
    p.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"default: {CONFIG_PATH}")
    p.add_argument("--list", action="store_true", help="list the categories and exit")
    p.add_argument("--json", action="store_true", help="print the plan as JSON (no deletion)")
    p.add_argument("-v", "--verbose", action="store_true", help="also list what is kept and why")
    args = p.parse_args(argv)

    if args.list:
        for c in CATEGORIES:
            print(f"  {c.name:<16}{'default' if c.default else 'opt-in ':<9}{c.help}")
        print("\n  aliases: " + ", ".join(f"{k} ({', '.join(v)})" for k, v in ALIASES.items() if k != "all") + ", all")
        return 0
    if args.yes and not args.plan:
        print("devclean: --yes skips the review, so it needs a reviewed plan: "
              "--plan-out FILE, edit FILE, then --apply --plan FILE --yes.", file=sys.stderr)
        return 2
    if args.plan and not args.apply:
        print("devclean: --plan is applied with --apply.", file=sys.stderr)
        return 2
    if args.apply and not args.yes and not sys.stdin.isatty():
        print("devclean: --apply reviews in a terminal. Without one, use --plan FILE --yes.",
              file=sys.stderr)
        return 2

    reviewed = read_plan(args.plan) if args.plan else None
    names = args.categories or (sorted({cat for cat, _ in reviewed}) if reviewed else None)
    if reviewed is not None and not names:
        print("devclean: the plan is empty. Nothing to do.")
        return 0

    config = load_config(args.config)
    roots = [Path(r).expanduser().resolve() for r in (args.roots or config["roots"])]
    categories = resolve_categories(names)
    ctx = Context(config, roots, {c.name for c in categories})

    plan: list[tuple[Category, list[Item]]] = []
    for c in categories:
        items = c.scan(ctx)
        if reviewed is not None:
            # Only what the review kept in the plan, and only if it is still safe now.
            for i in items:
                if (c.name, i.label) not in reviewed and not i.kept:
                    i.action, i.reason = None, "not in the reviewed plan"
            current = {i.label for i in items if not i.kept}
            for cat, label in sorted(reviewed):
                if cat == c.name and label not in current:
                    items.append(Item(label, size=0, reason="in the plan, but no longer safe or present"))
        measure(items)
        plan.append((c, items))

    if args.json:
        print(json.dumps([
            {"category": c.name, "items": [
                {"label": i.label, "bytes": i.size, "delete": not i.kept, "reason": i.reason}
                for i in items]}
            for c, items in plan], indent=2))
        return 0

    total = 0
    for c, items in plan:
        doomed = [i for i in items if not i.kept]
        kept = [i for i in items if i.kept]
        size = sum(max(i.size, 0) for i in doomed)
        total += size
        print(f"\n== {c.name}: {human(size)} in {len(doomed)} item(s)"
              + (f", {len(kept)} kept" if kept else ""))
        for i in sorted(doomed, key=lambda i: -i.size):
            print(f"  {human(i.size):>9}  {i.label}  ({i.reason})")
        for i in kept:
            if not args.verbose and (
                i.reason == "not in the reviewed plan"
                or (args.apply and not i.reason.startswith("in the plan"))
            ):
                continue
            print(f"  {'kept':>9}  {i.label}  ({i.reason})")
    print(f"\nReclaimable: {human(total)}. Free now: {human(free_bytes())}.")

    if args.plan_out:
        count = write_plan(args.plan_out, plan)
        print(f"Wrote {count} item(s) to {args.plan_out}. Review it, then: "
              f"devclean --apply --plan {args.plan_out}")
        return 0
    if not args.apply:
        print("Dry run. Review with --apply (asks per category), or --plan-out FILE.")
        return 0

    selected: list[tuple[Category, list[Item]]] = []
    if reviewed is not None:
        doomed_all = [(c, [i for i in items if not i.kept]) for c, items in plan]
        count = sum(len(d) for _, d in doomed_all)
        if not count:
            print("Nothing left to delete.")
            return 0
        if not args.yes and choose(
                f"\nDelete the {count} reviewed item(s), {human(total)}? [y/n]", "yn") != "y":
            return 0
        selected = doomed_all
    else:
        for c, items in plan:
            doomed = [i for i in items if not i.kept]
            if doomed:
                selected.append((c, review(c, doomed)))

    before = free_bytes()
    failures = 0
    for c, doomed in selected:
        for i in doomed:
            try:
                assert i.action is not None
                i.action()
                log(f"OK   {c.name}: {i.label} ({human(i.size)})")
                print(f"  done  {i.label}")
            except Exception as e:  # keep going; report at the end
                failures += 1
                log(f"FAIL {c.name}: {i.label}: {e}")
                print(f"  FAIL  {i.label}: {e}", file=sys.stderr)
    print(f"\nFreed about {human(max(free_bytes() - before, 0))} (macOS can report freed space late)."
          f" Log: {LOG_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
