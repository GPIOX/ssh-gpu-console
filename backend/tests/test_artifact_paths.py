"""Phase 4A tests: safe artifact path components (per task plan).

Pure functions only; no SSH or executor fixtures required.
"""

from __future__ import annotations

import pytest
from app.core.errors import ConflictError
from app.workspace.paths import (
    dedupe_leaves,
    normalize_remote_path,
    paths_overlap,
    safe_artifact_leaf,
    sanitize_component,
)

# ---- sanitize_component -------------------------------------------------------


def test_slash_and_backslash_become_dash() -> None:
    assert sanitize_component("a/b") == "a-b"
    assert sanitize_component("a\\b") == "a-b"
    assert sanitize_component("a/b\\c/d") == "a-b-c-d"


def test_dot_and_dotdot_names_rejected() -> None:
    with pytest.raises(ConflictError):
        sanitize_component(".")
    with pytest.raises(ConflictError):
        sanitize_component("..")
    # Internal dots are safe; only exact all-dot components are dangerous.
    assert sanitize_component("v1..final") == "v1..final"


def test_unicode_letters_and_digits_kept() -> None:
    assert sanitize_component("华为模型") == "华为模型"
    # fullwidth colon is Po punctuation -> dash; noqa: ambiguous-char warning is the point
    assert sanitize_component("模型：v1") == "模型-v1"  # noqa: RUF001
    assert sanitize_component("café") == "café"


def test_spaces_become_dash() -> None:
    assert sanitize_component("my model v2") == "my-model-v2"
    assert sanitize_component("  spaced  ") == "spaced"


def test_colon_becomes_dash() -> None:
    assert sanitize_component("J3:seed42") == "J3-seed42"


def test_all_shell_metacharacters_rejected() -> None:
    with pytest.raises(ConflictError):
        sanitize_component("`\"';&|$()<>?!#%{}[]~")
    with pytest.raises(ConflictError):
        sanitize_component(":::")
    with pytest.raises(ConflictError):
        sanitize_component("---")  # dashes strip off both ends


def test_control_and_format_characters_become_dash() -> None:
    assert sanitize_component("a\x00b") == "a-b"
    assert sanitize_component("a\r\nb") == "a-b"
    assert sanitize_component("a\tb") == "a-b"
    assert sanitize_component("a\x1bb") == "a-b"
    assert sanitize_component("a\x7fb") == "a-b"
    assert sanitize_component("a\u200bb") == "a-b"  # zero width space (Cf)
    assert sanitize_component("a\u00adb") == "a-b"  # soft hyphen (Cf)
    assert sanitize_component("a\u202eb") == "a-b"  # bidi override (Cf)
    with pytest.raises(ConflictError):
        sanitize_component("\x00\n\t")


def test_dash_runs_collapse_and_edges_strip() -> None:
    assert sanitize_component("a--b") == "a-b"
    assert sanitize_component("a///b") == "a-b"
    assert sanitize_component("-name-") == "name"
    assert sanitize_component(".hidden") == "hidden"
    assert sanitize_component("name.") == "name"


def test_case_is_preserved() -> None:
    assert sanitize_component("IVMSD") == "IVMSD"
    assert sanitize_component("Model-V1") == "Model-V1"


def test_long_name_truncated_to_max_length() -> None:
    assert sanitize_component("m" * 100) == "m" * 80
    assert sanitize_component("abcdefghij-klmnop", max_length=11) == "abcdefghij"
    assert sanitize_component("ab.cd", max_length=3) == "ab"
    assert sanitize_component("abcdef", max_length=6) == "abcdef"
    assert sanitize_component("abcdef", max_length=1) == "a"
    with pytest.raises(ValueError):
        sanitize_component("abc", max_length=0)


def test_empty_and_whitespace_only_rejected() -> None:
    with pytest.raises(ConflictError):
        sanitize_component("")
    with pytest.raises(ConflictError):
        sanitize_component("   ")


# ---- safe_artifact_leaf -------------------------------------------------------


