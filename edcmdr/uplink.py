"""The engine: reads journals, keeps per-commander state, pushes what is shared.

Runs on its own thread. The tray and settings window call its methods and
listen through on_change; nothing here imports Qt.
"""
import hashlib
import json
import logging
import threading
import time
from typing import Callable

from . import journal, keys
from .config import SPEC, Settings
from .proto import Client, ProtoError
from .state import EVENTS, Commander

log = logging.getLogger('edcmdr')

TICK = 1.0
DEBOUNCE = 60
PRESENCE_EVERY = 300
HELLO_EVERY = 6 * 3600
BACKOFF_MIN, BACKOFF_MAX = 30, 900


def digest(v) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Runtime:
    """What the engine knows about one commander's link to EDCMDR."""

    def __init__(self):
        self.enrolled = False
        self.verified = False
        self.pub_id: str | None = None
        self.blocked: str | None = None
        self.blocked_until = 0.0
        self.prefs_sent: str | None = None
        self.sent: dict[str, str] = {}
        self.next_ok: dict[str, float] = {}
        self.presence_sent: tuple[str, float] = ('', 0.0)
        self.last_push = 0.0
        self.error: str | None = None
        self.verify_until = 0.0
        self.verify_next = 0.0


class Engine:
    def __init__(self, settings: Settings, client: Client | None = None):
        self.settings = settings
        self.client = client or Client()
        self.cmdrs: dict[str, Commander] = {}
        self.rt: dict[str, Runtime] = {}
        self.watcher = journal.Watcher(settings.journal_dirs, self._event, EVENTS)
        self.state = 'paused' if settings.data.get('start_paused') else 'running'
        self.status = 'Starting'
        self.server: dict | None = None
        self.hello_at = 0.0
        self.backoff = 0.0
        self.backoff_step = BACKOFF_MIN
        self.force = False
        self.lock = threading.RLock()
        self.listeners: list[Callable[[], None]] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread: threading.Thread | None = None

    # ── control ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name='edcmdr-engine', daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.thread:
            self.thread.join(5)

    def pause(self) -> None:
        self._set_state('paused')

    def halt(self) -> None:
        self._set_state('stopped')

    def resume(self) -> None:
        self._set_state('running')

    def push_now(self) -> None:
        with self.lock:
            self.force = True
            self.backoff = 0
            for r in self.rt.values():
                r.blocked_until = 0
        self._wake.set()

    def _set_state(self, s: str) -> None:
        with self.lock:
            self.state = s
        self._notify()
        self._wake.set()

    def set_dirs(self, dirs: list[str]) -> None:
        self.settings.set_journal_dirs(dirs)
        with self.lock:
            self.watcher.set_dirs(self.settings.journal_dirs)
        self._wake.set()

    def set_prefs(self, fid: str, prefs: dict) -> None:
        self.settings.set_prefs(fid, prefs)
        with self.lock:
            r = self._rt(fid)
            r.sent.clear()
            r.next_ok.clear()
            r.presence_sent = ('', 0.0)
        self._wake.set()

    def verify_start(self, fid: str) -> dict:
        """Ask the server for a verification link; the engine then watches for the result for ten minutes."""
        with self.lock:
            c = self.cmdrs.get(fid)
            name = c.name if c else self.settings.commander(fid).get('name', '')
        j = self.client.push(keys.commander_key(fid), fid, 'verify-start', {'name': name})
        with self.lock:
            r = self._rt(fid)
            r.blocked, r.blocked_until = None, 0.0
            r.verify_until = time.time() + 600
            r.verify_next = time.time() + 10
        self._wake.set()
        return {'url': j.get('url', ''), 'code': j.get('code', '')}

    def forget(self, fid: str) -> str | None:
        """Ask the server to delete this commander; on success drop the local key and settings."""
        with self.lock:
            r = self._rt(fid)
            if not r.enrolled:
                keys.drop_commander_key(fid)
                self.settings.forget_commander(fid)
                return None
        try:
            self.client.push(keys.commander_key(fid), fid, 'forget', {})
        except ProtoError as e:
            return str(e)
        keys.drop_commander_key(fid)
        self.settings.forget_commander(fid)
        with self.lock:
            self.rt[fid] = Runtime()
            self.rt[fid].blocked = 'Forgotten. Turn sharing back on to publish again.'
            self.rt[fid].blocked_until = float('inf')
        self._notify()
        return None

    def on_change(self, fn: Callable[[], None]) -> None:
        self.listeners.append(fn)

    def _notify(self) -> None:
        for fn in self.listeners:
            try:
                fn()
            except Exception:
                log.exception('listener failed')

    # ── views for the UI ────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self.lock:
            cm = []
            for fid, c in sorted(self.cmdrs.items(), key=lambda kv: kv[1].name):
                r = self._rt(fid)
                cm.append({'fid': fid, 'name': c.name, 'enrolled': r.enrolled, 'verified': r.verified,
                           'pub_id': r.pub_id, 'blocked': r.blocked, 'error': r.error, 'last_push': r.last_push,
                           'in_game': c.presence()['in_game']})
            return {'state': self.state, 'status': self.status, 'official': self.client.official,
                    'server': self.client.base, 'commanders': cm}

    # ── loop ────────────────────────────────────────────────────────────────
    def _rt(self, fid: str) -> Runtime:
        return self.rt.setdefault(fid, Runtime())

    def _event(self, fid: str, ev: dict) -> None:
        c = self.cmdrs.get(fid)
        if c is None:
            c = self.cmdrs[fid] = Commander(fid)
        c.handle(ev)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('engine tick failed')
            self._wake.wait(TICK)
            self._wake.clear()

    def tick(self) -> None:
        with self.lock:
            state = self.state
            if state != 'stopped':
                first = not self.watcher.imported
                self.watcher.poll()
                if first:
                    for c in self.cmdrs.values():
                        c.loaded = True
        if state != 'running':
            self.status = {'paused': 'Paused', 'stopped': 'Stopped'}[state]
            self._notify()
            return
        now = time.time()
        if now < self.backoff:
            return
        try:
            if now - self.hello_at > HELLO_EVERY or self.server is None:
                self._hello()
            for fid in list(self.cmdrs):
                self._sync(fid, now)
            self.backoff_step = BACKOFF_MIN
            self.force = False
            if self.status not in ('Update required', 'Unofficial build'):
                self.status = 'Connected'
        except ProtoError as e:
            self._fail(e)
        self._notify()

    def _hello(self) -> None:
        j = self.client.hello()
        self.server = j
        self.hello_at = time.time()
        if SPEC['protocol'] not in j.get('protocols', []):
            self.status = 'Update required'
            raise ProtoError('version', 'This client is too old for the server; update it.')
        if not self.client.official:
            self.status = 'Unofficial build'

    def _fail(self, e: ProtoError) -> None:
        if e.code == 'rate' and isinstance(e.extra.get('retry_after'), int):
            wait = e.extra['retry_after'] + 5
        else:
            wait = self.backoff_step
            self.backoff_step = min(BACKOFF_MAX, self.backoff_step * 2)
        self.backoff = time.time() + wait
        if e.code == 'build':
            self.status = 'Unofficial build'
            self.backoff = time.time() + 3600
        elif e.code == 'version':
            self.status = 'Update required'
            self.backoff = time.time() + 3600
        else:
            self.status = f'Retrying in {int(wait)} s: {e}'
        log.warning('push failed: %s (%s)', e, e.code)

    def _sync(self, fid: str, now: float) -> None:
        c = self.cmdrs[fid]
        r = self._rt(fid)
        if not c.name or (r.blocked and now < r.blocked_until):
            return
        if r.verify_until > now and not r.verified and now >= r.verify_next:
            r.verify_next = now + 10
            try:
                j = self.client.push(keys.commander_key(fid), fid, 'enroll', {'name': c.name})
                r.enrolled, r.verified = True, bool(j.get('verified'))
                r.pub_id = j.get('pub_id') or r.pub_id or self.settings.commander(fid).get('pub_id')
                if r.verified:
                    r.verify_until = 0.0
                    r.prefs_sent = None
                    r.sent.clear()
            except ProtoError as e:
                if e.code not in ('claimed', 'not-enrolled'):
                    raise
                return
        prefs = self.settings.prefs(fid)
        if not any(prefs['share'].values()) and not prefs['presence'] and not r.enrolled:
            r.blocked = None
            return
        key = keys.commander_key(fid)
        try:
            if not r.enrolled:
                j = self.client.push(key, fid, 'enroll', {'name': c.name})
                r.enrolled, r.verified = True, bool(j.get('verified'))
                r.pub_id = j.get('pub_id') or self.settings.commander(fid).get('pub_id')
                self.settings.set_commander(fid, pub_id=r.pub_id, name=c.name)
                r.blocked = None
            pd = digest(prefs)
            if r.prefs_sent != pd:
                j = self.client.push(key, fid, 'prefs', prefs)
                r.verified = bool(j.get('verified', r.verified))
                r.prefs_sent = pd
            self._snapshot(c, r, key, prefs, now)
            self._presence(c, r, key, prefs, now)
            r.error = None
        except ProtoError as e:
            if e.code in ('claimed', 'not-enrolled'):
                r.blocked = e.args[0]
                r.blocked_until = now + (15 if r.verify_until > now else 3600)
                r.enrolled = False
                return
            if e.code in ('implausible', 'body'):
                r.error = f'{e} ({e.extra.get("field") or e.extra.get("category") or ""})'.strip()
                return
            raise

    def _snapshot(self, c: Commander, r: Runtime, key, prefs: dict, now: float) -> None:
        cats = {}
        for cat, data in c.categories().items():
            if cat not in SPEC['snapshot_categories']:
                continue
            if cat not in SPEC['always_shared'] and not prefs['share'].get(cat):
                continue
            d = digest(data)
            if r.sent.get(cat) == d:
                continue
            if not self.force and now < r.next_ok.get(cat, 0):
                continue
            cats[cat] = (data, d)
        if not cats:
            return
        j = self.client.push(key, c.fid, 'snapshot', {'categories': {k: v[0] for k, v in cats.items()}})
        r.verified = bool(j.get('verified', r.verified))
        for cat in j.get('accepted', []):
            if cat in cats:
                r.sent[cat] = cats[cat][1]
                r.next_ok[cat] = now + DEBOUNCE
        for cat in j.get('refused', []):
            if cat in cats:
                r.sent[cat] = cats[cat][1]
        r.last_push = now

    def _presence(self, c: Commander, r: Runtime, key, prefs: dict, now: float) -> None:
        if not prefs['presence']:
            return
        p = c.presence()
        d = digest(p)
        last_d, last_t = r.presence_sent
        if d == last_d and (not p['in_game'] or now - last_t < PRESENCE_EVERY) and not self.force:
            return
        self.client.push(key, c.fid, 'presence', p)
        r.presence_sent = (d, now)
        r.last_push = now
