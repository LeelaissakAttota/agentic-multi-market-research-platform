from financial_intelligence.config.settings import Settings


def test_default_financial_cap_fits_large_cap_companyfacts() -> None:
    settings = Settings(_env_file=None, APP_ENV="test")
    assert settings.financial_data_max_response_bytes >= 8 * 1024 * 1024
