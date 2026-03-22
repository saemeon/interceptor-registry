import mypackage


def test_import():
    assert mypackage.__version__ != "unknown"
