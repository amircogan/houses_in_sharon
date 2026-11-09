"""Visible local rental search for Yad2 and Komo. Madlan remains unverified."""
import argparse
import base64
import getpass
import json
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import yad2
import komo

ROOT = Path(__file__).resolve().parent


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_url(url, source):
    domain = {'yad2': 'yad2.co.il', 'madlan': 'madlan.co.il', 'komo': 'komo.co.il'}[source]
    parts = urlsplit(url)
    if source == 'komo' and parts.hostname == 'account.komo.co.il':
        raise ValueError('Komo requires manual sign-in. Use --capture komo to sign in, then retry.')
    if parts.scheme != 'https' or parts.hostname not in (domain, 'www.' + domain):
        raise ValueError('Expected HTTPS URL on ' + domain)
    if parts.username or parts.password:
        raise ValueError('Credentials are not allowed in search URLs')


def read_text(card, selector):
    if not selector:
        return ''
    elements = card.locator(selector)
    if not elements.count():
        return ''
    return elements.first.inner_text().strip()


def read_price(value):
    match = re.fullmatch(r'\s*₪?\s*(\d{1,3}(?:,\d{3})+|\d+)\s*₪?\s*', value)
    if not match:
        raise ValueError('Missing numeric price')
    price = int(match.group(1).replace(',', ''))
    if price <= 0:
        raise ValueError('Invalid price')
    return price


def listing_url(href, source, page_url):
    if not href:
        raise ValueError('Missing listing link')
    parts = urlsplit(urljoin(page_url, href))
    if source == 'komo':
        check_url(urlunsplit(parts), source)
        numbers = parse_qs(parts.query).get('modaaNum', [])
        if parts.path.rstrip('/') != '/code/nadlan/details' or len(numbers) != 1 or not re.fullmatch(r'[0-9]{1,200}', numbers[0]):
            raise ValueError('Expected a Komo listing URL with one numeric modaaNum')
        url = urlunsplit((parts.scheme, parts.netloc, '/code/nadlan/details/', 'modaaNum=' + numbers[0], ''))
        return url, numbers[0]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))
    check_url(url, source)
    identity = parts.path.rstrip('/').split('/')[-1]
    if not identity or len(identity) > 200:
        raise ValueError('Missing stable listing ID')
    if source == 'yad2' and not re.fullmatch(r'/realestate/item/(?:[^/]+/)?[a-zA-Z0-9]+', parts.path.rstrip('/')):
        raise ValueError('Expected a Yad2 listing URL')
    return url, identity


def read_card(card, fields, source, page_url):
    if source in ('yad2', 'komo') and not fields:
        if source == 'yad2':
            item = yad2.read_card(card)
        else:
            item = komo.read_card(card, page_url)
        url, identity = listing_url(item.pop('href'), source, page_url)
        item['price'] = read_price(item.pop('price_text'))
        item.update(source=source, source_id=identity, url=url)
        return item
    href = card.locator(fields['url']).first.get_attribute('href')
    url, identity = listing_url(href, source, page_url)
    item = {'source': source, 'source_id': identity, 'url': url}
    for name in ('title', 'city', 'property_type'):
        item[name] = read_text(card, fields[name])
        if not item[name]:
            raise ValueError('Missing ' + name + '; check selectors')
    item['price'] = read_price(read_text(card, fields['price']))
    for name in ('address', 'description'):
        item[name] = read_text(card, fields.get(name))
    item['images'] = []
    if fields.get('images'):
        for image in card.locator(fields['images']).all():
            src = image.get_attribute('src')
            if src:
                src = urljoin(page_url, src)
                if src.startswith('https://'):
                    item['images'].append(src)
    item['images'] = item['images'][:30]
    return item


def save_results(items):
    save_json(ROOT / 'results.json', list(items.values()))


