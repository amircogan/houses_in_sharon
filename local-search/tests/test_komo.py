import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import search
import komo

URL = 'https://www.komo.co.il/code/nadlan/apartments-for-rent.asp?nehes=5&fromPrice=13000&toPrice=26000'
PAGE2 = URL + '&currPage=2'


def card(number, kind='וילות/ בתים', price='20,000 ₪', city='רעננה'):
    return f'''<div id="modaaRowDv{number}" class="modaaRowAd modaa__box">
      <div class="image__wrapper"><img src="/api/modaot/tmunot/showPic/list/?picNum={number}&amp;luachNum=2&amp;picSize=1"></div>
      <div class="contant"><a href="/code/nadlan/details/?modaaNum={number}"><h2 class="title">{city}, מרכז, רחוב לדוגמה 12</h2></a>
      <div class="price">{price}</div><div class="description">{kind} 5.5 חדרים (300 מ"ר)<br>קומה: קרקע מתוך 1</div></div></div>'''


def pager(next_page=False, total=21):
    link = '<a class="paging" href="' + PAGE2.replace('&', '&amp;') + '"> »</a>' if next_page else ''
    return '<div id="showKamut">נמצאו ' + str(total) + ' מודעות</div><span id="paging">' + link + '</span>'


class KomoTests(unittest.TestCase):
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
        self.redirect_to_login = False
        self.page.route('**/*', self.respond)

    def tearDown(self):
        self.page.close()
        self.root_patch.stop()
        self.temp.cleanup()

    def respond(self, route):
        if self.redirect_to_login and route.request.url == PAGE2:
            route.fulfill(status=302, headers={'Location': 'https://account.komo.co.il/'})
            return
        html = self.pages.get(route.request.url)
        if html is None:
            route.abort()
        else:
            route.fulfill(content_type='text/html; charset=utf-8', body=html)

    def collect(self, items):
        search.collect(self.page, {'source': 'komo', 'url': URL, 'max_pages': 5, 'delay_seconds': 1}, items, prompt=False)

    def test_extraction_query_ids_pagination_and_duplicates(self):
        self.pages[URL] = ''.join(card(number) for number in range(100, 120)) + pager(True)
        self.pages[PAGE2] = card(119) + card(120) + pager()
        items = {}
        self.collect(items)
        self.assertEqual(len(items), 21)
        item = items['komo:100']
        self.assertEqual(item['url'], 'https://www.komo.co.il/code/nadlan/details/?modaaNum=100')
        self.assertEqual((item['city'], item['property_type'], item['price'], item['rooms']), ('רעננה', 'בית פרטי', 20000, 5.5))
        self.assertIsNone(item['built_sqm'])
        self.assertIsNone(item['lot_sqm'])
        self.assertTrue(item['images'][0].startswith('https://www.komo.co.il/api/'))
        report = json.loads((Path(self.temp.name) / 'filter_report.json').read_text())
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(report['pages']), 2)

    def test_apartment_is_not_labeled_as_house(self):
        self.page.set_content(card(100, kind='דירה'))
        item = search.read_card(self.page.locator(komo.CARDS), None, 'komo', URL)
        self.assertEqual(item['property_type'], 'דירה')

    def test_missing_price_skipped_but_unexpected_price_stops(self):
        self.pages[URL] = card(100) + card(101, price='לא צוין מחיר') + pager(total=2)
        items = {}
        self.collect(items)
        self.assertEqual(list(items), ['komo:100'])
        self.pages[URL] = card(102, price='unexpected price') + pager(total=1)
        with self.assertRaisesRegex(ValueError, 'Missing numeric price'):
            self.collect({})

    def test_login_redirect_stops_without_losing_saved_page(self):
        self.pages[URL] = card(100) + pager(True)
        self.pages['https://account.komo.co.il/'] = '<title>Komo login</title>'
        self.redirect_to_login = True
        items = {}
        with self.assertRaisesRegex(ValueError, 'requires manual sign-in'):
            self.collect(items)
        saved = json.loads((Path(self.temp.name) / 'results.json').read_text())
        self.assertEqual([item['source_id'] for item in saved], ['100'])

    def test_missing_next_control_does_not_claim_completion(self):
        self.pages[URL] = card(100) + pager(total=21)
        with self.assertRaisesRegex(RuntimeError, 'more results'):
            self.collect({})

    def test_listing_url_requires_one_numeric_query_id(self):
        for href in ('/code/nadlan/details/', '/code/nadlan/details/?modaaNum=bad', '/code/nadlan/details/?modaaNum=100&modaaNum=101'):
            with self.assertRaises(ValueError):
                search.listing_url(href, 'komo', URL)
        url, identity = search.listing_url('/code/nadlan/details/?modaaNum=123&tracking=ignored', 'komo', URL)
        self.assertEqual((url, identity), ('https://www.komo.co.il/code/nadlan/details/?modaaNum=123', '123'))
