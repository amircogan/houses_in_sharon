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
        self.assertEqual(search.read_price('₪ 23,000'), 23000)
        for value in ('ללא מחיר', '0', '4 חדרים 20,000 ₪', '20,00 ₪', '20.000 ₪'):
            with self.assertRaises(ValueError):
                search.read_price(value)

    def test_upload_requires_https(self):
        with self.assertRaises(ValueError):
            search.upload([], 'http://example.org', 'homes', 'test')

    def test_preview_refuses_old_app_before_posting_records(self):
        from unittest.mock import MagicMock, patch
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with patch('search.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, 'does not support import previews'):
                search.upload([{'source_id': 'test'}], 'https://example.org', dry_run=True)
            self.assertEqual(opener.return_value.open.call_count, 1)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)

    def test_manually_reviewed_listing_can_be_excluded(self):
        item = {'source_id': 'sale-in-rent-feed'}
        self.assertEqual(search.filter_reasons(item, None, ['sale-in-rent-feed']), ['manually_excluded'])

    def test_upload_filters_old_file_before_sending_any_records(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        item = {'source': 'yad2', 'source_id': 'one', 'city': 'הרצליה', 'price': 26000, 'property_type': 'בית פרטי'}
        args = SimpleNamespace(site_url='http://127.0.0.1:8000', dry_run=True)
        with patch('search.upload', return_value={'dry_run': True}) as upload:
            search.confirm_upload([item, item | {'source_id': 'over', 'price': 26001}], args)
            self.assertEqual(upload.call_args.args[0], [item])
