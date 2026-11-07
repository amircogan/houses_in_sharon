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
    def test_bad_url(self):
        result=self.app.upsert_many([self.example()|{'url':'javascript:alert(1)'}])
        self.assertEqual(result['added'],0)

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
