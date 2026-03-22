# interceptor-registry

Add pre/post/around interceptors on bound methods at runtime — without modifying the original class.

Interception is always **instance-scoped**: patching `foo.bar` only affects that specific object, not other instances of the same class.  Works for instance methods, classmethods, and static methods.

## Installation

```bash
pip install interceptor-registry
```

## Quick start

```python
from contextlib import contextmanager
from interceptor_registry import add_interceptor, del_interceptor, del_interceptors

class Artist:
    def draw(self):
        print("drawing")
        return "done"

artist = Artist()

# Pre-hook — runs before draw()
add_interceptor(artist, "draw", lambda: print("before draw"), callorder=-1)

# Post-hook — runs after draw()
add_interceptor(artist, "draw", lambda: print("after draw"), callorder=1)

artist.draw()
# before draw
# drawing
# after draw
```

### Context manager around the call

```python
@contextmanager
def timer():
    import time
    t = time.perf_counter()
    yield
    print(f"elapsed: {time.perf_counter() - t:.4f}s")

add_interceptor(artist, "draw", timer, is_context_manager=True, callorder=-1)
```

### Removing interceptors

```python
iid = add_interceptor(artist, "draw", lambda: print("hook"), callorder=-1)

del_interceptor(artist, "draw", iid)   # remove one by id
del_interceptors(artist, "draw")       # remove all
```

### Classmethods and staticmethods

```python
class Foo:
    @classmethod
    def process(cls): ...

    @staticmethod
    def validate(x): ...

foo = Foo()
add_interceptor(foo, "process", lambda: print("before process"), callorder=-1)
add_interceptor(foo, "validate", lambda: print("before validate"), callorder=-1)
```

## callorder

`callorder` determines when an interceptor runs relative to the method call and to other interceptors:

| callorder | runs |
|-----------|------|
| negative  | before the method |
| positive  | after the method |
| 0         | invalid — raises `ValueError` |

Interceptors are sorted ascending, so `callorder=-2` runs before `callorder=-1`, and `callorder=1` runs before `callorder=2`.

`callorder` may be a callable — it is evaluated on every invocation, which is useful for dynamic ordering (e.g. `obj.get_zorder` for matplotlib artists).

## API

See the [API Reference](api.md) for the full function signatures and all parameters.
