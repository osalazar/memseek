"""Read every rule through the native view endpoints; print counts, never rule text or keys.

Uses only Python's standard library. Run against the native API origin, not an MCP-only gateway.
"""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PageChanged(RuntimeError):
    pass


def request(opener, url, key, path, data=None):
    body = None if data is None else json.dumps(data).encode()
    req = Request(url + path, data=body, headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with opener.open(req, timeout=180) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            code = json.load(exc).get('error')
        except (ValueError, AttributeError):
            code = None
        if code == 'response_too_large':
            raise RuntimeError('Response exceeds the server byte budget; retry with a smaller --limit') from None
        if code == 'page_changed':
            raise PageChanged('Rules changed during the read') from None
        raise RuntimeError(f'API returned HTTP {exc.code}; verify endpoint, token and runtime update') from None


def read_all(call, reference, arguments, limit):
    """Retry an interrupted enumeration from the start, never return partial data."""
    for attempt in range(3):
        try:
            ids, snapshot, total = set(), None, None
            offset, pages, rendered_truncated = 0, 0, False
            while True:
                args = {**arguments, 'limit': limit, 'offset': offset}
                if snapshot is not None:
                    args['snapshot'] = snapshot
                page = call('/views/' + quote(reference, safe='') + '/query', args)
                if snapshot is None:
                    snapshot, total = page['snapshot'], page['total_count']
                if page['snapshot'] != snapshot or page['total_count'] != total:
                    raise PageChanged('Snapshot changed')
                batch = [hit['id'] for hit in page['hits']]
                if page['offset'] != offset or page['count'] != len(batch):
                    raise RuntimeError('Invalid page accounting')
                if len(set(batch)) != len(batch) or ids.intersection(batch):
                    raise RuntimeError('Duplicate records across pages')
                ids.update(batch)
                pages += 1
                rendered_truncated |= page.get('rendered_truncated', False)
                if not page['has_more']:
                    if page['next_offset'] is not None or len(ids) != total:
                        raise RuntimeError('Incomplete enumeration; do not act on these rules')
                    return dict(total_count=total, unique_records=len(ids), pages=pages,
                                complete=True, snapshot=snapshot,
                                rendered_truncated=rendered_truncated)
                if not batch or page['next_offset'] != offset + len(batch):
                    raise RuntimeError('Invalid continuation; refusing a partial read')
                offset = page['next_offset']
        except PageChanged:
            if attempt == 2:
                raise RuntimeError('Rules kept changing; retry when writes settle') from None
    raise RuntimeError('Enumeration failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('MEMSEEK_URL', 'http://127.0.0.1:8120'))
    parser.add_argument('--owner', default=os.environ.get('MEMSEEK_OWNER', 'owner'))
    parser.add_argument('--key-file', default=os.environ.get('MEMSEEK_KEY_FILE', '.state/api_key'))
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.username or target.password or target.query or target.fragment or target.path not in ('', '/'):
        raise SystemExit('Use the native API origin without credentials, query parameters or /mcp')
    if target.scheme != 'https' and not (target.scheme == 'http' and target.hostname in ('localhost', '127.0.0.1', '::1', 'api')):
        raise SystemExit('Use HTTPS for a remote API origin')
    if not 1 <= args.limit <= 1000:
        raise SystemExit('--limit must be between 1 and 1000')
    key = os.environ.get('MEMSEEK_API_KEY') or Path(args.key_file).read_text().strip()
    opener = build_opener(NoRedirect())
    def call(path, data=None):
        return request(opener, args.url.rstrip('/'), key, path, data)
    tools = {tool['name']: tool for tool in call('/tools')['tools']}
    now = int(time.time())
    report = {}
    for name in ('hard_rules', 'preferences'):
        tool = tools[name]
        if not {'limit', 'offset', 'snapshot'} <= tool['input_schema']['properties'].keys():
            raise SystemExit(f'{name} does not expose pagination. Restart the patched API and refresh client tool discovery.')
        arguments = {'owner': args.owner, **({'now': now} if name == 'hard_rules' else {})}
        report[name] = read_all(call, tool['binding']['reference'], arguments, args.limit)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
