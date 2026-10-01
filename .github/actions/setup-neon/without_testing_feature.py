"""
pytest plugin (`-p without_testing_feature`) to run Neon's compatibility tests with the binaries of published images.

The images are built without the `testing` feature, so their pageserver has no testing APIs, and the test fixtures
skip a test calling one (`is_testing_enabled_or_skip`). The compatibility tests only call one of them, the timeline
checkpoint (in `test_create_snapshot` and at the end of `check_neon_works`), to make the pageserver flush and upload
its layers, which its graceful shutdown does as well. Without the testing APIs, the checkpoint is skipped instead of
the test.
"""

from __future__ import annotations

import pytest

WITHOUT_TESTING_FEATURE = "built without 'testing' feature"


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config):
    # Imported here: the `fixtures` package is importable once test_runner/conftest.py is loaded
    from fixtures.log_helper import log
    from fixtures.pageserver.http import PageserverHttpClient

    timeline_checkpoint = PageserverHttpClient.timeline_checkpoint

    def timeline_checkpoint_if_testing(self, *args, **kwargs):
        try:
            return timeline_checkpoint(self, *args, **kwargs)
        except pytest.skip.Exception as e:
            if WITHOUT_TESTING_FEATURE not in str(e):
                raise
            log.info("Skipping the timeline checkpoint, the pageserver was built without the testing APIs")

    PageserverHttpClient.timeline_checkpoint = timeline_checkpoint_if_testing
