import os
import tempfile
import unittest
from pathlib import Path

class HomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ['DB_PATH'] = str(Path(self.temp.name)/'test.sqlite')
        os.environ['APP_PASSWORD'] = 'a-password'
        import app
        app.DB_PATH = Path(os.environ['DB_PATH'])
        self.app = app
        app.init_db()
    def tearDown(self):
        self.temp.cleanup()
    def example(self):
        return {'source':'test','source_id':'1','url':'https://example.org/a','title':'בית בבצרה','city':'בצרה','price':20000,'property_type':'בית פרטי','images':[]}
    def test_hidden_and_note_persist_after_reimport(self):
        self.assertEqual(self.app.upsert_many([self.example()])['added'],1)
        with self.app.db() as c:
            c.execute('UPDATE homes SET hidden=1,note=? WHERE source_id=?',('לבקר בהמשך','1'))
        changed=self.example()|{'price':21000}
        self.assertEqual(self.app.upsert_many([changed])['updated'],1)
        with self.app.db() as c:
            r=c.execute('SELECT * FROM homes').fetchone()
            self.assertEqual((r['hidden'],r['note'],r['price']),(1,'לבקר בהמשך',21000))
    def test_filter(self):
        examples=[self.example()|{'source_id':'2','price':12000},self.example()|{'source_id':'3','city':'חיפה'},self.example()]
        result=self.app.upsert_many(examples)
        self.assertEqual(result['added'],1)
        self.assertEqual(result['skipped'],2)

    def test_price_bounds_include_13000_and_26000(self):
        examples = [self.example() | {'source_id': 'floor', 'url': 'https://example.org/floor', 'price': 13000},
                    self.example() | {'source_id': 'ceiling', 'url': 'https://example.org/ceiling', 'price': 26000},
                    self.example() | {'source_id': 'high', 'url': 'https://example.org/high', 'price': 26001},
                    self.example() | {'source_id': 'low', 'url': 'https://example.org/low', 'price': 12999}]
        result = self.app.upsert_many(examples)
        self.assertEqual((result['added'], result['skipped']), (2, 2))
        self.assertEqual(self.app.MAX_PRICE, 26000)

    def test_existing_over_budget_record_is_kept_but_not_displayed(self):
        from fastapi.testclient import TestClient
        self.app.upsert_many([self.example()])
        with self.app.db() as conn:
            conn.execute("UPDATE homes SET price=27000, favorite=1, note='keep'")
        with TestClient(self.app.app) as client:
            data = client.get('/api/homes?show=all', auth=('homes', 'a-password')).json()
        self.assertEqual(data['items'], [])
        self.assertEqual((data['stats']['total'], data['stats']['saved']), (0, 0))
        with self.app.db() as conn:
            row = conn.execute('SELECT * FROM homes').fetchone()
            self.assertEqual((row['price'], row['favorite'], row['note']), (27000, 1, 'keep'))

    def test_location_groups_and_rosh_haayin_import(self):
        expected = {'בצרה': 'moshavim', 'הרצליה': 'herzliya', 'הוד השרון': 'hod_hasharon',
                    'רמת השרון': 'ramat_hasharon', 'כפר סבא': 'kfar_saba',
                    'ראש העין': 'rosh_haayin', 'רעננה': 'other', 'שפיים': 'other'}
        for city, group in expected.items():
            self.assertEqual(self.app.location_group(city), group)
        self.assertEqual(self.app.upsert_many([self.example() | {'city': 'ראש העין'}])['added'], 1)
    def test_bad_url(self):
        result=self.app.upsert_many([self.example()|{'url':'javascript:alert(1)'}])
        self.assertEqual(result['added'],0)

    def test_http_import_preview_and_preserved_state(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        with patch.dict(os.environ, {'ENABLE_EMAIL_DELIVERY': '0', 'ENABLE_SCHEDULER': '0'}), patch('app.smtplib.SMTP_SSL') as smtp:
            with TestClient(self.app.app) as client:
                auth = ('homes', 'a-password')
                records = [self.example(), self.example() | {'source_id': 'apt', 'property_type': 'דירה'}]
                preview = client.post('/api/import?dry_run=true', json=records, auth=auth)
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.json()['added'], 1)
                self.assertEqual(preview.json()['skipped'], 1)
                with self.app.db() as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM homes').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
                result = client.post('/api/import', json=records[:1], auth=auth)
                self.assertEqual(result.json()['added'], 1)
                home = client.get('/api/homes', auth=auth).json()['items'][0]
                client.patch('/api/homes/' + str(home['id']), json={'favorite': True, 'hidden': True, 'note': 'keep'}, auth=auth)
                updated = client.post('/api/import', json=[self.example() | {'price': 22000}], auth=auth)
                self.assertEqual(updated.json()['updated'], 1)
                rows = client.get('/api/homes?show=all', auth=auth).json()['items']
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]['favorite'], rows[0]['hidden'], rows[0]['note'], rows[0]['price']), (1, 1, 'keep', 22000))
                self.assertIsNotNone(client.get('/api/homes', auth=auth).json()['last_scan'])
                smtp.assert_not_called()

    def test_identity_conflict_does_not_create_duplicate(self):
        self.app.upsert_many([self.example()])
        changed = self.example() | {'source_id': 'different', 'url': 'https://example.org/a/?tracking=1'}
        for dry_run in (True, False):
            result = self.app.upsert_many([changed], dry_run=dry_run)
            self.assertEqual(result['added'], 0)
            self.assertEqual(result['identity_conflicts'][0]['existing_source_id'], '1')
        with self.app.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM homes').fetchone()[0], 1)

    def test_http_import_requires_property_type(self):
        from fastapi.testclient import TestClient
        record = self.example()
        del record['property_type']
        with TestClient(self.app.app) as client:
            result = client.post('/api/import', json=[record], auth=('homes', 'a-password'))
            self.assertEqual(result.status_code, 422)

    def test_identity_conflict_with_legacy_yad2_url(self):
        original = self.example() | {'source': 'yad2', 'source_id': 'old-id', 'url': 'https://www.yad2.co.il/realestate/item/token123'}
        self.app.upsert_many([original])
        changed = original | {'source_id': 'token123', 'url': 'https://www.yad2.co.il/realestate/item/center-and-sharon/token123?tracking=1'}
        result = self.app.upsert_many([changed], dry_run=True)
        self.assertEqual(result['identity_conflicts'][0]['existing_source_id'], 'old-id')

    def test_komo_query_ids_are_distinct_and_preserve_user_state(self):
        first = self.example() | {'source': 'komo', 'source_id': '100', 'url': 'https://www.komo.co.il/code/nadlan/details/?modaaNum=100'}
        second = first | {'source_id': '101', 'url': 'https://www.komo.co.il/code/nadlan/details/?modaaNum=101'}
        result = self.app.upsert_many([first, second])
        self.assertEqual((result['added'], result['skipped']), (2, 0))
        with self.app.db() as conn:
            conn.execute("UPDATE homes SET favorite=1,hidden=1,note='keep' WHERE source_id='100'")
        updated = first | {'price': 21000, 'url': first['url'] + '&tracking=ignored'}
        self.assertEqual(self.app.upsert_many([updated])['updated'], 1)
        with self.app.db() as conn:
            row = conn.execute("SELECT * FROM homes WHERE source_id='100'").fetchone()
            self.assertEqual((row['favorite'], row['hidden'], row['note'], row['price']), (1, 1, 'keep', 21000))
            self.assertEqual(conn.execute('SELECT count(*) FROM homes').fetchone()[0], 2)

    def test_collect_remote_failure_isolated_and_state_preserved(self):
        from unittest.mock import patch
        self.app.upsert_many([self.example()])
        with self.app.db() as c:
            c.execute("UPDATE homes SET favorite=1,hidden=1,note='keep'")
        with patch('app.configured_sources', return_value=[
            ('broken', lambda: (_ for _ in ()).throw(ValueError('secret-token'))),
            ('good', lambda: [self.example() | {'price': 22000}])]):
            result = self.app.collect()
        self.assertEqual(result['updated'], 1)
        self.assertEqual(len(result['errors']), 1)
        with self.app.db() as c:
            row = c.execute('SELECT * FROM homes').fetchone()
            self.assertEqual((row['favorite'], row['hidden'], row['note'], row['price']), (1, 1, 'keep', 22000))
            self.assertNotIn('secret-token', str([tuple(r) for r in c.execute('SELECT * FROM runs')]))
        self.assertEqual(self.app.last_scan()['status'], 'partial')

    def test_email_disabled_even_with_smtp_settings(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'ENABLE_EMAIL_DELIVERY': '0', 'SMTP_HOST': 'example.org'}), patch('app.smtplib.SMTP_SSL') as smtp:
            self.assertEqual(self.app.send_digest(), {'status': 'disabled'})
            smtp.assert_not_called()

    def test_concurrent_scan_rejected(self):
        from fastapi import HTTPException
        self.app.SCAN_LOCK.acquire()
        try:
            with self.assertRaises(HTTPException) as error:
                self.app.collect()
            self.assertEqual(error.exception.status_code, 409)
        finally:
            self.app.SCAN_LOCK.release()

    def test_apify_optional_and_mapping(self):
        import sources
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sources.configured_sources(), [])
        config = {'APIFY_TOKEN': 'test-key', 'APIFY_YAD2_ACTOR': 'owner/actor',
                  'APIFY_YAD2_INPUT': '{"rent":true}',
                  'APIFY_YAD2_FIELD_MAP': '{"source_id":"id","city":"location.city"}'}
        with patch.dict(os.environ, config, clear=True), patch('sources.request_json', return_value=[{'id': 7, 'location': {'city': 'בצרה'}}]) as request:
            configured = sources.configured_sources()
            self.assertEqual(len(configured), 1)
            self.assertEqual(configured[0][1]()[0]['source_id'], '7')
            self.assertEqual(request.call_args.args[1], {'rent': True})
            self.assertEqual(request.call_args.args[2], 'test-key')
            self.assertIn('owner~actor/run-sync-get-dataset-items', request.call_args.args[0])

    def test_json_https_and_response_contract(self):
        import sources
        from unittest.mock import MagicMock, patch
        with self.assertRaises(ValueError):
            sources.request_json('http://example.org/feed')
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"listings":[]}'
        with patch('sources.build_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(sources.request_json('https://example.org/feed'), [])
        with patch.dict(os.environ, {'AUTHORIZED_JSON_URLS':'https://example.org/a\nhttps://example.org/b'}, clear=True):
            self.assertEqual(len(sources.configured_sources()), 2)
