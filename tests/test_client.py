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

from edcmdr_core import keys  # noqa: E402
from edcmdr_core.proto import ProtoError  # noqa: E402

from edcmdr import journal  # noqa: E402
from edcmdr.config import Settings  # noqa: E402
from edcmdr.uplink import make_client as Client  # noqa: E402
from edcmdr_core.state import EVENTS, Commander  # noqa: E402

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
    station: dict = {}
    position: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ('/market', '/shipyard') and self.headers.get('Authorization') != 'Bearer fresh':
            return self._reply(401, b'{}')
        if self.path in ('/market', '/shipyard') and Svc.station:
            return self._reply(200, json.dumps(Svc.station[self.path]).encode())
        if self.path == '/market':
            return self._reply(200, json.dumps({'id': 900, 'name': 'N2Z-N8M', 'orders': {'onfootmicroresources': {
                'sales': {'1': {'name': 'chemicalsample', 'locName': 'Chemical Sample', 'price': 100, 'stock': 3}},
                'purchases': [{'name': 'healthmonitor', 'locName': 'Health Monitor', 'price': 50, 'total': 9}]}}}).encode())
        if self.path.startswith('/position'):
            return self._reply(200, json.dumps(Svc.position).encode())
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
        it.EDSM_POSITION_URL = base + '/position'
        it.EDSM.url, it.EDAstro.url, it.EDDN.url = base + '/edsm', base + '/astro', base + '/eddn'
        it.Inara.url = base + '/inara'
        it.Inara.gap = 0.05
        it.EDSM.gap = it.EDAstro.gap = it.EDDN.gap = 0.05
        it.Sender.retry_wait = 30
        it.CAPI_DELAY = 0.05

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Svc.got, Svc.fail, Svc.station = [], 0, {}
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
        for s in self.it.SENDERS:
            if h.config(self.settings, 'F1', s)['enabled']:
                h.marks.arm(s, 'F1', '')
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
        self.assertEqual(body['header']['commanderFrontierID'], 'F1')
        names = [e['eventName'] for e in body['events']]
        self.assertEqual(names, ['addCommanderTravelFSDJump', 'setCommanderRankPilot'])
        self.assertEqual(body['events'][0]['eventData']['jumpDistance'], 12.35)
        ranks = {r['rankName']: r for r in body['events'][1]['eventData']}
        self.assertEqual((ranks['trade']['rankValue'], ranks['trade']['rankProgress']), (13, 1.0))

    def test_inara_needs_a_key(self):
        from edcmdr import inara
        self.assertEqual(inara.INARA_APP_KEY, '', 'placeholder until Inara issues the application key')
        self.settings.set_integration('F1', 'inara', {'enabled': True, 'api_key': ''})
        h = self.hub()
        h.live('F1', {'event': 'FSDJump', 'StarSystem': 'Ogmar', '_cursor': 'c1'})
        time.sleep(0.3)
        self.assertEqual(Svc.got, [], 'commander data goes only under the player\'s own key')
        self.assertIn('no Inara API key', h.senders['inara'].status)
        self.settings.set_integration('F1', 'inara', {'enabled': True, 'api_key': 'ik'})
        h.wake()
        body = self.wait(1)[0][1]
        self.assertEqual(body['header']['APIkey'], 'ik', 'held events go once the key is set')
        self.assertTrue(inara.throttled(400, 'Too much requests, slow down, cowboy.'))

    def test_inara_documented_events_only(self):
        from edcmdr import inara
        documented = {
            'addCommanderFriend', 'delCommanderFriend', 'addCommanderPermit', 'setCommanderCredits',
            'setCommanderGameStatistics', 'setCommanderRankEngineer', 'setCommanderRankPilot', 'setCommanderRankPower',
            'setCommanderReputationMajorFaction', 'setCommanderReputationMinorFaction', 'resetCommanderInventory',
            'setCommanderInventory', 'setCommanderInventoryCargo', 'setCommanderInventoryMaterials',
            'setCommanderStorageModules', 'addCommanderShip', 'delCommanderShip', 'setCommanderShip',
            'setCommanderShipLoadout', 'setCommanderShipTransfer', 'delCommanderSuitLoadout', 'setCommanderSuitLoadout',
            'updateCommanderSuitLoadout', 'addCommanderTravelCarrierJump', 'addCommanderTravelDock',
            'addCommanderTravelFSDJump', 'addCommanderTravelLand', 'setCommanderTravelLocation', 'addCommanderMission',
            'setCommanderMissionAbandoned', 'setCommanderMissionCompleted', 'setCommanderMissionFailed',
            'addCommanderCombatDeath', 'addCommanderCombatInterdicted', 'addCommanderCombatInterdiction',
            'addCommanderCombatInterdictionEscape', 'addCommanderCombatKill', 'setCommanderCommunityGoalProgress',
            'setCommunityGoal'}
        b = inara.Builder()
        evs = [
            {'event': 'LoadGame', 'Credits': 5, 'Loan': 0, 'Ship': 'cobramkv', 'ShipID': 3},
            {'event': 'Rank', 'Combat': 4}, {'event': 'Promotion', 'Combat': 5},
            {'event': 'Statistics', 'Bank_Account': {'Current_Wealth': 9}, 'Combat': {'Bounties_Claimed': 1}},
            {'event': 'Reputation', 'Empire': 50.0}, {'event': 'EngineerProgress', 'Engineer': 'Felicity Farseer', 'Progress': 'Unlocked', 'Rank': 5},
            {'event': 'PowerplayJoin', 'Power': 'Aisling Duval'}, {'event': 'PowerplayLeave', 'Power': 'Aisling Duval'},
            {'event': 'FSDJump', 'StarSystem': 'A', 'StarPos': [1, 2, 3], 'JumpDist': 5,
             'Factions': [{'Name': 'F', 'MyReputation': 25.0}]},
            {'event': 'CarrierJump', 'StarSystem': 'B', 'Docked': True, 'StationName': 'K', 'MarketID': 1},
            {'event': 'Location', 'StarSystem': 'B'}, {'event': 'Docked', 'StarSystem': 'B', 'StationName': 'S', 'MarketID': 2},
            {'event': 'Touchdown', 'PlayerControlled': True, 'Latitude': 1, 'Longitude': 2, 'Body': 'B 1'},
            {'event': 'DropshipDeploy', 'StarSystem': 'B', 'Body': 'B 1'},
            {'event': 'MissionAccepted', 'MissionID': 7, 'Name': 'M', 'Faction': 'F', 'DestinationSystem': 'C'},
            {'event': 'MissionCompleted', 'MissionID': 7, 'Reward': 10, 'PermitsAwarded': ['Sol']},
            {'event': 'MissionFailed', 'MissionID': 8}, {'event': 'MissionAbandoned', 'MissionID': 9},
            {'event': 'Died', 'KillerName': 'Cmdr X'}, {'event': 'Interdicted', 'Interdictor': 'Y', 'Submitted': True},
            {'event': 'Interdiction', 'Interdicted': 'Z', 'Success': True}, {'event': 'EscapeInterdiction', 'Interdictor': 'Y'},
            {'event': 'PVPKill', 'Victim': 'Cmdr W'}, {'event': 'Friends', 'Name': 'Q', 'Status': 'Added'},
            {'event': 'Friends', 'Name': 'Q', 'Status': 'Lost'},
            {'event': 'Materials', 'Raw': [{'Name': 'iron', 'Count': 3}]},
            {'event': 'Cargo', 'Vessel': 'Ship', 'Inventory': [{'Name': 'gold', 'Count': 2, 'Stolen': 1}]},
            {'event': 'ShipLocker', 'Items': [{'Name': 'x', 'Count': 1}], 'Components': [], 'Data': [], 'Consumables': []},
            {'event': 'StoredModules', 'Items': [{'Name': 'int_x', 'BuyPrice': 5, 'StarSystem': 'B', 'MarketID': 2}]},
            {'event': 'ShipyardNew', 'ShipType': 'mandalay', 'NewShipID': 12},
            {'event': 'ShipyardSwap', 'ShipType': 'mandalay', 'ShipID': 12, 'SellOldShip': 'sidewinder', 'SellShipID': 1},
            {'event': 'ShipyardSell', 'ShipType': 'adder', 'SellShipID': 4},
            {'event': 'SellShipOnRebuy', 'ShipType': 'eagle', 'SellShipId': 5},
            {'event': 'ShipyardTransfer', 'ShipType': 'cobramkv', 'ShipID': 3, 'TransferTime': 60, 'MarketID': 2},
            {'event': 'StoredShips', 'StationName': 'S', 'StarSystem': 'B', 'ShipsHere': [{'ShipType': 'eagle', 'ShipID': 6}]},
            {'event': 'Loadout', 'Ship': 'mandalay', 'ShipID': 12, 'MaxJumpRange': 50.1, 'CargoCapacity': 8,
             'Modules': [{'Slot': 'Armour', 'Item': 'mandalay_armour_grade1'}]},
            {'event': 'SuitLoadout', 'LoadoutID': 1, 'SuitID': 2, 'SuitName': 'tacticalsuit', 'Modules': []},
            {'event': 'RenameSuitLoadout', 'LoadoutID': 1, 'LoadoutName': 'n'}, {'event': 'DeleteSuitLoadout', 'LoadoutID': 1},
            {'event': 'CommunityGoal', 'CurrentGoals': [{'CGID': 3, 'Title': 'T', 'TierReached': 'Tier 2',
                                                          'PlayerContribution': 4, 'PlayerPercentileBand': 50}]}]
        out = []
        for ev in evs:
            out += b.events(ev)
            b.track(ev)
        names = {n for n, _ in out}
        self.assertLessEqual(names, documented, 'only events in the Inara API documentation')
        self.assertEqual(names, documented, 'every documented commander event is produced')
        d = dict(out)
        batch = [{'eventName': n, 'eventData': x} for n, x in out]
        kept = self.it.Inara.coalesce(batch)
        self.assertEqual([e['eventName'] for e in kept].count('setCommanderRankPilot'), 1)
        self.assertEqual([e for e in kept if e['eventName'] == 'setCommanderRankPower'][0]['eventData']['rankValue'], -1,
                         'the last whole-state event is the one kept')
        self.assertLess([e['eventName'] for e in kept].index('resetCommanderInventory'),
                        [e['eventName'] for e in kept].index('setCommanderInventory'))
        self.assertEqual(d['setCommanderShip']['shipMaxJumpRange'], 50.1)
        self.assertEqual(d['setCommanderRankPower'], {'powerName': 'Aisling Duval', 'rankValue': -1})
        self.assertEqual(d['addCommanderCombatDeath'], {'starsystemName': 'B', 'opponentName': 'Cmdr X', 'isPlayer': True})
        self.assertEqual(d['setCommanderInventoryCargo'], [{'itemName': 'gold', 'itemCount': 2, 'isStolen': True}])

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
        if os.name != 'nt':  # Windows has no owner/group/other modes; the file sits in the user's profile
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

    def test_capi_station_to_eddn(self):
        schemas = Path(os.environ.get('EDDN_SCHEMAS', '') or '/nonexistent')
        Svc.station = {
            '/market': {'id': 5, 'name': 'Galileo', 'economies': {'0': {'name': 'Colony', 'proportion': 1.0}},
                        'prohibited': {'1': 'Slaves'}, 'commodities': [
                            {'name': 'Gold', 'categoryname': 'Metals', 'legality': '', 'meanPrice': 1, 'buyPrice': 2,
                             'stock': 3, 'stockBracket': 1, 'sellPrice': 4, 'demand': 0, 'demandBracket': '',
                             'statusFlags': ['rare']},
                            {'name': 'Drones', 'categoryname': 'NonMarketable', 'legality': '', 'meanPrice': 1,
                             'buyPrice': 1, 'stock': 1, 'stockBracket': 1, 'sellPrice': 1, 'demand': 0, 'demandBracket': 0}]},
            '/shipyard': {'id': 5, 'name': 'Galileo',
                          'modules': {'1': {'name': 'Int_Engine_Size2_Class1'}, '2': {'name': 'Int_PlanetApproachSuite'},
                                      '3': {'name': 'Hpt_X', 'sku': 'ELITE_SPECIFIC_V_POWER'}},
                          'ships': {'shipyard_list': {'Adder': {'name': 'Adder'}}, 'unavailable_list': [{'name': 'Eagle'}]}}}
        self.settings.set_integration('F1', 'eddn', {'enabled': True})
        self.settings.set_integration('F1', 'capi', {'enabled': True})
        h, store = self.capi_hub()
        h.live('F1', {'event': 'Docked', 'StationType': 'Coriolis', 'StationName': 'Galileo', 'MarketID': 5,
                      'StarSystem': 'Sol', 'SystemAddress': 10477373803, 'timestamp': 't',
                      'StationServices': ['dock', 'commodities', 'outfitting', 'shipyard']})
        got = {g[1]['$schemaRef'].split('/')[-2]: g[1] for g in self.wait(5) if g[0] == 'eddn'}
        end = time.time() + 3
        while len(got) < 4 and time.time() < end:
            time.sleep(0.05)
            got = {g[1]['$schemaRef'].split('/')[-2]: g[1] for g in Svc.got if g[0] == 'eddn'}
        self.assertEqual(set(got), {'journal', 'commodity', 'outfitting', 'shipyard'})
        self.assertEqual([c['name'] for c in got['commodity']['message']['commodities']], ['gold'])
        self.assertEqual(got['commodity']['header']['gameversion'], 'CAPI-Live-market')
        self.assertEqual(got['shipyard']['header']['gameversion'], 'CAPI-Live-shipyard')
        self.assertEqual(got['outfitting']['message']['modules'], ['Int_engine_size2_class1'])
        self.assertEqual(got['shipyard']['message']['ships'], ['adder', 'eagle'])
        self.assertTrue(got['commodity']['message']['horizons'])
        if schemas.is_dir():
            import jsonschema
            for kind, file in (('commodity', 'commodity-v3.0.json'), ('outfitting', 'outfitting-v2.0.json'),
                               ('shipyard', 'shipyard-v2.0.json')):
                jsonschema.validate(got[kind], json.loads((schemas / file).read_text()))

    def test_capi_refused_refresh_forgets(self):
        from edcmdr import capi
        h, store = self.capi_hub()
        t = store.load()
        t['refresh_token'] = 'bad'
        store.save(t)
        with self.assertRaises(capi.SignInNeeded):
            capi.Client(store, h.capi_base).token()
        left = store.load()
        self.assertNotIn('refresh_token', left, 'a refused sign-in is deleted')
        self.assertNotIn('access_token', left)
        self.assertIn('expired_at', left, 'when it ended is kept, for the reminder')
        self.assertFalse(store.signed_in())

    def test_signin_reminders(self):
        from edcmdr import capi
        from edcmdr.uplink import Engine
        s = Settings(TMP / 'remind.json')
        s.set_integration('F9', 'capi', {'enabled': True})
        e = Engine(s, Client('http://127.0.0.1:9'))
        e._cmdrs['F9'] = Commander('F9')
        e._cmdrs['F9'].name = 'NINE'
        store = capi.Store('F9')
        store.save({'access_token': 'a', 'refresh_token': 'r', 'signed_in': time.time() - 10 * 86400})
        self.assertEqual(e.snapshot()['notices'], [])
        store.save({'access_token': 'a', 'refresh_token': 'r', 'signed_in': time.time() - 27 * 86400})
        self.assertIn('ends in about', e.snapshot()['notices'][0]['text'])
        store.ended()
        self.assertIn('has ended', e.snapshot()['notices'][0]['text'], 'stays until signed in again')
        e.set_signin_reminders(False)
        self.assertEqual(e.snapshot()['notices'], [], 'switched off for good')
        self.assertFalse(Settings(TMP / 'remind.json').data['signin_reminders'])

    def test_retry_keeps_the_mark(self):
        self.settings.set_integration('F1', 'edastro', {'enabled': True})
        Svc.fail = 1
        h = self.hub()
        h.live('F1', {'event': 'Scan', '_cursor': 'c0'})
        time.sleep(0.4)
        s = h.senders['edastro']
        self.assertIn('retry', s.status)
        self.assertEqual(h.marks.get('edastro', 'F1'), '', 'switched on with no history: the mark starts empty')
        h.shutdown()
        h2 = self.it.Hub(self.settings, self.dir / 'q')
        self.addCleanup(h2.shutdown)
        h2.observe('F1', {'event': 'Scan', '_cursor': 'c0'})
        self.assertEqual(self.wait(1)[0][1][0]['event'], 'Scan', 'the next run sends it from the journal')
        time.sleep(0.2)
        self.assertEqual(h2.marks.get('edastro', 'F1'), 'c0')

    def test_marks_fill_gaps(self):
        jd = self.dir / 'jd'
        jd.mkdir()
        log = jd / 'Journal.2026-11-20T100000.01.log'
        line = lambda **e: json.dumps({'timestamp': '2026-11-20T10:00:00Z', **e}) + '\n'
        log.write_text(line(event='Fileheader', part=1, gameversion='4.2.1.0', build='r1') +
                       line(event='Commander', FID='F1', Name='Tester') + line(event='Scan', BodyName='old'))

        def run():
            h = self.it.Hub(self.settings, self.dir / 'q')
            w = journal.Watcher([jd], lambda f, e: h.feed(f, e, jd, live=w.imported))
            w.poll()
            h.imported()
            return h, w

        self.settings.set_integration('F1', 'edastro', {'enabled': True})
        h, w = run()
        time.sleep(0.3)
        self.assertEqual(Svc.got, [], 'switching on starts from now, not from the history')
        mark = h.marks.get('edastro', 'F1')
        self.assertTrue(mark.endswith('|Journal.2026-11-20T100000.01.log|' + f'{log.read_bytes().rindex(b"{"):012d}'))
        with log.open('a') as f:
            f.write(line(event='Scan', BodyName='live'))
        w.poll()
        self.assertEqual(self.wait(1)[0][1][0]['BodyName'], 'live')
        time.sleep(0.2)
        h.shutdown()
        Svc.got = []
        with log.open('a') as f:
            f.write(line(event='Scan', BodyName='while closed') + line(event='Scan', BodyName='also closed'))
        h, w = run()
        self.addCleanup(h.shutdown)
        got = self.wait(1)[0][1]
        self.assertEqual([e['BodyName'] for e in got], ['while closed', 'also closed'], 'only what came after the mark')
        self.assertNotIn('_cursor', got[0])
        self.settings.set_integration('F1', 'edastro', {'enabled': False})
        h.imported()
        self.assertIsNone(h.marks.get('edastro', 'F1'), 'switching off forgets the place')

    def catchup(self, events):
        """A journal of (hours ago, event) lines, imported by a fresh hub; returns the hub."""
        jd = self.dir / 'cj'
        jd.mkdir(exist_ok=True)
        ts = lambda h: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - h * 3600))
        lines = [{'timestamp': ts(80), 'event': 'Fileheader', 'part': 1, 'gameversion': '4.2.1.0', 'build': 'r1'},
                 {'timestamp': ts(80), 'event': 'Commander', 'FID': 'F1', 'Name': 'Tester'}]
        lines += [{'timestamp': ts(h), **e} for h, e in events]
        (jd / 'Journal.2026-11-20T100000.01.log').write_text(''.join(json.dumps(x) + '\n' for x in lines))
        h = self.it.Hub(self.settings, self.dir / 'q', lambda: [jd])
        self.addCleanup(h.shutdown)
        w = journal.Watcher([jd], lambda f, e: h.feed(f, e, jd, live=w.imported))
        w.poll()
        h.imported()
        end = time.time() + 5
        while h.pending and time.time() < end:
            time.sleep(0.02)
        self.assertFalse(h.pending)
        return h

    def test_catchup_from_the_session(self):
        self.settings.set_integration('F1', 'edastro', {'enabled': True})
        self.catchup([(50, {'event': 'LoadGame', 'Commander': 'Tester'}), (49, {'event': 'Scan', 'BodyName': 'old'}),
                      (2, {'event': 'LoadGame', 'Commander': 'Tester'}), (1, {'event': 'Scan', 'BodyName': 'today'})])
        got = self.wait(1)[0][1]
        self.assertEqual([e['BodyName'] for e in got], ['today'], 'the current session, not the old one')

    def test_catchup_edsm_from_its_position(self):
        self.settings.set_integration('F1', 'edsm', {'enabled': True, 'api_key': 'k'})
        when = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - 30 * 3600))
        Svc.position = {'msgnum': 100, 'system': 'B', 'date': when}
        self.catchup([(40, {'event': 'LoadGame', 'Commander': 'Tester'}), (35, {'event': 'FSDJump', 'StarSystem': 'A'}),
                      (34, {'event': 'Scan', 'BodyName': 'a'}), (30, {'event': 'FSDJump', 'StarSystem': 'B'}),
                      (29, {'event': 'Scan', 'BodyName': 'b'}), (3, {'event': 'Scan', 'BodyName': 'b2'})])
        msg = json.loads(self.wait(1)[0][1]['message'])
        self.assertEqual([m.get('BodyName') for m in msg], ['b', 'b2'], 'after the jump EDSM last knows')
        self.assertEqual(msg[0]['_systemName'], 'B')

    def test_catchup_inara_state_only(self):
        self.settings.set_integration('F1', 'inara', {'enabled': True, 'api_key': 'ik'})
        h = self.catchup([
            (900, {'event': 'LoadGame', 'Commander': 'Tester', 'Credits': 1}),
            (40, {'event': 'LoadGame', 'Commander': 'Tester', 'Credits': 5, 'Ship': 'adder', 'ShipID': 2}),
            (39, {'event': 'Loadout', 'Ship': 'adder', 'ShipID': 2, 'Modules': [{'Slot': 'Armour', 'Item': 'a'}]}),
            (38, {'event': 'FSDJump', 'StarSystem': 'A', 'StarPos': [1, 2, 3]}),
            (37, {'event': 'MissionAccepted', 'MissionID': 4, 'Name': 'm'}),
            (36, {'event': 'Loadout', 'Ship': 'eagle', 'ShipID': 3, 'Modules': [{'Slot': 'Armour', 'Item': 'e'}]}),
            (35, {'event': 'ShipyardSell', 'ShipType': 'adder', 'SellShipID': 2}),
            (2, {'event': 'LoadGame', 'Commander': 'Tester', 'Credits': 9}),
            (1, {'event': 'Docked', 'StarSystem': 'A', 'StationName': 'S', 'MarketID': 7})])
        body = self.wait(1)[0][1]
        names = [e['eventName'] for e in body['events']]
        self.assertNotIn('addCommanderMission', names, 'no old log entries until Inara says it ignores repeats')
        self.assertNotIn('addCommanderTravelFSDJump', names)
        self.assertNotIn('addCommanderTravelDock', names)
        credits = [e['eventData'] for e in body['events'] if e['eventName'] == 'setCommanderCredits']
        self.assertEqual([c['commanderCredits'] for c in credits], [9])
        ships = {e['eventData']['shipGameID']: e['eventName'] for e in body['events']
                 if e['eventName'] in ('setCommanderShip', 'delCommanderShip')}
        self.assertEqual(ships, {2: 'delCommanderShip', 3: 'setCommanderShip'})
        self.assertEqual([e['eventData']['shipGameID'] for e in body['events'] if e['eventName'] == 'setCommanderShipLoadout'], [3])
        loc = [e['eventData'] for e in body['events'] if e['eventName'] == 'setCommanderTravelLocation'][-1]
        self.assertEqual((loc['starsystemName'], loc.get('stationName')), ('A', 'S'))
        self.assertEqual(h.marks.get('inara', 'F1'), h.latest['F1'], 'then on from now')

    def test_journal_cursor(self):
        got = []
        w = journal.Watcher([FIX], lambda f, e: got.append(e['_cursor']))
        w.poll()
        self.assertEqual(got, sorted(got), 'cursors sort in journal order')
        name, off = got[1].split('|')[1:]
        raw = (FIX / name).read_bytes()
        self.assertEqual(raw[int(off)], ord('{'))
        self.assertGreater(int(off), 0)


