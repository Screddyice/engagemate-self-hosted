from engagemate.db.accounts import account_seats

def test_owner_workspace_is_independent_of_legacy_subscription():
    for plan in ["self_hosted", "starter", "pro", b"agency", None]:
        assert account_seats(plan) == 100
