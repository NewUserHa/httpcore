import logging
import ssl
from base64 import b64encode
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .._backends.base import SOCKET_OPTION, AsyncNetworkBackend
from .._exceptions import ProxyError
from .._models import (
    URL,
    Origin,
    Request,
    Response,
    enforce_bytes,
    enforce_headers,
    enforce_url,
)
from .._ssl import default_ssl_context
from .._synchronization import AsyncLock
from .._trace import Trace
from .connection import AsyncHTTPConnection
from .connection_pool import AsyncConnectionPool
from .http11 import AsyncHTTP11Connection
from .interfaces import AsyncConnectionInterface

HeadersAsSequence = Sequence[Tuple[Union[bytes, str], Union[bytes, str]]]
HeadersAsMapping = Mapping[Union[bytes, str], Union[bytes, str]]


logger = logging.getLogger("httpcore.proxy")


def merge_headers(
    default_headers: Optional[Sequence[Tuple[bytes, bytes]]] = None,
    override_headers: Optional[Sequence[Tuple[bytes, bytes]]] = None,
) -> List[Tuple[bytes, bytes]]:
    """
    Append default_headers and override_headers, de-duplicating if a key exists
    in both cases.
    """
    default_headers = [] if default_headers is None else list(default_headers)
    override_headers = [] if override_headers is None else list(override_headers)
    has_override = set(key.lower() for key, value in override_headers)
    default_headers = [
        (key, value)
        for key, value in default_headers
        if key.lower() not in has_override
    ]
    return default_headers + override_headers


def build_auth_header(username: bytes, password: bytes) -> bytes:
    userpass = username + b":" + password
    return b"Basic " + b64encode(userpass)


class AsyncHTTPProxy(AsyncConnectionPool):
    """
    A connection pool that sends requests via an HTTP proxy.
    """

    def __init__(
        self,
        proxy_url: Union[URL, bytes, str],
        proxy_auth: Optional[Tuple[Union[bytes, str], Union[bytes, str]]] = None,
        proxy_headers: Union[HeadersAsMapping, HeadersAsSequence, None] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        proxy_ssl_context: Optional[ssl.SSLContext] = None,
        max_connections: Optional[int] = 10,
        max_keepalive_connections: Optional[int] = None,
        keepalive_expiry: Optional[float] = None,
        http1: bool = True,
        http2: bool = False,
        retries: int = 0,
        local_address: Optional[str] = None,
        uds: Optional[str] = None,
        network_backend: Optional[AsyncNetworkBackend] = None,
        socket_options: Optional[Iterable[SOCKET_OPTION]] = None,
    ) -> None:
        """
        A connection pool for making HTTP requests.

        Parameters:
            proxy_url: The URL to use when connecting to the proxy server.
                For example `"http://127.0.0.1:8080/"`.
            proxy_auth: Any proxy authentication as a two-tuple of
                (username, password). May be either bytes or ascii-only str.
            proxy_headers: Any HTTP headers to use for the proxy requests.
                For example `{"Proxy-Authorization": "Basic <username>:<password>"}`.
            ssl_context: An SSL context to use for verifying connections.
                If not specified, the default `httpcore.default_ssl_context()`
                will be used.
            proxy_ssl_context: The same as `ssl_context`, but for a proxy server rather than a remote origin.
            max_connections: The maximum number of concurrent HTTP connections that
                the pool should allow. Any attempt to send a request on a pool that
                would exceed this amount will block until a connection is available.
            max_keepalive_connections: The maximum number of idle HTTP connections
                that will be maintained in the pool.
            keepalive_expiry: The duration in seconds that an idle HTTP connection
                may be maintained for before being expired from the pool.
            http1: A boolean indicating if HTTP/1.1 requests should be supported
                by the connection pool. Defaults to True.
            http2: A boolean indicating if HTTP/2 requests should be supported by
                the connection pool. Defaults to False.
            retries: The maximum number of retries when trying to establish
                a connection.
            local_address: Local address to connect from. Can also be used to
                connect using a particular address family. Using
                `local_address="0.0.0.0"` will connect using an `AF_INET` address
                (IPv4), while using `local_address="::"` will connect using an
                `AF_INET6` address (IPv6).
            uds: Path to a Unix Domain Socket to use instead of TCP sockets.
            network_backend: A backend instance to use for handling network I/O.
            socket_options: Socket options that have to be included
             in the TCP socket when the connection was established.
        """
        super().__init__(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=retries,
            local_address=local_address,
            uds=uds,
            network_backend=network_backend,
            socket_options=socket_options,
        )

        self._proxy_url = enforce_url(proxy_url, name="proxy_url")
        if (
            self._proxy_url.scheme == b"http" and proxy_ssl_context is not None
        ):  # pragma: no cover
            raise RuntimeError(
                "The `proxy_ssl_context` argument is not allowed for the http scheme"
            )

        self._ssl_context = ssl_context
        self._proxy_ssl_context = proxy_ssl_context
        self._proxy_headers = enforce_headers(proxy_headers, name="proxy_headers")
        if proxy_auth is not None:
            username = enforce_bytes(proxy_auth[0], name="proxy_auth")
            password = enforce_bytes(proxy_auth[1], name="proxy_auth")
            authorization = build_auth_header(username, password)
            self._proxy_headers = [
                (b"Proxy-Authorization", authorization)
            ] + self._proxy_headers

    def create_connection(self, origin: Origin) -> AsyncConnectionInterface:
        if origin.scheme == b"http":
            return AsyncForwardHTTPConnection(
                proxy_origin=self._proxy_url.origin,
                remote_origin=origin,
                proxy_ssl_context=self._proxy_ssl_context,
                proxy_headers=self._proxy_headers,
                keepalive_expiry=self._keepalive_expiry,
                retries=self._retries,
                local_address=self._local_address,
                uds=self._uds,
                network_backend=self._network_backend,
                socket_options=self._socket_options,
            )
        return AsyncTunnelHTTPConnection(
            proxy_origin=self._proxy_url.origin,
            remote_origin=origin,
            ssl_context=self._ssl_context,
            proxy_ssl_context=self._proxy_ssl_context,
            proxy_headers=self._proxy_headers,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            retries=self._retries,
            local_address=self._local_address,
            uds=self._uds,
            network_backend=self._network_backend,
            socket_options=self._socket_options,
        )