def save_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def filter_reasons(item, filters, excluded_ids=None):
    reasons = []
    if item['source_id'] in (excluded_ids or []):
        reasons.append('manually_excluded')
    if not filters:
        return reasons
    if item['price'] < filters['min_price']:
        reasons.append('price_below_minimum')
    if filters.get('max_price') is not None and item['price'] > filters['max_price']:
        reasons.append('price_above_maximum')
    if item['city'] not in filters['allowed_cities']:
        reasons.append('city_not_allowed')
    if item['property_type'] not in filters['allowed_property_types']:
        reasons.append('property_type_not_allowed')
    return reasons


def card_ids(cards, source, page_url, fields):
    identities = set()
    if source == 'yad2' and not fields:
        # Read every href in one browser call; promoted cards can move or vanish.
        links = cards.evaluate_all('(cards) => cards.map(card => card.getAttribute("href"))')
        for href in links:
            url, identity = listing_url(href, source, page_url)
            identities.add(identity)
        return identities
    if source == 'komo' and not fields:
        links = cards.evaluate_all('(cards) => cards.map(card => card.querySelector("a[href*=modaaNum]")?.getAttribute("href"))')
        for href in links:
            url, identity = listing_url(href, source, page_url)
            identities.add(identity)
        return identities
    for card in cards.all():
        href = card.locator(fields['url']).first.get_attribute('href')
        url, identity = listing_url(href, source, page_url)
        identities.add(identity)
    return identities


def check_access(page):
    title = page.title().strip().lower()
    if title in ('just a moment...', 'access denied', 'attention required! | cloudflare'):
        raise RuntimeError('Website access challenge is still visible; handle it manually in capture mode')


def collect(page, config, items, prompt=True, report=None):
    source = config['source']
    check_url(config['url'], source)
    config = dict(config)
    if source == 'yad2' and not config.get('fields'):
        config.setdefault('cards', yad2.CARDS)
        config.setdefault('next_page', yad2.NEXT_PAGE)
    if source == 'komo' and not config.get('fields'):
        config.setdefault('cards', komo.CARDS)
        config.setdefault('next_page', komo.NEXT_PAGE)
    for name in ('cards', 'next_page'):
        if not config.get(name):
            raise ValueError('Verified selector required: ' + name)
    fields = config.get('fields')
    filters = config.get('filters')
    if report is None:
        report = {'pages': [], 'rejected': [], 'status': 'running'}
    if source not in ('yad2', 'komo') or fields:
        for name in ('url', 'title', 'city', 'price', 'property_type'):
            if not fields or not fields.get(name):
                raise ValueError('Verified field selector required: ' + name)
    limit = int(config.get('max_pages', 5))
    delay = float(config.get('delay_seconds', 3))
    if not 1 <= limit <= 100 or delay < 1:
        raise ValueError('Use 1–100 pages and at least one second between pages')
    page.goto(config['url'], wait_until='domcontentloaded')
    if prompt:
        input('Check filters; handle login/CAPTCHA manually. Press Enter when results are ready: ')
    seen = set()
    seen_ids = set()
    for page_number in range(1, limit + 1):
        check_url(page.url, source)
        check_access(page)
        cards = page.locator(config['cards'])
        try:
            cards.first.wait_for(state='visible', timeout=15000)
        except Exception:
            if not prompt:
                raise RuntimeError('No listing cards: access challenge, empty results or changed layout')
            input('No listings found. Check browser access/layout, then Enter to retry once: ')
            cards.first.wait_for(state='visible', timeout=15000)
        page_ids = card_ids(cards, source, page.url, fields)
        page_items = {}
        rejected = []
        unpriced = 0
        if source == 'yad2' and not fields:
            links = cards.evaluate_all('(cards) => cards.map(card => card.getAttribute("href"))')
            page_cards = []
            for href in links:
                link = page.locator('[href=' + json.dumps(href) + ']')
                page_cards.append(cards.and_(link).first)
        else:
            page_cards = cards.all()
        for card in page_cards:
            try:
                item = read_card(card, fields, source, page.url)
            except (yad2.PriceNotListed, komo.PriceNotListed):
                unpriced += 1
                continue
            reasons = filter_reasons(item, filters, config.get('excluded_ids'))
            if reasons:
                rejected.append({'source': source, 'source_id': item['source_id'],
                                 'city': item['city'], 'price': item['price'],
                                 'property_type': item['property_type'], 'reasons': reasons})
                continue
            page_items[source + ':' + item['source_id']] = item
        signature = frozenset(page_ids)
        if not signature or signature in seen or not page_ids - seen_ids:
            raise RuntimeError('No new listing page; check selectors')
        seen.add(signature)
        seen_ids.update(page_ids)
        items.update(page_items)
        save_results(items)
        report['rejected'].extend(rejected)
        report['pages'].append({'source': source, 'url': page.url, 'page': page_number,
                                'cards': len(page_ids), 'matched': len(page_items),
                                'unpriced': unpriced, 'rejected': len(rejected)})
        save_json(ROOT / 'filter_report.json', report)
        print(source + ' page ' + str(page_number) + ': ' + str(len(page_items))
              + ' matches, ' + str(len(rejected)) + ' filtered out, '
              + str(unpriced) + ' without a published price skipped')
        buttons = page.locator(config['next_page'])
        if not buttons.count():
            if source == 'yad2':
                raise RuntimeError('Yad2 next-page control missing; cannot confirm end of results')
            if source == 'komo':
                komo.confirm_last_page(page)
            report['status'] = 'complete'
            save_json(ROOT / 'filter_report.json', report)
            break
        button = buttons.first
        if not button.is_visible() or not button.is_enabled() or button.get_attribute('aria-disabled') == 'true':
            report['status'] = 'complete'
            save_json(ROOT / 'filter_report.json', report)
            break
        if page_number == limit:
            report['status'] = 'page_limit'
            save_json(ROOT / 'filter_report.json', report)
            raise RuntimeError('Page limit reached with more results remaining; results saved, upload stopped')
        previous = page_ids
        time.sleep(delay)
        button.click()
        for attempt in range(50):
            page.wait_for_timeout(200)
            check_url(page.url, source)
            check_access(page)
            if cards.count() and card_ids(cards, source, page.url, fields) - previous:
                break
        else:
            raise RuntimeError('Next page did not load; automatic upload stopped')


