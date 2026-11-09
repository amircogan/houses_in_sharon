"""Real Chromium against local fixtures based on the observed Yad2 DOM."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import search

URL = 'https://www.yad2.co.il/realestate/rent/center-and-sharon'


def card(identity, kind="בית פרטי/ קוטג'", price='₪ 20,000'):
    return f'''<a data-nagish="feed-item-layout-link"
        href="/realestate/item/center-and-sharon/{identity}?opened-from=feed">
        <span data-testid="street-name">רחוב לדוגמה 12</span>
        <span data-testid="price">{price}</span>
        <span data-testid="item-info-line-1st">{kind}, שכונה, רעננה</span>
        <span data-testid="item-info-line-2nd">5.5 חדרים • קומה קרקע • 280 מ״ר</span>
        <img data-nagish="feed-item-image" src="https://img.yad2.co.il/example.jpg">
        </a>'''


def pagination(disabled=False):
    state = 'true' if disabled else 'false'
    return f'''<nav data-nagish="pagination-navbar"><a aria-label="עמוד הבא"
        aria-disabled="{state}" href="{URL}?page=2">הבא</a></nav>'''


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(search, 'ROOT', Path(self.temp.name))
        self.root_patch.start()
        self.page = self.browser.new_page()
        self.pages = {}
        self.page.route('**/*', self.respond)

    def tearDown(self):
        self.page.close()
        self.root_patch.stop()
        self.temp.cleanup()

    def respond(self, route):
        html = self.pages.get(route.request.url)
        if html is None:
            route.abort()
        else:
            content_type = 'text/html; charset=utf-8'
            if route.request.url.endswith('.js'):
                content_type = 'application/javascript; charset=utf-8'
            elif route.request.url.endswith('.css'):
                content_type = 'text/css; charset=utf-8'
            route.fulfill(status=200, content_type=content_type, body=html)

    def collect(self, items, filters=None, max_pages=3):
        config = {'source': 'yad2', 'url': URL, 'max_pages': max_pages, 'delay_seconds': 1, 'filters': filters}
        search.collect(self.page, config, items, prompt=False)

    def test_pagination_deduplication_and_property_types(self):
        self.pages[URL] = card('one') + card('two', 'דירה') + pagination()
        self.pages[URL + '?page=2'] = card('one') + card('three', 'דירת גן') + pagination(True)
        items = {}
        self.collect(items)
        saved = json.loads((Path(self.temp.name) / 'results.json').read_text())
        self.assertEqual(len(saved), 3)
        house = items['yad2:one']
        self.assertEqual(house['source_id'], 'one')
        self.assertEqual(house['url'], 'https://www.yad2.co.il/realestate/item/center-and-sharon/one')
        self.assertEqual((house['city'], house['price'], house['property_type'], house['rooms']),
                         ('רעננה', 20000, 'בית פרטי', 5.5))
        self.assertIsNone(house['built_sqm'])
        self.assertIsNone(house['lot_sqm'])
        self.assertEqual(items['yad2:two']['property_type'], 'דירה')
        self.assertEqual(items['yad2:three']['property_type'], 'דירת גן')

    def test_broken_card_does_not_save_partial_page(self):
        self.pages[URL] = card('one') + pagination()
        self.pages[URL + '?page=2'] = card('two') + card('bad', price='unexpected format') + pagination(True)
        items = {}
        with self.assertRaisesRegex(ValueError, 'Missing numeric price'):
            self.collect(items)
        self.assertEqual(list(items), ['yad2:one'])
        saved = json.loads((Path(self.temp.name) / 'results.json').read_text())
        self.assertEqual([row['source_id'] for row in saved], ['one'])

    def test_reordered_duplicate_page_stops(self):
        self.pages[URL] = card('one') + card('two') + pagination()
        self.pages[URL + '?page=2'] = card('two') + card('one') + pagination(True)
        items = {}
        with self.assertRaisesRegex(RuntimeError, 'Next page did not load'):
            self.collect(items)
        self.assertEqual(len(items), 2)

    def test_missing_pagination_stops_instead_of_claiming_completion(self):
        self.pages[URL] = card('one')
        with self.assertRaisesRegex(RuntimeError, 'next-page control missing'):
            self.collect({})

    def test_unpriced_listing_skipped_and_project_ads_excluded(self):
        project = '<a data-nagish="feed-item-layout-link" href="/yad1/project/123">פרויקט חדש</a>'
        self.pages[URL] = project + card('one') + card('noPrice', price='למידע נוסף') + pagination(True)
        items = {}
        self.collect(items)
        self.assertEqual(list(items), ['yad2:one'])

    def test_madlan_without_verified_selectors_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Verified selector required'):
            search.collect(self.page, {'source': 'madlan', 'url': 'https://www.madlan.co.il/for-rent'}, {}, prompt=False)

    def test_access_challenge_stops_without_saving(self):
        self.pages[URL] = '<title>Just a moment...</title><p>Challenge</p>'
        with self.assertRaisesRegex(RuntimeError, 'access challenge'):
            self.collect({})
        self.assertFalse((Path(self.temp.name) / 'results.json').exists())

    def test_price_and_town_filter_with_rejection_report(self):
        filters = {'min_price': 13000, 'max_price': None,
                   'allowed_cities': ['רעננה'], 'allowed_property_types': ['בית פרטי']}
        outside = card('outside').replace('רעננה', 'תל אביב')
        self.pages[URL] = (card('floor', price='₪ 13,000') + card('high', price='₪ 52,500')
                           + card('low', price='₪ 12,999') + card('apt', 'דירה') + outside + pagination(True))
        items = {}
        self.collect(items, filters)
        self.assertEqual(set(items), {'yad2:floor', 'yad2:high'})
        report = json.loads((Path(self.temp.name) / 'filter_report.json').read_text())
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['pages'][0]['rejected'], 3)
        reasons = {row['source_id']: row['reasons'] for row in report['rejected']}
        self.assertEqual(reasons, {'low': ['price_below_minimum'], 'apt': ['property_type_not_allowed'], 'outside': ['city_not_allowed']})

    def test_page_limit_prevents_upload_of_incomplete_search(self):
        self.pages[URL] = card('one') + pagination()
        items = {}
        with self.assertRaisesRegex(RuntimeError, 'Page limit reached'):
            self.collect(items, max_pages=1)
        self.assertEqual(list(items), ['yad2:one'])
        report = json.loads((Path(self.temp.name) / 'filter_report.json').read_text())
        self.assertEqual(report['status'], 'page_limit')

    def test_location_tabs_work_with_favorites_search_and_mobile_layout(self):
        folder = Path(__file__).resolve().parents[2] / 'sharon-homes'
        settings = json.loads((folder / 'search_filters.json').read_text())
        groups = settings['location_groups']
        samples = [('moshavim', 'בצרה'), ('herzliya', 'הרצליה'), ('hod_hasharon', 'הוד השרון'),
                   ('ramat_hasharon', 'רמת השרון'), ('kfar_saba', 'כפר סבא'),
                   ('rosh_haayin', 'ראש העין'), ('other', 'רעננה')]
        items = []
        for index, (group, city) in enumerate(samples):
            items.append({'id': index + 1, 'source': 'yad2', 'city': city, 'title': city,
                          'price': 20000, 'property_type': 'בית פרטי', 'location_group': group,
                          'images': [], 'favorite': group == 'herzliya', 'hidden': False,
                          'url': 'https://www.yad2.co.il/realestate/item/example',
                          'first_seen': '2026-11-09T12:00:00', 'last_seen': '2026-11-09T12:00:00'})
        origin = 'http://127.0.0.1'
        self.pages[origin + '/'] = (folder / 'static/index.html').read_text()
        self.pages[origin + '/static/app.js'] = (folder / 'static/app.js').read_text()
        self.pages[origin + '/static/styles.css'] = (folder / 'static/styles.css').read_text()
        self.pages[origin + '/api/homes?show=all'] = json.dumps({
            'items': items, 'stats': {'active': 7, 'saved': 1, 'yesterday': 0},
            'location_groups': groups, 'filters': {'min_price': 13000, 'max_price': 26000}})
        self.page.goto(origin)
        self.page.locator('.home').first.wait_for()
        self.assertEqual(self.page.locator('#scan').count(), 0)
        self.assertEqual(self.page.locator('#locationFilters button').count(), 8)
        for group, city in samples:
            self.page.locator('[data-location="' + group + '"]').click()
            self.assertEqual(self.page.locator('.home').count(), 1)
            self.assertEqual(self.page.locator('.city-tag').inner_text(), city)
        self.page.locator('[data-location="herzliya"]').click()
        self.page.locator('[data-mode="favorites"]').click()
        self.assertEqual(self.page.locator('.home').count(), 1)
        self.page.locator('#search').fill('בצרה')
        self.assertEqual(self.page.locator('.home').count(), 0)
        self.page.locator('#search').fill('')
        self.assertEqual(self.page.locator('.home').count(), 1)
        self.assertEqual(self.page.locator('[data-location="herzliya"]').get_attribute('aria-pressed'), 'true')
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
