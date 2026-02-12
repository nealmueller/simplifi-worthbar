#!/usr/bin/env python3
import argparse
import codecs
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HOME = Path.home()
APP_SUPPORT_DIR = HOME / 'Library' / 'Application Support' / 'SimplifiWorthBar'
CONTAINER = HOME / 'Library' / 'Containers' / 'com.app.menubarx' / 'Data'
WEBKIT_DEFAULT = CONTAINER / 'Library/WebKit/WebsiteData/Default'
STATE_FILE = APP_SUPPORT_DIR / 'last_label.txt'
TOKEN_CACHE_FILE = APP_SUPPORT_DIR / 'token_cache.json'
OAUTH_CONFIG_FILE = APP_SUPPORT_DIR / 'oauth_config.json'

SIMPLIFI_HOST = 'simplifi.quicken.com'
SIMPLIFI_BASE_URL = f'https://{SIMPLIFI_HOST}'
HTTP_TIMEOUT_SECONDS = 30


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _parse_iso_datetime(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        parsed = dt.datetime.fromisoformat(value)
        return parsed
    except ValueError:
        return None


def _parse_js_single_quoted_string(s: str, start_idx: int) -> Tuple[str, int]:
    """Return (raw_contents, end_quote_idx). start_idx points to the char after the opening quote."""
    out: list[str] = []
    i = start_idx

    while i < len(s):
        ch = s[i]
        if ch == "'":
            return ''.join(out), i
        if ch == '\\':
            # Preserve escapes for a later unicode_escape decode.
            if i + 1 < len(s):
                out.append(ch)
                out.append(s[i + 1])
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1

    raise ValueError('Unterminated JS string')


def _fetch_main_bundle_url() -> str:
    html = urlopen(SIMPLIFI_BASE_URL + '/', timeout=HTTP_TIMEOUT_SECONDS).read().decode('utf-8', errors='ignore')
    m = re.search(r'src="(/main\.[^"]+\.js)"', html)
    if not m:
        raise RuntimeError('Could not find main bundle script in Simplifi HTML')
    return SIMPLIFI_BASE_URL + m.group(1)


def _extract_oauth_config_from_main_bundle(js_text: str) -> Dict[str, Any]:
    # The bundle contains a webpack module with JSON.parse('<config>')
    marker = '451557:e=>'
    start = js_text.find(marker)
    if start < 0:
        raise RuntimeError('Could not find Simplifi config module in main bundle')

    parse_marker = "e.exports=JSON.parse('"
    start = js_text.find(parse_marker, start)
    if start < 0:
        raise RuntimeError('Could not find config JSON.parse marker in main bundle')

    start = start + len(parse_marker)
    raw, _ = _parse_js_single_quoted_string(js_text, start)
    decoded = codecs.decode(raw, 'unicode_escape')

    config = json.loads(decoded)
    if not isinstance(config, dict):
        raise RuntimeError('Simplifi config payload is not a JSON object')

    hosts = config.get('hosts') or {}
    envs = config.get('environments') or {}

    host_cfg = hosts.get(SIMPLIFI_HOST)
    if not isinstance(host_cfg, dict):
        raise RuntimeError(f'No host config for {SIMPLIFI_HOST}')

    env_key = host_cfg.get('environment_default')
    redirect_uri = host_cfg.get('redirect_uri')
    if not isinstance(env_key, str) or not env_key:
        raise RuntimeError('Host environment_default missing')
    if not isinstance(redirect_uri, str) or not redirect_uri:
        raise RuntimeError('Host redirect_uri missing')

    env_cfg = envs.get(env_key)
    if not isinstance(env_cfg, dict):
        raise RuntimeError(f'No environment config for {env_key}')

    services_url = env_cfg.get('services_url')
    client_id = env_cfg.get('client_id')
    client_secret = env_cfg.get('client_secret')

    if not isinstance(services_url, str) or not services_url.startswith('http'):
        raise RuntimeError('services_url missing/invalid')
    if not isinstance(client_id, str) or not client_id:
        raise RuntimeError('client_id missing/invalid')
    if not isinstance(client_secret, str) or not client_secret:
        raise RuntimeError('client_secret missing/invalid')

    return {
        'services_url': services_url,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'environment': env_key,
    }


def _load_oauth_config() -> Dict[str, str]:
    if OAUTH_CONFIG_FILE.exists():
        try:
            cached = json.loads(OAUTH_CONFIG_FILE.read_text())
            if isinstance(cached, dict) and cached.get('services_url') and cached.get('client_id') and cached.get('client_secret') and cached.get('redirect_uri'):
                return {
                    'services_url': str(cached['services_url']),
                    'client_id': str(cached['client_id']),
                    'client_secret': str(cached['client_secret']),
                    'redirect_uri': str(cached['redirect_uri']),
                }
        except Exception:
            pass

    main_url = _fetch_main_bundle_url()
    js_text = urlopen(main_url, timeout=HTTP_TIMEOUT_SECONDS).read().decode('utf-8', errors='ignore')
    extracted = _extract_oauth_config_from_main_bundle(js_text)

    payload = {
        **extracted,
        'fetched_at': _now_utc().isoformat(),
        'main_bundle_url': main_url,
    }

    OAUTH_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    OAUTH_CONFIG_FILE.write_text(json.dumps(payload, indent=2) + '\n')
    _chmod_600(OAUTH_CONFIG_FILE)

    return {
        'services_url': str(extracted['services_url']),
        'client_id': str(extracted['client_id']),
        'client_secret': str(extracted['client_secret']),
        'redirect_uri': str(extracted['redirect_uri']),
    }


def _find_simplifi_origin_dir() -> Path:
    if not WEBKIT_DEFAULT.exists():
        raise FileNotFoundError(f'MenubarX WebKit storage missing: {WEBKIT_DEFAULT}')

    for origin_file in WEBKIT_DEFAULT.glob('*/*/origin'):
        try:
            origin_data = origin_file.read_bytes()
        except OSError:
            continue
        if SIMPLIFI_HOST.encode('utf-8') in origin_data:
            return origin_file.parent

    raise RuntimeError(f'Could not locate MenubarX origin directory for {SIMPLIFI_HOST}')


def _load_auth_session() -> Dict[str, Any]:
    origin_dir = _find_simplifi_origin_dir()
    db_path = origin_dir / 'LocalStorage' / 'localstorage.sqlite3'
    if not db_path.exists():
        raise FileNotFoundError(f'MenubarX LocalStorage DB missing: {db_path}')

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key='authSession'")
        row = cur.fetchone()
        if not row:
            raise RuntimeError('authSession not found in MenubarX LocalStorage')

        blob = row[0]
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)):
            raise RuntimeError('authSession value is not a blob')

        text = bytes(blob).decode('utf-16le', errors='ignore')
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise RuntimeError('authSession is not a JSON object')
        return obj
    finally:
        conn.close()


