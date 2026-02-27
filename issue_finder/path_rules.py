from __future__ import annotations


def is_python_file(path: str) -> bool:
    return path.lower().endswith(".py")


def is_probably_test_file(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    name = p.split("/")[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if "/tests/" in p or p.startswith("tests/"):
        return True
    if "/test/" in p or p.startswith("test/"):
        return True
    return False


def is_probably_doc_file(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    if p.startswith("docs/") or p.startswith("doc/"):
        return True
    if "/docs/" in p or "/doc/" in p:
        return True
    return False


def is_non_test_non_doc_python(path: str) -> bool:
    return is_python_file(path) and (not is_probably_test_file(path)) and (not is_probably_doc_file(path))