def upload(items, site_url, username='', password='', dry_run=False):
    parts = urlsplit(site_url)
    local_http = parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')
    if (parts.scheme != 'https' and not local_http) or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise ValueError('Use your app HTTPS URL (HTTP is allowed only on localhost)')
    auth = base64.b64encode((username + ':' + password).encode()).decode()
    endpoint = site_url.rstrip('/') + '/api/import'
    opener = build_opener(NoRedirect())
    if dry_run:
        check = Request(endpoint, headers={'Authorization': 'Basic ' + auth})
        with opener.open(check, timeout=60) as response:
            capabilities = json.load(response)
        if capabilities.get('supports_dry_run') is not True:
            raise ValueError('App does not support import previews; update the app before uploading')
        endpoint += '?dry_run=true'
    request = Request(endpoint, data=json.dumps(items).encode(),
                      headers={'Content-Type': 'application/json', 'Authorization': 'Basic ' + auth})
    with opener.open(request, timeout=60) as response:
        return json.load(response)


def capture(page, source):
    url = input('Paste the ' + source + ' search URL: ').strip()
    check_url(url, source)
    page.goto(url, wait_until='domcontentloaded')
    input('Open results normally; handle login/CAPTCHA yourself. Press Enter to save: ')
    check_url(page.url, source)
    folder = ROOT / 'captures'
    folder.mkdir(exist_ok=True)
    path = folder / (source + '.html')
    path.write_text(page.content(), encoding='utf-8')
    path.with_suffix('.url.txt').write_text(page.url, encoding='utf-8')
    print('Saved ' + str(path))
    print('HTML may contain private data/tokens. Review before sharing; never share profile/.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', choices=['yad2', 'madlan', 'komo'])
    parser.add_argument('--source', choices=['yad2', 'madlan', 'komo'], help='Run only this source from the config')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--upload', action='store_true')
    parser.add_argument('--upload-file', type=Path, help='Upload an existing JSON result file without opening the browser')
    parser.add_argument('--site-url', help='App origin; HTTP allowed only on localhost')
    parser.add_argument('--dry-run', action='store_true', help='Preview import without changing app data')
    parser.add_argument('--no-prompt', action='store_true', help='Use configured filters without pausing; challenges stop collection')
    args = parser.parse_args()
    if args.upload_file:
        if args.capture or args.upload:
            parser.error('--upload-file cannot be combined with --capture or --upload')
        records = json.loads(args.upload_file.read_text(encoding='utf-8'))
        if not isinstance(records, list) or not records:
            parser.error('Upload file must contain a nonempty listing array')
        confirm_upload(records, args)
        return
    if args.dry_run and not args.upload:
        parser.error('--dry-run requires --upload or --upload-file')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(str(ROOT / 'profile'), headless=False)
        try:
            page = browser.pages[0]
            if args.capture:
                capture(page, args.capture)
                return
            config = json.loads(args.config.read_text(encoding='utf-8'))
            searches = config['searches']
            if args.source:
                searches = [search for search in searches if search['source'] == args.source]
                if not searches:
                    parser.error('No search configured for ' + args.source)
            filters = None
            if config.get('filters_file'):
                filter_path = args.config.resolve().parent / config['filters_file']
                filters = json.loads(filter_path.read_text(encoding='utf-8'))
            items = {}
            report = {'pages': [], 'rejected': [], 'status': 'running'}
            for search in searches:
                search = dict(search)
                search['filters'] = filters
                report['status'] = 'running'
                try:
                    collect(page, search, items, prompt=not args.no_prompt, report=report)
                except Exception:
                    if report['status'] != 'page_limit':
                        report['status'] = 'failed'
                    save_json(ROOT / 'filter_report.json', report)
                    raise
        finally:
            browser.close()
    print('Saved ' + str(len(items)) + ' unique listings.')
    unique_rejections = {}
    for row in report['rejected']:
        unique_rejections[(row['source'], row['source_id'])] = row
    reasons = Counter()
    for row in unique_rejections.values():
        reasons.update(row['reasons'])
    print('Rejected ' + str(len(unique_rejections)) + ' unique listings: '
          + json.dumps(dict(reasons), ensure_ascii=False))
    print('Details saved to filter_report.json.')
    if args.upload and items:
        confirm_upload(list(items.values()), args)


