"""Sharon Homes: responsive rental tracker + permitted-source importer + daily digest."""
import base64
import hmac
import json
import os
import sqlite3
import smtplib
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from sources import configured_sources
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Header, Request, Response
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DB_PATH', str(ROOT / 'data' / 'homes.sqlite')))
TZ = ZoneInfo('Asia/Jerusalem')
SEARCH_FILTERS = json.loads((ROOT / 'search_filters.json').read_text(encoding='utf-8'))
MIN_PRICE = int(os.getenv('MIN_PRICE', str(SEARCH_FILTERS['min_price'])))
maximum = os.getenv('MAX_PRICE', '')
MAX_PRICE = int(maximum) if maximum else SEARCH_FILTERS['max_price']
ALLOWED_CITIES = {s.strip() for s in os.getenv('ALLOWED_CITIES', ','.join(SEARCH_FILTERS['allowed_cities'])).split(',') if s.strip()}
ALLOWED_TYPES = set(SEARCH_FILTERS['allowed_property_types'])
app = FastAPI(title='בתים בשרון')
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


def now():
    return datetime.now(TZ)


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db():
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS homes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL, source_id TEXT NOT NULL, url TEXT NOT NULL,
          title TEXT NOT NULL, city TEXT NOT NULL, address TEXT DEFAULT '',
          property_type TEXT DEFAULT 'בית פרטי', price INTEGER NOT NULL,
          rooms REAL, built_sqm REAL, lot_sqm REAL,
          description TEXT DEFAULT '', images TEXT NOT NULL DEFAULT '[]',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          hidden INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
          favorite INTEGER NOT NULL DEFAULT 0,
          UNIQUE(source, source_id)
        );
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, start_time TEXT NOT NULL,
          finish_time TEXT, source TEXT NOT NULL, added INTEGER DEFAULT 0,
          updated INTEGER DEFAULT 0, status TEXT NOT NULL, details TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS digest_log (
          date TEXT PRIMARY KEY, sent_at TEXT NOT NULL, new_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bulk_hide_undo (
          home_id INTEGER PRIMARY KEY
        );
        CREATE INDEX IF NOT EXISTS homes_filter_idx ON homes(hidden, city, price, first_seen);
        ''')


def credentials(authorization: str | None = Header(default=None)):
    password = os.getenv('APP_PASSWORD', '').strip()
    if not password:
        # The site must not become public without configured auth.
        
        if os.getenv('ALLOW_LOCAL_NO_PASSWORD') == '1':
            return True
        raise HTTPException(status_code=503, detail='Configure APP_PASSWORD')
    if not authorization or not authorization.startswith('Basic '):
        raise HTTPException(status_code=401, detail='Login required', headers={'WWW-Authenticate':'Basic realm="Sharon Homes"'})
    try:
        user, pwd = base64.b64decode(authorization[6:], validate=True).decode().split(':', 1)
    except Exception:
        raise HTTPException(status_code=401, detail='Invalid login', headers={'WWW-Authenticate':'Basic realm="Sharon Homes"'})
    if not (hmac.compare_digest(user, os.getenv('APP_USER', 'homes')) and hmac.compare_digest(pwd, password)):
        raise HTTPException(status_code=401, detail='Invalid login', headers={'WWW-Authenticate':'Basic realm="Sharon Homes"'})
    return True


# Pydantic validates incoming data and rejects unsupported URL schemes.
class Listing(BaseModel):
    source: str = Field(min_length=1, max_length=60)
    source_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=12, max_length=2000)
    title: str = Field(min_length=2, max_length=400)
    city: str = Field(min_length=2, max_length=100)
    address: str = Field(default='', max_length=300)
    property_type: str = Field(min_length=1, max_length=100)
    price: int = Field(gt=0)
    rooms: float | None = None
    built_sqm: float | None = None
    lot_sqm: float | None = None
    description: str = Field(default='', max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=30)


def https_url(value):
    parts = urlsplit(value)
    return parts.scheme == 'https' and bool(parts.hostname) and not parts.username and not parts.password


def import_url_key(source, url):
    parts = urlsplit(url)
    host = (parts.hostname or '').removeprefix('www.')
    path = parts.path.rstrip('/')
    if source == 'yad2' and host == 'yad2.co.il' and path.startswith('/realestate/item/'):
        # Older imports omit the region segment. The listing token is unchanged.
        path = '/realestate/item/' + path.split('/')[-1]
    if source == 'komo' and host == 'komo.co.il' and path == '/code/nadlan/details':
        # Komo identifies every listing by a query value on the same URL path.
        number = parse_qs(parts.query).get('modaaNum', [''])[0]
        if number.isdigit():
            path += '?modaaNum=' + number
    return source, host, path


def upsert_many(records, dry_run=False):
    added = updated = skipped = 0
    rejected = []
    identity_conflicts = []
    stamp = now().isoformat(timespec='seconds')
    with db() as conn:
        existing = {}
        known_ids = set()
        for row in conn.execute('SELECT source,source_id,url FROM homes'):
            existing[import_url_key(row['source'], row['url'])] = row['source_id']
            known_ids.add((row['source'], row['source_id']))
        for index, record in enumerate(records):
            try:
                item = Listing.model_validate(record)
                reason = ''
                if not https_url(item.url):
                    reason = 'invalid_url'
                elif item.price < MIN_PRICE or (MAX_PRICE is not None and item.price > MAX_PRICE):
                    reason = 'price_out_of_range'
                elif item.city not in ALLOWED_CITIES:
                    reason = 'city_not_allowed'
                elif item.property_type not in ALLOWED_TYPES:
                    reason = 'property_type_not_allowed'
                url_key = import_url_key(item.source, item.url)
                existing_id = existing.get(url_key)
                if existing_id is not None and existing_id != item.source_id:
                    reason = 'identity_conflict'
                    identity_conflicts.append({'index': index, 'source': item.source,
                                               'source_id': item.source_id, 'existing_source_id': existing_id})
                if reason:
                    skipped += 1
                    rejected.append({'index': index, 'source_id': item.source_id, 'reason': reason})
                    continue
                images = [s for s in item.images if https_url(s)]
                key = (item.source, item.source_id)
                is_update = key in known_ids
                known_ids.add(key)
                existing[url_key] = item.source_id
                if dry_run:
                    if is_update:
                        updated += 1
                    else:
                        added += 1
                    continue
                old = conn.execute('SELECT id FROM homes WHERE source=? AND source_id=?', (item.source,item.source_id)).fetchone()
                if old:
                    conn.execute('''UPDATE homes SET url=?,title=?,city=?,address=?,property_type=?,price=?,rooms=?,built_sqm=?,lot_sqm=?,description=?,images=?,last_seen=? WHERE id=?''',
                                 (item.url,item.title,item.city,item.address,item.property_type,item.price,item.rooms,item.built_sqm,item.lot_sqm,item.description,json.dumps(images,ensure_ascii=False),stamp,old['id']))
                    updated += 1
                else:
                    conn.execute('''INSERT INTO homes(source,source_id,url,title,city,address,property_type,price,rooms,built_sqm,lot_sqm,description,images,first_seen,last_seen)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                 (item.source,item.source_id,item.url,item.title,item.city,item.address,item.property_type,item.price,item.rooms,item.built_sqm,item.lot_sqm,item.description,json.dumps(images,ensure_ascii=False),stamp,stamp))
                    added += 1
            except (ValidationError, ValueError, TypeError):
                skipped += 1
                rejected.append({'index': index, 'reason': 'invalid_record'})
    result = {'added':added,'updated':updated,'skipped':skipped}
    if dry_run:
        result['dry_run'] = True
    result['rejected'] = rejected
    result['identity_conflicts'] = identity_conflicts
    return result


