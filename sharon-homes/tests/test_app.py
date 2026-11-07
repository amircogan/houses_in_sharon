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
