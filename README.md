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

Player documentation is the [wiki](https://github.com/indevlin/EDCMDR-client/wiki). This README is
for working on the client.

## Integrations

Optional, per commander, off by default (Settings → Integrations):

- **EDDN**: markets, outfitting, shipyards and exploration, on EDDN's schemas, under your commander name.
- **EDSM**: your journal, sent under your EDSM account with your EDSM API key.
- **EDAstro**: exploration and travel events, no account; fleet carrier events only if you tick them.
- **Inara**: your commander, under your Inara account with your Inara API key: every commander event in
  Inara's API documentation, and nothing outside it.
- **Frontier CAPI**: keeps your Frontier sign-in on this computer after Verify with Frontier; with EDDN on,
  shares each docked station's market, outfitting and shipyard, and a fleet carrier's bartender orders.

Uploads go straight from your computer to each service. Each keeps its place in the journals, the last
event it accepted, and everything after that is sent: play while the client was closed, offline or paused
is filled in on the next run.

A service switched on for the first time catches up once, whatever app the player used before: EDSM from
the last position EDSM itself holds, EDDN and EDAstro from the start of a session begun within the last day,
and Inara with the commander's current state (ranks, credits, statistics, reputation, ships, fittings,
storage, inventory, suits and location) from the last 30 days, without older log entries.

The application name sent to Inara is `EDCMDR-client`. Commander data goes under the player's own Inara API
key. Inara's application key is for its read-only requests; when Inara issues it, add it to this repository
as the `INARA_APP_KEY` secret and release builds carry it.

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
python3 scripts/build.py --release  # dist/EDCMDR-<version>-<linux|windows|macos>
```

`--release` refuses to build without the build key and the pinned server key
in `edcmdr/keys.py`. The version is `YYYYMMDD`: `$EDCMDR_VERSION`, else a
`YYYYMMDD` tag on the commit being built, else the `version` file, else
today's date (UTC). PyInstaller builds for the system it runs on; Windows and
macOS binaries come from the Release workflow.

## Releases

Work lands on `dev`. A release writes the date to `version`, merges `dev` into
`main`, tags `main` `YYYYMMDD` (annotated) and pushes the tag. The Release
workflow checks the tag matches `version` and is on `main`, tests and builds
Linux and Windows binaries and the macOS app (zipped), signs a SHA-256 manifest,
and publishes the release with the notes under `## Released in YYYYMMDD` in
`CHANGELOG.md`. Run it by hand from the Actions tab to build any branch without
publishing. edcmdr.com mirrors the latest published release within the hour.
`docs/wiki/` and CHANGELOG are copied to the GitHub wiki on every push to `main`.

## Tests

```sh
pip install -e ../EDCMDR-core --config-settings editable_mode=compat   # a local EDCMDR-core checkout
python3 tests/test_client.py
EDCMDR_SERVER_REPO=~/projects/indevlin/EDCMDR python3 tests/test_client.py   # plus end-to-end against the server
```

## Files

    edcmdr_client.py     entry point
    (EDCMDR-core)        protocol, keys, sharing settings, journal events → categories, the publisher; pinned in requirements.txt
    edcmdr/journal.py    discovery, commander routing, tailing
    edcmdr/uplink.py     the engine: journals and integrations around edcmdr_core's publisher
    edcmdr/integrations.py  optional EDDN, EDSM, EDAstro and Inara uploads
    edcmdr/game.py       whether the game is running
    edcmdr/eddn.py       EDDN messages from journal events
    edcmdr/inara.py      Inara events from journal events
    edcmdr/capi.py       Frontier CAPI, with the sign-in kept on this computer
    edcmdr/ui/           tray and settings window
    scripts/build.py     one-file build with the build key injected
    docs/wiki/           player documentation, synced to the GitHub wiki

## Licence

MIT. See [LICENSE](LICENSE).
