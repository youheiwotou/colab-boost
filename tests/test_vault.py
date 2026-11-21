"""Unit & concurrency tests for CredentialVault, keyring fallback, and recursive secret redaction."""

from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Optional

import pytest

from colab_boost.config import WorkspacePaths
import colab_boost.vault as vault_mod
from colab_boost.vault import (
    COLAB_CLI_SERVICE,
    COLAB_CLI_USER,
    VAULT_SERVICE,
    CredentialVault,
    _extract_token_secrets,
    _restrict_windows_acl,
    redact_secrets,
    redact_value,
)


class _FakeKeyring:
    def __init__(
        self,
        *,
        mismatch_on_get: bool = False,
        fail_get: bool = False,
        fail_set: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.mismatch_on_get = mismatch_on_get
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.fail_delete = fail_delete
        self.calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> Optional[str]:
        self.calls.append(("get", service, username))
        if self.fail_get:
            raise RuntimeError("keyring get failed")
        if self.mismatch_on_get:
            return "corrupted-readback"
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", service, username))
        if self.fail_set:
            raise RuntimeError("keyring set failed")
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
        if self.fail_delete:
            raise RuntimeError("keyring delete failed")
        self.store.pop((service, username), None)


def test_keyring_success_read_write_delete(isolated_paths: WorkspacePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLAB_BOOST_VAULT_BACKEND", raising=False)
    fake_kr = _FakeKeyring()
    monkeypatch.setattr(vault_mod, "keyring", fake_kr)

    vault = CredentialVault(isolated_paths, force_file_backend=False)
    assert vault.use_file is False

    vault.set_secret("hf_token", "  hf_live_keyring_12345  ")
    assert fake_kr.store[(VAULT_SERVICE, "secret:hf_token")] == "hf_live_keyring_12345"
    assert not isolated_paths.vault_fallback_file.exists()
    assert vault.get_secret("hf_token") == "hf_live_keyring_12345"
    assert vault.has_secret("hf_token") is True
    assert vault.secret_status() == {"civitai_token": False, "hf_token": True}

    vault.delete_secret("hf_token")
    assert (VAULT_SERVICE, "secret:hf_token") not in fake_kr.store
    assert vault.get_secret("hf_token") is None
    assert vault.has_secret("hf_token") is False


def test_keyring_readback_mismatch_degrades_to_file_store(
    isolated_paths: WorkspacePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COLAB_BOOST_VAULT_BACKEND", raising=False)
    fake_kr = _FakeKeyring(mismatch_on_get=True)
    monkeypatch.setattr(vault_mod, "keyring", fake_kr)

    vault = CredentialVault(isolated_paths, force_file_backend=False)
    vault.set_secret("civitai_token", "civ_token_xyz987")

    # Because keyring readback did not match, vault must have degraded to fallback file
    assert isolated_paths.vault_fallback_file.exists()
    raw_file = json.loads(isolated_paths.vault_fallback_file.read_text(encoding="utf-8"))
    assert raw_file["secret:civitai_token"] == "civ_token_xyz987"

    # When keyring later returns None, vault reads seamlessly from fallback file
    fake_kr.mismatch_on_get = False
    fake_kr.store.clear()
    assert vault.get_secret("civitai_token") == "civ_token_xyz987"


def test_keyring_exceptions_fallback_to_file_store(
    isolated_paths: WorkspacePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COLAB_BOOST_VAULT_BACKEND", raising=False)
    fake_kr = _FakeKeyring(fail_get=True, fail_set=True, fail_delete=True)
    monkeypatch.setattr(vault_mod, "keyring", fake_kr)

    vault = CredentialVault(isolated_paths, force_file_backend=False)
    vault.set_secret("hf_token", "hf_fallback_secret_999")
    assert isolated_paths.vault_fallback_file.exists()
    assert vault.get_secret("hf_token") == "hf_fallback_secret_999"

    # Setting blank/whitespace clears the secret via _delete (surviving keyring delete error)
    vault.set_secret("hf_token", "   ")
    assert vault.get_secret("hf_token") is None


def test_read_existing_colab_cli_token_keyring_legacy_missing_and_corrupt(
    isolated_paths: WorkspacePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. File-mode vault never reads external OS CLI token
    file_vault = CredentialVault(isolated_paths, force_file_backend=True)
    assert file_vault.read_existing_colab_cli_token() is None

    # 2. Keyring hit takes priority
    monkeypatch.delenv("COLAB_BOOST_VAULT_BACKEND", raising=False)
    fake_kr = _FakeKeyring()
    fake_kr.store[(COLAB_CLI_SERVICE, COLAB_CLI_USER)] = '{"token": "cli-kr-tok"}'
    monkeypatch.setattr(vault_mod, "keyring", fake_kr)

    vault = CredentialVault(isolated_paths, force_file_backend=False)
    assert vault.read_existing_colab_cli_token() == '{"token": "cli-kr-tok"}'

    # 3. Keyring error + missing legacy file -> returns None
    fake_kr.fail_get = True
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(
        vault_mod.os.path,
        "expanduser",
        lambda p: str(fake_home / ".config" / "colab-cli" / "token.json") if "colab-cli" in p else p,
    )
    assert vault.read_existing_colab_cli_token() is None

    # 4. Legacy file exists with valid JSON
    legacy_path = fake_home / ".config" / "colab-cli" / "token.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('  {"token": "legacy-tok", "refresh_token": "legacy-rt"}  \n', encoding="utf-8")
    assert vault.read_existing_colab_cli_token() == '{"token": "legacy-tok", "refresh_token": "legacy-rt"}'

    # 5. Legacy file exists with corrupt JSON (read_existing returns raw text; downstream inspect_token validates)
    legacy_path.write_text("{corrupt-json", encoding="utf-8")
    assert vault.read_existing_colab_cli_token() == "{corrupt-json"

    # 6. Legacy file exists but empty/whitespace -> returns None
    legacy_path.write_text("   \n\t ", encoding="utf-8")
    assert vault.read_existing_colab_cli_token() is None

    # 7. Legacy file read OSError -> returns None
    legacy_path.write_text('{"token": "unreadable"}', encoding="utf-8")
    real_open = open

    def failing_open(file, *args, **kwargs):
        if Path(file) == legacy_path:
            raise OSError("permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    assert vault.read_existing_colab_cli_token() is None


def test_invalid_secret_slots_delete_and_corrupt_file_store(isolated_paths: WorkspacePaths) -> None:
    vault = CredentialVault(isolated_paths, force_file_backend=True)

    for bad_slot in ("", "openai_token", "../hf_token", "HF_TOKEN"):
        with pytest.raises(ValueError, match="Unsupported secret slot"):
            vault.set_secret(bad_slot, "val1234")
        with pytest.raises(ValueError, match="Unsupported secret slot"):
            vault.get_secret(bad_slot)
        with pytest.raises(ValueError, match="Unsupported secret slot"):
            vault.has_secret(bad_slot)
        with pytest.raises(ValueError, match="Unsupported secret slot"):
            vault.delete_secret(bad_slot)

    # Corrupt JSON & non-dict JSON in fallback file store never crash reads
    isolated_paths.vault_fallback_file.write_text("{not-valid-json", encoding="utf-8")
    assert vault.get_secret("hf_token") is None
    isolated_paths.vault_fallback_file.write_text('["not", "a", "dict"]', encoding="utf-8")
    assert vault.get_secret("hf_token") is None

    # Account token & delete_account_token + corrupt run_secrets handling
    tok_json = json.dumps({"token": "acc-tok-1234", "refresh_token": "acc-rt-5678"})
    vault.set_account_token("acc-1", tok_json)
    assert vault.has_account_token("acc-1") is True

    # Fresh vault instance hydrates _account_secret_values on get_account_token
    vault2 = CredentialVault(isolated_paths, force_file_backend=True)
    assert vault2.get_account_token("acc-1") == tok_json
    assert "acc-tok-1234" in vault2.all_known_secrets()
    assert "acc-rt-5678" in vault2.all_known_secrets()

    # Overwriting with non-secret JSON removes previous account secret cache
    vault2.set_account_token("acc-1", "not-json")
    assert "acc-tok-1234" not in vault2.all_known_secrets()
    vault2.delete_account_token("acc-1")
    assert vault2.has_account_token("acc-1") is False

    # Run secrets lifecycle + empty clearing + explicit delete + corrupt JSON resilience
    vault2.set_run_secrets("run-1", ["run_secret_alpha", "x", "run_secret_alpha"])
    assert vault2.get_run_secrets("run-1") == ["run_secret_alpha"]
    vault2.set_run_secrets("run-1", ["ab"])  # shorter than MIN_REDACT_LENGTH -> deletes entry
    assert vault2.get_run_secrets("run-1") == []

    vault2.set_run_secrets("run-2", ["run_secret_beta"])
    assert vault2.get_run_secrets("run-2") == ["run_secret_beta"]
    vault2.delete_run_secrets("run-2")
    assert vault2.get_run_secrets("run-2") == []

    # Corrupt run_secrets entry in file store
    raw_store = vault2._read_file_store()
    raw_store["run_secrets:run-corrupt"] = "{broken"
    vault2._write_file_store(raw_store)
    assert vault2.get_run_secrets("run-corrupt") == []
    assert isinstance(vault2.all_known_secrets(), list)


def test_multithreaded_file_store_concurrent_writes_no_lost_updates_or_permission_errors(
    isolated_paths: WorkspacePaths,
) -> None:
    vault = CredentialVault(isolated_paths, force_file_backend=True)
    num_threads = 8
    writes_per_thread = 15
    errors: list[Exception] = []
    barrier = threading.Barrier(num_threads)

    def worker(tid: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            for idx in range(writes_per_thread):
                acc_id = f"acc-t{tid}-{idx}"
                payload = json.dumps({"token": f"tok-t{tid}-{idx}-secret"})
                vault.set_account_token(acc_id, payload)
                read_back = vault.get_account_token(acc_id)
                assert read_back == payload
                if idx % 3 == 0:
                    vault.set_run_secrets(f"run-t{tid}-{idx}", [f"rsec-t{tid}-{idx}"])
                    assert vault.get_run_secrets(f"run-t{tid}-{idx}") == [f"rsec-t{tid}-{idx}"]
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20.0)

    assert not errors, f"Concurrent vault access raised errors: {errors}"
    # Verify every single key survived without lost updates
    for tid in range(num_threads):
        for idx in range(writes_per_thread):
            acc_id = f"acc-t{tid}-{idx}"
            assert vault.get_account_token(acc_id) == json.dumps({"token": f"tok-t{tid}-{idx}-secret"})


def test_register_secret_extract_and_recursive_redact_value(isolated_paths: WorkspacePaths) -> None:
    # _extract_token_secrets edge cases
    assert _extract_token_secrets(None) == []
    assert _extract_token_secrets("") == []
    assert _extract_token_secrets("{bad") == []
    assert _extract_token_secrets('["not-dict"]') == []
    assert set(
        _extract_token_secrets(
            json.dumps(
                {
                    "token": "tok_abcd",
                    "access_token": "acc_efgh",
                    "refresh_token": "ref_ijkl",
                    "id_token": "id_mnop",
                    "client_secret": "sec_qrst",
                    "short": "abc",
                    "other": "public_value",
                }
            )
        )
    ) == {"tok_abcd", "acc_efgh", "ref_ijkl", "id_mnop", "sec_qrst"}

    vault = CredentialVault(isolated_paths, force_file_backend=True)
    vault.set_secret("hf_token", "hf_outer_secret_token_999")
    vault.register_secret("hf_outer")  # shorter prefix of hf_token
    vault.register_secret("dyn_ephemeral_777")
    vault.register_secret("ab")  # ignored (< MIN_REDACT_LENGTH)
    vault.set_run_secrets("run-r1", ["per_run_secret_444"])

    known = vault.all_known_secrets()
    assert "hf_outer_secret_token_999" in known
    assert "hf_outer" in known
    assert "dyn_ephemeral_777" in known
    assert "per_run_secret_444" in known
    assert "ab" not in known

    # Longest secret replaced first so full token becomes single "***" (not "***_secret_token_999")
    assert redact_secrets("tok=hf_outer_secret_token_999", known) == "tok=***"
    assert redact_secrets("", known) == ""
    assert redact_secrets("plain", []) == "plain"

    # Recursive redaction across strings, dict keys/values, lists, tuples, and scalars
    nested = {
        "key_dyn_ephemeral_777": "value with hf_outer_secret_token_999",
        "list_field": ["prefix-per_run_secret_444", {"inner": ("tuple-dyn_ephemeral_777", 42, True, None)}],
        123: ("safe", "hf_outer"),
    }
    redacted = redact_value(nested, known)
    assert redacted == {
        "key_***": "value with ***",
        "list_field": ["prefix-***", {"inner": ("tuple-***", 42, True, None)}],
        123: ("safe", "***"),
    }
    assert redact_value(None, known) is None
    assert redact_value({"a": "b"}, []) == {"a": "b"}

    # unregister_secret removes dynamic secret
    vault.unregister_secret("dyn_ephemeral_777")
    assert "dyn_ephemeral_777" not in vault.all_known_secrets()


def test_restrict_windows_acl_handles_non_windows_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy = tmp_path / "vault.json"
    dummy.write_text("{}", encoding="utf-8")

    # Non-win32 early return
    monkeypatch.setattr(vault_mod.sys, "platform", "linux")
    _restrict_windows_acl(dummy)

    # Win32 path with subprocess error swallowed gracefully
    monkeypatch.setattr(vault_mod.sys, "platform", "win32")
    monkeypatch.setattr(vault_mod, "_WINDOWS_USER_SID", None)
    monkeypatch.setattr(
        vault_mod.subprocess,
        "Popen",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("whoami blocked")),
    )
    _restrict_windows_acl(dummy)

    # Empty principal returns without invoking icacls
    monkeypatch.setattr(vault_mod, "_WINDOWS_USER_SID", None)
    monkeypatch.setattr(
        vault_mod.subprocess,
        "Popen",
        lambda *_a, **_kw: SimpleNamespace(stdout=None, returncode=1, wait=lambda timeout=5: 1),
    )
    monkeypatch.delenv("USERNAME", raising=False)
    _restrict_windows_acl(dummy)