def _load_cached_access_token(refresh_token: str) -> Optional[Tuple[str, dt.datetime]]:
    if not TOKEN_CACHE_FILE.exists():
        return None

    try:
        cached = json.loads(TOKEN_CACHE_FILE.read_text())
    except Exception:
        return None

    if not isinstance(cached, dict):
        return None

    if cached.get('refreshToken') != refresh_token:
        return None

    access = cached.get('accessToken')
    exp = _parse_iso_datetime(cached.get('accessTokenExpired'))
    if not isinstance(access, str) or not access or exp is None:
        return None

    return access, exp


def _write_token_cache(refresh_token: str, token_obj: Dict[str, Any]) -> None:
    payload = {
        'refreshToken': refresh_token,
        'accessToken': token_obj.get('accessToken'),
        'accessTokenExpired': token_obj.get('accessTokenExpired'),
        'updated_at': _now_utc().isoformat(),
    }
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_FILE.write_text(json.dumps(payload, indent=2) + '\n')
    _chmod_600(TOKEN_CACHE_FILE)


def _refresh_access_token(refresh_token: str, oauth_cfg: Dict[str, str]) -> Dict[str, Any]:
    url = oauth_cfg['services_url'].rstrip('/') + '/oauth/token'
    payload = {
        'clientId': oauth_cfg['client_id'],
        'clientSecret': oauth_cfg['client_secret'],
        'grantType': 'refreshToken',
        'responseType': 'token',
        'redirectUri': oauth_cfg['redirect_uri'],
        'refreshToken': refresh_token,
    }

    req = Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )

    with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read()

    obj = json.loads(body)
    if not isinstance(obj, dict) or not obj.get('accessToken'):
        raise RuntimeError('Refresh token exchange did not return accessToken')

    return obj


