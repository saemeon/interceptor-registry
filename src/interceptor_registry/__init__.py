# Copyright (c) Simon Niederberger.
# Distributed under the terms of the MIT License.


from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("interceptor-registry")
except PackageNotFoundError:
    __version__ = "unknown"

from interceptor_registry._registry import (
    get_interceptors,
    register,
    remove,
    remove_all,
)

__all__ = [
    "get_interceptors",
    "register",
    "remove",
    "remove_all",
]
