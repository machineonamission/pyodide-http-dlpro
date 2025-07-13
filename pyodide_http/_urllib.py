from io import BytesIO

import urllib.request
from http.client import HTTPResponse

from ._core import Request, send

_IS_PATCHED = False


class FakeSock:
    def __init__(self, data):
        self.data = data

    def makefile(self, mode):
        return BytesIO(self.data)


def urlopen(url, *args, **kwargs):
    method = "GET"
    data = None
    headers = {}
    if isinstance(url, urllib.request.Request):
        current_jar.add_cookie_header(url)
        method = url.get_method()
        data = url.data
        headers = dict(url.header_items())
        url = url.full_url

    request = Request(method, url, headers=headers, body=data)
    resp = send(request)

    # Build a fake http response
    # Strip out the content-length header. When Content-Encoding is 'gzip' (or other
    # compressed format) the 'Content-Length' is the compressed length, while the
    # data itself is uncompressed. This will cause problems while decoding our
    # fake response.
    headers_without_content_length = {
        k: v for k, v in resp.headers.items() if k != "content-length"
    }
    response_data = (
            b"HTTP/1.1 "
            + str(resp.status_code).encode("ascii")
            + b"\n"
            + "\n".join(
        f"{key}: {value}" for key, value in headers_without_content_length.items()
    ).encode("ascii")
            + b"\n\n"
            + resp.body
    )

    """
    Ok so, pyodide_http reconstructs a raw HTTP response from the body that XHR returns. Problem is, XHR handles 
    things like gzip and chunking, so if we leave those headers, and send it to python's http, it freaks out trying
    to decode nonsense. super simple fix, we just remove the transfer-encoding header, and it behaves like normal
    bytes, and thats fine
    """
    if "transfer-encoding" in resp.headers:
        del resp.headers["transfer-encoding"]

    response = HTTPResponse(FakeSock(response_data))
    response.url = url
    response.begin()

    # patch
    if isinstance(url, urllib.request.Request):
        response.url = url.full_url
    else:
        response.url = url

    return response


def urlopen_self_removed(self, url, *args, **kwargs):
    return urlopen(url, *args, **kwargs)


current_jar = None
current_cookies = None
# patch: add cookies
class CookiePatch(urllib.request.HTTPCookieProcessor):
    def __init__(self, cookiejar):
        super().__init__(cookiejar)
        global current_jar
        current_jar = cookiejar

def patch():
    global _IS_PATCHED

    if _IS_PATCHED:
        return

    # cookie patch
    urllib.request.HTTPCookieProcessor = CookiePatch

    urllib.request.urlopen = urlopen
    urllib.request.OpenerDirector.open = urlopen_self_removed

    _IS_PATCHED = True
