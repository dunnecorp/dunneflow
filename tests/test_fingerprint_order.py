"""A reader's stamp is the same on every platform that builds it (rule 93, and rule 12).

`fingerprint_files` hashed in `sorted()` order, which for `Path` is the platform's: Windows
compares case-insensitively. The 0.3.1 builds showed it. GIZA's C and C++ and Rust stamps
differed from the Mac's with every hashed byte identical, because `BuildSystem.h` and
`LICENSE-APACHE` sorted among lower-case names differently. A map made on Windows was then
stale on a Mac, for no change at all.

The test runs in each machine's suite, and on Windows it is the one that would have failed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from dunneflow.detectors import fingerprint_files, lang_extraction_inputs


def _by_hand(files: list[Path]) -> str:
    """The stamp, hashed in the order POSIX sorts these names: byte order, upper first."""
    h = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.name.encode("utf-8")):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def test_mixed_case_names_hash_in_one_order_everywhere(tmp_path: Path) -> None:
    names = ["b.h", "BuildSystem.h", "a.h", "README.md", "lib.rs", "LICENSE-APACHE"]
    files = []
    for i, name in enumerate(names):
        path = tmp_path / name
        path.write_text(f"body {i}\n", encoding="utf-8")
        files.append(path)
    assert fingerprint_files(files) == _by_hand(files)
    assert fingerprint_files(reversed(files)) == _by_hand(files)


def test_the_readers_that_mix_case_still_have_files_that_do() -> None:
    """If these readers stop mixing case, the test above stops guarding anything real."""
    mixed = {lang: [p.name for p in lang_extraction_inputs(lang)
                    if p.name != p.name.lower()]
             for lang in ("cpp", "rust")}
    assert all(mixed.values()), mixed
