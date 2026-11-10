#!/usr/bin/env python3
"""Game mode for the Omarchy bar: pick what to pause before a game, bring it back after.

With no arguments (or `status`) prints one JSON object for BarWidget.qml: the
memory picture and one item per thing that can be paused, each an app
(Chrome, Slack, …), a Docker compose project, or the file indexer.

  select <key> on|off   tick or untick an item; remembered for next time
  on                    pause every ticked item
  off                   bring back everything that is paused
  pause <key>           pause one more item while game mode is on
  resume <key>          bring back one item; the last one turns game mode off

Apps are asked to quit (SIGTERM), never killed, and reopen the way they were
launched, Chrome with its last session. Compose projects stop as a whole and
start again dependencies first, waiting for healthchecks. Terminals, anything
hosting an AI coding agent, and game launchers (Steam, Heroic, Wine…) are
never offered.

What is paused lives in $XDG_STATE_HOME/omarchy-gamemode/state.json, not in
$XDG_RUNTIME_DIR, on purpose: `docker stop` stops even `unless-stopped`
containers for good, so after a reboot the record is what brings them back.
That record holds the command lines `off` runs to reopen apps, so the folder is
kept private (0700) and a file that another account could have written is
ignored. An app saved by a version before 1.0.2, which could be fooled into
recording a sandboxed process, is never reopened: you start it yourself.
"""

import fcntl
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

STATE_NAME = "omarchy-gamemode"
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), STATE_NAME)
STATE = os.path.join(STATE_DIR, "state.json")
PREFS = os.path.join(STATE_DIR, "selection.json")
LOCK = os.path.join(STATE_DIR, "lock")
LOG = os.path.join(STATE_DIR, "log")

INDEXER = "localsearch-3.service"
MIN_MB = 150         # apps smaller than this aren't worth listing

LABELS = {"chrome": "Chrome", "chromium": "Chromium", "brave": "Brave", "firefox": "Firefox",
          "slack": "Slack", "Discord": "Discord", "discord": "Discord", "spotify": "Spotify",
          "code": "VS Code", "obsidian": "Obsidian", "signal-desktop": "Signal", "telegram-desktop": "Telegram",
          "voxtype": "Voxtype", "nautilus": "Files", "zoom": "Zoom"}
# Ticked the first time they show up; other apps start unticked, compose projects ticked.
DEFAULT_ON = {"app:chrome", "app:chromium", "app:slack", "app:discord", "app:Discord", "indexer"}
DEFAULT_OFF_PROJECTS = set()
# Flags added when an app is reopened, so it comes back as it was.
RESTORE_FLAGS = {"chrome": ["--restore-last-session"], "chromium": ["--restore-last-session"]}
# Marks app records saved since 1.0.2. Older versions trusted a parent by its process name, so
# a sandboxed process could get its command line saved; an app record without this never runs.
RECORD_FORMAT = 2

# Never offered.
SYSTEM = {"Hyprland", "start-hyprland", "Xwayland", "quickshell", "omarchy-launch-shell", "uwsm",
          "pipewire", "pipewire-pulse", "wireplumber", "localsearch-3", "systemd", "dbus-broker",
          "dbus-daemon", "xdg-desktop-portal", "xdg-desktop-portal-gtk", "xdg-desktop-portal-hyprland"}
TERMINALS = {"kitty", "alacritty", "foot", "ghostty", "wezterm-gui", "konsole", "gnome-terminal-server", "xterm"}
# Sandbox launchers: their command line can't start the app again (Flatpak hands bwrap its arguments
# through a file descriptor), and the app inside is the sandbox's, not this session's.
SANDBOXES = {"bwrap", "flatpak", "flatpak-spawn", "snap", "snap-confine", "firejail"}
HOSTS = {"claude", "codex", "opencode", "aider"}  # a tree with one of these in it is someone's live session
GAME_WORDS = ("steam", "heroic", "lutris", "wine", "proton", "gamescope", "bottles", "legendary", "umu-run")
INTERPRETERS = {"bash", "sh", "dash", "zsh", "env"} | {f"python3.{n}" for n in range(8, 20)} | {"python3", "python", "node"}
# Browsers started by test tools and agents (chrome-devtools-mcp, Playwright) are not the user's.
FOREIGN_ARGS = ("--remote-debugging", "--headless", "--test-type", "--enable-automation")

