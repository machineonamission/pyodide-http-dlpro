"""
Support for streaming http requests.

A couple of caveats -

Firstly, you can't do streaming http in the main UI thread, because atomics.wait isn't allowed. This only
works if you're running pyodide in a web worker.

Secondly, this uses an extra web worker and SharedArrayBuffer to do the asynchronous fetch
operation, so it requires that you have crossOriginIsolation enabled, by serving over https
(or from localhost) with the two headers below set:

    Cross-Origin-Opener-Policy: same-origin
    Cross-Origin-Embedder-Policy: require-corp

You can tell if cross origin isolation is successfully enabled by looking at the global crossOriginIsolated variable in
javascript console. If it isn't, requests with stream set to True will fallback to XMLHttpRequest, i.e. getting the whole
request into a buffer and then returning it. it shows a warning in the javascript console in this case.
"""
import asyncio
import io
import json
import time
import traceback

import js
from js import SharedArrayBuffer
from pyodide.ffi import to_js
from urllib.request import Request

SUCCESS_HEADER = -1
SUCCESS_EOF = -2
ERROR_TIMEOUT = -3
ERROR_EXCEPTION = -4


def _obj_from_dict(dict_val: dict) -> any:
    return to_js(dict_val, dict_converter=js.Object.fromEntries)


class _ReadStream(io.RawIOBase):
    def __init__(self, int_buffer, byte_buffer, timeout, worker, connection_id):
        self.int_buffer = int_buffer
        self.byte_buffer = byte_buffer
        self.read_pos = 0
        self.read_len = 0
        self.connection_id = connection_id
        self.worker = worker
        self.timeout = int(1000 * timeout) if timeout > 0 else None

    def __del__(self):
        self.worker.postMessage(_obj_from_dict({"close": self.connection_id}))

    def readable(self) -> bool:
        return True

    def writeable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def readinto(self, byte_obj) -> bool:
        if not self.int_buffer:
            return 0
        if self.read_len == 0:
            # wait for the worker to send something
            js.Atomics.store(self.int_buffer, 0, 0)
            self.worker.postMessage(_obj_from_dict({"getMore": self.connection_id}))
            if js.Atomics.wait(self.int_buffer, 0, 0, self.timeout) == "timed-out":
                from ._core import _StreamingTimeout

                raise _StreamingTimeout
            data_len = self.int_buffer[0]
            if data_len > 0:
                self.read_len = data_len
                self.read_pos = 0
            elif data_len == ERROR_EXCEPTION:
                from ._core import _StreamingError

                raise _StreamingError
            else:
                # EOF, free the buffers and return zero
                self.read_len = 0
                self.read_pos = 0
                self.int_buffer = None
                self.byte_buffer = None
                return 0
        # copy from int32array to python bytes
        ret_length = min(self.read_len, len(byte_obj))
        self.byte_buffer.subarray(self.read_pos, self.read_pos + ret_length).assign_to(
            byte_obj[0:ret_length]
        )
        self.read_len -= ret_length
        self.read_pos += ret_length
        return ret_length


class _StreamingFetcher:
    def __init__(self):
        # print ("STREAMER INIT")
        # # make web-worker and data buffer on startup
        # dataBlob = js.Blob.new(
        #     [_STREAMING_WORKER_CODE], _obj_from_dict({"type": "application/javascript"})
        # )
        # dataURL = js.URL.createObjectURL(dataBlob)
        # print(dataURL)
        # self._worker = js.Worker.new(dataURL)
        from pyodide.ffi import run_sync
        self._worker = run_sync(js.spawn_worker())
        # print("STREAMER WORKER CREATED")
        # asyncio.run(asyncio.sleep(3))
        # js.console.log(self._worker)

    def send(self, request, credentials: bool):
        from ._core import Response

        headers = request.headers
        body = request.body
        fetch_data = {"headers": headers, "body": to_js(body), "method": request.method,
                      "credentials": "include" if credentials else "omit"}
        # start the request off in the worker
        timeout = int(1000 * request.timeout) if request.timeout > 0 else None
        shared_buffer = js.SharedArrayBuffer.new(1048576)
        int_buffer = js.Int32Array.new(shared_buffer)
        byte_buffer = js.Uint8Array.new(shared_buffer, 8)

        js.Atomics.store(int_buffer, 0, 0)
        js.Atomics.notify(int_buffer, 0)
        absolute_url = js.URL.new(request.url, js.location).href
        # js.console.log(
        #     _obj_from_dict(
        #         {
        #             "buffer": shared_buffer,
        #             "url": absolute_url,
        #             "fetchParams": fetch_data,
        #         }
        #     )
        # )
        self._worker.postMessage(
            _obj_from_dict(
                {
                    "buffer": shared_buffer,
                    "url": absolute_url,
                    "fetchParams": fetch_data,
                }
            ),
        )

        # print(self._worker)
        # wait for the worker to send something
        js.Atomics.wait(int_buffer, 0, 0, timeout)
        if int_buffer[0] == 0:
            from ._core import _StreamingTimeout

            raise _StreamingTimeout(
                "Timeout connecting to streaming request",
                request=request,
                response=None,
            )
        if int_buffer[0] == SUCCESS_HEADER:
            # got response
            # header length is in second int of intBuffer
            string_len = int_buffer[1]
            # decode the rest to a JSON string
            decoder = js.TextDecoder.new()
            # this does a copy (the slice) because decode can't work on shared array
            # for some silly reason
            json_str = decoder.decode(byte_buffer.slice(0, string_len))
            # get it as an object
            response_obj = json.loads(json_str)
            return Response(
                status_code=response_obj["status"],
                headers=response_obj["headers"],
                body=io.BufferedReader(
                    _ReadStream(
                        int_buffer,
                        byte_buffer,
                        request.timeout,
                        self._worker,
                        response_obj["connectionID"],
                    ),
                    buffer_size=1048576,
                ),
                stream=True
            )
        if int_buffer[0] == ERROR_EXCEPTION:
            string_len = int_buffer[1]
            # decode the error string
            decoder = js.TextDecoder.new()
            json_str = decoder.decode(byte_buffer.slice(0, string_len))
            from ._core import _StreamingError

            raise _StreamingError(
                f"Exception thrown in fetch: {json_str}", request=request, response=None
            )


if SharedArrayBuffer:
    _fetcher = _StreamingFetcher()
else:
    _fetcher = None


def send_streaming_request(request: Request, credentials: bool):
    # global _fetcher
    # if _fetcher is None:
    #     _fetcher = _StreamingFetcher()
    return _fetcher.send(request, credentials)
