# EDCMDR client CHANGELOG

Last updated: 20261123

---

## Unreleased

### Added: Inara and Frontier CAPI
- Inara: your commander, ranks, credits, ships and fittings, travel, missions, engineers and materials, under your Inara account with your Inara API key. The client identifies to Inara as EDCMDR-client.
- Frontier CAPI: switch it on and verify with Frontier once, and the sign-in stays on your computer (never on edcmdr.com). With EDDN on, your fleet carrier's bartender orders are shared from CAPI when you dock there.

### Fixed
- In game: shown while the game is running and you are in a session, not at the main menu, however quietly you play; the game is found the way EDLD finds it. The client repeats it every 2 minutes, and the site shows you offline a few minutes after it stops.
- Journal continuation files (part 2 and up of a long session) are read as the commander's, like the file before them.
- The Sharing tab no longer shows unused columns for other services; those live on the Integrations tab.
- Ship values are the game's own hull and module values; stored ships aboard a fleet carrier are shown at the carrier and move with it; the current ship is no longer listed with a stale location.
- Time pledged to your power is sent with Powerplay.

### Changed: simpler download names
- `EDCMDR-<version>-linux`, `EDCMDR-<version>-windows.exe` and `EDCMDR-<version>-macos.zip`.

### Added: EDDN, EDSM and EDAstro uploads
- New Integrations tab: per commander, share markets, outfitting, shipyards and exploration with EDDN, send your journal to EDSM (with your EDSM API key), and send exploration and travel events to EDAstro (no account; fleet carrier events optional).
- Off by default. Only what you play while the client runs is sent, never the imported history; Pause stops it.
- Uploads go from your computer straight to each service. A batch that fails is kept and retried, including after a restart.
- Not active when EDLD hosts the client: EDLD's own uploaders handle these there.

## Released in 20261123

### Added: the EDCMDR client

The first release. The client reads the Elite Dangerous journal folders you
point it at, finds every commander in them, and publishes to edcmdr.com only
the sections you switch on in Sharing, each with its own delay. It sits in the
tray, where you can pause, resume or push at any time, and opens its settings
window when there is no tray. Every push is signed with a key made for each
commander on this computer, and every reply from the server is checked against
the server key built into the client.

### Added: Verify with Frontier

Verify with Frontier proves a commander is yours with a one-time sign-in on
Frontier's own site. The client makes the sign-in's secret and keeps it in
memory, exchanges Frontier's code for a token from your own connection, hands
the token to edcmdr.com once and forgets it. The server reads the commander's
account id from Frontier's copy of your journal and the name and squadron from
Frontier's profile service, then keeps neither token nor account id. A
verified commander can be published by this installation only, appears on the
leaderboards, and a verified squadron leader gets the Squadron tab. Verifying
needs a session played in the last two weeks. The Commanders tab shows each
stage of a verification, and the full text of any status on hover.

### Added: Codex portraits

Portrait… takes a screenshot of the Codex hub with your commander tile
highlighted and publishes the portrait above the label. The tile is found
whatever your HUD colours, in a full-screen capture or a capture of several
monitors.
