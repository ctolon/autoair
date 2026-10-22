"""Knowledge base overrides / disable / reset, per-user LLM key storage, SSRF policy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from autoair.ai.config import AiSettings, KnowledgeSettings, UserLlmSettings
from autoair.ai.knowledge import KnowledgeBase, KnowledgeError
from autoair.ai.net import (
    UrlNotAllowed,
    check_url,
    check_url_sync,
    host_matches,
    is_public_ip,
    prefix_matches,
)
from autoair.ai.user_llm import (
    UserLlmError,
    UserLlmStore,
    UserLlmUpdate,
    build_user_llm,
    load_encryption_key,
    validate_update,
)

# --------------------------------------------------------------------------- knowledge


def kb(tmp_path: Path, **kw: object) -> KnowledgeBase:
    settings = KnowledgeSettings(include_repo_docs=False, **kw)  # type: ignore[arg-type]
    return KnowledgeBase(settings, str(tmp_path / "store"))


def test_builtin_override_disable_enable_reset(tmp_path: Path) -> None:
    base = kb(tmp_path)
    docs = {d.id: d for d in base.entries()}
    assert docs["airflow3-best-practices"].origin == "builtin"
    original = base.content("airflow3-best-practices")

    over = base.put("airflow3-best-practices", "# Our rules\n\n## Retries\nAlways 5 retries.")
    assert (over.origin, over.title, over.has_default) == ("overridden", "Our rules", True)
    assert "Always 5 retries" in base.content("airflow3-best-practices")
    assert base.content("airflow3-best-practices", default=True) == original
    texts = " ".join(d.text for d in base.documents())
    assert "Always 5 retries" in texts
    assert "data interval" not in texts.lower()

    off = base.set_enabled("dagspec-authoring", False)
    assert (off.enabled, off.disabled_by) == (False, "ui")
    assert not any(d.id.startswith("doc:dagspec-authoring") for d in base.documents())
    assert base.set_enabled("dagspec-authoring", True).enabled

    back = base.reset("airflow3-best-practices")
    assert back is not None
    assert back.origin == "builtin"
    assert base.content("airflow3-best-practices") == original


def test_settings_disabled_custom_docs_and_extra_dirs(tmp_path: Path) -> None:
    extra = tmp_path / "site-docs"
    extra.mkdir()
    (extra / "dagspec-authoring.md").write_text("# Site authoring guide\nUse our template.")
    (extra / "team-rules.md").write_text("# Team rules\nOwner is data-eng.")
    base = kb(tmp_path, disabled=["airflow3-best-practices"], extra_dirs=[str(extra)])
    docs = {d.id: d for d in base.entries()}
    assert docs["airflow3-best-practices"].enabled is False
    assert docs["airflow3-best-practices"].disabled_by == "settings"
    assert docs["dagspec-authoring"].origin == "extra"
    assert docs["team-rules"].origin == "extra"
    # UI re-enables a doc disabled by settings; reset restores the setting.
    assert base.set_enabled("airflow3-best-practices", True).enabled is True
    assert base.reset("airflow3-best-practices").enabled is False  # type: ignore[union-attr]
    custom = base.put("runbook", "# Runbook\nPage the on-call.")
    assert (custom.origin, custom.has_default) == ("custom", False)
    assert base.reset("runbook") is None
    with pytest.raises(KnowledgeError):
        base.put("../../etc/passwd", "x")
    with pytest.raises(KnowledgeError):
        base.content("missing-doc")
    small = kb(tmp_path, max_upload_bytes=1024)
    with pytest.raises(KnowledgeError, match="larger"):
        small.put("big", "x" * 5000)


# --------------------------------------------------------------------------- SSRF


def test_url_policy() -> None:
    assert is_public_ip("8.8.8.8")
    for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "100.64.0.1", "::ffff:10.0.0.1"):
        assert not is_public_ip(ip), ip
    assert host_matches("docs.airflow.apache.org", ["airflow.apache.org"])
    assert not host_matches("airflow.apache.org.evil.com", ["airflow.apache.org"])
    assert prefix_matches("https://llm.acme.io/v1/chat", ["https://llm.acme.io/v1"])
    assert not prefix_matches("https://llm.acme.io/v10", ["https://llm.acme.io/v1"])
    with pytest.raises(UrlNotAllowed, match="scheme"):
        check_url_sync("file:///etc/passwd")
    with pytest.raises(UrlNotAllowed, match="credentials"):
        check_url_sync("https://user:pw@example.com/")
    with pytest.raises(UrlNotAllowed, match="non-public"):
        check_url_sync("http://127.0.0.1:8080/")
    with pytest.raises(UrlNotAllowed, match="allow-list"):
        asyncio.run(check_url("https://evil.com/x", domains=["pypi.org"]))
    with pytest.raises(UrlNotAllowed, match="non-public"):
        asyncio.run(check_url("http://169.254.169.254/latest/meta-data"))
    asyncio.run(check_url("http://10.0.0.5/", allow_private=True))


# --------------------------------------------------------------------------- user LLMs


def test_user_key_encrypted_at_rest_and_never_returned(tmp_path: Path) -> None:
    key = load_encryption_key(UserLlmSettings(), tmp_path)
    assert (tmp_path / "ai-user-llm.key").stat().st_mode & 0o777 == 0o600
    assert load_encryption_key(UserLlmSettings(), tmp_path) == key  # reused
    store = UserLlmStore(str(tmp_path / "users"), key)
    raw = "sk-user-secret-abcdef123456"
    view = store.put(
        "alice", UserLlmUpdate(provider="openai", model="gpt-4.1", api_key=SecretStr(raw))
    )
    assert view.api_key_set
    assert view.api_key_last4 == "3456"
    assert raw not in view.model_dump_json()
    files = list((tmp_path / "users").glob("*.json"))
    assert len(files) == 1
    assert "alice" not in files[0].name
    on_disk = files[0].read_text()
    assert raw not in on_disk
    assert json.loads(on_disk)["api_key_enc"]
    stored = store.get("alice")
    assert stored is not None
    assert store.api_key(stored) == raw
    # update without a key keeps it; clear removes it
    store.put("alice", UserLlmUpdate(provider="openai", model="gpt-4.1-mini"))
    assert store.view("alice").api_key_set
    store.put("alice", UserLlmUpdate(provider="openai", clear_api_key=True))
    assert not store.view("alice").api_key_set
    assert store.view("bob").configured is False
    deleted = store.delete("alice")
    assert deleted
    assert not store.view("alice").configured
    other = UserLlmStore(
        str(tmp_path / "users"), load_encryption_key(UserLlmSettings(), tmp_path / "x")
    )
    store.put("carol", UserLlmUpdate(provider="openai", api_key=SecretStr(raw)))
    with pytest.raises(UserLlmError, match="decrypted"):
        other.api_key(other.get("carol"))  # type: ignore[arg-type]


def test_user_policy_restrictions() -> None:
    policy = UserLlmSettings(
        allowed_providers=["openai", "ollama"],
        allowed_models=["gpt-4.1*", "llama*"],
        allowed_base_urls=["http://10.0.0.5:11434"],
        allowed_conn_ids=["ai_*"],
    )

    def check(**kw: object) -> None:
        asyncio.run(validate_update(UserLlmUpdate(**kw), policy))  # type: ignore[arg-type]

    check(provider="openai", model="gpt-4.1-mini")
    with pytest.raises(UserLlmError, match="provider"):
        check(provider="anthropic")
    with pytest.raises(UserLlmError, match="model"):
        check(provider="openai", model="o3")
    with pytest.raises(UserLlmError, match="connection"):
        check(conn_id="prod_db")
    check(conn_id="ai_team")
    with pytest.raises(UserLlmError, match="allowed list"):
        check(provider="ollama", base_url="http://evil.example:11434")
    with pytest.raises(UserLlmError, match="non-public"):  # SSRF: private address
        check(provider="ollama", base_url="http://10.0.0.5:11434")
    open_policy = policy.model_copy(update={"allow_private_networks": True})
    asyncio.run(
        validate_update(
            UserLlmUpdate(provider="ollama", base_url="http://10.0.0.5:11434"), open_policy
        )
    )
    with pytest.raises(UserLlmError, match="provider defaults only"):
        asyncio.run(
            validate_update(
                UserLlmUpdate(provider="openai", base_url="https://x.io"), UserLlmSettings()
            )
        )


def test_build_user_llm(tmp_path: Path) -> None:
    store = UserLlmStore(str(tmp_path / "u"), load_encryption_key(UserLlmSettings(), tmp_path))
    store.put(
        "dan",
        UserLlmUpdate(provider="anthropic", model="claude-haiku-5-5", api_key=SecretStr("k" * 20)),
    )
    llm = asyncio.run(build_user_llm(store.get("dan"), store, AiSettings()))  # type: ignore[arg-type]
    assert (llm.provider, llm.model) == ("anthropic", "claude-haiku-5-5")
    store.put("eve", UserLlmUpdate(provider="anthropic"))
    with pytest.raises(UserLlmError, match="needs an API key"):
        asyncio.run(build_user_llm(store.get("eve"), store, AiSettings()))  # type: ignore[arg-type]
    restricted = AiSettings(user_llm=UserLlmSettings(allowed_providers=["openai"]))
    with pytest.raises(UserLlmError, match="no longer allowed"):
        asyncio.run(build_user_llm(store.get("dan"), store, restricted))  # type: ignore[arg-type]
