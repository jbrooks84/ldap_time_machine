"""Smoke tests verifying the package imports cleanly."""


def test_ltm_imports():
    import ltm.config
    import ltm.db
    import ltm.pipeline
    import ltm.report

    assert ltm.config.VERSION
