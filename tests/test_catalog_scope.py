from pathlib import Path

from pkas.catalog_scope import candidate_priority, load_auto_policy
from pkas.parsers import parse_file


def test_automatic_scope_keeps_training_labels_and_empty_files_at_l0():
    root = Path("D:/workspace")
    policy = load_auto_policy(root)
    cases = [
        (root / "dataset" / "labels" / "train" / "001.txt", 256),
        (root / "project" / "empty.md", 0),
        (root / "PKAS-builds" / "release" / "README.md", 1024),
        (root / "PKAS-build-cache" / "guide.pdf", 1024),
        (root / "project" / "package-lock.json", 1024),
        (root / ".vscode" / "settings.json", 1024),
    ]
    for path, size in cases:
        priority, reason = candidate_priority(str(path), path.suffix, policy, size)
        assert priority is None
        assert reason

    important = root / "project" / "README.md"
    assert candidate_priority(str(important), important.suffix, policy, 600)[0] == 0
    tiny_readme = root / "project" / "readme.txt"
    assert candidate_priority(str(tiny_readme), tiny_readme.suffix, policy, 80)[0] == 2
    small_note = root / "project" / "run.txt"
    assert candidate_priority(str(small_note), small_note.suffix, policy, 24)[0] == 2
    md_only = {**policy, "rules": {**policy["rules"], "documents": "md_only"}}
    assert candidate_priority(str(small_note), small_note.suffix, md_only, 24)[0] is None


def test_other_markdown_formats_have_a_full_text_path(tmp_path: Path):
    for suffix in (".mdx", ".rst", ".adoc"):
        source = tmp_path / f"guide{suffix}"
        source.write_text("项目背景和关键决定。", encoding="utf-8")
        document = parse_file(source)
        assert "项目背景和关键决定" in document.text
