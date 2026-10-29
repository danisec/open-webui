"""Final proof, run inside the container: direct vs proxied, same key, same host.

`/verify` already returned models through the proxy while this host is IP-banned
directly, which is strong evidence. This makes the comparison explicit and also
prints the exit IP, so the tunnel is demonstrated rather than inferred.
"""
import asyncio
import json
import sys

sys.path.insert(0, '/app/backend')

import aiohttp
from open_webui.utils.session_pool import close_session, get_session

KEY = 'tb_4TXACPj5MR7wC9pT'
URL = 'https://api.sakoo.biz.id/v1/models'
PROXY = 'socks5://ibx1:D8zFWhTPfFUTzW5GyNG7qGyF4SL4@100.106.8.115:1080'


async def probe(label, proxy):
    session = await get_session(proxy)
    try:
        async with session.get(
            URL,
            headers={'Authorization': f'Bearer {KEY}'},
            timeout=aiohttp.ClientTimeout(total=45),
        ) as r:
            body = await r.text()
            try:
                data = json.loads(body)
                if isinstance(data, dict) and 'data' in data:
                    ids = [m.get('id') for m in data['data']]
                    print(f'  {label:8s} HTTP {r.status}  models={ids}')
                else:
                    code = (data.get('error') or {}).get('code')
                    print(f'  {label:8s} HTTP {r.status}  code={code}')
            except Exception:
                print(f'  {label:8s} HTTP {r.status}  {body[:120]}')
    except Exception as e:
        print(f'  {label:8s} failed {type(e).__name__}: {str(e)[:120]}')


async def exit_ip(label, proxy):
    session = await get_session(proxy)
    try:
        async with session.get(
            'https://api.ipify.org', timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            print(f'  {label:8s} exit IP {await r.text()}')
    except Exception as e:
        print(f'  {label:8s} exit IP failed {type(e).__name__}')


async def main():
    print('=== GET /v1/models on api.sakoo.biz.id (this host is IP-banned) ===')
    await probe('direct', None)
    await probe('proxied', PROXY)

    print('\n=== exit IP through the pool ===')
    await exit_ip('direct', None)
    await exit_ip('proxied', PROXY)

    print('\n=== pool state ===')
    from open_webui.utils import session_pool

    for key in session_pool._sessions:
        print(f'  cached session: {key or "direct"}')

    await close_session()


asyncio.run(main())
