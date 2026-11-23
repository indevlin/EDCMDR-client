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

    def test_presence_stale(self):
        _, cm = load([FIX])
        c = cm['F1000002']
        self.assertFalse(c.presence()['in_game'], 'old LoadGame without Shutdown is stale')


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
