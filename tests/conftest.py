import pytest

from revenue_leakage.generator import Config, generate


@pytest.fixture(scope="session")
def sample():
    return generate(Config(customers=80, invoices=400, leakage_rate=0.25))