APP_GRACE = 25       # seconds an app gets to save and quit
STOP_GRACE = 20      # seconds `docker stop` gives a container before it kills it
HEALTH_WAIT = 90     # seconds to wait for a restarted container's healthcheck


# ---------------------------------------------------------------- files

def ensure_dir():
    """Private to this account, also when an older version or another tool made it first."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def private_open(path, flags):
    ensure_dir()
    return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def read_json(path, default):
    """Only a file this account owns and nobody else can write."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return default
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            return default
        try:
            return json.load(f)
        except ValueError:
            return default


def write_json(path, data):
    tmp = path + ".tmp"
    ensure_dir()
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = private_open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def log(message):
    fd = private_open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, "a") as f:
        f.write(time.strftime("%F %T ") + message + "\n")


def valid_record(r):
    if not isinstance(r, dict) or not isinstance(r.get("key"), str) or not isinstance(r.get("label"), str):
        return False
    kind = r.get("kind")
    if kind == "app":
        argv = r.get("argv")
        return isinstance(r.get("name"), str) and isinstance(argv, list) and bool(argv) \
            and all(isinstance(a, str) for a in argv)
    if kind == "docker":
        cs = r.get("containers")
        return isinstance(cs, list) and all(isinstance(c, dict) and isinstance(c.get("id"), str)
                                            and isinstance(c.get("name"), str) for c in cs)
    return kind == "indexer"


def trusted(record):
    """Whether resuming this record is safe: only app records saved since 1.0.2 run a command line."""
    return record["kind"] != "app" or record.get("format") == RECORD_FORMAT


def read_state():
    s = read_json(STATE, None)
    if not isinstance(s, dict) or not isinstance(s.get("items"), list):
        return None
    s["items"] = [r for r in s["items"] if valid_record(r)]
    return s if s["items"] else None


def save_state(state):
    if state["items"]:
        write_json(STATE, state)
    elif os.path.exists(STATE):
        os.remove(STATE)


def read_prefs():
    p = read_json(PREFS, {})
    return {k: v for k, v in p.items() if isinstance(k, str) and isinstance(v, bool)} if isinstance(p, dict) else {}


