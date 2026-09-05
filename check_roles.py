#!/usr/bin/env python3
"""
check_roles.py — Vergleicht Sphinx-Rollen (:ref:, :term:, :class:, :func:, ...)
zwischen msgid und msgstr in .po-Dateien, um Inkonsistenzen zu finden, OHNE
dass ein kompletter Sphinx-Build nötig ist. Erkennt dieselbe Fehlerklasse wie
Sphinxs 'i18n.inconsistent_references', plus zusätzliche Muster
(fehlende/überzählige Rollen, falsche Ziele).
"""
import re
import sys
from pathlib import Path

ROLE_RE = re.compile(r":([a-zA-Z][\w-]*):`([^`]+)`")


def normalize_target(content: str) -> str:
    content = content.strip()
    m = re.match(r"^(.*)<([^<>]+)>$", content)
    if m:
        return m.group(2).strip()
    return content


ROLES_WITHOUT_FIXED_TARGET = {"dfn"}


def extract_roles(text: str) -> list[tuple[str, str]]:
    roles = []
    for role, content in ROLE_RE.findall(text):
        if role in ROLES_WITHOUT_FIXED_TARGET:
            continue
        roles.append((role, normalize_target(content)))
    return sorted(roles)


def parse_po_entries(path: Path):
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    i = 0
    entries = []
    while i < len(lines):
        line = lines[i]
        if line.startswith("#~"):
            i += 1
            continue
        is_fuzzy = False
        while i < len(lines) and lines[i].startswith("#"):
            if ", fuzzy" in lines[i]:
                is_fuzzy = True
            i += 1
        if i >= len(lines) or not lines[i].startswith("msgid"):
            i += 1
            continue
        msgid_lineno = i + 1
        msgid_parts = []
        m = re.match(r'msgid\s+"(.*)"$', lines[i])
        if m:
            msgid_parts.append(m.group(1))
        i += 1
        while i < len(lines) and lines[i].startswith('"'):
            msgid_parts.append(lines[i].strip()[1:-1])
            i += 1
        if i >= len(lines) or not lines[i].startswith("msgstr"):
            continue
        msgstr_parts = []
        m = re.match(r'msgstr\s+"(.*)"$', lines[i])
        if m:
            msgstr_parts.append(m.group(1))
        i += 1
        while i < len(lines) and lines[i].startswith('"'):
            msgstr_parts.append(lines[i].strip()[1:-1])
            i += 1
        msgid = "".join(msgid_parts)
        msgstr = "".join(msgstr_parts)
        entries.append((msgid_lineno, msgid, msgstr, is_fuzzy))
    return entries


def check_file(path: Path) -> list[str]:
    findings = []
    try:
        entries = parse_po_entries(path)
    except Exception as e:
        return [f"  [Parse-Fehler] {e}"]

    for lineno, msgid, msgstr, is_fuzzy in entries:
        if is_fuzzy or not msgstr or not msgid:
            continue
        orig_roles = extract_roles(msgid)
        trans_roles = extract_roles(msgstr)
        if orig_roles != trans_roles:
            missing = [r for r in orig_roles if r not in trans_roles]
            extra = [r for r in trans_roles if r not in orig_roles]
            parts = []
            if missing:
                parts.append(f"fehlt: {missing}")
            if extra:
                parts.append(f"zusätzlich/falsch: {extra}")
            findings.append(f"  Zeile ~{lineno}: {', '.join(parts)}")
    return findings


def main():
    if len(sys.argv) < 2:
        print("Nutzung: check_roles.py <verzeichnis-oder-datei> [--exclude dir1,dir2]")
        sys.exit(1)

    root = Path(sys.argv[1])
    excludes = set()
    if "--exclude" in sys.argv:
        idx = sys.argv.index("--exclude")
        excludes = set(sys.argv[idx + 1].split(","))

    if root.is_file():
        po_files = [root]
    else:
        po_files = sorted(
            p for p in root.rglob("*.po")
            if not excludes.intersection(p.relative_to(root).parts)
        )

    total_findings = 0
    files_with_findings = 0
    for po_file in po_files:
        findings = check_file(po_file)
        if findings:
            files_with_findings += 1
            total_findings += len(findings)
            rel = po_file.relative_to(root) if root.is_dir() else po_file
            print(f"\n{rel}")
            for f in findings:
                print(f)

    print(f"\n{'=' * 60}")
    print(f"{total_findings} mögliche Rollen-Inkonsistenz(en) in {files_with_findings} "
          f"von {len(po_files)} Dateien gefunden.")
    print("Hinweis: Das ist eine Heuristik (Textvergleich, kein echter Sphinx-Build) "
          "-> bitte jeden Fund manuell gegenprüfen, es können auch Fehlalarme dabei sein.")
    sys.exit(1 if total_findings else 0)


if __name__ == "__main__":
    main()