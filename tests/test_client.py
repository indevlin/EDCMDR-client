#!/usr/bin/env python3
"""Client tests. Unit tests always run; the end-to-end class runs against a real
EDCMDR server checkout when EDCMDR_SERVER_REPO points at one (needs php and MariaDB,
as that repo's tests do).

  python3 tests/test_client.py
  EDCMDR_SERVER_REPO=~/projects/indevlin/EDCMDR python3 tests/test_client.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix='edcmdr-test-'))
os.environ['EDCMDR_HOME'] = str(TMP / 'home')

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from edcmdr import journal, keys  # noqa: E402
from edcmdr.config import Settings  # noqa: E402
from edcmdr.proto import Client, ProtoError  # noqa: E402
from edcmdr.state import EVENTS, Commander  # noqa: E402

FIX = ROOT / 'tests' / 'journals'


def load(dirs):
    cm = {}
    w = journal.Watcher(dirs, lambda f, e: cm.setdefault(f, Commander(f)).handle(e), EVENTS)
    w.poll()
    return w, cm


class TestJournal(unittest.TestCase):
    def test_owners_and_routing(self):
        w, cm = load([FIX])
        self.assertEqual(w.commanders(), {'F1000001': 'ALPHA ONE', 'F1000002': 'BRAVO TWO'})
        self.assertEqual(cm['F1000002'].name, 'BRAVO TWO')

    def test_tail_and_switch(self):
        d = TMP / 'tail'
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
        shutil.copy(FIX / 'Journal.2026-11-20T100000.01.log', d)
        w, cm = load([d])
        c = cm['F1000001']
        self.assertEqual(c.ranks['rank']['Combat'], 4)
        with (d / 'Journal.2026-11-20T100000.01.log').open('a') as f:
            f.write('{ "timestamp":"2026-11-20T10:31:00Z", "event":"Promotion", "Combat":5 }\n{ "timestamp":"2026-11-20T10:3')
        w.poll()
        self.assertEqual(c.ranks['rank']['Combat'], 5)
        with (d / 'Journal.2026-11-20T100000.01.log').open('a') as f:
            f.write('2:00Z", "event":"Promotion", "Trade":14 }\n')
        w.poll()
        self.assertEqual(c.ranks['rank']['Trade'], 14, 'partial line joined across reads')
        new = d / 'Journal.2026-11-21T090000.01.log'
        new.write_text('{ "timestamp":"2026-11-21T09:00:00Z", "event":"Fileheader", "part":1 }\n')
        w.poll()
        with new.open('a') as f:
            f.write('{ "timestamp":"2026-11-21T09:00:05Z", "event":"Commander", "FID":"F1000001", "Name":"ALPHA ONE" }\n'
                    '{ "timestamp":"2026-11-21T09:00:06Z", "event":"LoadGame", "Commander":"ALPHA ONE", "GameMode":"Group" }\n')
        w.poll()
        self.assertEqual(c.mode, 'Group', 'new file assigned once its Commander line appears')


class Svc(BaseHTTPRequestHandler):
    """Stands in for EDSM and EDAstro."""
    got: list = []
    fail = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == '/market':
            if self.headers.get('Authorization') != 'Bearer fresh':
                return self._reply(401, b'{}')
            return self._reply(200, json.dumps({'id': 900, 'name': 'N2Z-N8M', 'orders': {'onfootmicroresources': {
                'sales': {'1': {'name': 'chemicalsample', 'locName': 'Chemical Sample', 'price': 100, 'stock': 3}},
                'purchases': [{'name': 'healthmonitor', 'locName': 'Health Monitor', 'price': 50, 'total': 9}]}}}).encode())
        body = {'/discard': ['Music', 'Friends'], '/interested': ['FSDJump', 'Scan', 'CarrierStatus']}
        self._reply(200, json.dumps(body.get(self.path, [])).encode())

    def do_POST(self):
        import gzip
        import urllib.parse
        raw = self.rfile.read(int(self.headers['Content-Length']))
        if Svc.fail:
            Svc.fail -= 1
            return self._reply(503, b'busy')
        if self.headers.get('Content-Encoding') == 'gzip':
            Svc.got.append(('eddn' if self.path == '/eddn' else 'edastro', json.loads(gzip.decompress(raw))))
            return self._reply(200, b'{}')
        if self.path == '/token':
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            Svc.got.append(('token', form))
            if form.get('refresh_token') != 'r1':
                return self._reply(400, b'{"error":"invalid_grant"}')
            return self._reply(200, json.dumps({'access_token': 'fresh', 'refresh_token': 'r2', 'expires_in': 7200}).encode())
        if self.path == '/inara':
            Svc.got.append(('inara', json.loads(raw)))
            return self._reply(200, json.dumps({'header': {'eventStatus': 200}}).encode())
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        Svc.got.append(('edsm', form))
        self._reply(200, json.dumps({'msgnum': 100, 'msg': 'OK'}).encode())

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestIntegrations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edcmdr import integrations as it
        cls.it = it
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), Svc)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{cls.srv.server_address[1]}'
        it.EDSM_DISCARD_URL, it.EDASTRO_INTEREST_URL = base + '/discard', base + '/interested'
        it.EDSM.url, it.EDAstro.url, it.EDDN.url = base + '/edsm', base + '/astro', base + '/eddn'
        it.Inara.url = base + '/inara'
        it.Inara.gap = 0.05
        it.EDSM.gap = it.EDAstro.gap = it.EDDN.gap = 0.05
        it.Sender.retry_wait = 30

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Svc.got, Svc.fail = [], 0
        self.dir = Path(tempfile.mkdtemp(dir=TMP))
        self.settings = Settings(self.dir / 'settings.json')

    def wait(self, n, t=5):
        end = time.time() + t
        while len(Svc.got) < n and time.time() < end:
            time.sleep(0.02)
        return Svc.got

    def hub(self):
        h = self.it.Hub(self.settings, self.dir / 'q')
        self.addCleanup(h.shutdown)
        for ev in ({'event': 'Fileheader', 'gameversion': '4.2.1.0', 'build': 'r1'},
                   {'event': 'Commander', 'FID': 'F1', 'Name': 'Tester'},
                   {'event': 'FSDJump', 'StarSystem': 'Sol', 'SystemAddress': 10477373803, 'StarPos': [0, 0, 0]}):
            h.observe('F1', ev)
        return h

    def test_tracker(self):
        t = self.it.Tracker()
        t.update({'event': 'Docked', 'StarSystem': 'Sol', 'MarketID': 7, 'StationName': 'Abraham Lincoln'})
        self.assertEqual(t.annotate({'event': 'MarketSell'})['_marketId'], 7)
        t.update({'event': 'Undocked'})
        self.assertNotIn('_marketId', t.annotate({'event': 'X'}))
        self.assertEqual(t.annotate({'event': 'X'})['_systemName'], 'Sol')
        self.assertTrue(self.it.beta('4.0.0.100 beta') and self.it.beta('3.8.0.407'))

    def test_off_sends_nothing(self):
        h = self.hub()
        h.live('F1', {'event': 'FSDJump', 'StarSystem': 'Sol'})
        time.sleep(0.3)
        self.assertEqual(Svc.got, [])
        self.assertFalse(h.any_on())

    def test_edsm(self):
        self.settings.set_integration('F1', 'edsm', {'enabled': True, 'api_key': 'k123'})
        h = self.hub()
        h._sender('edsm')
        time.sleep(0.2)
        for ev in ({'event': 'Scan', 'BodyName': 'Earth'}, {'event': 'Music', 'MusicTrack': 'x'},
                   {'event': 'Shutdown'}, {'event': 'Docked', 'StarSystem': 'Sol', '_logtime': 1}):
            h.live('F1', ev)
        kind, form = self.wait(1)[0]
        self.assertEqual(kind, 'edsm')
        self.assertEqual((form['commanderName'], form['apiKey'], form['fromSoftware'], form['fromGameVersion']),
                         ('Tester', 'k123', 'EDCMDR-client', '4.2.1.0'))
        msg = json.loads(form['message'])
        self.assertEqual([m['event'] for m in msg], ['Scan', 'Docked'])
        self.assertEqual(msg[0]['_systemName'], 'Sol')
        self.assertNotIn('_logtime', msg[1])

    def test_edsm_needs_key(self):
        self.settings.set_integration('F1', 'edsm', {'enabled': True, 'api_key': ''})
        self.hub().live('F1', {'event': 'Scan'})
        time.sleep(0.3)
        self.assertEqual(Svc.got, [])

    def test_edastro_and_carrier(self):
        self.settings.set_integration('F1', 'edastro', {'enabled': True, 'carrier': False})
        h = self.hub()
        h._sender('edastro')
        time.sleep(0.2)
        for e in ('FSDJump', 'CarrierStatus', 'Docked', 'Scan'):
            h.live('F1', {'event': e})
        kind, batch = self.wait(1)[0]
        self.assertEqual(kind, 'edastro')
        self.assertEqual([b['event'] for b in batch], ['FSDJump', 'Scan'])

    def test_beta_skipped(self):
        self.settings.set_integration('F1', 'edastro', {'enabled': True})
        h = self.hub()
        h.observe('F1', {'event': 'LoadGame', 'gameversion': '4.3.0.1 beta'})
        h.live('F1', {'event': 'FSDJump'})
        time.sleep(0.3)
        self.assertEqual(Svc.got, [])

    def test_eddn(self):
        self.settings.set_integration('F1', 'eddn', {'enabled': True})
        jd = self.dir / 'journals'
        jd.mkdir()
        (jd / 'Market.json').write_text(json.dumps({
            'timestamp': 't', 'MarketID': 5, 'StationName': 'Galileo', 'StarSystem': 'Sol', 'Items': [
                {'Name': '$gold_name;', 'Category': '$MARKET_category_metals;', 'MeanPrice': 1, 'BuyPrice': 2,
                 'Stock': 3, 'StockBracket': 1, 'SellPrice': 4, 'Demand': 0, 'DemandBracket': 0},
                {'Name': '$drones_name;', 'Category': '$MARKET_category_nonmarketable;', 'MeanPrice': 1,
                 'BuyPrice': 1, 'Stock': 1, 'StockBracket': 1, 'SellPrice': 1, 'Demand': 0, 'DemandBracket': 0}]}))
        h = self.hub()
        h.observe('F1', {'event': 'LoadGame', 'Odyssey': True, 'Horizons': True})
        h.live('F1', {'event': 'Scan', 'BodyName': 'Earth', 'SystemAddress': 10477373803, 'Latitude': 1,
                      'Materials_Localised': 'x', 'timestamp': 't'})
        h.live('F1', {'event': 'Docked', 'MarketID': 5, 'StationName': 'Galileo', 'StarSystem': 'Sol',
                      'SystemAddress': 10477373803, 'timestamp': 't'})
        h.live('F1', {'event': 'Market', 'MarketID': 5, 'timestamp': 't'}, jd)
        h.live('F1', {'event': 'Market', 'MarketID': 5, 'timestamp': 't'}, jd)
        got = [g[1] for g in self.wait(3)]
        time.sleep(0.3)
        self.assertEqual(len(Svc.got), 3, 'an unchanged market is sent once')
        scan, docked, market = got
        self.assertEqual(scan['$schemaRef'], 'https://eddn.edcd.io/schemas/journal/1')
        self.assertEqual(scan['header']['uploaderID'], 'Tester')
        self.assertEqual(scan['header']['softwareName'], 'EDCMDR-client')
        m = scan['message']
        self.assertEqual((m['StarSystem'], m['StarPos'], m['odyssey']), ('Sol', [0, 0, 0], True))
        self.assertNotIn('Latitude', m)
        self.assertNotIn('Materials_Localised', m)
        self.assertEqual(market['$schemaRef'], 'https://eddn.edcd.io/schemas/commodity/3')
        self.assertEqual([c['name'] for c in market['message']['commodities']], ['gold'])

    def test_eddn_builder(self):
        from edcmdr import eddn
        b = eddn.Builder()
        b.track({'event': 'FSDJump', 'StarSystem': 'A', 'SystemAddress': 1, 'StarPos': [1, 2, 3]})
        sig = {'event': 'FSSSignalDiscovered', 'SystemAddress': 1, 'SignalName': 'X', 'timestamp': 't'}
        miss = dict(sig, USSType='$USS_Type_MissionTarget;')
        self.assertEqual(b.messages(sig, None) + b.messages(miss, None), [])
        out = b.messages({'event': 'Music'}, None)
        self.assertEqual(out[0][0], 'https://eddn.edcd.io/schemas/fsssignaldiscovered/1')
        self.assertEqual(out[0][1]['signals'], [{'SignalName': 'X', 'timestamp': 't'}])
        b.track({'event': 'FSDJump', 'StarSystem': 'B', 'SystemAddress': 2, 'StarPos': [4, 5, 6]})
        late = b.messages({'event': 'FSSDiscoveryScan', 'SystemAddress': 1, 'Progress': 0.5}, None)[0][1]
        self.assertEqual((late['StarSystem'], late.get('Progress')), ('A', None), 'a late event keeps its own system')
        self.assertEqual(b.messages({'event': 'NavBeaconScan', 'SystemAddress': 99}, None), [])
        self.assertEqual(b.messages({'event': 'MarketSell', 'IllegalGoods': True, 'Type': 'x', 'SellPrice': 1}, None),
                         [], 'black market needs a station')

    def test_inara(self):
        self.settings.set_integration('F1', 'inara', {'enabled': True, 'api_key': 'ik'})
        h = self.hub()
        h.observe('F1', {'event': 'Rank', 'Combat': 4, 'Trade': 13})
        h.observe('F1', {'event': 'Progress', 'Combat': 43, 'Trade': 100})
        h.live('F1', {'event': 'FSDJump', 'StarSystem': 'Ogmar', 'StarPos': [1, 2, 3], 'JumpDist': 12.345,
                      'timestamp': '2026-11-23T10:00:00Z'})
        kind, body = self.wait(1)[0]
        self.assertEqual(kind, 'inara')
        self.assertEqual(body['header']['appName'], 'EDCMDR-client')
        self.assertEqual((body['header']['APIkey'], body['header']['commanderName']), ('ik', 'Tester'))
        self.assertNotIn('commanderFrontierID', body['header'], 'the FID goes only with the application key')
        names = [e['eventName'] for e in body['events']]
        self.assertEqual(names, ['addCommanderTravelFSDJump', 'setCommanderRankPilot'])
        self.assertEqual(body['events'][0]['eventData']['jumpDistance'], 12.35)
        ranks = {r['rankName']: r for r in body['events'][1]['eventData']}
        self.assertEqual((ranks['trade']['rankValue'], ranks['trade']['rankProgress']), (13, 1.0))

    def test_inara_needs_a_key(self):
        from edcmdr import inara
        self.assertEqual(inara.INARA_APP_KEY, '', 'placeholder until Inara issues the application key')
        self.settings.set_integration('F1', 'inara', {'enabled': True, 'api_key': ''})
        self.hub().live('F1', {'event': 'FSDJump', 'StarSystem': 'Ogmar'})
        time.sleep(0.3)
        self.assertEqual(Svc.got, [])
        self.assertTrue(inara.throttled(400, 'Too much requests, slow down, cowboy.'))

    def capi_hub(self):
        from edcmdr import capi
        base = f'http://127.0.0.1:{self.srv.server_address[1]}'
        h = self.hub()
        h.capi_base, h.capi_root = base, self.dir / 'capi'
        store = capi.Store('F1', h.capi_root)
        store.keep({'access_token': 'old', 'refresh_token': 'r1', 'expires_in': 1}, base + '/token', 'cid', 'https://edcmdr.com/cb')
        return h, store

    def test_capi_store_is_private(self):
        h, store = self.capi_hub()
        self.assertEqual(oct(store.path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(store.load()['refresh_token'], 'r1')

    def test_capi_carrier_materials(self):
        self.settings.set_integration('F1', 'eddn', {'enabled': True})
        self.settings.set_integration('F1', 'capi', {'enabled': True})
        h, store = self.capi_hub()
        h.live('F1', {'event': 'Docked', 'StationType': 'FleetCarrier', 'StationName': 'N2Z-N8M', 'MarketID': 900,
                      'StarSystem': 'Sol', 'SystemAddress': 10477373803, 'timestamp': 't'})
        got = self.wait(3)
        self.assertEqual([g for g in got if g[0] == 'token'][0], ('token', {'grant_type': 'refresh_token', 'client_id': 'cid',
                                            'redirect_uri': 'https://edcmdr.com/cb', 'refresh_token': 'r1'}))
        self.assertEqual(store.load()['refresh_token'], 'r2', 'the refreshed sign-in is kept')
        env = [g[1] for g in got if g[0] == 'eddn' and 'fcmaterials' in g[1]['$schemaRef']][0]
        self.assertEqual(env['header']['gameversion'], 'CAPI-Live-market')
        items = env['message']['Items']
        self.assertNotIn('locName', items['purchases'][0])
        self.assertNotIn('locName', items['sales']['1'])

    def test_capi_refused_refresh_forgets(self):
        from edcmdr import capi
        h, store = self.capi_hub()
        t = store.load()
        t['refresh_token'] = 'bad'
        store.save(t)
        with self.assertRaises(capi.SignInNeeded):
            capi.Client(store, h.capi_base).token()
        self.assertFalse(store.path.exists(), 'a refused sign-in is deleted')

    def test_retry_then_spill(self):
        self.settings.set_integration('F1', 'edastro', {'enabled': True})
        Svc.fail = 1
        h = self.hub()
        time.sleep(0.2)
        h.live('F1', {'event': 'FSDJump'})
        time.sleep(0.4)
        s = h.senders['edastro']
        self.assertIn('retry', s.status)
        h.shutdown()
        self.assertTrue(s.spill.exists())
        h2 = self.it.Hub(self.settings, self.dir / 'q')
        self.addCleanup(h2.shutdown)
        h2._sender('edastro')
        self.assertEqual(self.wait(1)[0][1][0]['event'], 'FSDJump')
        self.assertFalse(s.spill.exists())


class TestPublisherLock(unittest.TestCase):
    def test_one_publisher(self):
        from edcmdr.single import PublisherLock
        path = TMP / 'publisher.lock'
        a, b = PublisherLock(path), PublisherLock(path)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire(), 'a second publisher stands by')
        a.release()
        self.assertTrue(b.acquire(), 'and takes over once the first stops')
        b.release()


class TestState(unittest.TestCase):
    def test_categories(self):
        _, cm = load([FIX])
        cats = cm['F1000001'].categories()
        self.assertEqual(cats['identity'], {'name': 'ALPHA ONE'})
        self.assertEqual(cats['squadron'], {'id': 42, 'name': 'TEST WING', 'rank': 0, 'rank_name': 'Executive Director'})
        self.assertEqual(cats['powerplay']['merits'], 1202899)
        self.assertEqual(cats['location']['station'], 'Test Port')
        ships = {s['id']: s for s in cats['fleet']['ships']}
        self.assertEqual(set(ships), {0, 3, 5}, 'taxi excluded')
        self.assertEqual(cats['fleet']['current'], 0)
        self.assertEqual(ships[0]['type_name'], 'Mandalay')
        self.assertEqual(ships[0]['modules'][0]['eng']['experimental'], 'Deep Plating')
        self.assertNotIn('station', ships[3], 'positions belong to location')
        at = {s['id']: s for s in cats['location']['ships']}
        self.assertEqual(at[3]['station'], 'Test Port')
        self.assertEqual(at[5]['system'], 'Sol')
        self.assertEqual(ships[5]['type_name'], 'Cobra Mk V')
        self.assertFalse(cm['F1000001'].presence()['in_game'], 'Shutdown ends presence')

    def test_ships_on_a_carrier(self):
        c = Commander('F1')
        for ev in (
            {'event': 'CarrierStats', 'CarrierID': 900, 'CarrierType': 'FleetCarrier', 'Name': 'VECTURA', 'Callsign': 'N2Z-N8M'},
            {'event': 'Docked', 'StationName': 'N2Z-N8M', 'StationType': 'FleetCarrier', 'MarketID': 900, 'StarSystem': 'Sol'},
            {'event': 'Loadout', 'Ship': 'mandalay', 'ShipID': 0, 'ModulesValue': 6641893, 'Modules': []},
            {'event': 'ShipyardSwap', 'ShipType': 'cobramkv', 'ShipID': 19, 'StoreOldShip': 'mandalay', 'StoreShipID': 0},
            {'event': 'StoredShips', 'StationName': 'N2Z-N8M', 'MarketID': 900, 'StarSystem': 'Sol',
             'ShipsHere': [{'ShipID': 0, 'ShipType': 'mandalay', 'Value': 5977703}], 'ShipsRemote': []},
            {'event': 'Undocked'},
            {'event': 'CarrierLocation', 'CarrierType': 'FleetCarrier', 'CarrierID': 900, 'StarSystem': 'Kitchang Mu'},
            {'event': 'FSDJump', 'StarSystem': 'Ogmar'},
        ):
            c.handle(ev)
        cats = c.categories()
        self.assertEqual(cats['fleet']['ships'][0]['value'], 6641893, 'the Loadout value, not the stored-ship figure')
        self.assertEqual(cats['location']['ships'], [{'id': 0, 'system': 'Kitchang Mu', 'station': 'VECTURA (N2Z-N8M)'}],
                         'a ship aboard a carrier moves with it; the current ship is not listed')
        self.assertNotIn('market', cats['fleet']['ships'][0])

    def test_presence_stale(self):
        _, cm = load([FIX])
        c = cm['F1000002']
        self.assertFalse(c.presence()['in_game'], 'old LoadGame without Shutdown is stale')
        self.assertTrue(c.presence(time.time() - 60)['in_game'], 'a recent write by the game keeps it live')
        self.assertFalse(cm['F1000001'].presence(time.time())['in_game'], 'but not after Shutdown')

    def test_activity(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        (d / 'Journal.2026-11-21T100000.01.log').write_text(
            '{"timestamp":"2026-11-21T10:00:00Z","event":"Fileheader","part":1,"gameversion":"4.2.1.0"}\n'
            '{"timestamp":"2026-11-21T10:00:01Z","event":"Commander","FID":"F1000009","Name":"Idle"}\n')
        (d / 'Status.json').write_text('{}')
        w, cm = load([d])
        self.assertGreater(w.activity['F1000009'], time.time() - 5, 'the game writing its files counts')


SERVER = Ed25519PrivateKey.generate()
BUILD = Ed25519PrivateKey.generate()


class Fake(BaseHTTPRequestHandler):
    mode = 'ok'

    def log_message(self, *a):
        pass

    def _reply(self, obj, status=200, sign=True, key=SERVER):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        if sign:
            self.send_header('X-EDCMDR-Server', keys.key_id(keys.raw_pub(key)))
            self.send_header('X-EDCMDR-Reply-Sig', keys.b64u(key.sign(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Fake.agent = self.headers.get('User-Agent', '')
        n = self.path.split('n=')[1]
        self._reply({'ok': True, 'kind': 'hello', 'n': n if Fake.mode != 'nonce' else 'x' * 20, 'protocols': [1],
                     'time': int(time.time()) + 100})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        env = json.loads(raw)
        kid, sig = self.headers['X-EDCMDR-Build'].split('.')
        BUILD.public_key().verify(keys.unb64u(sig), raw)
        Ed25519PrivateKey  # noqa
        re_ = keys.b64u(hashlib.sha256(raw).digest())[:22]
        if Fake.mode == 'unsigned':
            return self._reply({'ok': True, 're': re_}, sign=False)
        if Fake.mode == 'wrongkey':
            return self._reply({'ok': True, 're': re_}, key=Ed25519PrivateKey.generate())
        if Fake.mode == 'wrongre':
            return self._reply({'ok': True, 're': 'x' * 22})
        if Fake.mode == 'claimed':
            return self._reply({'ok': False, 're': re_, 'code': 'claimed', 'message': 'held'}, 403)
        Fake.last = env
        self._reply({'ok': True, 'kind': env['kind'], 're': re_, 'accepted': list(env['body'].get('categories', {}))})


class TestProto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        os.environ['EDCMDR_BUILD_KEY'] = keys.b64u(BUILD.private_bytes_raw())
        os.environ['EDCMDR_DEV_SERVER_KEY'] = keys.b64u(keys.raw_pub(SERVER))
        cls.base = f'http://127.0.0.1:{cls.httpd.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        Fake.mode = 'ok'
        self.c = Client(self.base)
        self.k = Ed25519PrivateKey.generate()

    def test_hello_sets_clock(self):
        self.c.hello()
        self.assertGreaterEqual(self.c.clock_offset, 99)

    def test_every_request_names_the_client(self):
        self.c.hello()
        self.assertTrue(Fake.agent.startswith('EDCMDR-client/'), Fake.agent)

    def test_unsigned_reply_says_so(self):
        Fake.mode = 'unsigned'
        with self.assertRaises(ProtoError) as e:
            self.c.push(self.k, 'F1', 'prefs', {})
        self.assertIn('HTTP 200', str(e.exception))
        self.assertIn('unsigned', str(e.exception))

    def test_hello_nonce_mismatch(self):
        Fake.mode = 'nonce'
        with self.assertRaises(ProtoError) as e:
            self.c.hello()
        self.assertEqual(e.exception.code, 'reply')

    def test_push_ok_and_seq_rises(self):
        a = self.c.push(self.k, 'F1', 'prefs', {})
        s1 = Fake.last['seq']
        self.c.push(self.k, 'F1', 'prefs', {})
        self.assertTrue(a['ok'])
        self.assertGreater(Fake.last['seq'], s1)

    def test_reply_checks(self):
        for mode in ('unsigned', 'wrongkey', 'wrongre'):
            Fake.mode = mode
            with self.assertRaises(ProtoError, msg=mode) as e:
                self.c.push(self.k, 'F1', 'prefs', {})
            self.assertEqual(e.exception.code, 'reply', mode)

    def test_refusal_code(self):
        Fake.mode = 'claimed'
        with self.assertRaises(ProtoError) as e:
            self.c.push(self.k, 'F1', 'enroll', {'name': 'X'})
        self.assertEqual((e.exception.code, e.exception.status), ('claimed', 403))

    def test_update_notice_and_maintenance_backoff(self):
        from edcmdr.uplink import Engine
        e = Engine(Settings(TMP / 'notice.json'), Client(self.base))
        e.server = {'latest_client': '20991231'}
        self.assertEqual(e.snapshot()['update'], '20991231')
        for latest in ('20200101', 'garbage!', ''):
            e.server = {'latest_client': latest}
            self.assertEqual(e.update_available(), '', latest)
        now = time.time()
        e._fail(ProtoError('maintenance', 'Back soon', 503, {'retry_after': 300}))
        self.assertTrue(e.status.startswith('Server maintenance; retrying in '))
        self.assertTrue(now + 305 <= e.backoff <= now + 361, 'retry_after plus jitter, not exponential')

    def test_http_refused_for_remote(self):
        with self.assertRaises(ProtoError):
            Client('http://example.com')

    def test_no_build_key_cannot_push(self):
        c = Client(self.base)
        c.build = None
        with self.assertRaises(ProtoError) as e:
            c.push(self.k, 'F1', 'prefs', {})
        self.assertEqual(e.exception.code, 'build')


@unittest.skipUnless(os.environ.get('EDCMDR_SERVER_REPO'), 'EDCMDR_SERVER_REPO not set')
class TestEndToEnd(unittest.TestCase):
    """The engine against the real server: enroll, prefs, snapshot, page."""

    @classmethod
    def setUpClass(cls):
        repo = Path(os.environ['EDCMDR_SERVER_REPO']).expanduser()
        sys.path.insert(0, str(repo / 'tests'))
        import test_server as ts
        cls.ts = ts
        ts.setUpModule()
        os.environ['EDCMDR_BUILD_KEY'] = keys.b64u(ts.BUILD.private_bytes_raw())
        os.environ['EDCMDR_DEV_SERVER_KEY'] = keys.b64u(ts.SERVER_PUB.public_bytes_raw())
        cls.base = f'http://127.0.0.1:{ts.SERVER.port}'

    @classmethod
    def tearDownClass(cls):
        cls.ts.tearDownModule()

    def _verify_takeover(self):
        """Another install holds BRAVO TWO; this engine verifies with Frontier and takes it over."""
        import urllib.parse
        from edcmdr.uplink import Engine
        ts = self.ts
        holder = ts.Client(fid='F1000002')
        holder.push('enroll', {'name': 'BRAVO TWO'})
        s = Settings(TMP / 'e2e-verify.json')
        s.data['journal_dirs'] = [str(FIX)]
        p = s.prefs('F1000002')
        p['share']['ranks'] = True
        s.set_prefs('F1000002', p)
        s.set_integration('F1000002', 'capi', {'enabled': True})
        os.environ['EDCMDR_HOME'] = str(TMP / 'home-verify')
        e = Engine(s, Client(self.base))
        e.tick()
        snap = {c['fid']: c for c in e.snapshot()['commanders']}
        self.assertIn('Another installation', snap['F1000002']['blocked'] or '')
        v = e.verify_start('F1000002')
        self.assertRegex(v['code'], r'^[A-Z2-9]{3}-[A-Z2-9]{3}$')
        path = urllib.parse.urlsplit(v['url']).path
        st, h = ts.get_noredirect(path + '/go')
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(h['Location']).query)
        ts.Frontier.challenge = q['code_challenge'][0]
        ts.Frontier.profile = {'commander': {'id': 1000002, 'name': 'BRAVO TWO'},
                               'squadron': {'id': 4343, 'name': 'BRAVO WING', 'tag': 'BRV', 'rank': 0}}
        st, html, _ = ts.request('/verify/callback?' + urllib.parse.urlencode({'code': 'good-code', 'state': q['state'][0]}))
        self.assertEqual(st, 200, html)
        snap = {c['fid']: c for c in e.snapshot()['commanders']}
        self.assertIn('Verifying', snap['F1000002']['verify'])
        ts.Frontier.calls.clear()
        e.rt['F1000002'].verify_next = 0
        e.tick()
        self.assertEqual([k for k, _ in ts.Frontier.calls], ['token', 'journal', 'profile'], 'client exchanges, server reads')
        c = {c['fid']: c for c in e.snapshot()['commanders']}['F1000002']
        self.assertIsNone(c['verify'])
        self.assertIsNone(e.rt['F1000002'].vtoken, 'the token is dropped')
        from edcmdr import capi
        kept = capi.Store('F1000002').load()
        self.assertTrue(kept.get('access_token'), 'with CAPI on, the sign-in stays on this computer')
        self.assertTrue(kept['token_url'].endswith('/token'))
        self.assertTrue(c['verified'] and c['enrolled'], c)
        self.assertIsNone(c['blocked'])
        with self.assertRaises(ProtoError) as err:
            Client(self.base).push(holder.key, 'F1000002', 'prefs', {})
        self.assertEqual(err.exception.code, 'not-enrolled', 'the earlier installation is revoked')

        # leader: Squadron tab data, then a portrait
        e.set_prefs('F1000002', dict(s.prefs('F1000002'), share=dict(s.prefs('F1000002')['share'], portrait=True)))
        e.tick()
        c = {c['fid']: c for c in e.snapshot()['commanders']}['F1000002']
        self.assertEqual(c['leader_of'], 4343)
        e.save_squadron('F1000002', {'home_system': 'Deriso', 'power': 'Nakato Kaine', 'factions': ['Test Co'],
                                     'recruiting': 'open', 'tagline': 'Bravo'})
        st, html, _ = ts.request('/squadron/4343/bravo-wing')
        self.assertIn('Deriso', html)
        self.assertIn('Nakato Kaine', html)
        jpg = TMP / 'codex.jpg'
        import subprocess
        subprocess.run(['php', str(Path(os.environ['EDCMDR_SERVER_REPO']).expanduser() / 'tests/make_codex.php'), str(jpg)], check=True)
        url = e.push_portrait('F1000002', jpg.read_bytes())
        self.assertRegex(url, r'^/p/[a-z2-7]{10,12}-1\.webp$')
        st, html, _ = ts.request('/squadron/4343/bravo-wing')
        self.assertNotIn('BRAVO TWO', html, 'membership not shared, so not shown as leader')
        pub = c_pub = {c['fid']: c for c in e.snapshot()['commanders']}['F1000002']['pub_id']
        st, html, _ = ts.request(f'/cmdr/{pub}/bravo-two')
        self.assertIn(url, html, 'commander page shows the portrait')
        os.environ['EDCMDR_HOME'] = str(TMP / 'home')

    def test_engine_publishes(self):
        from edcmdr.uplink import Engine
        s = Settings(TMP / 'e2e-settings.json')
        s.data['journal_dirs'] = [str(FIX)]
        p = s.prefs('F1000001')
        p['share'].update(ranks=True, statistics=True, fleet=True, squadron=True, location=True, powerplay=True)
        p['delay']['location'] = 3600
        s.set_prefs('F1000001', p)
        e = Engine(s, Client(self.base))
        e.tick()
        snap = {c['name']: c for c in e.snapshot()['commanders']}
        self.assertEqual(e.status, 'Connected', e.status)
        self.assertTrue(snap['ALPHA ONE']['enrolled'])
        self.assertFalse(snap['BRAVO TWO']['enrolled'], 'nothing shared, nothing sent')
        pub = snap['ALPHA ONE']['pub_id']
        st, html, _ = self.ts.request(f'/cmdr/{pub}/alpha-one?tab=fleet')
        self.assertIn('SPECULATOR', html)
        self.assertNotIn('Test Port', html, 'location is delayed an hour')
        st, html, _ = self.ts.request(f'/cmdr/{pub}/alpha-one?tab=statistics')
        self.assertNotIn('Current wealth', html)
        st, html, _ = self.ts.request('/squadron/42/test-wing')
        self.assertIn('Executive Director', html)
        e.tick()
        self.assertEqual(e.status, 'Connected', 'second pass sends nothing new and stays connected')
        self._verify_takeover()
        self.assertIsNone(e.forget('F1000001'))
        st, html, _ = self.ts.request(f'/cmdr/{pub}/alpha-one')
        self.assertEqual(st, 404)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