def test_leaf_joins_name_and_version_with_double_dash() -> None:
    assert safe_artifact_leaf("IVMSD", "v1", "art-1") == "IVMSD--v1"
    assert safe_artifact_leaf("IVMSD", "v1", "art-1") != safe_artifact_leaf(
        "IVMSD", "v2-clean", "art-1"
    )
    leaf = safe_artifact_leaf("prima_model", "seed42-warm10", "art-2")
    assert leaf == "prima_model--seed42-warm10"


def test_leaf_sanitizes_each_part() -> None:
    assert safe_artifact_leaf("prima model", "seed:42", "art-3") == "prima-model--seed-42"


def test_leaf_without_version_is_bare_name() -> None:
    assert safe_artifact_leaf("IVMSD", None, "art-1") == "IVMSD"
    assert safe_artifact_leaf("IVMSD", "", "art-1") == "IVMSD"


def test_leaf_rejects_unusable_name_or_version() -> None:
    with pytest.raises(ConflictError):
        safe_artifact_leaf("", "v1", "art-1")
    with pytest.raises(ConflictError):
        safe_artifact_leaf("///", None, "art-1")
    with pytest.raises(ConflictError):
        safe_artifact_leaf("IVMSD", "   ", "art-1")


# ---- normalize_remote_path ----------------------------------------------------


def test_normalize_remote_path_strips_trailing_slashes() -> None:
    assert normalize_remote_path("/data/x/") == "/data/x"
    assert normalize_remote_path("/data//x") == "/data/x"
    assert normalize_remote_path("/a/b/../c") == "/a/c"
    assert normalize_remote_path("  /data/x  ") == "/data/x"
    assert normalize_remote_path("/") == "/"
    assert normalize_remote_path("//") == "/"
    assert normalize_remote_path("///") == "/"


def test_normalize_remote_path_keeps_posix_double_leading_slash() -> None:
    # posixpath.normpath preserves exactly two leading slashes (POSIX);
    # paths_overlap is component-wise, so this quirk never affects it.
    assert normalize_remote_path("//data/x") == "//data/x"


def test_normalize_remote_path_rejects_empty() -> None:
    with pytest.raises(ConflictError):
        normalize_remote_path("")
    with pytest.raises(ConflictError):
        normalize_remote_path("   ")


# ---- paths_overlap ------------------------------------------------------------


def test_paths_overlap_exact_and_slash_variants() -> None:
    assert paths_overlap("/data/x", "/data/x") is True
    assert paths_overlap("/data/x/", "/data/x") is True
    assert paths_overlap("/data//x", "/data/x") is True


def test_paths_overlap_ancestor_and_descendant() -> None:
    assert paths_overlap("/data", "/data/x/cache") is True
    assert paths_overlap("/data/x", "/data/x/cache") is True
    assert paths_overlap("/data/x/cache", "/data/x") is True


def test_paths_overlap_sibling_not_confused() -> None:
    assert paths_overlap("/data/x", "/data/y") is False
    assert paths_overlap("/data/x", "/data/xy") is False  # component boundary
    assert paths_overlap("/data/x1", "/data/x2") is False


def test_paths_overlap_root_is_ancestor_of_everything() -> None:
    assert paths_overlap("/", "/data/x") is True
    assert paths_overlap("/", "/") is True


def test_paths_overlap_rejects_empty_side() -> None:
    with pytest.raises(ConflictError):
        paths_overlap("", "/data/x")


# ---- dedupe_leaves ------------------------------------------------------------


def test_dedupe_leaves_unique_batch_unchanged() -> None:
    leaves = {"a1": "model-a", "a2": "model-b", "a3": "数据集"}
    ids = {"a1": "a1", "a2": "a2", "a3": "a3"}
    assert dedupe_leaves(leaves, ids) == leaves


def test_dedupe_appends_six_char_id_prefix_to_later_duplicates() -> None:
    leaves = {"a1": "model", "a2": "model"}
    ids = {"a1": "aaaaaa1111", "a2": "bbbbbb2222"}
    assert dedupe_leaves(leaves, ids) == {"a1": "model", "a2": "model--bbbbbb"}


def test_dedupe_widens_prefix_when_still_colliding() -> None:
    leaves = {"a1": "leaf", "a2": "leaf", "a3": "leaf"}
    ids = {"a1": "aaaaaa1111", "a2": "bbbbbb2222", "a3": "bbbbbb9999"}
    assert dedupe_leaves(leaves, ids) == {
        "a1": "leaf",
        "a2": "leaf--bbbbbb",
        "a3": "leaf--bbbbbb99",
    }


