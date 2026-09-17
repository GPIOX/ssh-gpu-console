"""Host-key trust store and transport gate tests (no SSH server involved)."""

from __future__ import annotations

import json
from pathlib import Path

import asyncssh
from app.ssh.executor import Executor
from app.ssh.known_hosts import HostKeyTrust, TrustedHostKey
from app.ssh.transport import AsyncsshExecutor, HostKeyDecision, _HostKeyGate, load_user_known_hosts

# Local key generation only: no SSH server, no network.
_PUBLIC_LINE_A = (
    asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode("ascii").strip()
)
_PUBLIC_LINE_B = (
    asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode("ascii").strip()
)


def _trust(tmp_path: Path) -> HostKeyTrust:
    return HostKeyTrust(tmp_path / "trusted_host_keys.json")


def test_add_persists_json_atomically_and_dedupes(tmp_path: Path) -> None:
    store = _trust(tmp_path)
    store.add(
        TrustedHostKey(host="Lab-Box", port=22, key_type="ssh-ed25519", fingerprint="SHA256:abc")
    )
    store.add(
        TrustedHostKey(host="lab-box", port=22, key_type="ssh-ed25519", fingerprint="SHA256:abc")
    )
    data = json.loads((tmp_path / "trusted_host_keys.json").read_text())
    assert data["version"] == 1
    assert len(data["entries"]) == 1
    assert data["entries"][0]["host"] == "lab-box"  # normalized casefold
    assert store.is_trusted("LAB-BOX", 22, "SHA256:abc")
    assert not store.is_trusted("lab-box", 2222, "SHA256:abc")
    assert not store.is_trusted("lab-box", 22, "SHA256:different")
    assert store.has_host("lab-box", 22)
    assert not store.has_host("other", 22)


def test_corrupt_trust_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "trusted_host_keys.json"
    path.write_text("{broken")
    store = HostKeyTrust(path)
    assert store.entries() == ()
    assert path.read_text() == "{broken"  # corrupt file is not renamed or deleted


def test_user_known_hosts_is_never_modified(tmp_path: Path) -> None:
    user_file = tmp_path / "known_hosts"
    original = "server-a ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIkey1\n"
    user_file.write_text(original)
    store = _trust(tmp_path)
    store.add(
        TrustedHostKey(host="server-x", port=22, key_type="ssh-ed25519", fingerprint="SHA256:k")
    )
    assert user_file.read_text() == original


def test_load_user_known_hosts_skips_junk_keeps_valid(tmp_path: Path) -> None:
    user_file = tmp_path / "known_hosts"
    user_file.write_text(
        "# comment\n"
        f"host-a {_PUBLIC_LINE_A}\n"
        f"@revoked host-b {_PUBLIC_LINE_B}\n"
        "junkline\n"
        "host-c ssh-ed25519 not-valid-base64!!\n"
    )
    known = load_user_known_hosts(user_file)
    host_a, *_rest = known.match("host-a", "1.2.3.4", 22)
    assert len(host_a) == 1
    _host_keys, _ca_keys, revoked, *_x509 = known.match("host-b", "1.2.3.4", 22)
    assert len(revoked) == 1  # marker lines stay honored
    missing, *_rest = known.match("missing", "1.2.3.4", 22)
    assert missing == []


def test_load_user_known_hosts_missing_file_means_empty_not_disabled(tmp_path: Path) -> None:
    known = load_user_known_hosts(tmp_path / "does_not_exist")
    host_keys, *_rest = known.match("anything", "1.2.3.4", 22)
    assert host_keys == []


def _gate(tmp_path: Path, user_text: str = "") -> tuple[_HostKeyGate, HostKeyDecision]:
    user_file = tmp_path / "known_hosts"
    if user_text:
        user_file.write_text(user_text)
    decision = HostKeyDecision()
    gate = _HostKeyGate(load_user_known_hosts(user_file), _trust(tmp_path), decision)
    return gate, decision


def test_gate_unknown_host_captures_key_for_prompt(tmp_path: Path) -> None:
    gate, decision = _gate(tmp_path)
    key = asyncssh.import_public_key(_PUBLIC_LINE_B)
    assert gate.validate_host_public_key("new-host", "1.2.3.4", 22, key) is False
    assert decision.captured is True
    assert decision.mismatch is False
    assert decision.fingerprint == key.get_fingerprint()
    assert decision.key_type == "ssh-ed25519"


def test_gate_mismatch_against_user_known_hosts_entry(tmp_path: Path) -> None:
    gate, decision = _gate(tmp_path, f"host-a {_PUBLIC_LINE_A}\n")
    other = asyncssh.import_public_key(_PUBLIC_LINE_B)
    assert gate.validate_host_public_key("host-a", "1.2.3.4", 22, other) is False
    assert decision.captured is True
    assert decision.mismatch is True  # security event: no trust path


def test_gate_mismatch_against_trust_store_fingerprint(tmp_path: Path) -> None:
    key_a = asyncssh.import_public_key(_PUBLIC_LINE_A)
    store = _trust(tmp_path)
    store.add(
        TrustedHostKey(
            host="host-a", port=22, key_type="ssh-ed25519", fingerprint=key_a.get_fingerprint()
        )
    )
    decision = HostKeyDecision()
    gate = _HostKeyGate(load_user_known_hosts(tmp_path / "known_hosts"), store, decision)
    other = asyncssh.import_public_key(_PUBLIC_LINE_B)
    assert gate.validate_host_public_key("host-a", "1.2.3.4", 22, other) is False
    assert decision.mismatch is True

    # The trusted key itself is accepted through the gate.
    assert gate.validate_host_public_key("host-a", "1.2.3.4", 22, key_a) is True


def test_gate_respects_port_in_trust_store(tmp_path: Path) -> None:
    key = asyncssh.import_public_key(_PUBLIC_LINE_A)
    store = _trust(tmp_path)
    store.add(
        TrustedHostKey(
            host="host-a", port=2222, key_type="ssh-ed25519", fingerprint=key.get_fingerprint()
        )
    )
    decision = HostKeyDecision()
    gate = _HostKeyGate(load_user_known_hosts(tmp_path / "known_hosts"), store, decision)
    assert gate.validate_host_public_key("host-a", "1.2.3.4", 22, key) is False
    assert decision.mismatch is False  # port 22 was never trusted: a fresh prompt
    assert decision.fingerprint == key.get_fingerprint()


def test_executor_conforms_to_protocol() -> None:
    assert issubclass(AsyncsshExecutor, Executor)  # runtime-checkable protocol
