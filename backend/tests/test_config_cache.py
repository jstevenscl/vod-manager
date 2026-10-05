import json

import config


def test_config_reads_are_cached_until_written(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.setattr(config, "_raw_cache", None)

    config_path.write_text(json.dumps({"value": 1}))
    assert config._read_raw()["value"] == 1

    config_path.write_text(json.dumps({"value": 2}))
    assert config._read_raw()["value"] == 1

    config._write_raw({"value": 3})
    assert config._read_raw()["value"] == 3
