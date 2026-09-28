from pipeline import client


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"result": "OK"}


def test_reprocess_documents_posts_ids_to_reprocess_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(client.session, "post", lambda url, **kw: calls.append((url, kw)) or _Resp())

    result = client.reprocess_documents([7, 9])

    assert result == "OK"
    url, kw = calls[0]
    assert url.endswith("/api/documents/reprocess/")
    assert kw["json"] == {"documents": [7, 9]}
