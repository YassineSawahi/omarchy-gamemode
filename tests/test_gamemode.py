"""Tests for gamemode.py. State goes to a throwaway $XDG_STATE_HOME; nothing is
closed, stopped or started.

    python3 -m unittest discover -s tests
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "gamemode.py"


def load(state_home):
    os.environ["XDG_STATE_HOME"] = str(state_home)
    spec = importlib.util.spec_from_file_location("gamemode", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def proc(gm, pid, ppid, comm, argv, mb=0, exe=None):
    p = gm.Proc()
    p.pid, p.ppid, p.comm, p.argv, p.kb = pid, ppid, comm, argv, mb * 1024
    p.exe = exe or comm
    return p


class GameModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gamemode-test-"))
        self.gm = load(self.tmp)
        self.dir = Path(self.gm.STATE_DIR)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    # ------------------------------------------------------------ files

    def test_selection_is_remembered_privately(self):
        self.gm.cmd_select("app:slack", False)
        self.assertEqual(self.gm.read_prefs(), {"app:slack": False})
        self.assertEqual(self.mode(self.dir), 0o700)
        self.assertEqual(self.mode(self.gm.PREFS), 0o600)

    def test_existing_open_folder_is_tightened(self):
        self.dir.mkdir(mode=0o755)
        os.chmod(self.dir, 0o755)
        self.gm.cmd_select("indexer", True)
        self.assertEqual(self.mode(self.dir), 0o700)

    def test_file_others_can_write_is_ignored(self):
        self.gm.write_json(self.gm.STATE, {"items": [{"key": "indexer", "kind": "indexer", "label": "File indexer"}]})
        self.assertIsNotNone(self.gm.read_state())
        os.chmod(self.gm.STATE, 0o622)
        self.assertIsNone(self.gm.read_state())

    def test_symlinked_state_is_ignored(self):
        other = self.tmp / "elsewhere.json"
        other.write_text(json.dumps({"items": [{"key": "indexer", "kind": "indexer", "label": "x"}]}))
        os.chmod(other, 0o600)
        self.gm.ensure_dir()
        os.symlink(other, self.gm.STATE)
        self.assertIsNone(self.gm.read_state())

    def test_malformed_records_are_dropped(self):
        good = {"key": "app:slack", "kind": "app", "label": "Slack", "name": "slack", "argv": ["/usr/bin/slack"]}
        bad = [
            {"key": "app:x", "kind": "app", "label": "X", "name": "x", "argv": "rm -rf ~"},
            {"key": "app:y", "kind": "app", "label": "Y", "name": "y", "argv": []},
            {"key": "docker:z", "kind": "docker", "label": "z", "containers": [{"id": 5}]},
            {"key": "w", "kind": "shell", "label": "w"},
        ]
        self.gm.write_json(self.gm.STATE, {"since": 1, "items": [good] + bad})
        self.assertEqual(self.gm.read_state()["items"], [good])

    def test_empty_state_removes_the_file(self):
        self.gm.write_json(self.gm.STATE, {"items": [{"key": "indexer", "kind": "indexer", "label": "x"}]})
        self.gm.save_state({"items": []})
        self.assertFalse(os.path.exists(self.gm.STATE))

    # ------------------------------------------------------------ choosing

    def test_defaults(self):
        self.assertTrue(self.gm.selected("app:chrome", {}))
        self.assertTrue(self.gm.selected("indexer", {}))
        self.assertTrue(self.gm.selected("docker:shop", {}))
        self.assertFalse(self.gm.selected("app:obsidian", {}))
        self.assertFalse(self.gm.selected("app:chrome", {"app:chrome": False}))

    def test_names(self):
        gm = self.gm
        self.assertEqual(gm.app_name(proc(gm, 1, 0, "python3", ["python3", "-u", "/opt/x/tool.py"], exe="python3.14")), "tool")
        self.assertEqual(gm.app_name(proc(gm, 1, 0, "voxtype", ["voxtype"], exe="voxtype-avx2")), "voxtype")
        self.assertTrue(gm.is_game(proc(gm, 1, 0, "Game.exe", ["C:\\Game.exe"], exe="Game.exe")))
        self.assertTrue(gm.is_game(proc(gm, 1, 0, "bash", ["bash", "/home/u/.local/share/Steam/steam.sh"])))
        self.assertFalse(gm.is_game(proc(gm, 1, 0, "chrome", ["/opt/google/chrome/chrome"])))

    def test_only_safe_apps_are_offered(self):
        gm = self.gm
        ps = [
            proc(gm, 10, 1, "systemd", ["/usr/lib/systemd/systemd", "--user"]),
            proc(gm, 11, 10, "Hyprland", ["Hyprland"]),
            # Chrome: main process plus a renderer, counted together.
            proc(gm, 20, 10, "chrome", ["/opt/google/chrome/chrome"], mb=400),
            proc(gm, 21, 20, "chrome", ["/opt/google/chrome/chrome", "--type=renderer"], mb=600),
            # A browser an automation tool started.
            proc(gm, 25, 10, "chrome", ["/opt/google/chrome/chrome", "--remote-debugging-pipe"], mb=900),
            # A terminal running an agent, and a launcher running a game.
            proc(gm, 30, 11, "kitty", ["kitty"], mb=300),
            proc(gm, 31, 30, "claude", ["claude"], mb=400),
            proc(gm, 40, 10, "heroic", ["/opt/Heroic/heroic"], mb=500),
            proc(gm, 41, 40, "Game.exe", ["Game.exe"], mb=4000, exe="Game.exe"),
            # Something with an agent inside that isn't a terminal.
            proc(gm, 50, 11, "herdr", ["herdr", "server"], mb=300),
            proc(gm, 51, 50, "claude", ["claude"], mb=300),
            # Too small to bother with, and system plumbing.
            proc(gm, 60, 10, "kdeconnectd", ["kdeconnectd"], mb=50),
            proc(gm, 70, 10, "pipewire", ["pipewire"], mb=900),
            proc(gm, 80, 11, "slack", ["/usr/lib/slack/slack"], mb=300),
        ]
        found = gm.apps({p.pid: p for p in ps})
        self.assertEqual(sorted(found), ["chrome", "slack"])
        self.assertEqual(found["chrome"]["mb"], 1000)
        self.assertEqual(found["chrome"]["pids"], [20])

    # ------------------------------------------------------------ docker

    def test_projects_start_dependencies_first(self):
        gm = self.gm
        started = []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(*cmd, timeout=30):
            if cmd[:2] == ("docker", "start"):
                started.append(cmd[2])
            return Done()

        gm.run = fake_run
        gm.container_state = lambda cid: ("running", "healthy")
        cs = [
            {"id": "nginx", "name": "p-nginx-1", "service": "nginx", "depends": ["backend"]},
            {"id": "backend", "name": "p-backend-1", "service": "backend", "depends": ["db", "redis"]},
            {"id": "db", "name": "p-db-1", "service": "db", "depends": []},
            {"id": "redis", "name": "p-redis-1", "service": "redis", "depends": []},
        ]
        self.assertEqual(gm.start_project(cs), [])
        self.assertEqual(set(started[:2]), {"db", "redis"})
        self.assertEqual(started[2:], ["backend", "nginx"])

    def test_taken_fixed_address_is_reclaimed(self):
        """Another project started first and took this container's fixed IP."""
        gm = self.gm
        calls = []

        class R:
            def __init__(self, code=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = code, out, err

        door_started = []
        nets = {
            "door": {"NetworkSettings": {"Networks": {"shared": {"IPAMConfig": {"IPv4Address": "192.0.2.2"}}}}},
            "other": {"NetworkSettings": {"Networks": {"shared": {"IPAMConfig": {}}}}},
        }

        def fake_run(*cmd, timeout=30):
            calls.append(cmd[1:])
            if cmd[1:3] == ("network", "inspect"):
                return R(out=json.dumps([{"Containers": {"other": {"IPv4Address": "192.0.2.2/29"}}}]))
            if cmd[1] == "inspect" and cmd[2] in nets:
                return R(out=json.dumps([nets[cmd[2]]]))
            if cmd[1] == "start" and cmd[2] == "door":
                if not door_started and ("network", "disconnect", "shared", "other") not in calls:
                    return R(1, err="failed to set up container networking: Address already in use")
                door_started.append(True)
            return R()

        gm.run = fake_run
        gm.container_state = lambda cid: ("running", "")
        self.assertEqual(gm.start_project([{"id": "door", "name": "p-door-1", "service": "door", "depends": []}]), [])
        order = [c for c in calls if c[:2] in (("network", "disconnect"), ("start", "door"), ("network", "connect"))]
        self.assertEqual(order, [("start", "door"), ("network", "disconnect", "shared", "other"),
                                 ("start", "door"), ("network", "connect", "shared", "other")])

    def test_real_clash_is_left_alone(self):
        """Two containers asking for the same fixed address: nothing is moved."""
        gm = self.gm
        calls = []

        class R:
            def __init__(self, code=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = code, out, err

        fixed = {"NetworkSettings": {"Networks": {"shared": {"IPAMConfig": {"IPv4Address": "192.0.2.2"}}}}}

        def fake_run(*cmd, timeout=30):
            calls.append(cmd[1:])
            if cmd[1:3] == ("network", "inspect"):
                return R(out=json.dumps([{"Containers": {"other": {"IPv4Address": "192.0.2.2/29"}}}]))
            if cmd[1] == "inspect":
                return R(out=json.dumps([fixed]))
            if cmd[1] == "start":
                return R(1, err="Address already in use")
            return R()

        gm.run = fake_run
        failed = gm.start_project([{"id": "door", "name": "p-door-1", "service": "door", "depends": []}])
        self.assertEqual(failed, ["p-door-1"])
        self.assertFalse(any(c[:2] == ("network", "disconnect") for c in calls))

    # ------------------------------------------------------------ cli

    def test_unknown_command_is_refused(self):
        env = dict(os.environ, XDG_STATE_HOME=str(self.tmp))
        r = subprocess.run([sys.executable, str(SCRIPT), "rm", "-rf"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, str(SCRIPT), "pause"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
