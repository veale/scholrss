import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: marks tests that hit real external APIs")
