from tennis_predictor.config import get_settings


def test_settings_load_defaults():
    settings = get_settings()
    assert settings.env in {"local", "prod"}
    assert settings.postgres_dsn.startswith("postgresql+psycopg2://")