class TestSquadLines(unittest.TestCase):
    def test_lines(self):
        from edcmdr.uplink import squad_lines
        cm = [{'name': 'ONE', 'events_note': 'Sign in to Frontier to count in Squad Goals.',
               'events': {'events': [
                   {'title': 'Rush', 'unit': 't', 'state': 'open', 'standings': [{}, {}],
                    'me': {'rank': 2, 'value': 400, 'credits': 10_000_000}},
                   {'title': 'Old', 'unit': 'cr', 'state': 'closed', 'standings': [{}], 'me': None},
                   {'title': 'Gone', 'unit': 't', 'state': 'cancelled'}]}},
              {'name': 'TWO', 'events': None}]
        self.assertEqual(squad_lines(cm), ['CMDR ONE: Sign in to Frontier to count in Squad Goals.',
                                           'CMDR ONE · Rush: #2 of 2, 400 t → 10.0M cr',
                                           'CMDR ONE · Old (final): nothing counted yet'])


class TestPublisherLock(unittest.TestCase):
    def test_one_publisher(self):
        from edcmdr_core.single import PublisherLock
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
        self.assertEqual(cats['squadron'], {'id': 42, 'name': 'TEST WING', 'rank': 0, 'rank_name': 'Executive Director',
                                            'seen': 1795168807})
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
        self.assertFalse(cm['F1000001'].presence(True)['in_game'], 'Shutdown ends presence')

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

    def test_default_squadron_rank_titles(self):
        c = Commander('F1')
        c.handle({'event': 'SquadronStartup', 'timestamp': '2026-11-20T10:00:00Z', 'SquadronID': 7, 'SquadronName': 'X',
                  'CurrentRank': 0, 'CurrentRankName': '$Squadron_DefaultRankName_Rank0;'})
        self.assertEqual(c.categories()['squadron']['rank_name'], 'Leader')
        c.handle({'event': 'SquadronDemotion', 'SquadronName': 'X', 'OldRank': 0, 'NewRank': 4})
        self.assertEqual(c.categories()['squadron']['rank_name'], 'Rookie', 'no new name: the default for the new rank')
        c.handle({'event': 'SquadronPromotion', 'SquadronName': 'X', 'OldRank': 4, 'NewRank': 1, 'NewRankName': 'Wing Lead'})
        self.assertEqual(c.categories()['squadron']['rank_name'], 'Wing Lead')

    def test_carriers(self):
        """Own fleet carrier and the squadron carrier, from real journal shapes."""
        c = Commander('F1')
        crew = [{'CrewRole': 'Captain', 'Activated': True, 'Enabled': True},
                {'CrewRole': 'Refuel', 'Activated': True, 'Enabled': True},
                {'CrewRole': 'Bartender', 'Activated': True, 'Enabled': False},
                {'CrewRole': 'BlackMarket', 'Activated': False}]
        for ev in (
            {'event': 'SquadronStartup', 'SquadronID': 7, 'SquadronName': 'HOES', 'CurrentRank': 0, 'CurrentRankName': 'Boss'},
            {'timestamp': '2026-11-23T03:37:01Z', 'event': 'CarrierStats', 'CarrierID': 900, 'CarrierType': 'FleetCarrier',
             'Callsign': 'N2Z-N8M', 'Name': 'VECTURA', 'DockingAccess': 'squadron', 'AllowNotorious': True, 'FuelLevel': 687,
             'PendingDecommission': False,
             'SpaceUsage': {'TotalCapacity': 25000, 'Crew': 6020, 'Cargo': 4342, 'FreeSpace': 12638},
             'Finance': {'CarrierBalance': 402756911, 'ReserveBalance': 180290031}, 'Crew': crew,
             'ShipPacks': [], 'ModulePacks': [{'PackTheme': 'x'}]},
            {'timestamp': '2026-11-22T09:49:28Z', 'event': 'CarrierBuy', 'CarrierType': 'SquadronCarrier', 'CarrierID': 901,
             'Location': 'Deriso', 'Price': 25000000000, 'Callsign': 'N2Z-N8M'},
            {'event': 'CarrierBankTransfer', 'CarrierID': 901, 'CarrierType': 'SquadronCarrier', 'Deposit': 2000000000,
             'CarrierBalance': 2000000000},
            {'event': 'CarrierNameChange', 'CarrierID': 901, 'Name': 'TRITIUM', 'Callsign': 'HOES'},
            {'event': 'CarrierCrewServices', 'CarrierID': 901, 'CrewRole': 'Shipyard', 'Operation': 'Activate'},
            {'event': 'CarrierCrewServices', 'CarrierID': 900, 'CrewRole': 'Refuel', 'Operation': 'Pause'},
            {'event': 'CarrierDepositFuel', 'CarrierID': 900, 'Amount': 313, 'Total': 1000},
            {'event': 'CarrierLocation', 'CarrierType': 'SquadronCarrier', 'CarrierID': 901, 'StarSystem': 'Ogmar'},
            {'event': 'CarrierFinance', 'CarrierID': 900, 'CarrierBalance': 500, 'ReserveBalance': 100},
            {'event': 'CarrierStats', 'CarrierID': 5, 'CarrierType': 'Battleship'},
        ):
            c.handle(ev)
        out = c.categories()['carriers']['carriers']
        self.assertEqual([k['id'] for k in out], [900, 901], 'own fleet carrier first; unknown types ignored')
        fc, sc = out
        self.assertEqual((fc['fuel'], fc['balance'], fc['reserve'], fc['services'], fc['module_packs']),
                         (1000, 500, 100, [], 1), 'captain is not a service; paused and inactive roles are off')
        self.assertEqual(fc['space']['free'], 12638)
        self.assertEqual((sc['type'], sc['name'], sc['callsign'], sc['system'], sc['balance'], sc['services']),
                         ('SquadronCarrier', 'TRITIUM', 'HOES', 'Ogmar', 2000000000, ['Shipyard']),
                         "CarrierBuy's callsign is not trusted")
        c.handle({'event': 'CarrierDecommission', 'CarrierID': 900, 'ScrapRefund': 1, 'ScrapTime': int(time.time()) - 5})
        self.assertEqual([k['id'] for k in c.categories()['carriers']['carriers']], [901], 'gone once decommissioned')
        c.handle({'event': 'LeftSquadron', 'SquadronName': 'HOES'})
        self.assertNotIn('carriers', c.categories(), 'leaving the squadron leaves its carrier')

    def test_tails_new_journals(self):
        """Journals that appear while running are picked up, continuation parts included."""
        d = Path(tempfile.mkdtemp(dir=TMP))
        got = []
        w = journal.Watcher([d], lambda f, e: got.append((f, e['event'])), EVENTS)

        def line(ev, extra=''):
            return '{ "timestamp":"2026-11-23T10:00:00Z", "event":"%s"%s }\n' % (ev, extra)
        (d / 'Journal.2026-11-22T100000.01.log').write_text(line('Fileheader', ', "part":1') + line('Commander', ', "FID":"F7", "Name":"A"'))
        w.poll()
        f2 = d / 'Journal.2026-11-23T100000.01.log'
        f2.write_text(line('Fileheader', ', "part":1'))
        w.poll()
        with f2.open('a') as f:
            f.write(line('Commander', ', "FID":"F7", "Name":"A"') + line('LoadGame', ', "FID":"F7", "Commander":"A"'))
        w.poll()
        with f2.open('a') as f:
            f.write(line('Statistics'))
        w.poll()
        (d / 'Journal.2026-11-23T100000.02.log').write_text(line('Fileheader', ', "part":2') + line('Rank'))
        w.poll()
        self.assertEqual([e for _, e in got], ['Commander', 'Commander', 'LoadGame', 'Statistics', 'Rank'])

    def test_presence(self):
        _, cm = load([FIX])
        c = cm['F1000002']
        self.assertFalse(c.presence(False)['in_game'], 'a session with the game not running is not in game')
        self.assertTrue(c.presence(True)['in_game'], 'loaded, and the game running: in game, however quiet')
        c.handle({'event': 'Music', 'MusicTrack': 'MainMenu'})
        self.assertFalse(c.presence(True)['in_game'], 'back at the main menu')
        c.handle({'event': 'LoadGame', 'Commander': 'BRAVO TWO', 'GameMode': 'Open'})
        self.assertEqual(c.presence(True)['mode'], 'Open')

    def test_game_running(self):
        from edcmdr import game
        d = Path(tempfile.mkdtemp(dir=TMP))
        (d / 'Status.json').write_text('{}')
        self.assertTrue(game.running(d), 'a fresh Status.json means the game is running')
        self.assertTrue(game._is_game(['/home/x/.steam/steamapps/common/Elite Dangerous/Products/elite-dangerous-odyssey-64/EliteDangerous64.exe']))
        self.assertTrue(game._is_game(['Z:\\Games\\ED\\EliteDangerous64.exe', '/autorun']))
        self.assertFalse(game._is_game(['/usr/bin/python3', 'edld.py']))
        w, _ = load([FIX])
        self.assertEqual(set(w.where.values()), {FIX}, 'each commander is placed in its journal folder')


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


