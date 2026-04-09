"""Tests for bambox credentials — storage, path resolution, cloud token."""

from __future__ import annotations

import json
import sys
import tomllib

import pytest

from bambox.credentials import (
    _credentials_path,
    _escape_toml_value,
    _load_raw,
    _quote_toml_key,
    _write_credentials,
    cloud_token_json,
    list_printers,
    load_cloud_credentials,
    load_printer_credentials,
    mask_serial,
    save_cloud_credentials,
    save_printer,
)


class TestMaskSerial:
    def test_long_serial(self):
        assert mask_serial("01P00A451601106") == "***********1106"

    def test_short_serial(self):
        assert mask_serial("AB") == "AB"

    def test_exactly_four(self):
        assert mask_serial("ABCD") == "ABCD"

    def test_five_chars(self):
        assert mask_serial("ABCDE") == "*BCDE"

    def test_empty_string(self):
        assert mask_serial("") == ""

    def test_one_char(self):
        assert mask_serial("X") == "X"

    def test_eight_chars(self):
        assert mask_serial("12345678") == "****5678"


class TestEscapeTomlValue:
    def test_plain_string(self):
        assert _escape_toml_value("hello") == "hello"

    def test_escapes_backslash(self):
        assert _escape_toml_value("a\\b") == "a\\\\b"

    def test_escapes_double_quote(self):
        assert _escape_toml_value('say "hi"') == 'say \\"hi\\"'

    def test_escapes_newline(self):
        assert _escape_toml_value("line1\nline2") == "line1\\nline2"

    def test_combined(self):
        assert _escape_toml_value('"a\\b\n"') == '\\"a\\\\b\\n\\"'


class TestQuoteTomlKey:
    def test_simple_identifier(self):
        assert _quote_toml_key("workshop") == "workshop"

    def test_key_with_dot(self):
        assert _quote_toml_key("my.printer") == '"my.printer"'

    def test_key_with_space(self):
        assert _quote_toml_key("my printer") == '"my printer"'

    def test_key_with_hyphen(self):
        # Hyphens are not valid in Python identifiers, so must be quoted
        assert _quote_toml_key("my-printer") == '"my-printer"'


