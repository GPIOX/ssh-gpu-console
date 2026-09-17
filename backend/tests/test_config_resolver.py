"""SSH config resolver and discovery tests (pure parsing, no real config)."""

from __future__ import annotations

import os
from pathlib import Path

from app.servers.discovery import SSHConfigDiscovery
from app.ssh.config_resolver import SSHConfigResolver, resolve_connect_params

CFG = """# personal
Host github.com
    HostName github.com
    User git

Host lab-*
    HostName %h.internal
    User alice
    Port 2222
    IdentityFile ~/.ssh/lab_id_rsa
    IdentityFile ~/.ssh/lab2_id_ed25519

Host alpha4090 beta3090
    User train

Host jump-host
    HostName bastion.example.com
    ProxyJump relay01

Host *
    ServerAliveInterval 60
"""


def write_cfg(tmp_path: Path, text: str = CFG) -> SSHConfigResolver:
    path = tmp_path / "ssh_config"
    path.write_text(text)
    return SSHConfigResolver(path)


def test_list_aliases(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path)
    assert "github.com" in resolver.list_aliases()
    # wildcard patterns are not offered as aliases for onboarding
    assert "lab-*" not in resolver.list_aliases()
    assert "alpha4090" in resolver.list_aliases()
    assert "jump-host" in resolver.list_aliases()


def test_resolve_hostname_from_alias(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path)
    entry = resolver.resolve("lab-3090")
    # %h substitution: HostName lab-3090.internal
    assert entry.hostname == "lab-3090.internal"
    assert entry.user == "alice"
    assert entry.port == 2222
    assert len(entry.identity_files) == 2


def test_wildcard_folded_before_global_block(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path)
    # The `Host *` block must not clobber more specific blocks (no user set).
    entry = resolver.resolve("lab-3090")
    assert entry.user == "alice"


def test_multi_alias_block(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path)
    assert resolver.resolve("alpha4090").user == "train"
    assert resolver.resolve("beta3090").user == "train"


def test_proxy_jump(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path)
    assert resolver.resolve("jump-host").proxy_jump == "relay01"


def test_unknown_alias_defaults(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path)
    entry = resolver.resolve("no-such-host")
    assert entry.hostname is None
    assert entry.user is None
    assert entry.port is None


def test_resolve_connect_params_priority(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".ssh" / "lab_id_rsa").write_text("key")
    (fake_home / ".ssh" / "lab2_id_ed25519").write_text("key2")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))  # os.path.expanduser reads HOME
    resolver = write_cfg(tmp_path)
    # Explicit registry username/port wins over config.
    params = resolve_connect_params("lab-3090", resolver, username="root", port=2223)
    assert params.username == "root"
    assert params.port == 2223
    # Identity files only kept when they exist.
    params = resolve_connect_params("lab-3090", resolver)
    assert any(p.name == "lab_id_rsa" for p in params.identity_files)
    assert params.source == "ssh_config"


def test_resolve_connect_params_explicit_host(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path)
    params = resolve_connect_params("10.1.2.3", resolver, username="bob", port=2200)
    assert params.host == "10.1.2.3"
    assert params.port == 2200


def test_inline_comments_are_stripped(tmp_path: Path) -> None:
    resolver = write_cfg(
        tmp_path,
        "Host commented\n    Port 5024 # 图形界面 10.0.0.1:3391\n    User root # secondary\n",
    )
    entry = resolver.resolve("commented")
    assert entry.port == 5024
    assert entry.user == "root"


def test_invalid_directives_ignored(tmp_path: Path) -> None:
    resolver = write_cfg(
        tmp_path, "Host stray\n    UnknownDirective 1\n    LocalForward 8080 localhost:80\n"
    )
    assert resolver.resolve("stray").hostname is None


def test_negated_patterns_exclude_alias(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path, "Host lab-* !lab-broken\n    User alice\n")
    assert resolver.matches("lab-01") is True
    assert resolver.matches("lab-broken") is False
    assert resolver.resolve("lab-broken").user is None


def test_first_value_wins(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path, "Host dup\n    User first\n    User second\n")
    assert resolver.resolve("dup").user == "first"


