import sys
import traceback
from io import BytesIO

import urllib.request
import urllib.error
from http.client import HTTPResponse

from ._core import Request, send

_IS_PATCHED = False

import io


class FakeSock:
    def __init__(self, data):
        self.data = data

    def makefile(self, mode):
        return BytesIO(self.data)


# fake socket that supports streaming
class StreamSock:
    def __init__(self, header, body):
        self.stream = PrefixedReader(header, body)

    def makefile(self, mode):
        return self.stream


# prepends a constant byte string to a dynamic stream and returns a new stream. this is to add the http headers to
#  streams
class PrefixedReader(io.RawIOBase):
    def __init__(self, prefix: bytes, reader: io.BufferedReader):
        # keep a memoryview so we can slice off bytes without copying on each read
        self._prefix = memoryview(prefix)
        self._reader = reader

    def readable(self):
        return True

    def readinto(self, b: bytearray) -> int:
        # chatgpt wrote this code, idfk
        size = len(b)
        buf = bytearray()

        # 1) Serve from prefix if any remains
        if self._prefix is not None:
            if len(self._prefix) <= size:
                buf += self._prefix.tobytes()
                remaining = size - len(self._prefix)
                self._prefix = None
                if remaining:
                    chunk = self._reader.read(remaining)
                    if chunk:
                        buf += chunk
            else:
                buf += self._prefix[:size].tobytes()
                self._prefix = self._prefix[size:]
        else:
            # 2) No prefix left → drain straight from the reader
            chunk = self._reader.read(size)
            if chunk:
                buf += chunk

        # copy into the supplied buffer and report how many bytes we wrote
        b_view = memoryview(b)
        b_view[:len(buf)] = buf
        return len(buf)


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
    from js import console
    from pyodide.ffi import to_js
    # print(resp)

    # Build a fake http response
    # Strip out the content-length header. When Content-Encoding is 'gzip' (or other
    # compressed format) the 'Content-Length' is the compressed length, while the
    # data itself is uncompressed. This will cause problems while decoding our
    # fake response.

    """
    Ok so, pyodide_http reconstructs a raw HTTP response from the body that XHR returns. Problem is, XHR handles 
    things like gzip and chunking, so if we leave those headers, and send it to python's http, it freaks out trying
    to decode nonsense. super simple fix, we just remove the transfer-encoding header, and it behaves like normal
    bytes, and thats fine
    """
    headers_without_content_length = {
        k: v for k, v in resp.headers.items() if k.lower() not in ["content-length", "transfer-encoding"]
    }

    if resp.stream:
        response_header = (
                b"HTTP/1.1 "
                + str(resp.status_code).encode("ascii")
                + b"\n"
                + "\n".join(
            f"{key}: {value}" for key, value in headers_without_content_length.items()
        ).encode("ascii")
                + b"\n\n"
        )

        # wrap streaming array in fake socket
        response = HTTPResponse(StreamSock(response_header, resp.body))
        response.url = url
        response.begin()
    else:
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
        # print("PROXY:", response_data.decode())
        response = HTTPResponse(FakeSock(response_data))
        response.url = url
        response.begin()

    # patch
    if isinstance(url, urllib.request.Request):
        response.url = url.full_url
    else:
        response.url = url

    # urlopen actually throws an exception on http errors. i dont think yt-dlp cares, but this is "proper"
    # wrote this as i was chasing down a different bug, but i think its more proper so it stays
    if not (200 <= response.status < 300):
        raise urllib.error.HTTPError(response.url, response.status, f"HTTP Error {response.status}", response.headers, None)

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
