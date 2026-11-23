# EDCMDR client (Python)

Publishes your Elite Dangerous commander to [ED Commanders](https://edcmdr.com).
It reads your journals, keeps what you chose to share, and sends it, signed,
to EDCMDR. It sits in the system tray: right-click for Pause, Resume, Stop,
Push now, Settings and Exit.

- **Nothing is sent until you switch it on**, section by section, each with an
  optional delay (1 hour to 1 week) that EDCMDR enforces.
- **Several commanders**, in one journal folder or several: each journal file
  belongs to the commander named in its first lines.
- **Your Frontier ID never appears on the site**; EDCMDR keeps only a keyed hash.
- **Verify with Frontier** proves the commander is yours (a one-time sign-in on
  Frontier's site; nothing is kept but the result).
- **Portrait:** pick a screenshot of the Codex hub with your commander tile
  highlighted; EDCMDR cuts the portrait out. Switching portrait sharing off
  deletes it.
- **Squadron tab**, shown only to a squadron's verified leader: home system,
  power, supported factions, recruiting status and a tagline for the
  squadron's page.
- **Forget on EDCMDR** deletes everything the site holds for a commander.

This branch is the Python build (PySide6). The `go` branch is the same
client in Go; one of them becomes the client.

## Run from source

```sh
python3 -m pip install -r requirements.txt
python3 edcmdr_client.py            # tray (Settings opens by itself until something is shared)
python3 edcmdr_client.py --settings # tray, with the settings window open
python3 edcmdr_client.py --list     # commanders found in your journals
python3 edcmdr_client.py --headless # no window, logs to the terminal
```

Journal folders are found automatically for Windows and Steam/Proton on
Linux; add others in Settings → Commanders, or with `--journal DIR`.

**Linux trays:** KDE, XFCE, Cinnamon and X11 trays (i3bar, polybar's tray)
work as they are. GNOME needs the AppIndicator extension. With no tray at
all, the settings window opens instead, and closing it exits.

## Publishing needs an official build

EDCMDR accepts pushes only from builds signed with a build key it lists. The
key is not in this repository. Release builds get it from the
`EDCMDR_BUILD_KEY` secret; local builds read `$EDCMDR_BUILD_KEY` or
`~/.config/edcmdr/build.key`. A build without it runs and reads journals, and
the server refuses what it sends.

```sh
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 scripts/build.py --release  # dist/EDCMDR-<version>-<os>-<arch>
```

`--release` refuses to build without the build key and the pinned server key
in `edcmdr/keys.py`. The version is `YYYYMMDD`: `$EDCMDR_VERSION`, else a
`YYYYMMDD` tag on the commit being built, else today's date (UTC). PyInstaller
builds for the system it runs on; Windows and macOS binaries come from the
Release workflow: push a `YYYYMMDD` tag on `main`, or run it by hand with
`dry_run` to get the binaries as run artefacts without publishing. edcmdr.com mirrors the
latest published release within the hour.

## Tests

```sh
python3 tests/test_client.py
EDCMDR_SERVER_REPO=~/projects/indevlin/EDCMDR python3 tests/test_client.py   # plus end-to-end against the server
```

## Files

    edcmdr_client.py     entry point
    edcmdr/journal.py    discovery, commander routing, tailing
    edcmdr/state.py      journal events → protocol categories
    edcmdr/proto.py      signed pushes, signed-reply checks
    edcmdr/uplink.py     the engine: sharing, debounce, retries
    edcmdr/ui/           tray and settings window
    protocol/v1/         spec.json, vendored from the EDCMDR server repo (protocol.lock)
    scripts/build.py     one-file build with the build key injected

## Licence

MIT. See [LICENSE](LICENSE).
