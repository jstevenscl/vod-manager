"""Persistent notices that stay until acknowledged."""

import notices
import notifications


def test_notice_add_list_ack(db):
    nid = notices.add("import_stall", "warn", "Still waiting for the encoder: X", "body", job_id=3)
    assert notices.count_open() == 1
    row = notices.list_open()[0]
    assert row["id"] == nid and row["job_id"] == 3 and row["severity"] == "warn" and row["acknowledged_at"] is None
    assert notices.acknowledge(nid) is True
    assert notices.count_open() == 0 and notices.list_open() == []
    assert notices.acknowledge(nid) is False


def test_notice_email_failure_does_not_raise(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(notifications, "send_email", boom)
    nid = notices.add("import_failed", "error", "Import failed: X", "b", email=True)
    assert nid and notices.count_open() == 1


def test_notice_email_sent_to_admin_recipients(db, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "send_email", lambda to, subj, body: sent.append((to, subj)) or True)
    monkeypatch.setattr(notices.config, "get_smtp_settings", lambda: {"host": "h", "admin_recipients": ["a@b.c"]})
    notices.add("import_failed", "error", "Import failed: X", "b", email=True)
    assert sent == [(["a@b.c"], "[VOD & DVR Manager] Import failed: X")]
