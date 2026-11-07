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
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DB_PATH', str(ROOT / 'data' / 'homes.sqlite')))
TZ = ZoneInfo('Asia/Jerusalem')
MIN_PRICE = int(os.getenv('MIN_PRICE', '15000'))
MAX_PRICE = int(os.getenv('MAX_PRICE', '25000'))
ALLOWED_CITIES = {s.strip() for s in os.getenv('ALLOWED_CITIES', 'בצרה,בני ציון,רשפון,געש,רמות השבים,גן חיים,חרות,כפר הס,תל מונד,קדימה צורן,כפר יונה,אבן יהודה,כפר נטר,בית יהושע,אודים,יקום,שפיים,כפר שמריהו,רעננה,כפר סבא,הוד השרון,הרצליה,רמת השרון,גני עם,ירקונה,עדנים,נווה ירק,שדה ורבורג,צופית,ניר אליהו,בני דרור,עין ורד,עין שריד,משמרת,פורת,כפר מלל,כפר מעש,גבעת חן,מגשימים,צור משה,תל יצחק,נטעים,בית יצחק-שער חפר,בית יצחק').split(',') if s.strip()}
ALLOWED_TYPES = {'בית פרטי','וילה','קוטג׳','קוטג\'','דו משפחתי','בית במושב','בית'}
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
    property_type: str = 'בית פרטי'
    price: int = Field(gt=0)
    rooms: float | None = None
    built_sqm: float | None = None
    lot_sqm: float | None = None
    description: str = Field(default='', max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=30)


def https_url(value):
    parts = urlsplit(value)
    return parts.scheme == 'https' and bool(parts.hostname) and not parts.username and not parts.password


def upsert_many(records):
    added = updated = skipped = 0
    stamp = now().isoformat(timespec='seconds')
    with db() as conn:
        for record in records:
            try:
                item = Listing.model_validate(record)
                if not (MIN_PRICE <= item.price <= MAX_PRICE) or item.city not in ALLOWED_CITIES or item.property_type not in ALLOWED_TYPES or not https_url(item.url):
                    skipped += 1
                    continue
                images = [s for s in item.images if https_url(s)]
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
            except Exception:
                skipped += 1
    return {'added':added,'updated':updated,'skipped':skipped}


def read_feed_file(path):
    """Only import explicitly provided/permitted JSON; no bypass of site restrictions."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return data['listings'] if isinstance(data, dict) else data


def collect():
    """Scheduled local collector hook. Drop a permitted source export in feeds/ as JSON."""
    folder = ROOT / 'feeds'
    files = sorted(folder.glob('*.json')) if folder.exists() else []
    outcome = {'added':0,'updated':0,'skipped':0,'sources':len(files)}
    for feed in files:
        started = now().isoformat(timespec='seconds')
        try:
            items = read_feed_file(feed)
            result = upsert_many(items)
            for k in ('added','updated','skipped'):
                outcome[k] += result[k]
            with db() as conn:
                conn.execute('INSERT INTO runs(start_time,finish_time,source,added,updated,status) VALUES (?,?,?,?,?,?)',
                             (started,now().isoformat(timespec='seconds'),feed.name,result['added'],result['updated'],'ok'))
        except Exception as exc:
            with db() as conn:
                conn.execute('INSERT INTO runs(start_time,finish_time,source,status,details) VALUES (?,?,?,?,?)',
                             (started,now().isoformat(timespec='seconds'),feed.name,'error',str(exc)[:500]))
    return outcome


def previous_day_bounds(reference=None):
    ref = reference or now()
    yesterday = ref.date() - timedelta(days=1)
    return yesterday, datetime.combine(yesterday,datetime.min.time(),TZ).isoformat(), datetime.combine(ref.date(),datetime.min.time(),TZ).isoformat()


def send_digest(reference=None):
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
            if local.hour >= 8:
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
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/api/homes')
def homes(show: str = 'active', search: str = '', _: bool = Depends(credentials)):
    condition = '' if show == 'all' else 'WHERE hidden = 1' if show == 'hidden' else 'WHERE hidden = 0'
    with db() as conn:
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM homes {condition} ORDER BY favorite DESC, first_seen DESC, id DESC').fetchall()]
        stats = dict(conn.execute('SELECT count(*) total, sum(CASE WHEN hidden=0 THEN 1 ELSE 0 END) active, sum(CASE WHEN favorite=1 AND hidden=0 THEN 1 ELSE 0 END) saved FROM homes').fetchone())
        date, start, end = previous_day_bounds()
        stats['yesterday'] = conn.execute('SELECT count(*) FROM homes WHERE first_seen>=? AND first_seen<?',(start,end)).fetchone()[0]
    for r in rows:
        r['images'] = json.loads(r['images'])
    if search:
        q = search.casefold()
        rows = [r for r in rows if q in ' '.join(str(r.get(k) or '') for k in ('title','city','address','description','note')).casefold()]
    return {'items':rows,'stats':stats,'last_scan':last_scan()}


def last_scan():
    with db() as conn:
        row = conn.execute("SELECT start_time,finish_time,status,added FROM runs WHERE source='daily' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


class Edit(BaseModel):
    hidden: bool | None = None
    favorite: bool | None = None
    note: str | None = Field(default=None, max_length=10000)


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
    return {'ok':True}


@app.post('/api/import')
def ingest(payload: list[Listing], _:bool = Depends(credentials)):
    # Authenticated endpoint for permitted-source exports. Not intended for public ingestion.
    return upsert_many([x.model_dump() for x in payload])


@app.post('/api/scan')
def scan(_:bool = Depends(credentials)):
    return collect()


@app.get('/api/status')
def status(_:bool = Depends(credentials)):
    return {'last_scan':last_scan(), 'configured_feed_files':len(list((ROOT/'feeds').glob('*.json'))) if (ROOT/'feeds').exists() else 0, 'scheduler_enabled':os.getenv('ENABLE_SCHEDULER')=='1', 'site_configured':bool(os.getenv('SITE_URL'))}


@app.post('/api/digest-test')
def digest_test(_:bool = Depends(credentials)):
    # Real email; deliberately not exposed in UI; idempotent per previous day.
    try:
        return send_digest()
    except RuntimeError as ex:
        raise HTTPException(503,str(ex))