def test_key_value_syntax(tmp_path: Path) -> None:
    resolver = write_cfg(
        tmp_path,
        "Host=kv-host\n"
        "    HostName=kv.example.com\n"
        "    User=kvuser\n"
        '    IdentityFile="~/.ssh/with space"\n'
        "    Port=2202\n",
    )
    entry = resolver.resolve("kv-host")
    assert entry.hostname == "kv.example.com"
    assert entry.user == "kvuser"
    assert entry.port == 2202
    assert entry.identity_files[0].name == "with space"


def test_key_value_with_space_separated_values(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path, "Host=a b\n    User=shared\n")
    assert resolver.matches("a") and resolver.matches("b")
    assert resolver.resolve("a").user == "shared"


def test_match_block_does_not_leak_into_host_block(tmp_path: Path) -> None:
    resolver = write_cfg(
        tmp_path,
        "Host alpha\n"
        "    User alice\n"
        "Match final all\n"
        "    User bob\n"
        "    Port 2222\n"
        "Host beta\n"
        "    User carol\n",
    )
    alpha = resolver.resolve("alpha")
    assert alpha.user == "alice"  # Match-block directives are not attributed here
    assert alpha.port is None
    assert resolver.resolve("beta").user == "carol"


def test_match_block_at_end_of_file_is_ignored(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path, "Host tail\n    User tina\nMatch canonical all\n    User bob\n")
    assert resolver.resolve("tail").user == "tina"
    assert resolver.list_aliases() == ["tail"]


def test_include_glob_and_cycle_guard(tmp_path: Path) -> None:
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "ssh_config").write_text("Host base\n    User baseuser\nInclude conf.d/*.conf\n")
    # The self-referential Include exercises the cycle guard.
    (tmp_path / "conf.d" / "extra.conf").write_text(
        "Host extra\n    User extrauser\nInclude ../conf.d/*.conf\n"
    )
    resolver = SSHConfigResolver(tmp_path / "ssh_config")
    assert resolver.resolve("base").user == "baseuser"
    assert resolver.resolve("extra").user == "extrauser"


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    resolver = write_cfg(tmp_path, "Host ok\n    User fine\n    'unterminated quote\n")
    assert resolver.resolve("ok").user == "fine"


# --- discovery ---


def _discovery(tmp_path: Path, text: str) -> tuple[SSHConfigDiscovery, Path]:
    config = tmp_path / "ssh_config"
    config.write_text(text)
    return SSHConfigDiscovery(SSHConfigResolver(config)), config


def _force_mtime(path: Path, stamp: int) -> None:
    os.utime(path, ns=(stamp, stamp))


def test_discovery_hot_reload_on_mtime_change(tmp_path: Path) -> None:
    discovery, config = _discovery(tmp_path, "Host a\n    HostName a.example.com\n")
    discovery.scan()
    assert [item.alias for item in discovery.aliases()] == ["a"]
    assert discovery.revision() == 1
    discovery.scan()  # unchanged mtime: no reparse
    assert discovery.revision() == 1

    config.write_text("Host a\n    HostName a.example.com\nHost b\n")
    _force_mtime(config, 2_000_000_000)
    discovery.scan()
    assert {item.alias for item in discovery.aliases()} == {"a", "b"}
    assert discovery.revision() == 2


def test_discovery_folds_duplicates_into_canonical_entry(tmp_path: Path) -> None:
    discovery, _ = _discovery(
        tmp_path,
        "Host web1\n    HostName 10.0.0.1\n    User ops\n"
        "Host web2\n    HostName 10.0.0.1\n    User ops\n",
    )
    discovery.scan()
    entries = discovery.aliases()
    assert len(entries) == 1
    assert entries[0].alias == "web1"
    assert entries[0].aliases == ("web2",)
    assert entries[0].hostname == "10.0.0.1"


def test_discovery_ignore_and_unignore(tmp_path: Path) -> None:
    discovery, config = _discovery(tmp_path, "Host a\n    HostName a.example.com\n")
    discovery.scan()
    discovery.ignore("a")
    assert discovery.aliases()[0].ignored is True
    discovery.unignore("a")
    assert discovery.aliases()[0].ignored is False  # refreshed even without an mtime change

    discovery.ignore("a")
    config.write_text("Host a\n    HostName a.example.com\nHost b\n")
    _force_mtime(config, 3_000_000_000)
    discovery.scan()
    ignored = {item.alias: item.ignored for item in discovery.aliases()}
    assert ignored == {"a": True, "b": False}
