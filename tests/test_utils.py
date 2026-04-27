from app.utils import branch_name, inject_token_into_url, mask_secrets, slugify


def test_slugify_branch_name():
    assert slugify("Fix: Login / Auth!!!") == "fix_login_auth"
    assert branch_name("ABC-12", "Fix login") == "ABC-12/by_claude_fix_login"


def test_mask_secrets():
    assert mask_secrets("token abc123", ["abc123"]) == "token ***"
    assert mask_secrets("https://me:abc123@example.com/repo.git", ["abc123"]) == "https://***@example.com/repo.git"


def test_inject_token_into_https_url():
    url = inject_token_into_url("https://github.com/acme/repo.git", "me", "tok")
    assert url == "https://me:tok@github.com/acme/repo.git"
