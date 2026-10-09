"""Shared test setup.

The serve/observability/security tests talk to a stdlib server on
127.0.0.1 via urllib. urllib honours HTTP(S)_PROXY from the environment,
so on a machine (or CI runner) with a proxy configured and no NO_PROXY for
loopback, every request went to the proxy and those tests failed with
404 / JSONDecodeError. Loopback must never be proxied.
"""

import os

_LOOPBACK = ["127.0.0.1", "localhost", "::1"]

for _key in ("NO_PROXY", "no_proxy"):
    _cur = [h.strip() for h in os.environ.get(_key, "").split(",") if h.strip()]
    os.environ[_key] = ",".join(_cur + [h for h in _LOOPBACK if h not in _cur])
