# Integrations

The client can also send your journal to community services, straight from your computer. Each is
off until you switch it on, per commander, on the **Integrations** tab, and starts from that moment,
never your older journals.

After that nothing is missed. Each service keeps its place: the last thing it accepted from you. Play
while the client was closed, while you were offline, or while it was paused is sent the next time it
runs, from that place on. **Pause** holds these uploads; switching a service off forgets its place.

| Service | What it gets | You need |
|---|---|---|
| EDDN | Markets, outfitting, shipyards and exploration, which tools like Inara, Spansh and EDSM use | Nothing |
| Inara | Ranks, credits, statistics, reputation, ships, fittings and stored modules, travel and landings, missions and permits, combat, engineers, cargo, materials and locker, suits, friends, community goals | Your Inara API key (inara.cz → Settings) |
| EDSM | Your journal | Your EDSM API key (edsm.net → Settings) |
| EDAstro | Exploration and travel; your fleet carrier's jumps only if you tick them | Nothing |
| Frontier CAPI | Keeps your Frontier sign-in on this computer | Verify with Frontier once after switching it on |

EDDN messages carry your commander name, as EDDN requires. Your API keys and your Frontier sign-in
stay on your computer; ED Commanders never sees these uploads.

**Frontier CAPI:** with EDDN on too, each station you dock at shares its market, outfitting and
shipyard from Frontier's Companion API, and a fleet carrier its bartender orders. Switching CAPI off
deletes the sign-in from your computer.

The line at the bottom of the tab shows how each upload is going. An upload that fails, or waits for
an API key, is tried again, even after a restart.

When EDLD publishes for you instead of the client, use EDLD's own uploads; the client's stay off
there.
