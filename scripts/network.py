"""Bounded retries for transport failures, never artifact validation failures."""

import errno
from http.client import IncompleteRead, RemoteDisconnected
import socket
import ssl
import sys
import time
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError


Result = TypeVar("Result")
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
RETRYABLE_ERRNO = {errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ENETRESET}
MAX_ATTEMPTS = 3


def is_transient_network_error(error: BaseException) -> bool:
    if isinstance(error, HTTPError):
        return error.code in RETRYABLE_HTTP_STATUS
    if isinstance(error, URLError):
        return isinstance(error.reason, BaseException) and is_transient_network_error(
            error.reason
        )
    # Certificate and TLS protocol errors need investigation, not retries.
    if isinstance(error, ssl.SSLError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(error, (TimeoutError, IncompleteRead, RemoteDisconnected)):
        return True
    return isinstance(error, OSError) and error.errno in RETRYABLE_ERRNO


def with_network_retries(operation: Callable[[], Result], description: str) -> Result:
    """Repeat the whole request, including body reads, at most three times.

    The caller must initialize its digest and truncate any destination inside
    operation so interrupted downloads cannot append to a previous attempt.
    Schema, identity, digest and signature checks remain hard failures.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as error:
            if attempt == MAX_ATTEMPTS or not is_transient_network_error(error):
                raise
            if isinstance(error, HTTPError):
                error.close()
            delay = attempt
            print(
                f"warning: {description}: transient network failure "
                f"({attempt}/{MAX_ATTEMPTS}); retrying in {delay}s: {error}",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable retry state")
