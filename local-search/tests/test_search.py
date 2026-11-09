import unittest
import search


class SearchTests(unittest.TestCase):
    def test_allowed_hosts(self):
        search.check_url('https://www.yad2.co.il/realestate/rent', 'yad2')
        search.check_url('https://www.madlan.co.il/for-rent', 'madlan')
        for url in ('http://www.yad2.co.il', 'https://evil.example', 'https://user:password@www.yad2.co.il'):
            with self.assertRaises(ValueError):
                search.check_url(url, 'yad2')

    def test_price(self):
        self.assertEqual(search.read_price('20,000 ₪'), 20000)
        for value in ('ללא מחיר', '0'):
            with self.assertRaises(ValueError):
                search.read_price(value)

    def test_upload_requires_https(self):
        with self.assertRaises(ValueError):
            search.upload([], 'http://example.org', 'homes', 'test')