class AsyncForwardHTTPConnection(AsyncConnectionInterface):
    def __init__(
        self,
        proxy_origin: Origin,
        remote_origin: Origin,
        proxy_ssl_context: Optional[ssl.SSLContext] = None,
        proxy_headers: Union[HeadersAsMapping, HeadersAsSequence, None] = None,
        keepalive_expiry: Optional[float] = None,
        retries: int = 0,
        local_address: Optional[str] = None,
        uds: Optional[str] = None,
        network_backend: Optional[AsyncNetworkBackend] = None,
        socket_options: Optional[Iterable[SOCKET_OPTION]] = None,
    ) -> None:
        self._connection = AsyncHTTPConnection(
            origin=proxy_origin,
            ssl_context=proxy_ssl_context,
            keepalive_expiry=keepalive_expiry,
            retries=retries,
            local_address=local_address,
            uds=uds,
            network_backend=network_backend,
            socket_options=socket_options,
        )
        self._proxy_origin = proxy_origin
        self._proxy_headers = enforce_headers(proxy_headers, name="proxy_headers")
        self._remote_origin = remote_origin

    async def handle_async_request(self, request: Request) -> Response:
        headers = merge_headers(self._proxy_headers, request.headers)
        url = URL(
            scheme=self._proxy_origin.scheme,
            host=self._proxy_origin.host,
            port=self._proxy_origin.port,
            target=bytes(request.url),
        )
        proxy_request = Request(
            method=request.method,
            url=url,
            headers=headers,
            content=request.stream,
            extensions=request.extensions,
        )
        return await self._connection.handle_async_request(proxy_request)

    def can_handle_request(self, origin: Origin) -> bool:
        return origin == self._remote_origin

    async def aclose(self) -> None:
        await self._connection.aclose()

    def info(self) -> str:
        return self._connection.info()

    def is_available(self) -> bool:
        return self._connection.is_available()

    def has_expired(self) -> bool:
        return self._connection.has_expired()

    def is_idle(self) -> bool:
        return self._connection.is_idle()

    def is_closed(self) -> bool:
        return self._connection.is_closed()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} [{self.info()}]>"


