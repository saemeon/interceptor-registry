import interceptor_registry


def test_import():
    assert interceptor_registry.__version__ != "unknown"