def test_dedupe_escalates_to_full_artifact_id() -> None:
    leaves = {aid: "leaf" for aid in ("A", "B", "C", "D", "E", "F")}
    ids = {
        "A": "aaaaaaaa-aaaa",
        "B": "bbbbbb11-bbbb",
        "C": "bbbbbb11-cccc",
        "D": "bbbbbb11-dddd",
        "E": "bbbbbb11-eeee",
        # F shares its first 12 characters with E, forcing the full-id fallback.
        "F": "bbbbbb11-eeef",
    }
    assert dedupe_leaves(leaves, ids) == {
        "A": "leaf",
        "B": "leaf--bbbbbb",
        "C": "leaf--bbbbbb11",
        "D": "leaf--bbbbbb11-ddd",
        "E": "leaf--bbbbbb11-eee",
        "F": "leaf--bbbbbb11-eeef",
    }


def test_dedupe_detects_collision_against_earlier_final() -> None:
    leaves = {"a1": "leaf", "a2": "leaf--id-ccc", "a3": "leaf"}
    ids = {"a1": "id-aaaaaaaaaa", "a2": "id-bbbbbbbbbb", "a3": "id-cccccccccc"}
    assert dedupe_leaves(leaves, ids) == {
        "a1": "leaf",
        "a2": "leaf--id-ccc",
        "a3": "leaf--id-ccccc",  # 8-char prefix of "id-cccccccccc"
    }


def test_dedupe_output_leaves_are_unique() -> None:
    leaves = {"a1": "model", "a2": "model", "a3": "model--bbbbbb", "a4": "model"}
    ids = {"a1": "aaaaaa1111", "a2": "bbbbbb2222", "a3": "cccccc3333", "a4": "dddddd4444"}
    result = dedupe_leaves(leaves, ids)
    assert len(set(result.values())) == len(result)


def test_dedupe_is_deterministic_across_calls() -> None:
    leaves = {"a1": "model", "a2": "model", "a3": "model"}
    ids = {"a1": "aaaaaa1111", "a2": "bbbbbb2222", "a3": "bbbbbb9999"}
    first = dedupe_leaves(leaves, ids)
    assert first == dedupe_leaves(leaves, ids)
    assert first == dedupe_leaves(dict(leaves), dict(ids))


def test_dedupe_follows_input_order_stably() -> None:
    ids = {"a1": "aaaaaa1111", "a2": "bbbbbb2222"}
    forward = dedupe_leaves({"a1": "model", "a2": "model"}, ids)
    reversed_order = dedupe_leaves({"a2": "model", "a1": "model"}, ids)
    assert forward == {"a1": "model", "a2": "model--bbbbbb"}
    assert reversed_order == {"a2": "model", "a1": "model--aaaaaa"}


def test_dedupe_raises_when_even_full_id_is_taken() -> None:
    leaves = {"a1": "leaf", "a2": "leaf--id-ccc", "a3": "leaf"}
    ids = {"a1": "id-aaaaaaaaaa", "a2": "id-bbbbbbbbbb", "a3": "id-ccc"}
    with pytest.raises(ConflictError):
        dedupe_leaves(leaves, ids)


# ---- end-to-end batch composition ---------------------------------------------


def test_batch_of_artifacts_produces_unique_leaves() -> None:
    artifacts = [
        ("art-1", "IVMSD", "v1"),
        ("art-2", "IVMSD", "v2-clean"),
        ("art-3", "IVMSD", None),
        ("art-4", "数据集A", "2024"),
    ]
    leaves = {aid: safe_artifact_leaf(name, version, aid) for aid, name, version in artifacts}
    ids = {aid: aid for aid, _name, _version in artifacts}
    result = dedupe_leaves(leaves, ids)
    assert result["art-1"] == "IVMSD--v1"
    assert result["art-2"] == "IVMSD--v2-clean"
    assert result["art-3"] == "IVMSD"
    assert result["art-4"] == "数据集A--2024"
    assert len(set(result.values())) == len(result)