class AsyncTunnelHTTPConnection(AsyncConnectionInterface):
    def __init__(
        self,
        proxy_origin: Origin,
        remote_origin: Origin,
        ssl_context: Optional[ssl.SSLContext] = None,
        proxy_ssl_context: Optional[ssl.SSLContext] = None,
        proxy_headers: Optional[Sequence[Tuple[bytes, bytes]]] = None,
        keepalive_expiry: Optional[float] = None,
        http1: bool = True,
        http2: bool = False,
        retries: int = 0,
        local_address: Optional[str] = None,
        uds: Optional[str] = None,
        network_backend: Optional[AsyncNetworkBackend] = None,
        socket_options: Optional[Iterable[SOCKET_OPTION]] = None,
    ) -> None:
        self._connection: AsyncConnectionInterface = AsyncHTTPConnection(
            origin=proxy_origin,
            ssl_context=proxy_ssl_context,
            keepalive_expiry=keepalive_expiry,
            retries=retries,
            local_address=local_address,
            uds=uds,
            network_backend=network_backend,
            socket_options=socket_options,
        )
        self._proxy_origin = proxy_origin
        self._remote_origin = remote_origin
        self._ssl_context = ssl_context
        self._proxy_ssl_context = proxy_ssl_context
        self._proxy_headers = enforce_headers(proxy_headers, name="proxy_headers")
        self._keepalive_expiry = keepalive_expiry
        self._http1 = http1
        self._http2 = http2
        self._connect_lock = AsyncLock()
        self._connected = False

    async def handle_async_request(self, request: Request) -> Response:
        timeouts = request.extensions.get("timeout", {})
        timeout = timeouts.get("connect", None)

        async with self._connect_lock:
            if not self._connected:
                target = b"%b:%d" % (self._remote_origin.host, self._remote_origin.port)

                connect_url = URL(
                    scheme=self._proxy_origin.scheme,
                    host=self._proxy_origin.host,
                    port=self._proxy_origin.port,
                    target=target,
                )
                connect_headers = merge_headers(
                    [(b"Host", target), (b"Accept", b"*/*")], self._proxy_headers
                )
                connect_request = Request(
                    method=b"CONNECT",
                    url=connect_url,
                    headers=connect_headers,
                    extensions=request.extensions,
                )
                connect_response = await self._connection.handle_async_request(
                    connect_request
                )

                if connect_response.status < 200 or connect_response.status > 299:
                    reason_bytes = connect_response.extensions.get("reason_phrase", b"")
                    reason_str = reason_bytes.decode("ascii", errors="ignore")
                    msg = "%d %s" % (connect_response.status, reason_str)
                    await self._connection.aclose()
                    raise ProxyError(msg)

                stream = connect_response.extensions["network_stream"]

                # Upgrade the stream to SSL
                ssl_context = (
                    default_ssl_context()
                    if self._ssl_context is None
                    else self._ssl_context
                )
                alpn_protocols = ["http/1.1", "h2"] if self._http2 else ["http/1.1"]
                ssl_context.set_alpn_protocols(alpn_protocols)

                kwargs = {
                    "ssl_context": ssl_context,
                    "server_hostname": self._remote_origin.host.decode("ascii"),
                    "timeout": timeout,
                }
                async with Trace("start_tls", logger, request, kwargs) as trace:
                    stream = await stream.start_tls(**kwargs)
                    trace.return_value = stream

                # Determine if we should be using HTTP/1.1 or HTTP/2
                ssl_object = stream.get_extra_info("ssl_object")
                http2_negotiated = (
                    ssl_object is not None
                    and ssl_object.selected_alpn_protocol() == "h2"
                )

                # Create the HTTP/1.1 or HTTP/2 connection
                if http2_negotiated or (self._http2 and not self._http1):
                    from .http2 import AsyncHTTP2Connection

                    self._connection = AsyncHTTP2Connection(
                        origin=self._remote_origin,
                        stream=stream,
                        keepalive_expiry=self._keepalive_expiry,
                    )
                else:
                    self._connection = AsyncHTTP11Connection(
                        origin=self._remote_origin,
                        stream=stream,
                        keepalive_expiry=self._keepalive_expiry,
                    )

                self._connected = True
        return await self._connection.handle_async_request(request)

    def can_handle_request(self, origin: Origin) -> bool:
        return origin == self._remote_origin

    async def aclose(self) -> None:
        await self._connection.aclose()

    def info(self) -> str:
        return self._connection.info()

    def is_available(self) -> bool:
        return self._connection.is_available()

    def has_expired(self) -> bool:
        return self._connection.has_expired()

    def is_idle(self) -> bool:
        return self._connection.is_idle()

    def is_closed(self) -> bool:
        return self._connection.is_closed()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} [{self.info()}]>"
