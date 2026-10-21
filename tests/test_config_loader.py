"""config_loader: env-first config, only the settings the current system reads."""
import pytest

import config_loader

ENV_NAMES = sorted(set(config_loader.ENV_MAP.values()))

REQUIRED_ENV = {
    "PORTAL_USERNAME": "user",
    "PORTAL_PASSWORD": "pw",
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_CHAT_ID": "42",
}


@pytest.fixture
def no_yaml(tmp_path, monkeypatch):
    for name in ENV_NAMES + ["SMS_RECIPIENT_NUMBER", "SMS_API_USER", "SMS_API_PASSWORD"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_loader, "CONFIG_PATH", tmp_path / "config.yaml")
    return tmp_path / "config.yaml"


def test_env_map_has_only_current_settings():
    sections = {section for section, _ in config_loader.ENV_MAP}
    assert sections == {"portal", "anthropic", "telegram", "parts_portal"}
    assert not any(name.startswith(("SMS_", "TWILIO_", "WORKSHOP_", "BOOKING_"))
                   for name in config_loader.ENV_MAP.values())
    assert set(config_loader.ENV_MAP) == {
        ("portal", "username"), ("portal", "password"),
        ("portal", "bookings_path"), ("portal", "debug"),
        ("anthropic", "api_key"),
        ("telegram", "bot_token"), ("telegram", "chat_id"),
        ("parts_portal", "username"), ("parts_portal", "password"),
    }


def test_defaults_have_no_sms_section():
    assert {section for section, _ in config_loader.DEFAULTS} == {"portal"}


def test_builds_from_env_without_yaml(no_yaml, monkeypatch):
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PARTS_PORTAL_USERNAME", "parts")
    monkeypatch.setenv("PORTAL_DEBUG", "true")
    config = config_loader.load_config()
    assert config["portal"] == {"username": "user", "password": "pw",
                                "bookings_path": "/Booking/Index", "debug": True}
    assert config["telegram"] == {"bot_token": "123:abc", "chat_id": "42"}
    assert config["parts_portal"] == {"username": "parts"}
    assert "sms" not in config


def test_old_sms_env_vars_are_ignored(no_yaml, monkeypatch):
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SMS_RECIPIENT_NUMBER", "+46700000000")
    assert "sms" not in config_loader.load_config()


def test_retired_yaml_sections_are_harmless(no_yaml):
    no_yaml.write_text(
        "portal: {username: u, password: p}\n"
        "telegram: {bot_token: t, chat_id: '1'}\n"
        "sms: {provider: 46elks, api_user: x}\n"
        "email_agent: {enabled: true}\n"
        "trigger: {mode: always}\n"
        "workshop: {name: X}\n",
        encoding="utf-8",
    )
    config = config_loader.load_config()
    assert config["portal"]["username"] == "u"
    assert config["sms"] == {"provider": "46elks", "api_user": "x"}  # left alone


def test_env_overrides_yaml(no_yaml, monkeypatch):
    no_yaml.write_text("portal: {username: u, password: p}\n"
                       "telegram: {bot_token: t, chat_id: '1'}\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    assert config_loader.load_config()["telegram"]["chat_id"] == "99"


@pytest.mark.parametrize("missing", sorted(REQUIRED_ENV))
def test_missing_required_setting_exits(no_yaml, monkeypatch, missing):
    for name, value in REQUIRED_ENV.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit):
        config_loader.load_config()


def test_placeholder_value_counts_as_missing(no_yaml, monkeypatch):
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
    with pytest.raises(SystemExit):
        config_loader.load_config()
