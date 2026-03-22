[![PyPI](https://img.shields.io/pypi/v/interceptor-registry)](https://pypi.org/project/interceptor-registry/)
[![Python](https://img.shields.io/pypi/pyversions/interceptor-registry)](https://pypi.org/project/interceptor-registry/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![prek](https://img.shields.io/badge/prek-checked-blue)](https://github.com/saemeon/prek)

# interceptor-registry

Add pre/post/around interceptors on bound methods at runtime — without modifying the original class.

**Full documentation at [saemeon.github.io/interceptor-registry](https://saemeon.github.io/interceptor-registry/)**

## Installation

```bash
pip install interceptor-registry
```

## Quick Start

```python
from contextlib import contextmanager
from interceptor_registry import add_interceptor, del_interceptor, del_interceptors

class Foo:
    def bar(self):
        print("inside method call")
        return "result"

    @staticmethod
    def static_bar(x):
        return x * 2

foo = Foo()

def print_before():
    print("before")

@contextmanager
def around():
    print("enter context")
    try:
        yield
    finally:
        print("exit context")

add_interceptor(foo, 'bar', print_before, callorder=-2)
add_interceptor(foo, 'bar', around, is_context_manager=True, callorder=-1)

foo.bar()
# before
# enter context
# inside method call
# exit context

# Works the same for classmethods and staticmethods
add_interceptor(foo, 'static_bar', print_before, callorder=-1)
```

Use `del_interceptor` with the returned ID to remove a single interceptor,
or `del_interceptors` to clear all:

```python
iid = add_interceptor(foo, 'bar', print_before, callorder=-1)
del_interceptor(foo, 'bar', iid)

del_interceptors(foo, 'bar')
```

## License

MIT