def _api_get_json(services_url: str, path: str, access_token: str, dataset_id: str) -> Dict[str, Any]:
    url = services_url.rstrip('/') + path
    req = Request(
        url,
        headers={
            'Authorization': f'Bearer {access_token}',
            'qcs-dataset-id': str(dataset_id),
            'Accept': 'application/json',
        },
        method='GET',
    )

    with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read()

    obj = json.loads(body)
    if not isinstance(obj, dict):
        raise RuntimeError(f'Unexpected response type for {path}')

    return obj


# ----- MenubarX cache fallback -----

def clean_key(value) -> str:
    if isinstance(value, bytes):
        raw = value.decode('utf-8', errors='ignore')
    else:
        raw = str(value)
    return raw.replace('\x00', '')


def decode_payload(blob: bytes):
    for offset in (9, 10, 8, 0):
        payload = blob[offset:]
        if payload[:1] == b'\x80':
            payload = payload[1:]
        if payload[:1] not in (b'{', b'['):
            continue
        try:
            return json.loads(payload.decode('utf-8'))
        except json.JSONDecodeError:
            continue
    raise ValueError('Unable to decode Simplifi cache payload')


def find_blob_by_store_name(store_name: str) -> bytes:
    if not WEBKIT_DEFAULT.exists():
        raise FileNotFoundError(f'MenubarX WebKit storage missing: {WEBKIT_DEFAULT}')

    for origin_file in WEBKIT_DEFAULT.glob('*/*/origin'):
        try:
            origin_data = origin_file.read_bytes()
        except OSError:
            continue

        if SIMPLIFI_HOST.encode('utf-8') not in origin_data:
            continue

        indexeddb_root = origin_file.parent / 'IndexedDB'
        if not indexeddb_root.exists():
            continue

        for db_path in indexeddb_root.glob('*/IndexedDB.sqlite3'):
            try:
                conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
                cur = conn.cursor()
                cur.execute('SELECT key, value FROM Records')
                for key_obj, value_blob in cur.fetchall():
                    key = clean_key(key_obj)
                    if store_name in key and isinstance(value_blob, (bytes, bytearray)):
                        return bytes(value_blob)
            except sqlite3.Error:
                continue
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    raise RuntimeError(f'Could not find {store_name} in MenubarX cache')


def to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def compute_total(accounts_obj) -> float:
    resources = accounts_obj.get('data', {}).get('resourcesById', {})
    total = 0.0

    for rec in resources.values():
        if not isinstance(rec, dict):
            continue

        if rec.get('isDeleted') is True or rec.get('isIgnored') is True or rec.get('isClosed') is True:
            continue

        balance = (
            to_number(rec.get('normalizedBalance'))
            or to_number(rec.get('currentBalanceAsOf'))
            or to_number(rec.get('onlineBalance'))
            or to_number(rec.get('balanceAsOf'))
            or 0.0
        )
        total += balance

    return total


def compute_daily_percent(history_obj) -> float:
    rows = history_obj.get('data', {}).get('rows', [])
    totals_by_date = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        for cell in row.get('cellData', []):
            if not isinstance(cell, dict):
                continue
            date = cell.get('date')
            value = to_number(cell.get('value'))
            if not date or value is None:
                continue
            totals_by_date[date] = totals_by_date.get(date, 0.0) + value

    if len(totals_by_date) < 2:
        return 0.0

    dates = sorted(totals_by_date.keys())
    yesterday_total = totals_by_date[dates[-2]]
    today_total = totals_by_date[dates[-1]]

    if abs(yesterday_total) < 1e-9:
        return 0.0

    return ((today_total - yesterday_total) / abs(yesterday_total)) * 100.0


def format_compact_usd(value: float) -> str:
    sign = '-' if value < 0 else ''
    absolute = abs(value)

    if absolute >= 1_000_000_000_000:
        shown, suffix = absolute / 1_000_000_000_000, 'T'
    elif absolute >= 1_000_000_000:
        shown, suffix = absolute / 1_000_000_000, 'B'
    elif absolute >= 1_000_000:
        shown, suffix = absolute / 1_000_000, 'M'
    elif absolute >= 1_000:
        shown, suffix = absolute / 1_000, 'K'
    else:
        shown, suffix = absolute, ''

    if suffix:
        compact = f'{shown:.1f}'.rstrip('0').rstrip('.') + suffix
    else:
        compact = f'{shown:.0f}'

    return f'{sign}${compact}'


