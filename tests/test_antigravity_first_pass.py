import pytest

from antigravity_first_pass import (
    AntigravityFirstPassError, apply_validated_patch, build_source_context,
    extract_unified_diff, validate_patch_paths,
)


def test_antigravity_authors_patch_and_controller_applies_only_to_stage(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    source = stage / "app.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    context, paths = build_source_context(
        str(stage),
        {"goal": "修改 app.py", "architecture": "更新 app.py", "research": "app.py"},
        ["app.py"],
    )
    assert "VALUE = 1" in context
    assert paths == ["app.py"]

    response = """BEGIN_PATCH
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
END_PATCH
"""
    patch = extract_unified_diff(response)
    assert apply_validated_patch(str(stage), patch, ["app.py"]) == ["app.py"]
    assert source.read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.parametrize("path", ["../outside.py", "config.json", "other.py"])
def test_antigravity_patch_rejects_escape_control_and_unapproved_paths(path):
    patch = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -0,0 +1 @@
+unsafe
"""
    with pytest.raises(AntigravityFirstPassError):
        validate_patch_paths(patch, ["app.py"])


def test_antigravity_context_refuses_empty_approved_scope(tmp_path):
    with pytest.raises(AntigravityFirstPassError, match="没有冻结"):
        build_source_context(str(tmp_path), {"goal": "随便改改"}, [])