def lock(blocking):
    """Held while anything is being paused or brought back, so the bar can tell."""
    f = os.fdopen(private_open(LOCK, os.O_WRONLY | os.O_CREAT), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        return None
    return f


def busy():
    f = lock(False)
    if f is None:
        return True
    f.close()
    return False


# ---------------------------------------------------------------- helpers

def run(*cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def notify(title, body, urgency="normal"):
    run("notify-send", "-a", "Game mode", "-u", urgency, "-i", "input-gaming", title, body, timeout=5)


def meminfo():
    m = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                m[k] = int(v.split()[0])
    except OSError:
        pass
    return {
        "availableMB": m.get("MemAvailable", 0) // 1024,
        "totalMB": m.get("MemTotal", 0) // 1024,
        "swapUsedMB": (m.get("SwapTotal", 0) - m.get("SwapFree", 0)) // 1024,
    }


def launcher():
    # uwsm-app gives the app its own scope, so it outlives this script and the shell.
    return ["uwsm-app", "--"] if shutil.which("uwsm-app") else []


def selected(key, prefs):
    if key in prefs:
        return prefs[key]
    if key.startswith("docker:"):
        return key[7:] not in DEFAULT_OFF_PROJECTS
    return key in DEFAULT_ON


# ---------------------------------------------------------------- apps

class Proc:
    # path: the real program, "" when it can't be read. ns: its namespaces, None when they can't be read.
    __slots__ = ("pid", "ppid", "comm", "exe", "path", "ns", "argv", "kb")


def namespaces(pid):
    """The mount, user and PID namespaces a process lives in, or None when they can't be read.
    A process sandboxed by Flatpak, Snap, bubblewrap or a browser lives in other ones."""
    try:
        return tuple(os.readlink(f"/proc/{pid}/ns/{n}") for n in ("mnt", "user", "pid"))
    except OSError:
        return None


def processes():
    """This account's processes. Others' are never looked at, let alone signalled."""
    me = os.getuid()
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != me:
                continue
            p = Proc()
            p.pid = int(d)
            with open(f"/proc/{d}/stat") as f:
                p.ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            with open(f"/proc/{d}/comm") as f:
                p.comm = f.read().strip()
            with open(f"/proc/{d}/cmdline", "rb") as f:
                p.argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            p.ns = namespaces(d)
            try:
                p.path = os.readlink(f"/proc/{d}/exe")
                p.exe = os.path.basename(p.path)
            except OSError:
                p.path, p.exe = "", p.comm
            p.kb = 0
            with open(f"/proc/{d}/status") as f:
                for line in f:
                    if line.startswith(("VmRSS:", "VmSwap:")):
                        p.kb += int(line.split()[1])
        except (OSError, IndexError, ValueError):
            continue
        out[p.pid] = p
    return out


def tree(root, children):
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def app_name(p):
    """What to call an app: its program, or for a script, the script."""
    if p.exe in INTERPRETERS:
        for a in p.argv[1:]:
            if not a.startswith("-"):
                return os.path.splitext(os.path.basename(a))[0]
    return p.exe.removesuffix("-avx2")


def is_game(p):
    if p.exe.lower().endswith(".exe") or p.exe in ("wineserver", "wine", "wine64", "gamescope"):
        return True
    return any(w in " ".join(p.argv[:2]).lower() for w in GAME_WORDS)


def user_manager():
    """The PID of this account's systemd --user, as the system manager reports it. Its /proc
    entry can't be read, and any process can name itself systemd, so the name proves nothing."""
    r = run("systemctl", "show", f"user@{os.getuid()}.service", "-p", "MainPID", "--value", timeout=5)
    try:
        return int(r.stdout.strip()) if r is not None and r.returncode == 0 else 0
    except ValueError:
        return 0


def apps(procs, manager=None, own=None):
    """name → {pids, members, argv, mb}: top-level apps of this session (children of the
    user's service manager or of Hyprland) worth listing and safe to close.

    An app's command line is run again on restore, outside any sandbox it came from, so only
    processes in this session's own namespaces count, both as apps and as the Hyprland they
    hang off; a sandboxed process can call itself anything. The service manager is known by
    its PID. `manager` and `own` are for tests."""
    manager = user_manager() if manager is None else manager
    own = namespaces("self") if own is None else own

    def ours(p):
        return own is not None and p.ns == own

    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    parents = {p.pid for p in procs.values() if ours(p) and os.path.basename(p.path) in ("Hyprland", "start-hyprland")}
    if manager > 0:
        parents.add(manager)
    found = {}
    for p in procs.values():
        if p.ppid not in parents or p.pid in parents or not p.argv or not ours(p):
            continue
        name = app_name(p)
        if name in SYSTEM or p.exe in SYSTEM or p.exe in TERMINALS or p.exe in SANDBOXES:
            continue
        if any(a.startswith("--type=") or a.startswith(FOREIGN_ARGS) for a in p.argv):
            continue
        members = [procs[t] for t in tree(p.pid, children) if t in procs]
        if any(m.comm in HOSTS or m.exe in TERMINALS or is_game(m) for m in members):
            continue
        a = found.setdefault(name, {"pids": [], "members": [], "argv": p.argv, "mb": 0})
        a["pids"].append(p.pid)
        a["members"] += [m.pid for m in members]
        a["mb"] += sum(m.kb for m in members) // 1024
    return {k: v for k, v in found.items() if v["mb"] >= MIN_MB}


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie has quit; it's only waiting to be reaped
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def reopen(item):
    if not trusted(item):
        return f"{item['label']} wasn't reopened (an older Game mode saved it): start it yourself"
    if item["name"] in apps(processes()):
        return None
    argv = list(item["argv"])
    for flag in RESTORE_FLAGS.get(item["name"], []):
        if flag not in argv:
            argv.append(flag)
    try:
        subprocess.Popen(launcher() + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"{item['label']}: {e}"
    return None


# ---------------------------------------------------------------- docker

def cgroup_mb(cid):
    base = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
    total = 0
    for f in ("memory.current", "memory.swap.current"):
        try:
            with open(os.path.join(base, f)) as fh:
                total += int(fh.read())
        except (OSError, ValueError):
            pass
    return total // (1024 * 1024)


def containers():
    if not shutil.which("docker"):
        return []
    r = run("docker", "ps", "-q", "--no-trunc", timeout=10)
    if r is None or r.returncode != 0 or not r.stdout.split():
        return []
    r = run("docker", "inspect", *r.stdout.split(), timeout=15)
    if r is None or r.returncode != 0:
        return []
    out = []
    for c in json.loads(r.stdout):
        labels = c.get("Config", {}).get("Labels") or {}
        deps = labels.get("com.docker.compose.depends_on", "")
        name = c["Name"].lstrip("/")
        out.append({
            "id": c["Id"],
            "name": name,
            "project": labels.get("com.docker.compose.project", "") or name,
            "service": labels.get("com.docker.compose.service", "") or name,
            "depends": [d.split(":")[0] for d in deps.split(",") if d],
            "mb": cgroup_mb(c["Id"]),
        })
    return out


def stop_containers(cs):
    procs = [(c, subprocess.Popen(["docker", "stop", "-t", str(STOP_GRACE), c["id"]],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True))
             for c in cs]
    failed = []
    for c, p in procs:
        _, err = p.communicate()
        if p.returncode != 0:
            failed.append(c["name"])
            log(f"docker stop {c['name']}: {err.strip()}")
    return failed


def container_state(cid):
    r = run("docker", "inspect", "-f", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", cid, timeout=10)
    if r is None or r.returncode != 0:
        return "gone", ""
    parts = r.stdout.split() + [""]
    return parts[0], parts[1]


def inspect(*args):
    """First object of `docker <args…>`, e.g. inspect("inspect", id), inspect("network", "inspect", name)."""
    r = run("docker", *args, timeout=10)
    if r is None or r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)[0]
    except (ValueError, IndexError):
        return {}


def fixed_ips(cid):
    """network → address for the networks where this container asks for a fixed IPv4 address."""
    nets = inspect("inspect", cid).get("NetworkSettings", {}).get("Networks") or {}
    out = {}
    for net, conf in nets.items():
        addr = (conf.get("IPAMConfig") or {}).get("IPv4Address")
        if addr:
            out[net] = addr
    return out


def reclaim_ips(c):
    """Start `c` after moving any container that took one of its fixed addresses without
    asking for it (Docker hands out dynamic addresses from the same pool). That container
    is reconnected afterwards and gets another address. Returns whether `c` started."""
    moved = []
    for net, addr in fixed_ips(c["id"]).items():
        for cid, info in (inspect("network", "inspect", net).get("Containers") or {}).items():
            if info.get("IPv4Address", "").split("/")[0] != addr or cid == c["id"]:
                continue
            if fixed_ips(cid).get(net):
                continue  # it asked for that address too: a real clash, leave it
            r = run("docker", "network", "disconnect", net, cid, timeout=30)
            if r is not None and r.returncode == 0:
                moved.append((net, cid))
    if not moved:
        return False
    r = run("docker", "start", c["id"], timeout=60)
    for net, cid in moved:
        run("docker", "network", "connect", net, cid, timeout=30)
    ok = r is not None and r.returncode == 0
    log(f"{c['name']}: its fixed address was taken; moved {len(moved)} container(s) aside, "
        + ("started" if ok else "still failed"))
    return ok


def start_project(cs):
    """Start one compose project, dependencies first, each batch waiting for its
    healthchecks the way `docker compose up` would. Returns names that failed."""
    failed = []
    pending = {c.get("service") or c["name"]: c for c in cs}
    started = set()
    while pending:
        ready = [k for k, c in pending.items() if all(d in started or d not in pending for d in c.get("depends", []))]
        if not ready:  # a dependency loop; start the rest together
            ready = list(pending)
        batch = [pending.pop(k) for k in ready]
        for c in batch:
            r = run("docker", "start", c["id"], timeout=60)
            if r is None or r.returncode != 0:
                err = r.stderr.strip() if r else "timed out"
                # Another project, started in parallel, may have taken this one's fixed IP.
                if "address already in use" in err.lower() and reclaim_ips(c):
                    continue
                failed.append(c["name"])
                log(f"docker start {c['name']}: {err}")
        deadline = time.time() + HEALTH_WAIT
        waiting = [c for c in batch if c["name"] not in failed]
        while waiting and time.time() < deadline:
            still = []
            for c in waiting:
                status, health = container_state(c["id"])
                if status == "running" and health in ("", "healthy"):
                    continue
                if status in ("exited", "dead", "gone") or health == "unhealthy":
                    log(f"{c['name']} came back {status} {health}".rstrip())
                    continue
                still.append(c)
            waiting = still
            if waiting:
                time.sleep(1)
        started.update(c.get("service") or c["name"] for c in batch)
    return failed


# ---------------------------------------------------------------- gpu

# Per-client VRAM keys in DRM fdinfo (amdgpu, xe, i915), most specific first.
VRAM_KEYS = ("drm-resident-vram0", "drm-memory-vram", "drm-resident-local0", "drm-total-vram0", "drm-total-local0")
UNITS = {"B": 1 / 1024, "KiB": 1, "MiB": 1024, "GiB": 1024 * 1024}


def mib(text):
    try:
        return int(str(text).split()[0])
    except (ValueError, IndexError):
        return 0


def parse_nvidia(xml_text):
    """(pid → MB, usedMB, totalMB) from `nvidia-smi -q -x`, summed over GPUs."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    pids, used, total = {}, 0, 0
    for gpu in root.iter("gpu"):
        fb = gpu.find("fb_memory_usage")
        if fb is not None:
            used += mib(fb.findtext("used"))
            total += mib(fb.findtext("total"))
        for p in gpu.iter("process_info"):
            try:
                pid = int(p.findtext("pid"))
            except (TypeError, ValueError):
                continue
            pids[pid] = pids.get(pid, 0) + mib(p.findtext("used_memory"))
    return pids, used, total


def parse_fdinfo(text):
    """(client id, KiB of VRAM) from one DRM fdinfo file, or None if it isn't a GPU client."""
    fields = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        fields[k.strip()] = v.split()
    cid = fields.get("drm-client-id")
    if not cid:
        return None
    for key in VRAM_KEYS:
        v = fields.get(key)
        if v:
            try:
                return cid[0], int(int(v[0]) * UNITS.get(v[1] if len(v) > 1 else "B", 1 / 1024))
            except ValueError:
                return None
    return cid[0], 0


def fdinfo_vram(pid):
    """MB of VRAM a process holds, from DRM fdinfo; each GPU client counted once."""
    clients = {}
    try:
        fds = os.listdir(f"/proc/{pid}/fdinfo")
    except OSError:
        return 0
    for fd in fds:
        try:
            with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                text = f.read()
        except OSError:
            continue
        if "drm-client-id" in text:
            got = parse_fdinfo(text)
            if got:
                clients[got[0]] = got[1]
    return sum(clients.values()) // 1024


def docker_id(cgroup_text):
    m = re.search(r"docker-([0-9a-f]{64})\.scope|/docker/([0-9a-f]{64})", cgroup_text)
    return (m.group(1) or m.group(2)) if m else None


class Gpu:
    """VRAM in use, overall and per process. NVIDIA reports every process, Docker's
    included; elsewhere only this account's processes can be read, so containers show none."""

    def __init__(self):
        self.pids, self.used, self.total, self.nvidia = {}, 0, 0, False
        if shutil.which("nvidia-smi"):
            r = run("nvidia-smi", "-q", "-x", timeout=10)
            got = parse_nvidia(r.stdout) if r is not None and r.returncode == 0 else None
            if got and got[2]:
                self.pids, self.used, self.total = got
                self.nvidia = True
                return
        for card in glob.glob("/sys/class/drm/card*/device"):
            try:
                with open(f"{card}/mem_info_vram_used") as u, open(f"{card}/mem_info_vram_total") as t:
                    self.used += int(u.read()) // (1024 * 1024)
                    self.total += int(t.read()) // (1024 * 1024)
            except (OSError, ValueError):
                continue

    def of(self, pids):
        if not self.total:
            return 0
        if self.nvidia:
            return sum(self.pids.get(p, 0) for p in pids)
        return sum(fdinfo_vram(p) for p in pids)

    def by_container(self):
        out = {}
        for pid, mb in self.pids.items():
            try:
                with open(f"/proc/{pid}/cgroup") as f:
                    cid = docker_id(f.read())
            except OSError:
                continue
            if cid:
                out[cid] = out.get(cid, 0) + mb
        return out


def size(mb):
    return f"{mb} MB" if mb < 1024 else gb(mb)


# ---------------------------------------------------------------- items

def indexer_active():
    r = run("systemctl", "--user", "is-active", INDEXER, timeout=5)
    return bool(r) and r.stdout.strip() == "active"


def gb(mb):
    return f"{mb / 1024:.1f} GB"


def discover(gpu=None):
    """Everything that could be paused right now, biggest first, with the RAM (plus swap)
    and VRAM each one holds."""
    gpu = gpu or Gpu()
    items = []
    for name, a in apps(processes()).items():
        items.append({"key": "app:" + name, "kind": "app", "name": name, "label": LABELS.get(name, name.capitalize()),
                      "mb": a["mb"], "vram": gpu.of(a["members"]), "pids": a["pids"], "argv": a["argv"]})
    projects = {}
    for c in containers():
        projects.setdefault(c["project"], []).append(c)
    on_gpu = gpu.by_container()
    for project, cs in projects.items():
        items.append({"key": "docker:" + project, "kind": "docker", "label": project, "mb": sum(c["mb"] for c in cs),
                      "vram": sum(on_gpu.get(c["id"], 0) for c in cs), "containers": cs})
    items.sort(key=lambda i: -(i["mb"] + i["vram"]))
    if indexer_active():
        items.append({"key": "indexer", "kind": "indexer", "label": "File indexer", "mb": 0, "vram": 0})
    return items


def holds(item):
    out = gb(item["mb"]) + " RAM"
    if item.get("vram"):
        out += " · " + size(item["vram"]) + " VRAM"
    return out


def describe(item, paused):
    if item["kind"] == "app":
        if paused and not trusted(item):
            return "Closed by an older version · start it yourself"
        return "Closed · reopens when you switch it back" if paused else holds(item) + " · quits, reopens after"
    if item["kind"] == "docker":
        n = len(item["containers"])
        what = f"{n} container{'s' if n != 1 else ''}"
        return what + " stopped" if paused else f"{what} · {holds(item)}"
    return "Paused" if paused else "Stops reading the disk"


def pause(item):
    """Returns (record kept in the state file, wait-for-it callable returning problems)."""
    kind = item["kind"]
    record = {k: item[k] for k in ("key", "kind", "label")}
    if kind == "app":
        record.update(name=item["name"], argv=item["argv"], format=RECORD_FORMAT)
        for pid in item["pids"]:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        def wait():
            deadline = time.time() + APP_GRACE
            while time.time() < deadline and any(alive(p) for p in item["pids"]):
                time.sleep(0.25)
            return [item["label"] + " didn't close"] if any(alive(p) for p in item["pids"]) else []
        return record, wait
    if kind == "docker":
        record["containers"] = [{k: c[k] for k in ("id", "name", "project", "service", "depends")} for c in item["containers"]]
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("failed", stop_containers(item["containers"])))
        t.start()

        def wait():
            t.join()
            return [n + " didn't stop" for n in result.get("failed", [])]
        return record, wait
    run("systemctl", "--user", "stop", INDEXER, timeout=30)
    return record, lambda: []


def resume(record):
    """Bring one paused item back. Returns problems."""
    kind = record["kind"]
    if kind == "app":
        err = reopen(record)
        return [err] if err else []
    if kind == "docker":
        return [n + " didn't come back" for n in start_project(record["containers"])]
    run("systemctl", "--user", "start", INDEXER, timeout=30)
    return []


# ---------------------------------------------------------------- commands

def status():
    state = read_state()
    prefs = read_prefs()
    gpu = Gpu()
    out = {"on": bool(state), "busy": busy(), "mem": meminfo(), "items": [],
           "gpu": {"usedMB": gpu.used, "totalMB": gpu.total} if gpu.total else None}
    paused_keys = set()
    if state:
        out.update(since=state.get("since"), freedMB=state.get("freedMB", 0), freedVramMB=state.get("freedVramMB", 0))
        for r in state["items"]:
            paused_keys.add(r["key"])
            out["items"].append({"key": r["key"], "kind": r["kind"], "label": r["label"],
                                 "detail": describe(r, True), "mb": 0, "vram": 0, "paused": True, "selected": True})
    for i in discover(gpu):
        if i["key"] in paused_keys:
            continue
        out["items"].append({"key": i["key"], "kind": i["kind"], "label": i["label"], "detail": describe(i, False),
                             "mb": i["mb"], "vram": i["vram"], "paused": False, "selected": selected(i["key"], prefs)})
    ticked = [i for i in out["items"] if i["selected"] and not i["paused"]]
    out["selectedMB"] = sum(i["mb"] for i in ticked)
    out["selectedVramMB"] = sum(i["vram"] for i in ticked)
    return out


def pause_items(items, state):
    """Returns (RAM freed, RAM free now, VRAM freed or None without a GPU, problems)."""
    before, vbefore = meminfo()["availableMB"], Gpu()
    started = [pause(i) for i in items]
    # Recorded before waiting, so a crash halfway still leaves a way back.
    state["items"] += [record for record, _ in started]
    save_state(state)
    log("paused " + ", ".join(i["key"] for i in items))
    problems = [p for _, wait in started for p in wait()]
    time.sleep(1)
    after = meminfo()["availableMB"]
    vfreed = max(0, vbefore.used - Gpu().used) if vbefore.total else None
    state["freedMB"] = state.get("freedMB", 0) + max(0, after - before)
    state["freedVramMB"] = state.get("freedVramMB", 0) + (vfreed or 0)
    save_state(state)
    return after - before, after, vfreed, problems


def resume_records(records):
    """Apps first so the browser is back quickly; projects in parallel."""
    problems = []
    for r in records:
        if r["kind"] != "docker":
            problems += resume(r)
    results = {}
    threads = [threading.Thread(target=lambda r=r: results.__setitem__(r["key"], resume(r)))
               for r in records if r["kind"] == "docker"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for p in results.values():
        problems += p
    return problems


def cmd_on():
    if read_state():
        return
    prefs = read_prefs()
    items = [i for i in discover() if selected(i["key"], prefs)]
    if not items:
        notify("Game mode", "Nothing is ticked, so there's nothing to pause.")
        return
    state = {"since": int(time.time()), "items": [], "freedMB": 0, "freedVramMB": 0}
    freed, after, vfreed, problems = pause_items(items, state)
    body = (f"Paused {', '.join(i['label'] for i in items)}\nFreed {gb(max(0, freed))} RAM"
            + (f" and {size(vfreed)} VRAM" if vfreed is not None else "") + f" · {gb(after)} RAM free now")
    if problems:
        body += "\n" + "; ".join(problems)
    notify("Game mode on", body, "critical" if problems else "normal")


def cmd_off():
    state = read_state()
    if not state:
        return
    problems = resume_records(state["items"])
    os.remove(STATE)
    log("off")
    back = [r["label"] for r in state["items"] if trusted(r)]
    body = "Back: " + ", ".join(back) if back else "Nothing to reopen"
    if problems:
        body += "\n" + "; ".join(problems)
    notify("Game mode off", body, "critical" if problems else "normal")


def cmd_pause(key):
    state = read_state() or {"since": int(time.time()), "items": [], "freedMB": 0}
    if any(r["key"] == key for r in state["items"]):
        return
    items = [i for i in discover() if i["key"] == key]
    if not items:
        return
    _, _, _, problems = pause_items(items, state)
    if problems:
        notify("Game mode", "; ".join(problems), "critical")


def cmd_resume(key):
    state = read_state()
    if not state:
        return
    records = [r for r in state["items"] if r["key"] == key]
    if not records:
        return
    problems = resume_records(records)
    state["items"] = [r for r in state["items"] if r["key"] != key]
    save_state(state)
    log("resumed " + key)
    if problems:
        notify("Game mode", "; ".join(problems), "critical")
    elif not state["items"]:
        notify("Game mode off", "Everything is back")


def cmd_select(key, on):
    prefs = read_prefs()
    prefs[key] = on
    write_json(PREFS, prefs)


def main():
    os.umask(0o077)
    args = sys.argv[1:] or ["status"]
    cmd = args[0]
    if cmd == "status":
        print(json.dumps(status()))
        return
    if cmd == "select" and len(args) == 3 and args[2] in ("on", "off"):
        cmd_select(args[1], args[2] == "on")
        return
    if cmd not in ("on", "off", "toggle", "pause", "resume") or (cmd in ("pause", "resume") and len(args) != 2):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    held = lock(False)
    if held is None:
        print("Game mode is busy", file=sys.stderr)
        sys.exit(1)
    with held:
        if cmd == "toggle":
            cmd = "off" if read_state() else "on"
        if cmd == "on":
            cmd_on()
        elif cmd == "off":
            cmd_off()
        elif cmd == "pause":
            cmd_pause(args[1])
        else:
            cmd_resume(args[1])


if __name__ == "__main__":
    main()