def format_rounded_percent(value: float) -> str:
    rounded = int(round(value))
    sign = '+' if rounded >= 0 else ''
    return f'{sign}{rounded}%'


def compute_cache_networth_label() -> str:
    accounts_blob = find_blob_by_store_name('accountsStore')
    history_blob = find_blob_by_store_name('accountsBalancesHistoryStore')

    accounts_payload = decode_payload(accounts_blob)
    history_payload = decode_payload(history_blob)

    networth_text = format_compact_usd(compute_total(accounts_payload))
    daily_percent_text = format_rounded_percent(compute_daily_percent(history_payload))
    return f'{networth_text} {daily_percent_text}'


def compute_cache_snapshot() -> Dict[str, Any]:
    accounts_blob = find_blob_by_store_name('accountsStore')
    history_blob = find_blob_by_store_name('accountsBalancesHistoryStore')
    accounts_payload = decode_payload(accounts_blob)
    history_payload = decode_payload(history_blob)
    return {
        'total': compute_total(accounts_payload),
        'daily_percent': compute_daily_percent(history_payload),
        'source': 'cache',
    }


# ----- Live API path -----

def compute_live_networth_label() -> str:
    auth = _load_auth_session()

    refresh_token = auth.get('refreshToken')
    dataset_id = auth.get('datasetId')
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError('Missing refreshToken in authSession (are you logged in?)')
    if dataset_id is None:
        raise RuntimeError('Missing datasetId in authSession')

    oauth_cfg = _load_oauth_config()
    now = _now_utc()

    access_token: Optional[str] = None

    cached = _load_cached_access_token(refresh_token)
    if cached:
        cached_access, cached_exp = cached
        if cached_exp - now > dt.timedelta(minutes=2):
            access_token = cached_access

    if access_token is None:
        sess_access = auth.get('accessToken')
        sess_exp = _parse_iso_datetime(auth.get('accessTokenExpired'))
        if isinstance(sess_access, str) and sess_access and sess_exp and (sess_exp - now > dt.timedelta(minutes=2)):
            access_token = sess_access

    if access_token is None:
        token_obj = _refresh_access_token(refresh_token, oauth_cfg)
        access_token = str(token_obj['accessToken'])
        _write_token_cache(refresh_token, token_obj)

    services_url = oauth_cfg['services_url']

    try:
        accounts_obj = _api_get_json(services_url, '/accounts', access_token, str(dataset_id))
        balances_obj = _api_get_json(services_url, '/accounts/balances', access_token, str(dataset_id))
    except HTTPError as e:
        if e.code != 401:
            raise
        token_obj = _refresh_access_token(refresh_token, oauth_cfg)
        access_token = str(token_obj['accessToken'])
        _write_token_cache(refresh_token, token_obj)
        accounts_obj = _api_get_json(services_url, '/accounts', access_token, str(dataset_id))
        balances_obj = _api_get_json(services_url, '/accounts/balances', access_token, str(dataset_id))

    accounts = accounts_obj.get('resources')
    balances = balances_obj.get('resources')

    if not isinstance(accounts, list) or not isinstance(balances, list):
        raise RuntimeError('Unexpected accounts/balances response shape')

    preferred: Dict[str, str] = {}
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        if acct.get('isDeleted') is True or acct.get('isIgnored') is True or acct.get('isClosed') is True:
            continue

        account_id = acct.get('id')
        if account_id is None:
            continue

        # ONLINE for connected accounts, CURRENT for manual/offline.
        is_online = (
            isinstance(acct.get('onlineBalance'), (int, float))
            or acct.get('isConnected') is True
            or bool(acct.get('institutionLoginId'))
        )
        preferred[str(account_id)] = 'ONLINE' if is_online else 'CURRENT'

    totals_by_date: Dict[str, Dict[str, Any]] = {}
    for bal in balances:
        if not isinstance(bal, dict):
            continue

        account_id = bal.get('accountId')
        date = bal.get('balanceOn')
        btype = bal.get('balanceType')
        amount = bal.get('balanceAmount')

        if account_id is None or not isinstance(date, str) or not date:
            continue

        pref = preferred.get(str(account_id))
        if not pref or btype != pref:
            continue

        if not isinstance(amount, (int, float)):
            continue

        entry = totals_by_date.setdefault(date, {'total': 0.0, 'count': 0})
        entry['total'] = float(entry['total']) + float(amount)
        entry['count'] = int(entry['count']) + 1

    if not totals_by_date:
        raise RuntimeError('No balance totals available')

    max_count = max(int(v.get('count', 0)) for v in totals_by_date.values())
    complete_dates = sorted([d for d, v in totals_by_date.items() if int(v.get('count', 0)) == max_count])
    all_dates = sorted(totals_by_date.keys())

    latest_date = complete_dates[-1] if complete_dates else all_dates[-1]

    prev_date: Optional[str] = None
    if complete_dates and len(complete_dates) >= 2:
        prev_date = complete_dates[-2]
    elif len(all_dates) >= 2:
        prev_date = all_dates[-2]

    latest_total = float(totals_by_date[latest_date]['total'])
    percent = 0.0

    if prev_date:
        prev_total = float(totals_by_date[prev_date]['total'])
        if abs(prev_total) > 1e-9:
            percent = ((latest_total - prev_total) / abs(prev_total)) * 100.0

    networth_text = format_compact_usd(latest_total)
    daily_percent_text = format_rounded_percent(percent)
    return f'{networth_text} {daily_percent_text}'