def confirm_upload(records, args):
    filter_path = ROOT.parent / 'sharon-homes' / 'search_filters.json'
    filters = json.loads(filter_path.read_text(encoding='utf-8'))
    matching = []
    for record in records:
        if not filter_reasons(record, filters):
            matching.append(record)
    print('Before upload: ' + str(len(matching)) + ' matches; '
          + str(len(records) - len(matching)) + ' records excluded by current filters.')
    records = matching
    if not records:
        print('No matching records to upload.')
        return
    site_url = args.site_url or input('App URL: ').strip()
    parts = urlsplit(site_url)
    local = parts.hostname in ('localhost', '127.0.0.1', '::1')
    username = ''
    password = ''
    if not local:
        username = input('App username [homes]: ').strip() or 'homes'
        password = getpass.getpass('App password (not saved): ')
    preview = upload(records, site_url, username, password, dry_run=True)
    print('Import preview: ' + json.dumps(preview, ensure_ascii=False))
    if args.dry_run:
        return
    if preview.get('identity_conflicts'):
        raise ValueError('Existing listing IDs differ. Resolve identity conflicts before upload.')
    if input('Upload listings to ' + site_url + '? Type yes: ').strip() == 'yes':
        print(upload(records, site_url, username, password))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Stopped: ' + str(error))
        raise SystemExit(1)
