from app.config import save_config_text


def test_save_config_text_normalizes_blank_lines(tmp_path):
    path = tmp_path / "config.yaml"
    save_config_text("app:\n\n\n  host: 127.0.0.1\n\n\n  port: 8000\n", path)

    assert path.read_text(encoding="utf-8") == "app:\n  host: 127.0.0.1\n  port: 8000\n"
