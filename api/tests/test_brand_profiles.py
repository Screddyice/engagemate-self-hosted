from uuid import uuid4

import pytest

from engagemate.db.brand_profiles import BrandProfileRepository


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM ig_accounts" in sql:
            return _Cursor([{"ok": 1}])
        if "FROM discovery_candidates" in sql:
            return _Cursor(self.rows)
        return _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection_obj = connection

    def connection(self):
        return self.connection_obj


@pytest.mark.parametrize("username, identity", [("creator", "123"), (b"creator", b"123")])
def test_approve_candidates_imports_only_selected_rows(username, identity):
    candidate_id = uuid4()
    connection = _Connection([{"id": candidate_id, "username": username, "ig_user_id": identity}])
    count = BrandProfileRepository(_Pool(connection)).approve_candidates(
        user_id=uuid4(), profile_id=uuid4(), candidate_ids=[candidate_id], ig_account_id=uuid4()
    )
    assert count == 1
    assert any("INSERT INTO targets" in sql for sql, _ in connection.calls)
    params = next(params for sql, params in connection.calls if "INSERT INTO targets" in sql)
    assert params["username"] == "creator"
    assert params["ig_user_id"] == "123"
    assert any("status = 'imported'" in sql for sql, _ in connection.calls)


def test_approve_candidates_rejects_partial_selection():
    connection = _Connection([])
    with pytest.raises(ValueError, match="unavailable"):
        BrandProfileRepository(_Pool(connection)).approve_candidates(
            user_id=uuid4(), profile_id=uuid4(), candidate_ids=[uuid4()], ig_account_id=uuid4()
        )


def test_approve_candidates_rejects_a_candidate_without_instagram_identity():
    candidate_id = uuid4()
    connection = _Connection(
        [{"id": candidate_id, "username": "creator", "ig_user_id": None}]
    )

    with pytest.raises(ValueError, match="no Instagram identity"):
        BrandProfileRepository(_Pool(connection)).approve_candidates(
            user_id=uuid4(),
            profile_id=uuid4(),
            candidate_ids=[candidate_id],
            ig_account_id=uuid4(),
        )

    assert not any("INSERT INTO targets" in sql for sql, _ in connection.calls)
