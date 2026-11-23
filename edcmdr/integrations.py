"""Optional uploads to community services, straight from this computer: EDDN, EDSM and EDAstro.

Each is off until switched on for a commander in the Integrations tab. Only events read after the
history import are sent (what you play while the client runs); nothing from the import is. Events
are batched on a thread per service; a failed batch is kept on disk and retried. Paused or stopped
means nothing is sent.
"""
import gzip
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

from . import NAME, VERSION, eddn, paths

log = logging.getLogger('edcmdr')

TIMEOUT = 30
SPILL_MAX = 5000

EDSM_URL = 'https://www.edsm.net/api-journal-v1'
EDSM_DISCARD_URL = 'https://www.edsm.net/api-journal-v1/discard'
EDSM_SKIP = frozenset({'Fileheader', 'continued', 'Shutdown', 'ShutDown'})

EDASTRO_URL = 'https://edastro.com/api/journal'
EDASTRO_INTEREST_URL = 'https://edastro.com/api/journal/interested'
EDASTRO_CARRIER = frozenset({'CarrierStatus', 'CarrierJumpRequest'})
EDASTRO_FALLBACK = frozenset({
    'Scan', 'SAAScanComplete', 'SAASignalsFound', 'FSSBodySignals', 'FSSDiscoveryScan', 'FSSSignalDiscovered',
    'NavBeaconScan', 'CodexEntry', 'ScanOrganic', 'SellOrganicData', 'FSDJump', 'CarrierJump', 'Location',
    'Docked', 'Undocked', 'MarketBuy', 'MarketSell', 'MissionAccepted', 'MissionCompleted', 'MissionFailed',
    'MissionAbandoned', 'Bounty', 'RedeemVoucher', 'Rank', 'Progress', 'Promotion', 'Powerplay',
    'PowerplaySalary', 'PowerplayCollect', 'EngineerProgress', 'MaterialCollected', 'MaterialTrade',
    'MiningRefined', 'ProspectedAsteroid', 'ShipyardBuy', 'ShipyardSell', 'ModuleBuy', 'ModuleSell', 'Loadout',
    'CommunityGoal', 'CommunityGoalJoin', 'CommunityGoalReward', 'CarrierStats', 'CarrierJumpCancelled',
    'CarrierStatus', 'CarrierJumpRequest'})

SERVICES = {
    'eddn': {'label': 'EDDN', 'keys': {'enabled': False}},
    'edsm': {'label': 'EDSM', 'keys': {'enabled': False, 'api_key': ''}},
    'edastro': {'label': 'EDAstro', 'keys': {'enabled': False, 'carrier': False}},
}


def beta(gameversion: str) -> bool:
    g = (gameversion or '').lower()
    return 'beta' in g or g.startswith('3.')


class Tracker:
    """Where a commander is, as EDSM wants each event annotated, and the game version."""

    def __init__(self):
        self.system = self.address = self.pos = self.market = self.station = self.ship = None
        self.gameversion = self.build = ''

    def update(self, ev: dict) -> None:
        e = ev.get('event')
        if e in ('Fileheader', 'LoadGame'):
            self.gameversion = ev.get('gameversion') or self.gameversion
            self.build = ev.get('build') or self.build
        if e in ('FSDJump', 'CarrierJump', 'Location', 'Docked'):
            self.system = ev.get('StarSystem') or self.system
            self.address = ev.get('SystemAddress') or self.address
            self.pos = ev.get('StarPos') or self.pos
        if e in ('FSDJump', 'CarrierJump', 'Undocked'):
            self.market = self.station = None
        if e in ('Docked', 'Location') and ev.get('Docked', e == 'Docked'):
            self.market = ev.get('MarketID') or self.market
            self.station = ev.get('StationName') or self.station
        if e in ('LoadGame', 'Loadout', 'ShipyardSwap', 'ShipyardBuy') and isinstance(ev.get('ShipID'), int):
            self.ship = ev['ShipID']

    def annotate(self, ev: dict) -> dict:
        out = dict(ev)
        for k, v in (('_systemName', self.system), ('_systemAddress', self.address), ('_systemCoordinates', self.pos),
                     ('_marketId', self.market), ('_stationName', self.station), ('_shipId', self.ship)):
            if v is not None:
                out.setdefault(k, v)
        return out


