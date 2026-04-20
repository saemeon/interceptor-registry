# interceptor-registry

Add pre/post/around interceptors on bound methods at runtime — without modifying the original class.

Interception is always **instance-scoped**: patching `foo.bar` only affects that specific object, not other instances of the same class. Works for instance methods, classmethods, staticmethods, `async def` methods, generator methods, and async-generator methods.

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

### Async, generator, and async-generator methods

Interception works identically for all coroutine and iterator forms — the API is the same, the wrapper detects the method kind and awaits / iterates / async-iterates as appropriate so pre-hooks, post-hooks, and `is_context_manager=True` hooks all wrap the body correctly.

```python
import asyncio

class Service:
    async def fetch(self):                # async def
        await asyncio.sleep(0)
        return "data"

    def stream(self):                     # generator
        yield 1
        yield 2

    async def subscribe(self):            # async generator
        yield "event-a"
        yield "event-b"

svc = Service()
add_interceptor(svc, "fetch", lambda: print("before await"), callorder=-1)
add_interceptor(svc, "stream", lambda: print("after iteration done"), callorder=1)

asyncio.run(svc.fetch())        # prints "before await" before the body
list(svc.stream())              # prints "after iteration done" after iteration
```

Async generators also correctly forward `asend()`, `athrow()`, and `aclose()` through the interceptor layer to the underlying body.

### Exception suppression

If an `is_context_manager=True` hook's `__exit__` returns `True` to suppress an exception raised by the method body, the call returns `None` for non-generator methods, and generators / async-generators terminate without further yields. Pre-hooks that have already entered still receive their `__exit__` in LIFO order with the exception info.

## Not supported

- Properties and other non-callable custom descriptors (`TypeError` at registration).
- Async hook functions — `async def` or async-generator hooks (`TypeError` at registration).
- Async context-manager hooks — `@asynccontextmanager` passed with `is_context_manager=True` (`TypeError` when invoked).
- Pickle of a patched object (closures generally aren't picklable). Call `del_interceptors` before `pickle.dumps`, re-add after unpickling.
- `copy.copy` / `copy.deepcopy` of a patched object copies the wrapper closure too, so interceptors fire on the copy as if registered on the original. Known footgun — `del_interceptors` first if independent state is desired.

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
