"""First-boot diagnostics must not disclose a client authentication secret."""


def test_new_key_is_returned_but_never_printed(monkeypatch, tmp_path, capsys):
    import api.server as server

    capsys.readouterr()
    key = "test-only-client-key-do-not-log"
    monkeypatch.delenv("FERAL_API_KEY", raising=False)
    monkeypatch.setattr(server, "_get_api_key_path", lambda: tmp_path / "api_key")
    monkeypatch.setattr(server, "_generate_key_impl", lambda: key)
    assert server._load_or_generate_api_key() == key
    output = capsys.readouterr().out
    assert key not in output
    assert "Generated new API key" in output
    assert "value is not logged" in output


def test_existing_key_does_not_emit_first_boot_banner(monkeypatch, tmp_path, capsys):
    import api.server as server

    capsys.readouterr()
    key = "test-only-existing-client-key"
    path = tmp_path / "api_key"
    path.write_text(key)
    monkeypatch.delenv("FERAL_API_KEY", raising=False)
    monkeypatch.setattr(server, "_get_api_key_path", lambda: path)
    monkeypatch.setattr(server, "_generate_key_impl", lambda: key)
    assert server._load_or_generate_api_key() == key
    assert capsys.readouterr().out == ""
