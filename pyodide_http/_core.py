import json
from dataclasses import dataclass, field
from typing import Optional, Dict
from email.parser import Parser
from pyodide.ffi import to_js, run_sync

from . import _options

# need to import streaming here so that the web-worker is setup
from ._streaming import send_streaming_request
# import ._streaming

"""
There are some headers that trigger unintended CORS preflight requests.
See also https://github.com/koenvo/pyodide-http/issues/22
"""
HEADERS_TO_IGNORE = ("user-agent",)


class _RequestError(Exception):
    def __init__(self, message=None, *, request=None, response=None):
        self.request = request
        self.response = response
        self.message = message
        super().__init__(self.message)


class _StreamingError(_RequestError):
    pass


class _StreamingTimeout(_StreamingError):
    pass


@dataclass
class Request:
    method: str
    url: str
    params: Optional[Dict[str, str]] = None
    body: Optional[bytes] = None
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: int = 0

    def set_header(self, name: str, value: str):
        self.headers[name] = value

    def set_body(self, body: bytes):
        self.body = body

    def set_json(self, body: dict):
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_body(json.dumps(body).encode("utf-8"))


@dataclass
class Response:
    status_code: int
    headers: Dict[str, str]
    body: bytes
    stream: bool = False


_SHOWN_WARNING = False


def show_streaming_warning():
    global _SHOWN_WARNING
    if not _SHOWN_WARNING:
        _SHOWN_WARNING = True
        from js import console

        console.warn(
            "requests can't stream data in the main thread, using non-streaming fallback"
        )


def orig_send(request: Request, stream: bool = False, withCredentials: bool | None = None) -> Response:
    if request.params:
        from js import URLSearchParams

        params = URLSearchParams.new()
        for k, v in request.params.items():
            params.append(k, v)
        request.url += "?" + params.toString()

    from js import XMLHttpRequest

    try:
        from js import importScripts

        _IN_WORKER = True
    except ImportError:
        _IN_WORKER = False
    # support for streaming workers (in worker )
    if stream:
        if not _IN_WORKER:
            stream = False
            show_streaming_warning()
        else:
            result = send_streaming_request(request)
            if result == False:
                stream = False
                print("FALLING BACK TO NON STREAMING UH OH")
            else:
                newh = {}
                for [key, val] in result.headers:
                    newh[key] = val
                result.headers = newh
                return result

    xhr = XMLHttpRequest.new()
    # set timeout only if pyodide is in a worker, because
    # there is a warning not to set timeout on synchronous main thread
    # XMLHttpRequest https://developer.mozilla.org/en-US/docs/Web/API/XMLHttpRequest/timeout
    if _IN_WORKER and request.timeout != 0:
        xhr.timeout = int(request.timeout * 1000)

    if _IN_WORKER:
        xhr.responseType = "arraybuffer"
    else:
        xhr.overrideMimeType("text/plain; charset=ISO-8859-15")

    xhr.open(request.method, request.url, False)
    for name, value in request.headers.items():
        if name.lower() not in HEADERS_TO_IGNORE:
            xhr.setRequestHeader(name, value)

    body = request.body

    if hasattr(body, 'read'):
        body = body.read()

    xhr.withCredentials = _options.with_credentials if withCredentials is None else withCredentials
    xhr.send(to_js(body))

    headers = dict(Parser().parsestr(xhr.getAllResponseHeaders()))

    if _IN_WORKER:
        body = xhr.response.to_py().tobytes()
    else:
        body = xhr.response.encode("ISO-8859-15")

    return Response(status_code=xhr.status, headers=headers, body=body)


def dlpro_proxy_send(request: Request, credentials: bool = False):
    from js import proxy_fetch, Object
    # pyodide wont convert custom objects by default, so parse them out
    jsified_request = {
        "method": request.method,
        "url": request.url,
        "params": request.params,
        "body": request.body,
        "headers": request.headers,
        "timeout": request.timeout,
        "credentials": credentials,
    }
    # TODO: im aware sometimes yt-dlp doesnt want the exact cookies the browser has,
    #  but setting cookies is a hard and janky process
    # print(current_cookies)
    # oldcookies = run_sync(force_cookies(to_js(chromeify_cookies(current_cookies), dict_converter=Object.fromEntries)))
    # block until async js request is done
    js_response = run_sync(proxy_fetch(to_js(jsified_request, dict_converter=Object.fromEntries)))

    # run_sync(force_cookies(oldcookies))
    # idfk, ripped from pyodide
    headers = dict(Parser().parsestr(js_response["headers"]))
    # expected response object
    return Response(
        status_code=js_response["status_code"],
        headers=headers,
        body=js_response["body"]
    )


# major patch
def send(request: Request, stream: bool = False):
    # print(request)
    proxy = False
    credentials = False
    # these headers cannot be modified directly, but they are needed, so requests are proxied through a content script
    proxy_headers = ["origin"]
    # the browser doesnt let you set these
    blocked_headers = ['sec-fetch-mode', 'accept-encoding', "origin", "referer", "user-agent", "cookie", "cookie2"]
    # we cant directly set cookie headers, but we can ask the browser to include credentials if yt-dlp wishes to set them
    credentials_headers = ["cookie", "cookie2"]
    # handle headers
    new_headers = {}
    for header, value in request.headers.items():
        # dont add headers that are not allowed
        if header.lower() in blocked_headers:
            # print("Blocked header:", header, value)
            # signal we need to proxy this request
            if header.lower() in proxy_headers:
                proxy = True
            # signal we need to include credentials
            if header.lower() in credentials_headers:
                credentials = True
        else:
            # print("Allowed header:", header, value)
            new_headers[header] = value
    request.headers = new_headers
    # print(request)
    if proxy:
        if stream:
            raise Exception("Attempted to stream through proxy, which isnt supported.")
        return dlpro_proxy_send(request, credentials)
    else:
        # print(current_cookies)
        # oldcookies = run_sync(force_cookies(chromeify_cookies(to_js(current_cookies, dict_converter=Object.fromEntries))))
        # print("stream", stream)
        # try:
        #     nonlocal out
        return orig_send(request, True, credentials)
