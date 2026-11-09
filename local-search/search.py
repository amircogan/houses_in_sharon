"""Local browser search draft; site selectors still need verification."""
import argparse
import base64
import getpass
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

ROOT = Path(__file__).resolve().parent


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_url(url, source):
    domain = {'yad2': 'yad2.co.il', 'madlan': 'madlan.co.il'}[source]
    parts = urlsplit(url)
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
    match = re.search(r'\d[\d,]*', value)
    if not match:
        raise ValueError('Missing numeric price')
    price = int(match.group().replace(',', ''))
    if price <= 0:
        raise ValueError('Invalid price')
    return price


def read_card(card, fields, source, page_url):
    href = card.locator(fields['url']).first.get_attribute('href')
    if not href:
        raise ValueError('Missing listing link')
    parts = urlsplit(urljoin(page_url, href))
    url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))
    check_url(url, source)
    identity = parts.path.rstrip('/').split('/')[-1]
    if not identity or len(identity) > 200:
        raise ValueError('Missing stable listing ID')
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
    path = ROOT / 'results.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(list(items.values()), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def collect(page, config, items):
    source = config['source']
    check_url(config['url'], source)
    for name in ('cards', 'next_page'):
        if not config.get(name):
            raise ValueError('Verified selector required: ' + name)
    fields = config['fields']
    for name in ('url', 'title', 'city', 'price', 'property_type'):
        if not fields.get(name):
            raise ValueError('Verified field selector required: ' + name)
    limit = int(config.get('max_pages', 5))
    delay = float(config.get('delay_seconds', 3))
    if not 1 <= limit <= 100 or delay < 1:
        raise ValueError('Use 1–100 pages and at least one second between pages')
    page.goto(config['url'], wait_until='domcontentloaded')
    input('Check filters; handle login/CAPTCHA manually. Press Enter when results are ready: ')
    seen = set()
    for page_number in range(1, limit + 1):
        check_url(page.url, source)
        cards = page.locator(config['cards'])
        try:
            cards.first.wait_for(state='visible', timeout=15000)
        except Exception:
            input('No listings found. Check browser access/layout, then Enter to retry once: ')
            cards.first.wait_for(state='visible', timeout=15000)
        page_ids = []
        for card in cards.all():
            item = read_card(card, fields, source, page.url)
            page_ids.append(item['source_id'])
            items[source + ':' + item['source_id']] = item
        signature = tuple(page_ids)
        if not signature or signature in seen:
            raise RuntimeError('No new listing page; check selectors')
        seen.add(signature)
        save_results(items)
        print(source + ' page ' + str(page_number) + ': ' + str(len(page_ids)) + ' listings')
        if page_number == limit:
            print('Page limit reached; more results may remain.')
            break
        buttons = page.locator(config['next_page'])
        if not buttons.count():
            break
        button = buttons.first
        if not button.is_visible() or not button.is_enabled() or button.get_attribute('aria-disabled') == 'true':
            break
        previous = cards.all_text_contents()
        time.sleep(delay)
        button.click()
        for attempt in range(50):
            page.wait_for_timeout(200)
            if cards.count() and cards.all_text_contents() != previous:
                break
        else:
            raise RuntimeError('Next page did not load; automatic upload stopped')


def upload(items, site_url, username, password):
    parts = urlsplit(site_url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('Use your app HTTPS URL without credentials or a query')
    auth = base64.b64encode((username + ':' + password).encode()).decode()
    request = Request(site_url.rstrip('/') + '/api/import', data=json.dumps(items).encode(),
                      headers={'Content-Type': 'application/json', 'Authorization': 'Basic ' + auth})
    with build_opener(NoRedirect()).open(request, timeout=60) as response:
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
    print('Saved ' + str(path))
    print('HTML may contain private data/tokens. Review before sharing; never share profile/.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', choices=['yad2', 'madlan'])
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--upload', action='store_true')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(str(ROOT / 'profile'), headless=False)
        try:
            page = browser.pages[0]
            if args.capture:
                capture(page, args.capture)
                return
            config = json.loads(args.config.read_text(encoding='utf-8'))
            items = {}
            for search in config['searches']:
                collect(page, search, items)
        finally:
            browser.close()
    print('Saved ' + str(len(items)) + ' unique listings.')
    if args.upload and items:
        site_url = input('Railway app HTTPS URL: ').strip()
        username = input('App username [homes]: ').strip() or 'homes'
        password = getpass.getpass('App password (not saved): ')
        if input('Upload listings? Type yes: ').strip() == 'yes':
            print(upload(list(items.values()), site_url, username, password))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Stopped: ' + str(error))
        raise SystemExit(1)
