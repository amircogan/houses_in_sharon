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
        from playwright.sync_api import sync_playwright, expect
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
                extra = [records[0] | {'source_id': 'keep-favorite', 'url': 'https://www.yad2.co.il/realestate/item/keepfavorite'},
                         records[0] | {'source_id': 'hide-yad2', 'title': 'בית לבדיקה בתצוגה', 'url': 'https://www.yad2.co.il/realestate/item/hideyad2'},
                         records[0] | {'source': 'komo', 'source_id': '123', 'url': 'https://www.komo.co.il/code/nadlan/details/?modaaNum=123', 'city': 'הרצליה'}]
                self.assertEqual(search.upload(extra, origin)['added'], 3)
                with app.db() as conn:
                    conn.execute("UPDATE homes SET favorite=1,note='keep favorite' WHERE source_id='keep-favorite'")
                    conn.execute("UPDATE homes SET note='keep hidden note' WHERE source_id='hide-yad2'")
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page(viewport={'width': 375, 'height': 812})
                    page.goto(origin)
                    expect(page.locator('.home')).to_have_count(3)
                    page.select_option('#sourceFilter', 'komo')
                    expect(page.locator('.home')).to_have_count(1)
                    self.assertEqual(page.locator('.source-tag').inner_text(), 'קומו')
                    page.locator('[data-location="herzliya"]').click()
                    page.locator('#hideNonFavorites').click()
                    expect(page.locator('#message')).to_contain_text('הוסתרו 1')
                    expect(page.locator('#hideNonFavorites')).to_be_enabled()
                    page.select_option('#sourceFilter', 'all')
                    page.locator('[data-location="all"]').click()
                    expect(page.locator('.home')).to_have_count(2)
                    expect(page.locator('.heart.saved')).to_have_count(1)
                    page.select_option('#sourceFilter', 'yad2')
                    page.locator('[data-location="other"]').click()
                    page.locator('#search').fill('בית לבדיקה בתצוגה')
                    expect(page.locator('.home')).to_have_count(1)
                    page.locator('#hideNonFavorites').click()
                    expect(page.locator('#message')).to_contain_text('הוסתרו 1')
                    expect(page.locator('.home')).to_have_count(0)
                    page.locator('#search').fill('')
                    expect(page.locator('.home')).to_have_count(1)
                    expect(page.locator('.heart')).to_have_class('heart saved')
                    page.reload()
                    expect(page.locator('.home')).to_have_count(1)
                    self.assertEqual(page.locator('#activeCount').inner_text(), '1')
                    self.assertEqual(page.locator('#favoritesCount').inner_text(), '1')
                    page.locator('[data-mode="hidden"]').click()
                    page.select_option('#sourceFilter', 'komo')
                    expect(page.locator('.home')).to_have_count(1)
                    self.assertEqual(page.locator('.source-tag').inner_text(), 'קומו')
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                    browser.close()
                with app.db() as conn:
                    self.assertEqual(conn.execute("SELECT note FROM homes WHERE source_id='hide-yad2'").fetchone()[0], 'keep hidden note')
                    self.assertEqual(conn.execute('SELECT count(*) FROM homes').fetchone()[0], 4)
                smtp.assert_not_called()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
            self.assertFalse(thread.is_alive())

    def homes(self, origin):
        with urlopen(origin + '/api/homes?show=all') as response:
            return json.load(response)['items']
