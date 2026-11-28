"""The client's engine: reads journals into per-commander state, feeds the integrations, publishes through edcmdr_core.

Runs on its own thread. The tray and settings window call its methods and
listen through on_change; nothing here imports Qt.
"""
import logging

from edcmdr_core import keys
from edcmdr_core.proto import Client
from edcmdr_core.publisher import Publisher, Runtime  # noqa: F401

from . import NAME, VERSION, capi, game, journal
from .config import Settings
from .integrations import Hub
from .state import EVENTS, Commander

log = logging.getLogger('edcmdr')


def embedded_build_key() -> str | None:
    try:
        from . import _build_secret  # type: ignore[attr-defined]
        return _build_secret.BUILD_KEY
    except ImportError:
        return None


def make_client(base: str | None = None) -> Client:
    return Client(NAME, VERSION, keys.load_build_key(embedded_build_key()), base)


class Engine(Publisher):
    def __init__(self, settings: Settings, client: Client | None = None):
        super().__init__(settings, client or make_client(), bool(settings.data.get('start_paused')))
        self._cmdrs: dict[str, Commander] = {}
        self.watcher = journal.Watcher(settings.journal_dirs, self._event, EVENTS)
        self.needles = self.watcher.needles
        self.hub = Hub(settings)
        self.hub.paused = self.state != 'running'
        self.frontier_token = self._kept_token

    # ── Publisher hooks ─────────────────────────────────────────────────────
    def commanders(self) -> dict[str, Commander]:
        return self._cmdrs

    def poll(self) -> None:
        first = not self.watcher.imported
        self.watcher.needles = None if self.hub.any_on() else self.needles
        self.watcher.poll()
        if first:
            for c in self._cmdrs.values():
                c.loaded = True
            self.hub.imported()

    def presence_of(self, fid: str) -> dict:
        c = self._cmdrs.get(fid)
        return c.presence(self._running(fid)) if c else {'in_game': False}

    def on_verified(self, fid: str, tokens: dict, token_url: str, client_id: str, redirect_uri: str) -> None:
        if Hub.config(self.settings, fid, 'capi')['enabled']:
            capi.Store(fid).keep(tokens, token_url, client_id, redirect_uri)

    def on_state(self, state: str) -> None:
        self.hub.set_paused(state != 'running')

    def on_shutdown(self) -> None:
        self.hub.shutdown()

    def update_available(self) -> str:
        """The newer client build the server announces, or ''."""
        latest = str((self.server or {}).get('latest_client', ''))
        return latest if latest.isdigit() and len(latest) == 8 and latest > VERSION else ''

    # ── the client's own ────────────────────────────────────────────────────
    def set_dirs(self, dirs: list[str]) -> None:
        self.settings.set_journal_dirs(dirs)
        with self.lock:
            self.watcher.set_dirs(self.settings.journal_dirs)
        self._wake.set()

    def _event(self, fid: str, ev: dict) -> None:
        c = self._cmdrs.get(fid)
        if c is None:
            c = self._cmdrs[fid] = Commander(fid)
        c.handle(ev)
        self.hub.feed(fid, ev, self.watcher.current, live=self.watcher.imported)

    def _running(self, fid: str) -> bool:
        return game.running(self.watcher.where.get(fid))

    @staticmethod
    def _kept_token(fid: str) -> str | None:
        store = capi.Store(fid)
        if not store.signed_in():
            return None
        try:
            return capi.Client(store).token()
        except (capi.SignInNeeded, OSError, ValueError) as e:
            log.info('Frontier sign-in for a squadron refresh: %s', e)
            return None