def read_feed_file(path):
    """Only import explicitly provided/permitted JSON; no bypass of site restrictions."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return data['listings'] if isinstance(data, dict) else data


SCAN_LOCK = threading.Lock()


def collect():
    """Import configured permitted sources independently; never send email."""
    if not SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(409, 'Search already running')
    try:
        folder = ROOT / 'feeds'
        sources = [(f.name, lambda f=f: read_feed_file(f)) for f in sorted(folder.glob('*.json'))]
        sources.extend(configured_sources())
        outcome = dict(added=0, updated=0, skipped=0, sources=len(sources), errors=[])
        for name, fetch in sources:
            started = now().isoformat(timespec='seconds')
            try:
                items = fetch()
                if not isinstance(items, list):
                    raise ValueError('Expected a listing array')
                result = upsert_many(items)
                for k in ('added', 'updated', 'skipped'):
                    outcome[k] += result[k]
                status, details = 'ok', ''
            except Exception:
                # Remote errors may contain credentials or signed URLs.
                result = dict(added=0, updated=0)
                status, details = 'error', 'Source request or data validation failed'
                outcome['errors'].append({'source': name, 'message': details})
            with db() as conn:
                conn.execute('INSERT INTO runs(start_time,finish_time,source,added,updated,status,details) VALUES (?,?,?,?,?,?,?)',
                             (started, now().isoformat(timespec='seconds'), name, result['added'], result['updated'], status, details))
        with db() as conn:
            conn.execute('INSERT INTO runs(start_time,finish_time,source,added,updated,status) VALUES (?,?,?,?,?,?)',
                         (now().isoformat(timespec='seconds'), now().isoformat(timespec='seconds'), 'search', outcome['added'], outcome['updated'], 'partial' if outcome['errors'] else 'ok'))
        return outcome
    finally:
        SCAN_LOCK.release()


def previous_day_bounds(reference=None):
    ref = reference or now()
    yesterday = ref.date() - timedelta(days=1)
    return yesterday, datetime.combine(yesterday,datetime.min.time(),TZ).isoformat(), datetime.combine(ref.date(),datetime.min.time(),TZ).isoformat()


def send_digest(reference=None):
    if os.getenv('ENABLE_EMAIL_DELIVERY') != '1':
        return {'status': 'disabled'}
    day, start, end = previous_day_bounds(reference)
    with db() as conn:
        if conn.execute('SELECT date FROM digest_log WHERE date=?',(day.isoformat(),)).fetchone():
            return {'status':'already_sent','date':day.isoformat()}
        number = conn.execute('SELECT count(*) FROM homes WHERE first_seen>=? AND first_seen<?',(start,end)).fetchone()[0]
    site = os.getenv('SITE_URL','').rstrip('/')
    if not site.startswith('https://'):
        raise RuntimeError('SITE_URL must be a public HTTPS URL')
    host, user, pwd = [os.getenv(k, '') for k in ('SMTP_HOST','SMTP_USER','SMTP_PASSWORD')]
    if not (host and user and pwd):
        raise RuntimeError('SMTP_HOST, SMTP_USER, SMTP_PASSWORD are required')
    recipients = ['amir.cog@gmail.com','revital.kedemtal@gmail.com']
    msg = EmailMessage()
    msg['Subject'] = f'בתים בשרון — {number} מודעות חדשות ({day.strftime("%d/%m/%Y")})'
    msg['From'] = os.getenv('SMTP_FROM') or user
    msg['To'] = ', '.join(recipients)
    msg.set_content(f'שלום,\n\nאתמול ({day.strftime("%d/%m/%Y")}) נוספו {number} מודעות חדשות למעקב הבתים בשרון.\n\nכניסה לאתר: {site}\n\nהמערכת מציגה את המודעות שלא הסתרתם, עם הערות וקישורים למקור.\n')
    port = int(os.getenv('SMTP_PORT','465'))
    if os.getenv('SMTP_STARTTLS') == '1':
        with smtplib.SMTP(host,port,timeout=30) as server:
            server.starttls()
            server.login(user,pwd)
            server.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host,port,timeout=30) as server:
            server.login(user,pwd)
            server.send_message(msg)
    with db() as conn:
        conn.execute('INSERT INTO digest_log(date,sent_at,new_count) VALUES (?,?,?)',(day.isoformat(),now().isoformat(timespec='seconds'),number))
    return {'status':'sent','date':day.isoformat(),'count':number}


def scheduler_loop():
    # One-process setup only. For multi-worker deployment use external cron and --no-scheduler.
    while True:
        try:
            local = now()
            # At 07:30 collect, at 08:00 email yesterday's additions.
            with db() as conn:
                latest = conn.execute("SELECT start_time FROM runs WHERE source='daily' ORDER BY id DESC LIMIT 1").fetchone()
            if (local.hour > 7 or (local.hour == 7 and local.minute >= 30)) and (not latest or latest['start_time'][:10] != local.date().isoformat()):
                with db() as conn:
                    conn.execute("INSERT INTO runs(start_time,source,status) VALUES (?,?,?)",(local.isoformat(timespec='seconds'),'daily','started'))
                collect()
                with db() as conn:
                    conn.execute("UPDATE runs SET status='ok', finish_time=? WHERE id=(SELECT max(id) FROM runs WHERE source='daily')",(now().isoformat(timespec='seconds'),))
            if local.hour >= 8 and os.getenv('ENABLE_EMAIL_DELIVERY') == '1':
                try:
                    send_digest()
                except Exception as exc:
                    print('Digest not sent:',str(exc))
        except Exception as exc:
            print('Scheduler error:',str(exc))
        time.sleep(60)


@app.on_event('startup')
def startup():
    init_db()
    if os.getenv('ENABLE_SCHEDULER') == '1':
        threading.Thread(target=scheduler_loop,daemon=True).start()


@app.get('/', response_class=HTMLResponse)
def index(_: bool = Depends(credentials)):
    return FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.get('/api/homes')
def homes(response: Response, show: str = 'active', search: str = '', _: bool = Depends(credentials)):
    response.headers['Cache-Control'] = 'no-store'
    with db() as conn:
        stored = [dict(r) for r in conn.execute('SELECT * FROM homes ORDER BY favorite DESC, first_seen DESC, id DESC')]
        undo_count = conn.execute('SELECT count(*) FROM bulk_hide_undo JOIN homes ON homes.id=bulk_hide_undo.home_id WHERE homes.hidden=1').fetchone()[0]
    # Keep older records and their notes, but display only the current budget.
    rows = []
    for row in stored:
        if row['price'] < MIN_PRICE or (MAX_PRICE is not None and row['price'] > MAX_PRICE):
            continue
        if row['city'] not in ALLOWED_CITIES or row['property_type'] not in ALLOWED_TYPES:
            continue
        rows.append(row)
    date, start, end = previous_day_bounds()
    stats = {'total': len(rows), 'active': 0, 'saved': 0, 'yesterday': 0}
    for row in rows:
        if not row['hidden']:
            stats['active'] += 1
            if row['favorite']:
                stats['saved'] += 1
        if start <= row['first_seen'] < end:
            stats['yesterday'] += 1
    if show != 'all':
        hidden = show == 'hidden'
        rows = [row for row in rows if bool(row['hidden']) == hidden]
    for r in rows:
        r['images'] = json.loads(r['images'])
        r['location_group'] = location_group(r['city'])
    if search:
        q = search.casefold()
        rows = [r for r in rows if q in ' '.join(str(r.get(k) or '') for k in ('title','city','address','description','note')).casefold()]
    return {'items':rows,'stats':stats,'last_scan':last_scan(),
            'bulk_hide_undo_count': undo_count,
            'filters': {'min_price': MIN_PRICE, 'max_price': MAX_PRICE},
            'location_groups': [{'id': group['id'], 'label': group['label']} for group in SEARCH_FILTERS['location_groups']]}


def location_group(city):
    for group in SEARCH_FILTERS['location_groups']:
        if city in group['cities']:
            return group['id']
    return 'other'


def last_scan():
    with db() as conn:
        row = conn.execute("SELECT start_time,finish_time,status,added FROM runs WHERE source IN ('daily','search','import') ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


class Edit(BaseModel):
    hidden: bool | None = None
    favorite: bool | None = None
    note: str | None = Field(default=None, max_length=10000)


class HideNonFavorites(BaseModel):
    ids: list[int] = Field(max_length=10000)


@app.post('/api/homes/hide-non-favorites')
def hide_non_favorites(selection: HideNonFavorites, _: bool = Depends(credentials)):
    # Only the displayed IDs selected by the client; protect current favorites.
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        changed = []
        for identity in selection.ids:
            result = conn.execute('UPDATE homes SET hidden=1 WHERE id=? AND favorite=0 AND hidden=0', (identity,))
            if result.rowcount:
                changed.append((identity,))
        if changed:
            conn.execute('DELETE FROM bulk_hide_undo')
            conn.executemany('INSERT INTO bulk_hide_undo(home_id) VALUES (?)', changed)
    return {'hidden': len(changed)}


@app.post('/api/homes/undo-last-hide')
def undo_last_hide(_: bool = Depends(credentials)):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        result = conn.execute('UPDATE homes SET hidden=0 WHERE hidden=1 AND id IN (SELECT home_id FROM bulk_hide_undo)')
        count = result.rowcount
        conn.execute('DELETE FROM bulk_hide_undo')
    return {'restored': count}


@app.patch('/api/homes/{home_id}')
def edit(home_id: int, change: Edit, _:bool = Depends(credentials)):
    values = change.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(400,'No changes')
    for k in ('hidden','favorite'):
        if k in values:
            values[k] = int(values[k])
    with db() as conn:
        row = conn.execute('SELECT id FROM homes WHERE id=?',(home_id,)).fetchone()
        if not row:
            raise HTTPException(404,'Not found')
        conn.execute('UPDATE homes SET '+', '.join(f'{k}=?' for k in values)+' WHERE id=?',list(values.values())+[home_id])
        if 'hidden' in values:
            # Undo must not overwrite a later manual visibility decision.
            conn.execute('DELETE FROM bulk_hide_undo WHERE home_id=?', (home_id,))
    return {'ok':True}


@app.get('/api/import')
def import_capabilities(_:bool = Depends(credentials)):
    return {'supports_dry_run': True, 'required_fields': [
        'source', 'source_id', 'url', 'title', 'city', 'property_type', 'price'],
        'min_price': MIN_PRICE, 'max_price': MAX_PRICE,
        'allowed_cities': sorted(ALLOWED_CITIES), 'allowed_property_types': sorted(ALLOWED_TYPES)}


@app.post('/api/import')
def ingest(payload: list[Listing], dry_run: bool = False, _:bool = Depends(credentials)):
    # Authenticated endpoint for permitted-source exports. Not intended for public ingestion.
    started = now().isoformat(timespec='seconds')
    result = upsert_many([x.model_dump() for x in payload], dry_run=dry_run)
    if not dry_run:
        with db() as conn:
            conn.execute('INSERT INTO runs(start_time,finish_time,source,added,updated,status) VALUES (?,?,?,?,?,?)',
                         (started, now().isoformat(timespec='seconds'), 'import', result['added'], result['updated'], 'ok'))
    return result


@app.post('/api/scan')
def scan(_:bool = Depends(credentials)):
    return collect()


@app.get('/api/status')
def status(_:bool = Depends(credentials)):
    return {'last_scan':last_scan(), 'configured_feed_files':len(list((ROOT/'feeds').glob('*.json'))) if (ROOT/'feeds').exists() else 0, 'scheduler_enabled':os.getenv('ENABLE_SCHEDULER')=='1', 'email_enabled':os.getenv('ENABLE_EMAIL_DELIVERY')=='1', 'site_configured':bool(os.getenv('SITE_URL'))}


@app.post('/api/digest-test')
def digest_test(_:bool = Depends(credentials)):
    # Real email; deliberately not exposed in UI; idempotent per previous day.
    try:
        return send_digest()
    except RuntimeError as ex:
        raise HTTPException(503,str(ex))
