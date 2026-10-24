import os

from amrp.config.settings import Settings


def test_host_secret_env_is_not_visible_to_tests() -> None:
    assert "OPENROUTER_API_KEY" not in os.environ
    settings = Settings(_env_file=None, APP_ENV="test")
    assert settings.openrouter_api_key.get_secret_value() == ""
