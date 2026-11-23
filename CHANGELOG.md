# EDCMDR client CHANGELOG

Last updated: 20261123

---

## Unreleased

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
