import interceptor_registry


def test_import():
    assert hasattr(interceptor_registry, "__version__")


def test_public_api():
    from interceptor_registry import (
        add_interceptor,
        del_interceptor,
        del_interceptors,
        get_interceptors,
        has_interceptors,
    )
    assert callable(add_interceptor)
    assert callable(del_interceptor)
    assert callable(del_interceptors)
    assert callable(get_interceptors)
    assert callable(has_interceptors)