def compute_live_snapshot() -> Dict[str, Any]:
    auth = _load_auth_session()

    refresh_token = auth.get('refreshToken')
    dataset_id = auth.get('datasetId')
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError('Missing refreshToken in authSession (are you logged in?)')
    if dataset_id is None:
        raise RuntimeError('Missing datasetId in authSession')

    oauth_cfg = _load_oauth_config()
    now = _now_utc()

    access_token: Optional[str] = None

    cached = _load_cached_access_token(refresh_token)
    if cached:
        cached_access, cached_exp = cached
        if cached_exp - now > dt.timedelta(minutes=2):
            access_token = cached_access

    if access_token is None:
        sess_access = auth.get('accessToken')
        sess_exp = _parse_iso_datetime(auth.get('accessTokenExpired'))
        if isinstance(sess_access, str) and sess_access and sess_exp and (sess_exp - now > dt.timedelta(minutes=2)):
            access_token = sess_access

    if access_token is None:
        token_obj = _refresh_access_token(refresh_token, oauth_cfg)
        access_token = str(token_obj['accessToken'])
        _write_token_cache(refresh_token, token_obj)

    services_url = oauth_cfg['services_url']

    try:
        accounts_obj = _api_get_json(services_url, '/accounts', access_token, str(dataset_id))
        balances_obj = _api_get_json(services_url, '/accounts/balances', access_token, str(dataset_id))
    except HTTPError as e:
        if e.code != 401:
            raise
        token_obj = _refresh_access_token(refresh_token, oauth_cfg)
        access_token = str(token_obj['accessToken'])
        _write_token_cache(refresh_token, token_obj)
        accounts_obj = _api_get_json(services_url, '/accounts', access_token, str(dataset_id))
        balances_obj = _api_get_json(services_url, '/accounts/balances', access_token, str(dataset_id))

    accounts = accounts_obj.get('resources')
    balances = balances_obj.get('resources')

    if not isinstance(accounts, list) or not isinstance(balances, list):
        raise RuntimeError('Unexpected accounts/balances response shape')

    preferred: Dict[str, str] = {}
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        if acct.get('isDeleted') is True or acct.get('isIgnored') is True or acct.get('isClosed') is True:
            continue

        account_id = acct.get('id')
        if account_id is None:
            continue

        is_online = (
            isinstance(acct.get('onlineBalance'), (int, float))
            or acct.get('isConnected') is True
            or bool(acct.get('institutionLoginId'))
        )
        preferred[str(account_id)] = 'ONLINE' if is_online else 'CURRENT'

    totals_by_date: Dict[str, Dict[str, Any]] = {}
    for bal in balances:
        if not isinstance(bal, dict):
            continue

        account_id = bal.get('accountId')
        date = bal.get('balanceOn')
        btype = bal.get('balanceType')
        amount = bal.get('balanceAmount')

        if account_id is None or not isinstance(date, str) or not date:
            continue

        pref = preferred.get(str(account_id))
        if not pref or btype != pref:
            continue

        if not isinstance(amount, (int, float)):
            continue

        entry = totals_by_date.setdefault(date, {'total': 0.0, 'count': 0})
        entry['total'] = float(entry['total']) + float(amount)
        entry['count'] = int(entry['count']) + 1

    if not totals_by_date:
        raise RuntimeError('No balance totals available')

    max_count = max(int(v.get('count', 0)) for v in totals_by_date.values())
    complete_dates = sorted([d for d, v in totals_by_date.items() if int(v.get('count', 0)) == max_count])
    all_dates = sorted(totals_by_date.keys())

    latest_date = complete_dates[-1] if complete_dates else all_dates[-1]

    prev_date: Optional[str] = None
    if complete_dates and len(complete_dates) >= 2:
        prev_date = complete_dates[-2]
    elif len(all_dates) >= 2:
        prev_date = all_dates[-2]

    latest_total = float(totals_by_date[latest_date]['total'])
    percent = 0.0

    if prev_date:
        prev_total = float(totals_by_date[prev_date]['total'])
        if abs(prev_total) > 1e-9:
            percent = ((latest_total - prev_total) / abs(prev_total)) * 100.0

    return {
        'total': latest_total,
        'daily_percent': percent,
        'source': 'live',
    }


