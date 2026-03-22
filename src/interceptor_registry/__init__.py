# Copyright (c) Simon Niederberger.
# Distributed under the terms of the MIT License.


from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("interceptor-registry")
except PackageNotFoundError:
    __version__ = "unknown"

from interceptor_registry._registry import deregister_method_interceptor, register_method_interceptor

__all__ = ["register_method_interceptor", "deregister_method_interceptor"]
