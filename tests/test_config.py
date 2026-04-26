from app.config import load_config, load_config_data, save_config_text, write_config_data


def test_save_config_text_normalizes_blank_lines(tmp_path):
    path = tmp_path / "config.yaml"
    save_config_text("app:\n\n\n  host: 127.0.0.1\n\n\n  port: 8000\n", path)

    assert path.read_text(encoding="utf-8") == "app:\n  host: 127.0.0.1\n  port: 8000\n"


def test_write_config_data_preserves_unknown_sections(tmp_path):
    path = tmp_path / "config.yaml"
    write_config_data({"app": {"host": "0.0.0.0"}, "custom": {"enabled": True}}, path)

    assert load_config_data(path)["custom"]["enabled"] is True


def test_load_config_ignores_unknown_keys_and_coerces_booleans(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "claude:\n  allow_cr_fix: 'yes'\n  auto_cr_fix: 'falsegf'\n  future: value\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.claude.allow_cr_fix is True
    assert config.claude.auto_cr_fix is False
