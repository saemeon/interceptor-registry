# Copyright (c) Simon Niederberger.
# Distributed under the terms of the MIT License.


from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("interceptor-registry")
except PackageNotFoundError:  # pragma: no cover - import-time fallback when uninstalled
    __version__ = "unknown"

from interceptor_registry._registry import (
    add_interceptor,
    del_interceptor,
    del_interceptors,
    get_interceptors,
    has_interceptors,
)

__all__ = [
    "add_interceptor",
    "del_interceptor",
    "del_interceptors",
    "get_interceptors",
    "has_interceptors",
]
