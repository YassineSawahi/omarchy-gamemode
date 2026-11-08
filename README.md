# Game mode for Omarchy

An [Omarchy](https://omarchy.org) bar widget that frees memory before you play. It lists what's running that's worth pausing (big apps like Chrome or Slack, each Docker Compose project, the file indexer) with a switch each. Tick what you want gone, press one button, and play. Afterwards everything comes back: apps reopen the way they were (Chrome with its tabs), and containers start again in dependency order.

Made for a machine that froze whenever a game met a browser with fifty tabs and a dev stack in Docker: the kernel swapped until it was unusable, then `systemd-oomd` killed the game.

## What it does

- **Bar:** a gamepad icon, lit while game mode is on. Middle-click switches game mode on or off.
- **Panel** (left click): free memory, and a switch per item with what it holds (RAM plus swap). Before game mode, the switches choose what to pause, and your choices are remembered. While it's on, each switch acts at once: switch one off to bring just that back, or on to pause one more. **Restore all** brings back everything.
- **Notifications** say what was paused and how much memory it freed, and warn if something didn't close or come back.

Apps over 150 MB are listed. Chrome, Chromium, Slack, Discord and the indexer start ticked, compose projects too; everything else starts unticked.

## Never touched

- **Terminals**, and anything with an AI coding agent running inside it (Claude Code, Codex, opencode, aider), so you can't close a session by accident.
- **Game launchers and games:** Steam, Heroic, Lutris, Bottles, Wine, Proton, gamescope, and any `.exe`.
- **Browsers started by tools** (`--remote-debugging`, `--headless`, automation flags), such as chrome-devtools-mcp or Playwright.
- **The desktop itself:** Hyprland, Xwayland, the Omarchy shell, PipeWire, portals, D-Bus, systemd.
- **Other accounts' processes.** Only your own are looked at or signalled.

Nothing is ever killed. An app gets SIGTERM, which lets it save and quit; one that ignores it keeps running and you're told. `docker stop` gives each container 20 seconds to shut down cleanly.

## How it works

- **`gamemode.py`** is the helper behind the panel. `status` reads `/proc` for your apps (grouped by their top process, counting helpers and renderers with them), `docker ps`/`docker inspect` for running containers grouped by Compose project, and `systemctl --user` for the indexer (`localsearch-3.service`). Memory is RSS plus swap for apps and the cgroup's `memory.current` plus `memory.swap.current` for containers.
- **Pausing** writes a record first, then asks the apps to quit, stops the containers in parallel, and stops the indexer. The record holds each app's command line and each container's id, service and `depends_on`, so a crash halfway still leaves a way back.
- **Bringing back** reopens apps with their original command line through `uwsm-app` (Chrome and Chromium get `--restore-last-session`), skipping any that you already reopened yourself. Each Compose project starts dependencies first and waits for a batch's healthchecks before starting what depends on it, the way `docker compose up` does. Projects start in parallel. If one project's container joined another project's network and took a fixed IP that a container there asks for (Docker hands out dynamic addresses from the same pool), it's moved aside and reconnected with a new address, so both come up. The indexer starts again.
- **The record** lives in `~/.local/state/omarchy-gamemode/`, not in `$XDG_RUNTIME_DIR`, because `docker stop` keeps even `restart: unless-stopped` containers down after a reboot. So if you forget game mode is on, the next "Restore all" still brings them back, days later. Since the record holds command lines that get run, the folder is always set to 0700 and its files to 0600, and a record that another account could have written (or a symlink) is ignored.
- **The widget** runs `gamemode.py status` every 20 seconds (every 3 while the panel is open). Switching runs the helper detached, so a long restore carries on even if the shell restarts. A lock file tells the widget a switch is still in flight.

## Install

```bash
omarchy plugin add https://github.com/YassineSawahi/omarchy-gamemode.git --enable
```

Needs Python 3. Docker projects show up only if your account can run `docker` (it's in the `docker` group); without that, apps and the indexer still work.

## Controls

| Action | Does |
|---|---|
| Left click | Open or close the panel |
| Middle click | Game mode on or off |
| Right click | Read the state again |
| `omarchy-shell io.github.yassinesawahi.gamemode toggleGameMode` | Game mode on or off (also `turnOn`, `turnOff`, `toggle`, `refresh`, `close`) |

The bar is hidden while a game is fullscreen, so a key is handy. In `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + SHIFT + G", "Game mode", "omarchy-shell io.github.yassinesawahi.gamemode toggleGameMode")
```

The helper works on its own too:

```bash
gamemode.py                      # status as JSON
gamemode.py on | off | toggle
gamemode.py select app:chrome on # tick or untick an item (keys are in the status output)
gamemode.py pause docker:shop    # pause one more while game mode is on
gamemode.py resume app:slack     # bring one back
```

Turn game mode on before you launch the game, so the memory is free from the start.

## Tests

```bash
python3 -m unittest discover -s tests
```

They use a throwaway state folder and never close, stop or start anything.

## Remove

Bring everything back first, then remove the plugin and its state:

```bash
python3 ~/.config/omarchy/plugins/io.github.yassinesawahi.gamemode/gamemode.py off
omarchy plugin remove io.github.yassinesawahi.gamemode
rm -r ~/.local/state/omarchy-gamemode
```

## License

MIT