class Sender:
    """One service: a queue of (key, event) per commander, sent in batches on its own thread."""

    name = ''
    batch_max = 30
    gap = 10.0
    retry_wait = 60.0

    def __init__(self, spill_dir: Path):
        self.spill = spill_dir / f'{self.name}.jsonl'
        self.q: deque = deque(maxlen=SPILL_MAX)
        self.cv = threading.Condition()
        self.stop = False
        self.last_send = 0.0
        self.status = 'Idle'
        self.sent = 0
        self._load_spill()
        self.thread = threading.Thread(target=self._run, name=f'edcmdr-{self.name}', daemon=True)
        self.thread.start()

    def _load_spill(self) -> None:
        try:
            for line in self.spill.read_text().splitlines():
                self.q.append(tuple(json.loads(line)))
            self.spill.unlink()
        except (OSError, ValueError, TypeError):
            pass

    def _save_spill(self, items: list) -> None:
        try:
            self.spill.parent.mkdir(parents=True, exist_ok=True)
            with self.spill.open('a') as f:
                for it in items[-SPILL_MAX:]:
                    f.write(json.dumps(list(it)) + '\n')
        except OSError:
            log.warning('%s: could not keep %d unsent events', self.name, len(items))

    def push(self, group: tuple, ev: dict) -> None:
        with self.cv:
            self.q.append((list(group), ev))
            self.cv.notify()

    def shutdown(self) -> None:
        with self.cv:
            self.stop = True
            self.cv.notify()
        self.thread.join(5)
        with self.cv:
            if self.q:
                self._save_spill(list(self.q))
                self.q.clear()

    def _take(self) -> tuple[list, list[dict]]:
        """The first group's events, up to batch_max, in order."""
        group = self.q[0][0]
        out, rest = [], deque(maxlen=SPILL_MAX)
        while self.q:
            g, ev = self.q.popleft()
            if g == group and len(out) < self.batch_max:
                out.append(ev)
            else:
                rest.append((g, ev))
        self.q.extend(rest)
        return group, out

    def _run(self) -> None:
        while True:
            with self.cv:
                while not self.q and not self.stop:
                    self.cv.wait(5)
                if self.stop:
                    return
                wait = self.gap - (time.monotonic() - self.last_send)
            if wait > 0:
                time.sleep(min(wait, 1.0))
                continue
            with self.cv:
                group, batch = self._take()
            self.last_send = time.monotonic()
            try:
                self.send(group, batch)
                self.sent += len(batch)
                self.status = f'Sent {self.sent} events this session'
            except Retry as e:
                self.status = f'Will retry: {e}'
                with self.cv:
                    for ev in reversed(batch):
                        self.q.appendleft((group, ev))
                    self.cv.wait_for(lambda: self.stop, self.retry_wait)
            except Exception as e:  # noqa: BLE001
                self.status = f'Refused: {e}'
                log.warning('%s refused %d events: %s', self.name, len(batch), e)

    def send(self, group: list, batch: list[dict]) -> None:
        raise NotImplementedError

    @staticmethod
    def post(url: str, body: bytes, headers: dict) -> bytes:
        req = urllib.request.Request(url, data=body, method='POST',
                                     headers={'User-Agent': f'{NAME}/{VERSION}', **headers})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (408, 429) or e.code >= 500:
                raise Retry(f'HTTP {e.code}') from e
            raise RuntimeError(f'HTTP {e.code}') from e
        except (urllib.error.URLError, OSError) as e:
            raise Retry(str(e)) from e