class TestTomlRoundtripSpecialChars:
    def test_printer_name_with_dot(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        save_printer("my.printer", {"type": "bambu-cloud", "serial": "SN001"})

        raw = _load_raw()
        assert raw["printers"]["my.printer"]["serial"] == "SN001"

    def test_value_with_quotes(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        _write_credentials({"cloud": {"token": 'tok"with"quotes', "email": "a@b.com"}})

        raw = _load_raw()
        assert raw["cloud"]["token"] == 'tok"with"quotes'

    def test_value_with_backslash(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        _write_credentials({"cloud": {"token": "tok\\slash", "email": "a@b.com"}})

        raw = _load_raw()
        assert raw["cloud"]["token"] == "tok\\slash"


class TestCredentialsPath:
    def test_bambox_env_var(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom.toml"
        monkeypatch.setenv("BAMBOX_CREDENTIALS", str(custom))
        monkeypatch.delenv("ESTAMPO_CREDENTIALS", raising=False)
        assert _credentials_path() == custom

    def test_estampo_env_var_fallback(self, tmp_path, monkeypatch):
        custom = tmp_path / "estampo_creds.toml"
        monkeypatch.delenv("BAMBOX_CREDENTIALS", raising=False)
        monkeypatch.setenv("ESTAMPO_CREDENTIALS", str(custom))
        assert _credentials_path() == custom

    def test_bambox_env_takes_precedence(self, tmp_path, monkeypatch):
        bambox = tmp_path / "bambox.toml"
        estampo = tmp_path / "estampo.toml"
        monkeypatch.setenv("BAMBOX_CREDENTIALS", str(bambox))
        monkeypatch.setenv("ESTAMPO_CREDENTIALS", str(estampo))
        assert _credentials_path() == bambox

    def test_reads_estampo_file_when_bambox_missing(self, tmp_path, monkeypatch):
        """Falls back to estampo path when bambox file doesn't exist."""
        monkeypatch.delenv("BAMBOX_CREDENTIALS", raising=False)
        monkeypatch.delenv("ESTAMPO_CREDENTIALS", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("bambox.credentials.Path.home", lambda: tmp_path)

        estampo_path = tmp_path / ".config" / "estampo" / "credentials.toml"
        estampo_path.parent.mkdir(parents=True)
        estampo_path.write_text('[cloud]\ntoken = "old"\n')

        result = _credentials_path()
        assert result == estampo_path

    def test_defaults_to_bambox_for_new_install(self, tmp_path, monkeypatch):
        """When neither file exists, defaults to bambox path."""
        monkeypatch.delenv("BAMBOX_CREDENTIALS", raising=False)
        monkeypatch.delenv("ESTAMPO_CREDENTIALS", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("bambox.credentials.Path.home", lambda: tmp_path)

        result = _credentials_path()
        assert "bambox" in result.parts
        assert "estampo" not in result.parts

    def test_windows_path(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BAMBOX_CREDENTIALS", raising=False)
        monkeypatch.delenv("ESTAMPO_CREDENTIALS", raising=False)
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("bambox.credentials.Path.home", lambda: tmp_path)
        path = _credentials_path()
        assert "AppData" in str(path) or "Roaming" in str(path)


class TestWriteAndLoad:
    def test_roundtrip(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        _write_credentials(
            {
                "cloud": {"token": "t", "refresh_token": "r", "email": "a@b.com", "uid": "1"},
                "printers": {"workshop": {"type": "bambu-cloud", "serial": "SN001"}},
            }
        )

        raw = _load_raw()
        assert raw["cloud"]["token"] == "t"
        assert raw["printers"]["workshop"]["serial"] == "SN001"

    def test_file_permissions(self, tmp_path, monkeypatch):
        if sys.platform == "win32":
            pytest.skip("chmod not reliable on Windows")
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        _write_credentials({"cloud": {"token": "t"}})
        assert cred_path.stat().st_mode & 0o777 == 0o600

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "deep" / "nested" / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        _write_credentials({"cloud": {"token": "t"}})
        assert cred_path.exists()


class TestCloudCredentials:
    def test_save_and_load(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        save_cloud_credentials(
            token="tok123", refresh_token="ref456", email="user@test.com", uid="9999"
        )

        cloud = load_cloud_credentials()
        assert cloud["token"] == "tok123"
        assert cloud["refresh_token"] == "ref456"
        assert cloud["email"] == "user@test.com"
        assert cloud["uid"] == "9999"

    def test_load_returns_none_when_missing(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)
        assert load_cloud_credentials() is None

    def test_preserves_printers(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text('[printers.workshop]\ntype = "bambu-cloud"\nserial = "SN001"\n')
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        save_cloud_credentials(token="tok", refresh_token="ref", email="a@b.com", uid="1")

        with open(cred_path, "rb") as f:
            data = tomllib.load(f)
        assert data["cloud"]["token"] == "tok"
        assert data["printers"]["workshop"]["serial"] == "SN001"


class TestListPrinters:
    def test_lists_all(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text(
            '[printers.workshop]\ntype = "bambu-cloud"\nserial = "SN001"\n\n'
            '[printers.farm]\ntype = "bambu-cloud"\nserial = "SN002"\n'
        )
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        printers = list_printers()
        assert "workshop" in printers
        assert "farm" in printers

    def test_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)
        assert list_printers() == {}


class TestSavePrinter:
    def test_saves_new_printer(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        save_printer("workshop", {"type": "bambu-cloud", "serial": "SN001"})

        with open(cred_path, "rb") as f:
            data = tomllib.load(f)
        assert data["printers"]["workshop"]["type"] == "bambu-cloud"
        assert data["printers"]["workshop"]["serial"] == "SN001"

    def test_adds_to_existing(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text('[printers.old]\ntype = "bambu-cloud"\nserial = "SN001"\n')
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        save_printer("new", {"type": "bambu-cloud", "serial": "SN002"})

        with open(cred_path, "rb") as f:
            data = tomllib.load(f)
        assert data["printers"]["old"]["serial"] == "SN001"
        assert data["printers"]["new"]["serial"] == "SN002"


class TestLoadPrinterCredentials:
    def test_loads_named_printer(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text('[printers.workshop]\ntype = "bambu-cloud"\nserial = "SN001"\n')
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        creds = load_printer_credentials("workshop")
        assert creds["type"] == "bambu-cloud"
        assert creds["serial"] == "SN001"

    def test_env_var_override(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text('[printers.test]\ntype = "bambu-cloud"\nserial = "FILESERIAL"\n')
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)
        monkeypatch.setenv("BAMBU_SERIAL", "ENVSERIAL")

        creds = load_printer_credentials("test")
        assert creds["serial"] == "ENVSERIAL"

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "nonexistent" / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        with pytest.raises(RuntimeError, match="Credentials file not found"):
            load_printer_credentials("myprinter")

    def test_printer_not_found_raises(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text('[printers.workshop]\ntype = "bambu-cloud"\nserial = "SN001"\n')
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        with pytest.raises(RuntimeError, match="Printer 'missing' not found"):
            load_printer_credentials("missing")


class TestCloudTokenJson:
    def test_generates_temp_file(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text(
            '[cloud]\ntoken = "mytoken"\nrefresh_token = "myrefresh"\n'
            'email = "a@b.com"\nuid = "123"\n'
        )
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        with cloud_token_json() as path:
            assert path.exists()
            data = json.loads(path.read_text())
            assert data["token"] == "mytoken"
            assert data["refreshToken"] == "myrefresh"
            assert data["email"] == "a@b.com"
            assert data["uid"] == "123"

        assert not path.exists()

    def test_raises_without_cloud_creds(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        with pytest.raises(RuntimeError, match="No cloud credentials"):
            with cloud_token_json():
                pass

    def test_cleanup_on_exception(self, tmp_path, monkeypatch):
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text(
            '[cloud]\ntoken = "tok"\nrefresh_token = "ref"\nemail = "a@b.com"\nuid = "1"\n'
        )
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        temp_path = None
        with pytest.raises(RuntimeError):
            with cloud_token_json() as path:
                temp_path = path
                assert path.exists()
                raise RuntimeError("deliberate error")

        assert temp_path is not None
        assert not temp_path.exists()

    def test_file_permissions(self, tmp_path, monkeypatch):
        if sys.platform == "win32":
            pytest.skip("chmod not reliable on Windows")
        cred_path = tmp_path / "credentials.toml"
        cred_path.write_text(
            '[cloud]\ntoken = "tok"\nrefresh_token = "ref"\nemail = "a@b.com"\nuid = "1"\n'
        )
        monkeypatch.setattr("bambox.credentials._credentials_path", lambda: cred_path)

        with cloud_token_json() as path:
            assert path.stat().st_mode & 0o777 == 0o600
