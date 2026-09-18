from pkas.file_inspector import inspect_file


def test_detects_content_mismatch_from_real_file_prefix(source_root):
    disguised = source_root / "looks-like-note.txt"
    disguised.write_bytes(b"%PDF-1.7\nnot really a complete PDF")
    inspected = inspect_file(disguised)
    assert inspected["detected_type"] == "pdf"
    assert inspected["type_mismatch"] is True
    assert inspected["preview_bytes"] == disguised.stat().st_size


def test_text_preview_is_bounded(source_root):
    note = source_root / "note.md"
    note.write_text("useful text " * 1000, encoding="utf-8")
    inspected = inspect_file(note, preview_bytes=128)
    assert inspected["detected_type"] == "text"
    assert inspected["preview_bytes"] == 128
    assert len(inspected["text_preview"]) <= 128
