"""Yad2 rental cards observed in the local November 2026 capture."""
import re

CARDS = '[data-nagish="feed-item-layout-link"][href*="/realestate/item/"]'
NEXT_PAGE = 'nav[data-nagish="pagination-navbar"] a[aria-label="עמוד הבא"]'


class PriceNotListed(ValueError):
    pass


def text(card, selector):
    elements = card.locator(selector)
    if not elements.count():
        return ''
    return elements.first.inner_text().strip()


def property_type(value):
    # These are house labels, not apartment/penthouse/garden-apartment labels.
    if value in ("בית פרטי/ קוטג'", "בית פרטי/קוטג'", 'בית פרטי / קוטג׳'):
        return 'בית פרטי'
    if value in ('דו משפחתי', 'דו-משפחתי'):
        return 'דו משפחתי'
    return value


def read_card(card):
    price = text(card, '[data-testid="price"]')
    if price in ('למידע נוסף', 'לא צוין מחיר'):
        raise PriceNotListed('Yad2 does not publish a price for this listing')
    location = text(card, '[data-testid="item-info-line-1st"]')
    parts = location.split(',')
    if len(parts) < 2:
        raise ValueError('Yad2 card lacks separate property type and city')
    street = text(card, '[data-testid="street-name"]')
    city = parts[-1].strip()
    kind = property_type(parts[0].strip())
    if not street or not city or not kind:
        raise ValueError('Yad2 card is missing its address, city or property type')
    details = text(card, '[data-testid="item-info-line-2nd"]')
    rooms = re.search(r'([\d.]+)\s*חדרים', details)
    images = []
    for image in card.locator('img[data-nagish="feed-item-image"]').all():
        src = image.get_attribute('src')
        if src and src.startswith('https://') and src not in images:
            images.append(src)
    return {
        'href': card.get_attribute('href'),
        'title': street + ', ' + city,
        'city': city,
        'address': street,
        'property_type': kind,
        'price_text': price,
        'rooms': float(rooms.group(1)) if rooms else None,
        # The card's area does not distinguish built area from land area.
        'built_sqm': None,
        'lot_sqm': None,
        'description': '',
        'images': images[:30],
    }
