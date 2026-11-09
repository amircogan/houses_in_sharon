"""Exercise the real HTTP uploader and app with a temporary SQLite database."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import uvicorn
import search

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'sharon-homes'))
import app


class ImportFlowTests(unittest.TestCase):
    def test_extract_upload_reimport_and_conflict_over_http(self):
        from playwright.sync_api import sync_playwright
        from test_browser import card
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'DB_PATH', Path(folder) / 'homes.sqlite'), patch.dict(os.environ, {
            'APP_PASSWORD': '', 'ALLOW_LOCAL_NO_PASSWORD': '1',
            'ENABLE_SCHEDULER': '0', 'ENABLE_EMAIL_DELIVERY': '0',
        }), patch('app.smtplib.SMTP_SSL') as smtp:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.set_content(card('house') + card('apartment', 'דירה'))
                records = []
                for element in page.locator(search.yad2.CARDS).all():
                    records.append(search.read_card(element, None, 'yad2', 'https://www.yad2.co.il'))
                browser.close()
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            origin = 'http://127.0.0.1:' + str(sock.getsockname()[1])
            server = uvicorn.Server(uvicorn.Config(app.app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                for attempt in range(100):
                    if server.started:
                        break
                    time.sleep(0.05)
                self.assertTrue(server.started)
                preview = search.upload(records, origin, dry_run=True)
                self.assertEqual((preview['added'], preview['skipped']), (1, 1))
                self.assertEqual(preview['rejected'][0]['reason'], 'property_type_not_allowed')
                self.assertEqual(self.homes(origin), [])
                imported = search.upload(records, origin)
                self.assertEqual((imported['added'], imported['skipped']), (1, 1))
                house = self.homes(origin)[0]
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    page.goto(origin)
                    page.locator('.home').wait_for()
                    self.assertEqual(page.locator('.home').count(), 1)
                    self.assertIn('13,000', page.locator('#searchCriteria').inner_text())
                    self.assertIn('26,000', page.locator('#searchCriteria').inner_text())
                    self.assertNotIn('25,000', page.locator('#searchCriteria').inner_text())
                    self.assertEqual(page.locator('#scan').count(), 0)
                    page.locator('[data-location="other"]').click()
                    self.assertEqual(page.locator('.home').count(), 1)
                    page.locator('[data-location="moshavim"]').click()
                    self.assertEqual(page.locator('.home').count(), 0)
                    page.locator('[data-location="all"]').click()
                    self.assertEqual(page.locator('.home').count(), 1)
                    browser.close()
                request = Request(origin + '/api/homes/' + str(house['id']),
                                  data=json.dumps({'note': 'keep', 'favorite': True, 'hidden': True}).encode(),
                                  headers={'Content-Type': 'application/json'}, method='PATCH')
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                records[0]['price'] = 21000
                self.assertEqual(search.upload(records[:1], origin)['updated'], 1)
                rows = self.homes(origin)
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]['price'], rows[0]['note'], rows[0]['favorite'], rows[0]['hidden']),
                                 (21000, 'keep', 1, 1))
                records[0]['source_id'] = 'wrong-id'
                conflict = search.upload(records[:1], origin, dry_run=True)
                self.assertEqual(conflict['identity_conflicts'][0]['existing_source_id'], 'house')
                smtp.assert_not_called()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
            self.assertFalse(thread.is_alive())

    def homes(self, origin):
        with urlopen(origin + '/api/homes?show=all') as response:
            return json.load(response)['items']