def classify_error(exc: Exception) -> str:
    message = str(exc).lower()
    if (
        'authsession' in message
        or 'logged in' in message
        or 'refreshtoken' in message
        or 'datasetid' in message
    ):
        return 'signin_required'
    return 'unavailable'


def fetch_snapshot() -> Dict[str, Any]:
    live_error: Optional[Exception] = None
    try:
        return compute_live_snapshot()
    except Exception as e:
        live_error = e

    try:
        return compute_cache_snapshot()
    except Exception as cache_error:
        code = classify_error(live_error or cache_error)
        message = str(live_error or cache_error)
        raise RuntimeError(f'{code}:{message}') from cache_error


def compact_label(snapshot: Dict[str, Any]) -> str:
    return f"{format_compact_usd(float(snapshot['total']))} {format_rounded_percent(float(snapshot['daily_percent']))}"


def diagnostics_payload() -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        'webkits_default_exists': WEBKIT_DEFAULT.exists(),
        'container_exists': CONTAINER.exists(),
        'state_file_exists': STATE_FILE.exists(),
        'token_cache_exists': TOKEN_CACHE_FILE.exists(),
        'oauth_config_exists': OAUTH_CONFIG_FILE.exists(),
    }
    try:
        snap = fetch_snapshot()
        payload['snapshot_ok'] = True
        payload['source'] = snap['source']
        payload['total'] = round(float(snap['total']), 2)
        payload['daily_percent'] = round(float(snap['daily_percent']), 4)
    except Exception as e:
        payload['snapshot_ok'] = False
        msg = str(e)
        if ':' in msg:
            code, rest = msg.split(':', 1)
            payload['error_code'] = code
            payload['error'] = rest
        else:
            payload['error_code'] = 'unavailable'
            payload['error'] = msg
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--diagnostics', action='store_true')
    args, _ = parser.parse_known_args()

    if args.diagnostics:
        print(json.dumps(diagnostics_payload(), indent=2, sort_keys=True))
        return 0

    try:
        snapshot = fetch_snapshot()
        label = compact_label(snapshot)

        if args.json:
            print(json.dumps({
                'ok': True,
                'source': snapshot['source'],
                'total': float(snapshot['total']),
                'daily_percent': float(snapshot['daily_percent']),
                'label': label,
            }))
        else:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(label + '\n')
            print(label)
        return 0
    except Exception as e:
        msg = str(e)
        code = 'unavailable'
        text = msg
        if ':' in msg:
            code, text = msg.split(':', 1)

        if args.json:
            print(json.dumps({
                'ok': False,
                'error_code': code,
                'message': text,
            }))
            return 1

        if STATE_FILE.exists():
            print(STATE_FILE.read_text().strip())
            return 0
        if code == 'signin_required':
            print('Sign In')
            return 0
        print('$--')
        return 1


if __name__ == '__main__':
    sys.exit(main())
