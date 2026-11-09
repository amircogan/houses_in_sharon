"""Komo house rental cards observed in the local November 2026 capture."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit

CARDS = 'div.modaaRowAd[id^="modaaRowDv"]'
NEXT_PAGE = '#paging a:has-text("»")'


class PriceNotListed(ValueError):
    pass


def text(card, selector):
    elements = card.locator(selector)
    if not elements.count():
        return ''
    return elements.first.inner_text().strip()


def read_card(card, page_url):
    title = text(card, 'h2.title')
    city, separator, address = title.partition(',')
    if not city.strip() or not separator:
        raise ValueError('Komo card lacks a separate city and address')
    details = text(card, '.description')
    match = re.match(r'(.+?)\s+(\d+(?:\.\d+)?)\s+חדרים', details)
    if not match:
        raise ValueError('Komo property type or room count is missing')
    kind = match.group(1).strip()
    if kind == 'וילות/ בתים':
        kind = 'בית פרטי'
    price = text(card, '.price')
    if price == 'לא צוין מחיר':
        raise PriceNotListed('Komo does not publish a price for this listing')
    link = card.locator('a[href*="modaaNum="]').first
    images = []
    for image in card.locator('.image__wrapper img').all():
        src = image.get_attribute('src')
        if src:
            src = urljoin(page_url, src)
            if src.startswith('https://') and src not in images:
                images.append(src)
    return {'href': link.get_attribute('href'), 'title': title, 'city': city.strip(),
            'address': address.strip(), 'property_type': kind, 'price_text': price,
            'rooms': float(match.group(2)), 'built_sqm': None, 'lot_sqm': None,
            'description': '', 'images': images[:30]}


def confirm_last_page(page):
    if page.locator('#paging').count() != 1 or page.locator('#showKamut').count() != 1:
        raise RuntimeError('Komo pagination/count markup is missing; cannot confirm completion')
    count = re.search(r'\d[\d,]*', page.locator('#showKamut').inner_text())
    if not count:
        raise RuntimeError('Komo result count is unreadable')
    total = int(count.group().replace(',', ''))
    query = parse_qs(urlsplit(page.url).query)
    current = int(query.get('currPage', ['1'])[0])
    for link in page.locator('#paging a[href]').all():
        target = urlsplit(urljoin(page.url, link.get_attribute('href')))
        number = int(parse_qs(target.query).get('currPage', ['1'])[0])
        if number > current:
            raise RuntimeError('Komo next link is missing while later pages remain')
    # The observed server-rendered results contain 20 cards per page.
    if current * 20 < total:
        raise RuntimeError('Komo page count indicates more results; upload stopped')
