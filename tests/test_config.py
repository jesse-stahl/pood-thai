from pathlib import Path

from pood_thai.config import Settings


def test_from_env_uses_defaults_when_empty():
    settings = Settings.from_env({})
    assert settings.azure_key == ""
    assert settings.azure_region == "southeastasia"
    assert settings.claude_model == "sonnet"
    assert settings.whisper_model == "mlx-community/whisper-large-v3-turbo"
    assert settings.data_dir == Path("./data")
    assert settings.db_path == Path("./data") / "pood-thai.sqlite3"


def test_from_env_reads_values():
    settings = Settings.from_env(
        {
            "AZURE_SPEECH_KEY": "k",
            "AZURE_SPEECH_REGION": "eastus",
            "POOD_CLAUDE_MODEL": "haiku",
            "POOD_WHISPER_MODEL": "mlx-community/whisper-small",
            "POOD_DATA_DIR": "/tmp/pood",
        }
    )
    assert settings.azure_key == "k"
    assert settings.azure_region == "eastus"
    assert settings.claude_model == "haiku"
    assert settings.whisper_model == "mlx-community/whisper-small"
    assert settings.db_path == Path("/tmp/pood/pood-thai.sqlite3")
