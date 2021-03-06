import re
import socket
import asyncio
import warnings
from math import sqrt
from html import unescape
from base64 import b64decode
from urllib.parse import unquote, urlparse

import aiohttp

from .errors import *
from .utils import (log, get_headers, resolve_host,
                    IPPattern, IPPortPatternGlobal)


warnings.simplefilter('always', DeprecationWarning)


class Provider:
    _sem = None
    _loop = None
    _timeout = 20
    _cookies = {}
    _headers = get_headers()
    _pattern = IPPortPatternGlobal
    # maximum of concurrent grab providers:
    _sem_providers = asyncio.Semaphore(3)
    _attemptsConnect = 3
    _cached_hosts = {}

    def __init__(self, url=None, proto=(), max_conn=4):
        if not proto:
            sep = '|'
            _type = ()
            if url and sep in url:
                proto, url = url.split(sep)
        if url:
            self.domain = urlparse(url).netloc
        self.url = url
        self.host = False
        self.port = 80
        self.addrInfo = {}
        self._proxies = set()
        if isinstance(proto, str):
            proto = tuple(proto.split(','))
        self.proto = proto
        # 4 concurrent connections on provider
        self._sem_provider = asyncio.Semaphore(max_conn)

    @property
    def proxies(self):
        return self._proxies

    @proxies.setter
    def proxies(self, new):
        new = [(host, port, self.proto) for host, port in new if port]
        self._proxies.update(new)

    async def get_proxies(self):
        with (await self._sem), (await self._sem_providers):
            log.info('Try to get proxies from %s...' % self.domain)
            try:
                self._start_new_session()
                await self._resolve_host()
                if not self.host:
                    return []
                await self._pipe()
            finally:
                self._session.close()
            log.info('%d proxies received from %s: %s' % (
                     len(self.proxies), self.domain, self.proxies))
            return self.proxies

    def _start_new_session(self):
        connector = aiohttp.TCPConnector(use_dns_cache=True, loop=self._loop)
        # This is a dirty hack. I know.
        connector._cached_hosts = self._cached_hosts
        self._session = aiohttp.ClientSession(
            connector=connector, headers=self._headers,
            cookies=self._cookies, loop=self._loop)

    async def _resolve_host(self):
        domain = self.domain.split('^')[0]
        for _ in range(self._attemptsConnect):
            with (await self._sem):
                self.host = await resolve_host(domain, 5, self._loop)
            if self.host:
                break
        if not self.host:
            log.warning('%s: Could not resolve host' % domain)
            return
        log.debug('%s: Host resolved' % domain)
        addrInfo = {
            'hostname': domain, 'host': self.host, 'port': self.port,
            'family': socket.AF_INET, 'proto': socket.IPPROTO_TCP,
            'flags': socket.AI_NUMERICHOST}
        self._cached_hosts[(domain, self.port)] = [addrInfo]

    async def _pipe(self):
        await self._find_on_page(self.url)

    async def _find_on_pages(self, urls):
        if not urls:
            return
        tasks = []
        if not isinstance(urls[0], dict):
            urls = set(urls)
        for url in urls:
            if isinstance(url, dict):
                tasks.append(self._find_on_page(**url))
            else:
                tasks.append(self._find_on_page(url))
        await asyncio.gather(*tasks)

    async def _find_on_page(self, url, data=None, headers=None, method='GET'):
        page = await self.get(url, data=data, headers=headers, method=method)
        oldcount = len(self.proxies)
        try:
            received = self.find_proxies(page)
        except Exception as e:
            received = []
            log.error('Error when executing find_proxies.'
                      'Domain: %s; Error: %s' % (self.domain, e))
        self.proxies = received
        added = len(self.proxies)-oldcount
        log.debug('%d(%d) proxies added(received) from %s' % (
            added, len(received), url))

    async def get(self, url, data=None, headers=None, method='GET'):
        for _ in range(self._attemptsConnect):
            page = await self._get(url, data=data, headers=headers, method=method)
            if page:
                break
        return page

    async def _get(self, url, data=None, headers=None, method='GET'):
        page = ''
        try:
            with (await self._sem),\
                 (await self._sem_provider),\
                 aiohttp.Timeout(self._timeout, loop=self._loop):
                async with self._session.request(
                    method, url, data=data, headers=headers) as resp:
                    if resp.status == 200:
                        page = await resp.text()
                    else:
                        error_page = await resp.text()
                        log.debug('Url: %s\nErr.Headers: %s\nErr.Cookies: '
                                  '%s\nErr.Page:\n%s' % (
                                  url, resp.headers, resp.cookies, error_page))
                        raise BadStatusError('Status: %s' % resp.status)
        except (UnicodeDecodeError, BadStatusError, asyncio.TimeoutError,
                aiohttp.ClientOSError, aiohttp.ClientResponseError,
                aiohttp.ServerDisconnectedError) as e:
            log.error('%s is failed. Error: %r;' % (url, e))
        return page

    def find_proxies(self, page):
        return self._find_proxies(page)

    def _find_proxies(self, page):
        proxies = self._pattern.findall(page)
        return proxies


