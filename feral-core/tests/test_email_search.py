"""Deep email search — IMAP X-GM-RAW, structured RFC3501, header-only fetch."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from integrations.email import EmailIntegration


class _FakeVault:
    def __init__(self):
        self.store_calls: dict[str, str] = {}

    def store(self, key_name: str, value: str, stored_by: str = "user") -> None:
        self.store_calls[key_name] = value

    def retrieve(self, key_name: str, requester: str = "executor"):
        return self.store_calls.get(key_name)


class _FakeOAuth:
    def __init__(self, vault):
        self._vault = vault

    def is_connected(self, provider_id: str) -> bool:
        return False


class FakeIMAP:
    """Minimal imaplib stand-in that records SEARCH/FETCH calls."""

    instances: list["FakeIMAP"] = []

    def __init__(self, host: str, port: int, timeout: float | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in = False
        self.selected: str | None = None
        self.search_calls: list[tuple] = []
        self.uid_search_calls: list[tuple] = []
        self.fetch_calls: list[tuple] = []
        self.uid_fetch_calls: list[tuple] = []
        self.search_result = [b"1 2 3"]
        FakeIMAP.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []

    def login(self, user: str, password: str):
        self.logged_in = True

    def select(self, mailbox: str):
        self.selected = mailbox
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        self.search_calls.append((charset, criteria))
        return ("OK", self.search_result)

    def uid(self, command: str, *args):
        cmd = command.upper()
        if cmd == "SEARCH":
            self.uid_search_calls.append(args)
            return ("OK", self.search_result)
        if cmd == "FETCH":
            self.uid_fetch_calls.append(args)
            uid = args[0]
            header_bytes = _make_header_bytes(
                subject=f"Subject for {uid.decode() if isinstance(uid, bytes) else uid}",
                from_addr="sender@example.com",
            )
            meta = (
                f"{uid.decode() if isinstance(uid, bytes) else uid} "
                f"(FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] "
                f"{{{len(header_bytes)}}})"
            ).encode()
            return ("OK", [(meta, header_bytes)])
        raise AssertionError(f"Unexpected UID command: {command}")

    def fetch(self, seq: str, spec: str):
        self.fetch_calls.append((seq, spec))
        seq_s = seq.decode() if isinstance(seq, bytes) else str(seq)
        header_bytes = _make_header_bytes(subject=f"Subject for {seq_s}")
        meta = (
            f"{seq_s} (FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] "
            f"{{{len(header_bytes)}}})"
        ).encode()
        return ("OK", [(meta, header_bytes)])

    def logout(self):
        pass


def _make_header_bytes(subject: str = "Hello", from_addr: str = "a@example.com") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["Date"] = "Mon, 1 Jan 2024 12:00:00 +0000"
    msg["Message-ID"] = "<test@example.com>"
    return msg.as_bytes()


def _make_email(host: str = "imap.gmail.com") -> EmailIntegration:
    vault = _FakeVault()
    oauth = _FakeOAuth(vault)
    email = EmailIntegration(oauth_manager=oauth)
    email.store_app_password("me@gmail.com", "abcdefghijklmnop")
    email._imap_host = host
    email._imap_user = "me@gmail.com"
    email._imap_pass = "abcdefghijklmnop"
    return email


@pytest.fixture(autouse=True)
def _patch_imap(monkeypatch):
    FakeIMAP.reset()

    def _factory(host, port, timeout=None):
        return FakeIMAP(host, port, timeout=timeout)

    monkeypatch.setattr("integrations.email.imaplib.IMAP4_SSL", _factory)
    yield
    FakeIMAP.reset()


@pytest.mark.asyncio
async def test_gmail_host_uses_x_gm_raw():
    email = _make_email("imap.gmail.com")
    result = await email.search(query="from:boss@corp.com has:attachment", max_results=5)

    assert result["success"] is True
    data = result["data"]
    assert data["source"] == "imap"
    assert data["query_used"] == "from:boss@corp.com has:attachment"
    assert len(data["messages"]) == 3

    conn = FakeIMAP.instances[0]
    assert conn.selected == "INBOX"
    assert conn.uid_search_calls == [("X-GM-RAW", '"from:boss@corp.com has:attachment"')]
    assert len(conn.uid_fetch_calls) == 3
    assert "BODY.PEEK[HEADER.FIELDS" in conn.uid_fetch_calls[0][1]
    assert conn.fetch_calls == []


@pytest.mark.asyncio
async def test_gmail_structured_params_compose_x_gm_raw():
    email = _make_email("imap.gmail.com")
    result = await email.search(
        from_="alice@example.com",
        subject="invoice",
        since="2024-01-15",
        before="2024-02-01",
        has_attachment=True,
        max_results=2,
    )

    assert result["success"] is True
    assert result["data"]["query_used"] == (
        "from:alice@example.com subject:invoice after:2024/01/15 before:2024/02/01 has:attachment"
    )
    conn = FakeIMAP.instances[0]
    assert conn.uid_search_calls[0][0] == "X-GM-RAW"
    assert "alice@example.com" in conn.uid_search_calls[0][1]


@pytest.mark.asyncio
async def test_generic_imap_builds_rfc3501_search():
    email = _make_email("mail.example.com")
    result = await email.search(
        from_="bob@example.com",
        subject="report",
        since="2024-03-01",
        before="2024-04-01",
        body="budget",
        max_results=10,
    )

    assert result["success"] is True
    assert result["data"]["source"] == "imap"
    expected = (
        '(FROM "bob@example.com" SUBJECT "report" SINCE 01-Mar-2024 '
        'BEFORE 01-Apr-2024 TEXT "budget")'
    )
    assert result["data"]["query_used"] == expected

    conn = FakeIMAP.instances[0]
    assert conn.search_calls == [(None, (expected,))]
    assert conn.uid_search_calls == []
    assert len(conn.fetch_calls) == 3
    assert "BODY.PEEK[HEADER.FIELDS" in conn.fetch_calls[0][1]


@pytest.mark.asyncio
async def test_generic_imap_free_text_uses_text_criterion():
    email = _make_email("mail.example.com")
    result = await email.search(query="quarterly update")

    assert result["success"] is True
    assert result["data"]["query_used"] == 'TEXT "quarterly update"'
    conn = FakeIMAP.instances[0]
    assert conn.search_calls[0][1] == ('TEXT "quarterly update"',)


@pytest.mark.asyncio
async def test_folder_selection():
    email = _make_email("imap.gmail.com")
    await email.search(query="in:sent", folder="[Gmail]/Sent Mail")

    conn = FakeIMAP.instances[0]
    assert conn.selected == "[Gmail]/Sent Mail"


@pytest.mark.asyncio
async def test_max_results_clamping(monkeypatch):
    many = b" ".join(str(i).encode() for i in range(1, 151))

    class ManyFakeIMAP(FakeIMAP):
        def __init__(self, host, port, timeout=None):
            super().__init__(host, port, timeout=timeout)
            self.search_result = [many]

    monkeypatch.setattr("integrations.email.imaplib.IMAP4_SSL", ManyFakeIMAP)
    email = _make_email("imap.gmail.com")
    result = await email.search(query="all", max_results=500)
    assert result["success"] is True
    assert len(result["data"]["messages"]) == 100

    result_low = await email.search(query="all", max_results=0)
    assert len(result_low["data"]["messages"]) == 1


@pytest.mark.asyncio
async def test_response_shape():
    email = _make_email("imap.gmail.com")

    class SingleFakeIMAP(FakeIMAP):
        def __init__(self, host, port, timeout=None):
            super().__init__(host, port, timeout=timeout)
            self.search_result = [b"42"]

    import integrations.email as email_mod

    orig = email_mod.imaplib.IMAP4_SSL
    email_mod.imaplib.IMAP4_SSL = SingleFakeIMAP
    try:
        result = await email.search(query="hello", max_results=1)
    finally:
        email_mod.imaplib.IMAP4_SSL = orig

    assert result["success"] is True
    assert len(result["data"]["messages"]) == 1
    msg = result["data"]["messages"][0]
    assert {"id", "from", "subject", "date", "snippet", "thread_id"} <= set(msg.keys())
    assert result["data"]["query_used"] == "hello"
    assert result["data"]["source"] == "imap"
    assert "next_page_token" not in result["data"]


def test_clamp_max_results_helper():
    assert EmailIntegration._clamp_max_results(0) == 1
    assert EmailIntegration._clamp_max_results(500) == 100
    assert EmailIntegration._clamp_max_results("bad", default=10) == 10


def test_compose_gmail_query():
    q = EmailIntegration._compose_gmail_query(
        "label:work",
        from_="x@y.com",
        since="2024-05-01",
        has_attachment=True,
    )
    assert q == "label:work from:x@y.com after:2024/05/01 has:attachment"


def test_build_generic_imap_search_all_when_empty():
    assert EmailIntegration._build_generic_imap_search("") == "ALL"