class TestSquadronRefresh(unittest.TestCase):
    """A renamed squadron is confirmed again from Frontier without the player."""

    class Pushes:
        def __init__(self, reply):
            self.calls, self.reply = [], reply

        def push(self, key, fid, kind, body):
            self.calls.append((kind, body))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    def engine(self, reply, token='tok'):
        from edcmdr.uplink import Engine, Runtime
        e = Engine(Settings(TMP / 'sqr.json'), Client('http://127.0.0.1:9'))
        e.client = self.Pushes(reply)
        e.frontier_token = lambda fid: token
        r = Runtime()
        r.verified = True
        r.note({'squadron_refresh': True})
        return e, r

    def test_refreshes_when_asked(self):
        e, r = self.engine({'refreshed': True, 'leader_of': 7, 'leader_squadron': {'id': 7, 'name': 'NEW', 'tag': 'NEWT'}})
        e._squadron_refresh('F1', r, None, time.time())
        self.assertEqual(e.client.calls, [('verify-finish', {'access_token': 'tok', 'refresh': True})])
        self.assertFalse(r.sq_refresh)
        self.assertEqual(r.leader_squadron['tag'], 'NEWT', 'the Squadron tab names the new tag at once')
        e._squadron_refresh('F1', r, None, time.time())
        self.assertEqual(len(e.client.calls), 1)

    def test_at_most_every_few_hours(self):
        e, r = self.engine(ProtoError('rate', 'Refreshed recently', 429, {}))
        now = time.time()
        e._squadron_refresh('F1', r, None, now)
        e._squadron_refresh('F1', r, None, now + 60)
        self.assertEqual(len(e.client.calls), 1)
        e._squadron_refresh('F1', r, None, now + 7 * 3600)
        self.assertEqual(len(e.client.calls), 2)

    def test_without_a_sign_in_the_leader_is_told(self):
        e, r = self.engine({}, token=None)
        e._squadron_refresh('F1', r, None, time.time())
        self.assertEqual(e.client.calls, [])
        self.assertTrue(r.sq_stale)
        r.note({'squadron_refresh': False})
        self.assertFalse(r.sq_stale, 'another member refreshed it')

    def test_journal_time_goes_with_the_name(self):
        c = Commander('F1')
        c.handle({'timestamp': '2026-11-25T08:00:00Z', 'event': 'SquadronStartup', 'SquadronID': 7,
                  'SquadronName': 'NEW NAME', 'CurrentRank': 0, 'CurrentRankName': 'Leader'})
        self.assertEqual(c.squadron['seen'], 1795593600)
        c.handle({'timestamp': '2026-11-25T09:00:00Z', 'event': 'SquadronPromotion', 'NewRank': 1})
        self.assertEqual(c.squadron['seen'], 1795593600, 'a promotion does not rename')


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
        self.assertEqual(c['leader_of'], 4343, 'the Squadron tab shows straight after verifying')
        with self.assertRaises(ProtoError) as err:
            Client(self.base).push(holder.key, 'F1000002', 'prefs', {})
        self.assertEqual(err.exception.code, 'not-enrolled', 'the earlier installation is revoked')

        # leader: Squadron tab data, then a portrait
        e.set_prefs('F1000002', dict(s.prefs('F1000002'), share=dict(s.prefs('F1000002')['share'], portrait=True)))
        e.tick()
        c = {c['fid']: c for c in e.snapshot()['commanders']}['F1000002']
        self.assertEqual(c['leader_of'], 4343)
        self.assertEqual(c['leader_squadron'], {'id': 4343, 'name': 'BRAVO WING', 'tag': 'BRV'}, 'the tab names the squadron')
        with self.assertRaises(ProtoError):
            e.save_squadron('F1000002', {'tagline': 'not ours'}, 4344)
        self.assertIsNone(e.rt['F1000002'].leader_of, 'a refusal hides the tab until the server says otherwise')
        e.rt['F1000002'].leader_of = 4343
        j = e.save_squadron('F1000002', {'home_system': 'Deriso', 'power': 'Nakato Kaine', 'factions': ['Test Co'],
                                         'recruiting': 'open', 'tagline': 'Bravo'}, 4343)
        self.assertEqual(j['squadron'], 4343)
        self.assertEqual(s.commander('F1000002')['squadron_details']['squadron_id'], 4343)
        st, html, _ = ts.request('/squad/brv')
        self.assertIn('Deriso', html)
        self.assertIn('Nakato Kaine', html)
        jpg = TMP / 'codex.jpg'
        import subprocess
        subprocess.run(['php', str(Path(os.environ['EDCMDR_SERVER_REPO']).expanduser() / 'tests/make_codex.php'), str(jpg)], check=True)
        url = e.push_portrait('F1000002', jpg.read_bytes())
        self.assertRegex(url, r'^/p/[a-z2-7]{10,12}-1\.webp$')
        st, html, _ = ts.request('/squad/brv')
        self.assertNotIn('BRAVO TWO', html, 'membership not shared, so not shown as leader')
        st, html, _ = ts.request('/cmdr/bravo-two')
        self.assertIn(url, html, 'commander page shows the portrait')
        self._squad_goals(e, s)
        os.environ['EDCMDR_HOME'] = str(TMP / 'home')

    def _squad_goals(self, e, s):
        """The leader saves an event; the engine has the server read Frontier's journal with the kept sign-in."""
        import datetime as dt
        ts = self.ts
        now = int(time.time())
        d0 = now - now % 86400
        j = e.event_admin('F1000002', 'save', title='Bounty Week', metric='bounty_value', starts=d0 - 2 * 86400,
                          ends=d0 + 2 * 86400 - 1, payout={'pool': 5_000_000, 'places': [1]})
        self.assertEqual(j['event']['title'], 'Bounty Week')
        y = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime('%Y-%m-%dT12:00:00Z')
        ts.Frontier.days = {1: json.dumps({'timestamp': y, 'event': 'Bounty', 'TotalReward': 125000}) + '\n'}
        try:
            for _ in range(3):
                e.tick()
                ev = (e.events_of('F1000002') or {}).get('events') or []
                if ev and ev[0].get('me'):
                    break
                e.rt['F1000002'].sync_next = 0
        finally:
            ts.Frontier.days = {}
        self.assertEqual(ev[0]['me']['value'], 125000, 'scored from the journal Frontier returned')
        lines = e.snapshot()['squad']
        self.assertIn('CMDR BRAVO TWO · Bounty Week: #1 of 1, 125.0K cr → 5.0M cr', lines)
        p = s.prefs('F1000002')
        p['events'] = False
        e.set_prefs('F1000002', p)
        e.tick()
        self.assertEqual(e.snapshot()['squad'], [], 'switched off: nothing shown or read')

    def test_engine_publishes(self):
        from edcmdr.uplink import Engine
        s = Settings(TMP / 'e2e-settings.json')
        s.data['journal_dirs'] = [str(FIX)]
        p = s.prefs('F1000001')
        p['share'].update(ranks=True, statistics=True, fleet=True, squadron=True, location=True, powerplay=True,
                          carriers=True)
        p['delay']['location'] = 3600
        s.set_prefs('F1000001', p)
        e = Engine(s, Client(self.base))
        e.tick()
        snap = {c['name']: c for c in e.snapshot()['commanders']}
        self.assertEqual(e.status, 'Connected', e.status)
        self.assertTrue(snap['ALPHA ONE']['enrolled'])
        self.assertFalse(snap['BRAVO TWO']['enrolled'], 'nothing shared, nothing sent')
        self.assertTrue(snap['ALPHA ONE']['pub_id'])
        st, html, _ = self.ts.request('/cmdr/alpha-one?tab=fleet')
        self.assertIn('SPECULATOR', html)
        self.assertNotIn('Test Port', html, 'location is delayed an hour')
        st, html, _ = self.ts.request('/cmdr/alpha-one?tab=statistics')
        self.assertNotIn('Current wealth', html)
        st, html, _ = self.ts.request('/squadron/42/test-wing')
        self.assertIn('Executive Director', html)
        st, html, _ = self.ts.request('/cmdr/alpha-one?tab=carriers')
        self.assertIn('TEST DECK', html)
        self.assertIn('Fleet carrier · Q2X-T7K', html)
        self.assertNotIn('987.7 m Cr', html, 'carrier banks only with net worth')
        e.tick()
        self.assertEqual(e.status, 'Connected', 'second pass sends nothing new and stays connected')
        self._verify_takeover()
        self.assertIsNone(e.forget('F1000001'))
        st, html, _ = self.ts.request('/cmdr/alpha-one')
        self.assertEqual(st, 404)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