class Freeproxylists_com(Provider):
    domain = 'freeproxylists.com'
    async def _pipe(self):
        exp = r'''href\s*=\s*['"](?P<t>[^'"]*)/(?P<uts>\d{10})[^'"]*['"]'''
        urls = ['http://www.freeproxylists.com/socks.html',
                'http://www.freeproxylists.com/elite.html',
                'http://www.freeproxylists.com/anonymous.html']
        pages = await asyncio.gather(*[self.get(url) for url in urls])
        params = re.findall(exp, ''.join(pages))
        tpl = 'http://www.freeproxylists.com/load_{}_{}.html'
        # example: http://www.freeproxylists.com/load_socks_1448724717.html
        urls = [tpl.format(t, uts) for t, uts in params]
        await self._find_on_pages(urls)


class Blogspot_com_base(Provider):
    _cookies = {'NCR': 1}
    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*\.\w+/\d{4}/\d{2}/[^'"#]*)['"]>'''
        pages = await asyncio.gather(*[
                        self.get('http://%s/' % d) for d in self.domains])
        urls = re.findall(exp, ''.join(pages))
        await self._find_on_pages(urls)


class Blogspot_com(Blogspot_com_base):
    domain = 'blogspot.com'
    domains = ['sslproxies24.blogspot.com', 'proxyserverlist-24.blogspot.com',
               'newfreshproxies24.blogspot.com', 'irc-proxies24.blogspot.com',
               'freeschoolproxy.blogspot.com', 'getdailyfreshproxy.blogspot.com',
               'googleproxies24.blogspot.com']


class Blogspot_com_socks(Blogspot_com_base):
    domain = 'blogspot.com^socks'
    domains = ['www.proxyocean.com', 'www.socks24.org']


class Webanetlabs_net(Provider):
    domain = 'webanetlabs.net'
    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]*proxylist_at_[^'"]*)['"]'''
        page = await self.get('http://webanetlabs.net/publ/24')
        urls = ['http://webanetlabs.net%s' % path
                 for path in re.findall(exp, page)]
        await self._find_on_pages(urls)


class Checkerproxy_net(Provider):
    domain = 'checkerproxy.net'
    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]?\d{2}-\d{2}-\d{4}[^'"]*)['"]'''
        page = await self.get('http://checkerproxy.net/')
        urls = ['http://checkerproxy.net%s' % path
                 for path in re.findall(exp, page)]
        await self._find_on_pages(urls)


class Proxz_com(Provider):
    domain = 'proxz.com'
    def find_proxies(self, page):
        return self._find_proxies(unquote(page))

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]?proxy_list_high_anonymous_[^'"]*)['"]'''
        url = 'http://www.proxz.com/proxy_list_high_anonymous_0.html'
        page = await self.get(url)
        urls = ['http://www.proxz.com/%s' % path
                 for path in re.findall(exp, page)]
        urls.append(url)
        await self._find_on_pages(urls)


class Proxy_list_org(Provider):
    domain = 'proxy-list.org'
    _pattern = re.compile(r'''Proxy\('([\w=]+)'\)''')
    def find_proxies(self, page):
        return [b64decode(hp).decode().split(':')
                for hp in self._find_proxies(page)]

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]\./([^'"]?index\.php\?p=\d+[^'"]*)['"]'''
        url = 'http://proxy-list.org/english/index.php?p=1'
        page = await self.get(url)
        urls = ['http://proxy-list.org/english/%s' % path
                 for path in re.findall(exp, page)]
        urls.append(url)
        await self._find_on_pages(urls)


class Aliveproxy_com(Provider):
    # more: http://www.aliveproxy.com/socks-list/socks5.aspx/United_States-us
    domain = 'aliveproxy.com'
    async def _pipe(self):
        paths = [
        'socks5-list', 'high-anonymity-proxy-list', 'anonymous-proxy-list',
        'fastest-proxies', 'us-proxy-list', 'gb-proxy-list', 'fr-proxy-list',
        'de-proxy-list', 'jp-proxy-list', 'ca-proxy-list', 'ru-proxy-list',
        'proxy-list-port-80', 'proxy-list-port-81', 'proxy-list-port-3128',
        'proxy-list-port-8000', 'proxy-list-port-8080']
        urls = ['http://www.aliveproxy.com/%s/' % path for path in paths]
        await self._find_on_pages(urls)


class Maxiproxies_com(Provider):
    domain = 'maxiproxies.com'
    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*example[^'"#]*)['"]>'''
        page = await self.get('http://maxiproxies.com/category/proxy-lists/')
        urls = re.findall(exp, page)
        await self._find_on_pages(urls)


class _50kproxies_com(Provider):
    domain = '50kproxies.com'
    _timeout = 20
    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*-proxy-list-[^'"#]*)['"]>'''
        page = await self.get('http://50kproxies.com/category/proxy-list/')
        urls = re.findall(exp, page)
        await self._find_on_pages(urls)


class Proxymore_com(Provider):
    domain = 'proxymore.com'
    async def _pipe(self):
        urls = ['http://www.proxymore.com/proxy-list-%d.html' % n
                for n in range(1, 56)]
        await self._find_on_pages(urls)


class Proxylist_me(Provider):
    domain = 'proxylist.me'
    async def _pipe(self):
        exp = r'''href\s*=\s*['"][^'"]*/proxys/index/(\d+)['"]'''
        page = await self.get('http://proxylist.me/')
        lastId = max([int(n) for n in re.findall(exp, page)])
        urls = ['http://proxylist.me/proxys/index/%d' %
                n for n in range(lastId, -20, -20)]
        await self._find_on_pages(urls)


class Foxtools_ru(Provider):
    domain = 'foxtools.ru'
    async def _pipe(self):
        urls = ['http://api.foxtools.ru/v2/Proxy.txt?page=%d' % n
                for n in range(1, 6)]
        await self._find_on_pages(urls)


class Gatherproxy_com(Provider):
    domain = 'gatherproxy.com'
    _pattern_h = re.compile(
        r'''(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))'''
        r'''(?=.*?(?:(?:(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))|'(?P<port>[\d\w]+)'))''',
        flags=re.DOTALL)
    def find_proxies(self, page):
        # if 'gp.dep' in page:
        #     proxies = self._pattern_h.findall(page)  # for http(s)
        #     proxies = [(host, str(int(port, 16))) for host, port in proxies if port]
        # else:
        #     proxies = self._find_proxies(page)  # for socks
        return [(host, str(int(port, 16)))
                for host, port in self._pattern_h.findall(page) if port]

    async def _pipe(self):
        url = 'http://www.gatherproxy.com/proxylist/anonymity/'
        expNumPages = r'href="#(\d+)"'
        method = 'POST'
        # hdrs = {'Content-Type': 'application/x-www-form-urlencoded'}
        urls = []
        for t in ['anonymous', 'elite']:
            data = {'Type': t, 'PageIdx': 1}
            page = await self.get(url, data=data, method=method)
            if not page:
                continue
            lastPageId = max([int(n) for n in re.findall(expNumPages, page)])
            urls = [{'url': url, 'data': {'Type': t, 'PageIdx': pid},
                     'method': method} for pid in range(1, lastPageId+1)]
        # urls.append({'url': 'http://www.gatherproxy.com/sockslist/',
        #              'method': method})
        await self._find_on_pages(urls)


class Gatherproxy_com_socks(Provider):
    domain = 'gatherproxy.com^socks'
    async def _pipe(self):
        urls = [{'url': 'http://www.gatherproxy.com/sockslist/',
                 'method': 'POST'}]
        await self._find_on_pages(urls)


class Tools_rosinstrument_com_base(Provider):
    # more: http://tools.rosinstrument.com/cgi-bin/
    #       sps.pl?pattern=month-1&max=50&nskip=0&file=proxlog.csv
    domain = 'tools.rosinstrument.com'
    sqrtPattern = re.compile(r'''sqrt\((\d+)\)''')
    bodyPattern = re.compile(r'''hideTxt\(\n*'(.*)'\);''')
    _pattern = re.compile(
        r'''(?:(?P<domainOrIP>(?:[a-z0-9\-.]+\.[a-z]{2,6})|'''
        r'''(?:(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'''
        r'''(?:25[0-5]|2[0-4]\d|[01]?\d\d?))))(?=.*?(?:(?:'''
        r'''[a-z0-9\-.]+\.[a-z]{2,6})|(?:(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)'''
        r'''\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))|(?P<port>\d{2,5})))''',
        flags=re.DOTALL)

    def find_proxies(self, page):
        x = self.sqrtPattern.findall(page)
        if not x:
            return []
        x = round(sqrt(float(x[0])))
        hiddenBody = self.bodyPattern.findall(page)[0]
        hiddenBody = unquote(hiddenBody)
        toCharCodes = [ord(char)^(x if i % 2 else 0)
                       for i, char in enumerate(hiddenBody)]
        fromCharCodes = ''.join([chr(n) for n in toCharCodes])
        page = unescape(fromCharCodes)
        return self._find_proxies(page)


class Tools_rosinstrument_com(Tools_rosinstrument_com_base):
    domain = 'tools.rosinstrument.com'
    async def _pipe(self):
        tpl = 'http://tools.rosinstrument.com/raw_free_db.htm?%d&t=%d'
        urls = [tpl % (pid, t) for pid in range(51) for t in range(1, 3)]
        await self._find_on_pages(urls)


class Tools_rosinstrument_com_socks(Tools_rosinstrument_com_base):
    domain = 'tools.rosinstrument.com^socks'
    async def _pipe(self):
        tpl = 'http://tools.rosinstrument.com/raw_free_db.htm?%d&t=3'
        urls = [tpl % pid for pid in range(51)]
        await self._find_on_pages(urls)


class Xseo_in(Provider):
    domain = 'xseo.in'
    charEqNum = {}
    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(""\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        await self._find_on_page(
            url='http://xseo.in/proxylist', data={'submit': 1}, method='POST')


class Nntime_com(Provider):
    domain = 'nntime.com'
    charEqNum = {}
    _pattern = re.compile(
        r'''\b(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'''
        r'''(?:25[0-5]|2[0-4]\d|[01]?\d\d?))(?=.*?(?:(?:(?:(?:25'''
        r'''[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)'''
        r''')|(?P<port>\d{2,5})))''',
        flags=re.DOTALL)
    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(":"\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        tpl = 'http://www.nntime.com/proxy-updated-{:02}.htm'
        urls = [tpl.format(n) for n in range(1, 31)]
        await self._find_on_pages(urls)


class Proxynova_com(Provider):
    domain = 'proxynova.com'
    async def _pipe(self):
        expCountries = r'"([a-z]{2})"'
        page = await self.get('http://www.proxynova.com/proxy-server-list/')
        tpl = 'http://www.proxynova.com/proxy-server-list/country-%s/'
        urls = [tpl % isoCode for isoCode in re.findall(expCountries, page)
                if isoCode != 'en']
        await self._find_on_pages(urls)


class Spys_ru(Provider):
    domain = 'spys.ru'
    charEqNum = {}
    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0].split('+')
        # ex: '+(i9w3m3^k1y5)+(g7g7g7^v2e5)+(d4r8o5^i9u1)+(y5c3e5^t0z6)'
        # => ['', '(i9w3m3^k1y5)', '(g7g7g7^v2e5)', '(d4r8o5^i9u1)', '(y5c3e5^t0z6)']
        # => ['i9w3m3', 'k1y5'] => int^int
        num = ''
        for numOfChars in chars[1:]: # first - is ''
            var1, var2 = numOfChars.strip('()').split('^')
            digit = self.charEqNum[var1]^self.charEqNum[var2]
            num += str(digit)
        return num

    def find_proxies(self, page):
        expPortOnJS = r'(?P<js_port_code>(?:\+\([a-z0-9^+]+\))+)'
        # expCharNum = r'\b(?P<char>[a-z\d]+)=(?P<num>[a-z\d\^]+);'
        expCharNum = r'[>;]{1}(?P<char>[a-z\d]{4,})=(?P<num>[a-z\d\^]+)'
        # self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        res = re.findall(expCharNum, page)
        for char, num in res:
            if '^' in num:
                digit, tochar = num.split('^')
                num = int(digit) ^ self.charEqNum[tochar]
            self.charEqNum[char] = int(num)
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        expSession = r"'([a-z0-9]{32})'"
        url = 'http://spys.ru/proxies/'
        page = await self.get(url)
        sessionId = re.findall(expSession, page)[0]
        data = {'xf0': sessionId, # session id
                'xpp': 3,         # 3 - 200 proxies on page
                'xf1': None}      # 1 = ANM & HIA; 3 = ANM; 4 = HIA
        method = 'POST'
        urls = [{'url': url, 'data': {**data, 'xf1': lvl},
                 'method': method} for lvl in [3, 4]]
        await self._find_on_pages(urls)
        # expCountries = r'>([A-Z]{2})<'
        # url = 'http://spys.ru/proxys/'
        # page = await self.get(url)
        # links = ['http://spys.ru/proxys/%s/' %
        #          isoCode for isoCode in re.findall(expCountries, page)]


class My_proxy_com(Provider):
    domain = 'my-proxy.com'
    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]?free-[^'"]*)['"]'''
        url = 'http://www.my-proxy.com/free-proxy-list.html'
        page = await self.get(url)
        urls = ['http://www.my-proxy.com/%s' % path
                 for path in re.findall(exp, page)]
        urls.append(url)
        await self._find_on_pages(urls)


class Free_proxy_cz(Provider):
    domain = 'free-proxy.cz'
    _pattern = re.compile(r'''decode\("([\w=]+)".*?\("([\w=]+)"\)''',
                          flags=re.DOTALL)
    def find_proxies(self, page):
        return [(b64decode(h).decode(), b64decode(p).decode())
                for h, p in self._find_proxies(page)]

    async def _pipe(self):
        tpl = 'http://free-proxy.cz/en/proxylist/main/date/%d'
        urls = [tpl % n for n in range(1, 15)]
        await self._find_on_pages(urls)
        # _urls = []
        # for url in urls:
        #     if len(_urls) == 15:
        #         await self._find_on_pages(_urls)
        #         print('sleeping on 61 sec')
        #         await asyncio.sleep(61)
        #         _urls = []
        #     _urls.append(url)
        # =========
        # expNumPages = r'href="/en/proxylist/main/(\d+)"'
        # page = await self.get('http://free-proxy.cz/en/')
        # if not page:
        #     return
        # lastPageId = max([int(n) for n in re.findall(expNumPages, page)])
        # tpl = 'http://free-proxy.cz/en/proxylist/main/date/%d'
        # urls = [tpl % pid for pid in range(1, lastPageId+1)]
        # _urls = []
        # for url in urls:
        #     if len(_urls) == 15:
        #         await self._find_on_pages(_urls)
        #         print('sleeping on 61 sec')
        #         await asyncio.sleep(61)
        #         _urls = []
        #     _urls.append(url)

class Proxyb_net(Provider):
    domain = 'proxyb.net'
    _port_pattern_b64 = re.compile(r"stats\('([\w=]+)'\)")
    _port_pattern = re.compile(r"':(\d+)'")
    def find_proxies(self, page):
        if not page:
            return []
        _hosts, _ports = page.split('","ports":"')
        hosts, ports = [], []
        for host in _hosts.split('<\/tr><tr>'):
            host = IPPattern.findall(host)
            if not host:
                continue
            hosts.append(host[0])
        ports = [self._port_pattern.findall(b64decode(port).decode())[0]
                 for port in self._port_pattern_b64.findall(_ports)]
        return [(host, port) for host, port in zip(hosts, ports)]

    async def _pipe(self):
        url = 'http://proxyb.net/ajax.php'
        method = 'POST'
        data = {'action': 'getProxy', 'p': 0,
                'page': '/anonimnye_proksi_besplatno.html'}
        hdrs = {'X-Requested-With': 'XMLHttpRequest'}
        urls = [{'url': url, 'data': {**data, 'p': p},
                 'method': method, 'headers': hdrs} for p in range(0, 151)]
        await self._find_on_pages(urls)


class Proxylistplus_com(Provider):
    domain = 'list.proxylistplus.com'
    async def _pipe(self):
        urls = ['http://list.proxylistplus.com/Fresh-HTTP-Proxy-List-%d' % n
                for n in range(1, 7)]
        await self._find_on_pages(urls)


class ProxyProvider(Provider):
    def __init__(self, *args, **kwargs):
        warnings.warn('`ProxyProvider` is deprecated, use `Provider` instead.',
                      DeprecationWarning)
        super().__init__(*args, **kwargs)


providersList = [
    Provider(url='https://getproxy.net/en/', proto=('HTTP', 'HTTPS')),                    # 25/14
    Provider(url='http://www.proxylists.net/', proto=('HTTP', 'HTTPS')),                  # 46/26
    Provider(url='http://ipaddress.com/proxy-list/', proto=('HTTP', 'HTTPS')),            # 53/35
    Provider(url='http://www.sslproxies.org/', proto=('HTTP', 'HTTPS')),                  # 100/82
    Provider(url='http://2-proxy.com/proxylist?sort=last'
                 '&order=DESC&maxtime=30000&perpage=1000', proto=('HTTP', 'HTTPS')),      # 109/46
    Provider(url='http://marcosbl.com/lab/proxies/', proto=('HTTP', 'HTTPS')),            # 152/99
    Provider(url='https://freshfreeproxylist.wordpress.com/', proto=('HTTP', 'HTTPS')),   # 178/119
    Provider(url='http://proxytime.ru/http', proto=('HTTP', 'HTTPS')),                    # 281/202
    Provider(url='http://free-proxy-list.net/', proto=('HTTP', 'HTTPS')),                 # 300/220
    Provider(url='http://www.proxyservers.eu/', proto=('HTTP', 'HTTPS')),                 # 1785/627
    Provider(url='http://socks24.ru/proxy/httpProxies.txt', proto=('HTTP', 'HTTPS')),     # 3456/505
    Provider(url='http://fineproxy.org/eng/?p=6', proto=('HTTP', 'HTTPS')),               # 3647/661
    Provider(url='http://www.socks-proxy.net/', proto=('SOCKS4', 'SOCKS5')),              # 80/53
    Provider(url='http://www.cybersyndrome.net/pla.html', proto=('HTTP', 'HTTPS')),       # 2966/262
    Proxy_list_org(proto=('HTTP', 'HTTPS')),                  # 140/87
    Xseo_in(proto=('HTTP', 'HTTPS')),                         # 252/113
    Spys_ru(proto=('HTTP', 'HTTPS')),                         # 693/238
    Proxylistplus_com(proto=('HTTP', 'HTTPS')),               # 300/229
    Proxyb_net(proto=('HTTP', 'HTTPS')),                      # 4309/4113
    Proxz_com(proto=('HTTP', 'HTTPS'), max_conn=2),           # 3800/3486
    Proxymore_com(proto=('HTTP', 'HTTPS')),                   # 1356/780
    Proxylist_me(proto=('HTTP', 'HTTPS')),                    # 1078/587
    Foxtools_ru(proto=('HTTP', 'HTTPS'), max_conn=1),         # 500/187
    Gatherproxy_com(proto=('HTTP', 'HTTPS')),                 # 4800/1283
    Nntime_com(proto=('HTTP', 'HTTPS')),                      # 1050/582
    Proxynova_com(proto=('HTTP', 'HTTPS')),                   # 1229/878
    Blogspot_com(proto=('HTTP', 'HTTPS')),                    # 13570/?
    Gatherproxy_com_socks(proto=('SOCKS4', 'SOCKS5')),        # 30/26
    Blogspot_com_socks(proto=('SOCKS4', 'SOCKS5')),           # 7921/548
    Provider(url='http://codediaries.com/list.php', proto=('HTTP', 'HTTPS')),             # 75/22
    Provider(url='http://httptunnel.ge/ProxyListForFree.aspx', proto=('HTTP', 'HTTPS')),  # 100/48
    Provider(url='http://txt.proxyspy.net/proxy.txt', proto=('HTTP', 'HTTPS')),           # 300/176
    Provider(url='http://www.ip-adress.com/proxy_list/?k=time', proto=('HTTP', 'HTTPS')), # 57/40
    Provider(url='http://myproxylists.com/free-proxy-list', proto=('HTTP', 'HTTPS')),     # 6/3
    Provider(url='http://hugeproxies.com/home/', proto=('HTTP', 'HTTPS')),                # 118/16
    Provider(url='http://proxy.rufey.ru/', proto=('HTTP', 'HTTPS')),                      # 153/16
    Provider(url='http://go4free.xyz/Free-Proxy/', proto=('HTTP', 'HTTPS')),              # 196/10
    Provider(url='http://mitituti.com/content/proxy.txt', proto=('HTTP', 'HTTPS')),       # 227/42
    Provider(url='http://geekelectronics.org/my-servisy/proxy', proto=('HTTP', 'HTTPS')), # 395/7
    Tools_rosinstrument_com(proto=('HTTP', 'HTTPS')),         # 4980/2367
    Tools_rosinstrument_com_socks(proto=('SOCKS4', 'SOCKS5')),# 2550/457
    Provider(url='http://blackstarsecurity.com/proxy-list.txt'),                          # 7014/427 SOCKS(~175)
    My_proxy_com(max_conn=2),                                 # 894/408 SOCKS(~10)
    Checkerproxy_net(),                                       # 8382/1279 SOCKS(~30)
    Aliveproxy_com(),                                         # 210/63 SOCKS(~5)
    Freeproxylists_com(),                                     # 6094/4203 SOCKS(~94)
    Webanetlabs_net(),                                        # 2737/700 SOCKS(~325)
    # Free_proxy_cz(),                                          # 420/195 SOCKS(~8)
    # Provider(url='http://www.get-proxy.net/proxy-archives'),  # 519/188 SOCKS(~31)
    # Maxiproxies_com(),                                        # 626/169 SOCKS(~15)
    # _50kproxies_com(),                                        # 934/218 SOCKS(~38)
]
