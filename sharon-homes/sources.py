"""Explicitly configured, authorized JSON and Apify actor sources.

Actor schemas vary: field maps translate actor output to the Listing contract.
No actors or paid requests are enabled by default.
"""
import json
import os
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

MAX_BYTES = 10 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward bearer credentials to a redirected host.


def request_json(url, payload=None, token=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Sources require HTTPS URLs without embedded credentials')
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers), timeout=120) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Source response too large')
    result = json.loads(raw)
    records = result.get('listings') if isinstance(result, dict) else result
    if not isinstance(records, list):
        raise ValueError('Expected listing array or listings object')
    return records


def apify_records(source):
    prefix = 'APIFY_' + source.upper()
    actor = os.environ[prefix + '_ACTOR']
    payload = json.loads(os.getenv(prefix + '_INPUT', '{}'))
    mapping = json.loads(os.getenv(prefix + '_FIELD_MAP', '{}'))
    if not isinstance(payload, dict) or not isinstance(mapping, dict):
        raise ValueError('Actor input and field map must be objects')
    records = request_json('https://api.apify.com/v2/acts/' + quote(actor.replace('/', '~'), safe='~') +
                           '/run-sync-get-dataset-items?timeout=90&clean=true', payload, os.environ['APIFY_TOKEN'])
    normalized = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Expected listing objects')
        item = dict(record)
        for target, path in mapping.items():
            value = record
            for part in path.split('.'):
                value = value[part]
            item[target] = value
        item['source'] = source
        if 'source_id' in item:
            item['source_id'] = str(item['source_id'])
        normalized.append(item)
    return normalized


def configured_sources():
    sources = []
    # Parse inside fetch callbacks so bad configuration is reported as a source failure.
    urls = os.getenv('AUTHORIZED_JSON_URLS', '').splitlines()
    for index, url in enumerate(u.strip() for u in urls if u.strip()):
        sources.append((f'json-{index + 1}', lambda url=url: request_json(url)))
    if os.getenv('APIFY_TOKEN'):
        for source in ('yad2', 'madlan'):
            if os.getenv('APIFY_' + source.upper() + '_ACTOR'):
                sources.append((source, lambda source=source: apify_records(source)))
    return sources