class Retry(Exception):
    """A failure worth retrying: network, timeout, rate limit, server error."""


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={'User-Agent': f'{NAME}/{VERSION}'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


class EDSM(Sender):
    name = 'edsm'
    batch_max = 50
    gap = 12.0
    url = EDSM_URL

    def __init__(self, spill_dir: Path):
        self.discard: frozenset[str] = frozenset()
        super().__init__(spill_dir)
        threading.Thread(target=self._discard, daemon=True, name='edcmdr-edsm-discard').start()

    def _discard(self) -> None:
        try:
            d = fetch_json(EDSM_DISCARD_URL)
            if isinstance(d, list):
                self.discard = frozenset(x for x in d if isinstance(x, str))
        except Exception as e:  # noqa: BLE001
            log.info('EDSM discard list unavailable (%s); EDSM drops what it does not take', e)

    def wants(self, ev: dict) -> bool:
        return ev.get('event') not in EDSM_SKIP and ev.get('event') not in self.discard

    def send(self, group: list, batch: list[dict]) -> None:
        cmdr, key, gv, build = group
        form = {'commanderName': cmdr, 'apiKey': key, 'fromSoftware': NAME, 'fromSoftwareVersion': VERSION,
                'fromGameVersion': gv, 'fromGameBuild': build,
                'message': json.dumps(batch, separators=(',', ':'))}
        body = self.post(self.url, urllib.parse.urlencode(form).encode(),
                         {'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            j = json.loads(body)
        except ValueError as e:
            raise Retry('unreadable reply') from e
        if j.get('msgnum') != 100:
            raise RuntimeError(f"{j.get('msgnum')} {j.get('msg', '')}".strip())


class EDAstro(Sender):
    name = 'edastro'
    batch_max = 30
    gap = 10.0
    url = EDASTRO_URL

    def __init__(self, spill_dir: Path):
        self.interest = EDASTRO_FALLBACK
        super().__init__(spill_dir)
        threading.Thread(target=self._interest, daemon=True, name='edcmdr-edastro-interest').start()

    def _interest(self) -> None:
        try:
            d = fetch_json(EDASTRO_INTEREST_URL)
            if isinstance(d, list) and d:
                self.interest = frozenset(x for x in d if isinstance(x, str))
        except Exception as e:  # noqa: BLE001
            log.info('EDAstro interest list unavailable (%s); using the built-in list', e)

    def wants(self, ev: dict, carrier: bool) -> bool:
        e = ev.get('event')
        return e in self.interest and (carrier or e not in EDASTRO_CARRIER)

    def send(self, group: list, batch: list[dict]) -> None:
        self.post(self.url, gzip.compress(json.dumps(batch, separators=(',', ':')).encode()),
                  {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})


class EDDN(Sender):
    name = 'eddn'
    batch_max = 1
    gap = 0.2
    url = eddn.ENDPOINT

    def send(self, group: list, batch: list[dict]) -> None:
        for env in batch:
            self.post(self.url, gzip.compress(json.dumps(env, separators=(',', ':')).encode()),
                      {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})


class Hub:
    """Routes live journal events to the services each commander has switched on."""

    def __init__(self, settings, spill_dir: Path | None = None):
        self.settings = settings
        self.spill_dir = spill_dir or paths.config_dir() / 'queue'
        self.trackers: dict[str, Tracker] = {}
        self.names: dict[str, str] = {}
        self.senders: dict[str, Sender] = {}
        self.builders: dict[str, eddn.Builder] = {}
        self.lock = threading.Lock()

    @staticmethod
    def config(settings, fid: str, service: str) -> dict:
        c = settings.commander(fid).get('integrations', {}).get(service, {})
        return {**SERVICES[service]['keys'], **(c if isinstance(c, dict) else {})}

    def any_on(self) -> bool:
        return any(self.config(self.settings, fid, s)['enabled']
                   for fid in self.settings.data.get('commanders', {}) for s in SERVICES)

    def _sender(self, service: str) -> Sender:
        with self.lock:
            if service not in self.senders:
                self.senders[service] = {'eddn': EDDN, 'edsm': EDSM, 'edastro': EDAstro}[service](self.spill_dir)
            return self.senders[service]

    def observe(self, fid: str, ev: dict) -> None:
        """An event from the history import: keeps each commander's position and game version current."""
        self._track(fid, ev)
        self.builders.setdefault(fid, eddn.Builder()).track(ev)

    def _track(self, fid: str, ev: dict) -> Tracker:
        t = self.trackers.setdefault(fid, Tracker())
        t.update(ev)
        if ev.get('event') in ('Commander', 'LoadGame'):
            name = ev.get('Name') or ev.get('Commander')
            if name:
                self.names[fid] = name
        return t

    def live(self, fid: str, ev: dict, journal_dir: Path | None = None) -> None:
        """An event read after the history import: send it to each service switched on for this commander."""
        t = self._track(fid, ev)
        b = self.builders.setdefault(fid, eddn.Builder())
        try:
            self._send(fid, ev, t, b, journal_dir)
        finally:
            b.track(ev)

    def _send(self, fid: str, ev: dict, t: Tracker, b: eddn.Builder, journal_dir: Path | None) -> None:
        if beta(t.gameversion):
            return
        clean = {k: v for k, v in ev.items() if not k.startswith('_')}
        if self.config(self.settings, fid, 'eddn')['enabled'] and self.names.get(fid) and t.gameversion:
            msgs = b.messages(clean, journal_dir)
            if msgs:
                s = self._sender('eddn')
                header = {'uploaderID': self.names[fid], 'softwareName': NAME, 'softwareVersion': VERSION,
                          'gameversion': t.gameversion, 'gamebuild': t.build}
                for ref, msg in msgs:
                    s.push(('',), {'$schemaRef': ref, 'header': header, 'message': msg})
        edsm = self.config(self.settings, fid, 'edsm')
        if edsm['enabled'] and edsm['api_key'] and self.names.get(fid) and t.gameversion:
            s = self._sender('edsm')
            if s.wants(clean):
                s.push((self.names[fid], edsm['api_key'], t.gameversion, t.build), t.annotate(clean))
        astro = self.config(self.settings, fid, 'edastro')
        if astro['enabled']:
            s = self._sender('edastro')
            if s.wants(clean, astro['carrier']):
                s.push(('',), clean)

    def status(self) -> dict[str, str]:
        return {k: s.status for k, s in self.senders.items()}

    def shutdown(self) -> None:
        for s in list(self.senders.values()):
            s.shutdown()
