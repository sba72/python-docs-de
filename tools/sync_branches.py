#!/usr/bin/env python3
"""Gleicht Uebersetzungen zwischen zwei Branches ab -- Eintrag fuer Eintrag.

Arbeitsmodell: von 3.15 wird nach 3.14 zurueckgespiegelt.
Dieses Skript macht den Abgleich nachvollziehbar, statt
eine Datei pauschal zu ueberschreiben.

Warum nicht einfach pomerge? pomerge kennt nur Quelle und Ziel. Es sieht
nicht, WER einen Eintrag zuletzt angefasst hat, und ueberschreibt deshalb
auch Arbeit, die nur im Zielbranch existiert. Dieses Skript vergleicht
beide Seiten gegen ihren gemeinsamen Vorfahren und unterscheidet damit:

    nur die Quelle hat sich bewegt  -> wird uebertragen
    nur das Ziel hat sich bewegt    -> bleibt unangetastet
    beide haben sich bewegt         -> Konflikt, wird gemeldet
    Quelle ist leer oder fuzzy      -> nie uebertragen

Der letzte Punkt ist wichtig: ein msgmerge kann eine Uebersetzung im
Zielbranch entfernt haben. Mit --leere-ziele werden genau diese Luecken
aus der Quelle wieder gefuellt.

Aufruf (im Repo, das ZIEL muss ausgecheckt sein):

    python tools/sync_branches.py --von 3.15            # Trockenlauf
    python tools/sync_branches.py --von 3.15 --apply
    python tools/sync_branches.py --von 3.15 --leere-ziele --apply
    python tools/sync_branches.py --von 3.15 --nur reference,library
    python tools/sync_branches.py --von 3.15 -o konflikte.md

Ohne --apply wird nichts geschrieben. Konflikte werden nie automatisch
entschieden -- sie landen im Bericht und gehoeren von Hand geprueft.
"""

import argparse
import os
import subprocess
import sys
import tempfile

try:
    import polib
except ImportError:
    sys.exit("polib fehlt. Im aktiven venv installieren: pip install polib")


def git(*args, repo="."):
    e = subprocess.run(["git", "--no-optional-locks", *args], cwd=repo,
                       capture_output=True, text=True)
    return e.stdout if e.returncode == 0 else None


def karte(text):
    """{msgid: msgstr} aus einem PO-Text; fuzzy zaehlt als unuebersetzt."""
    with tempfile.NamedTemporaryFile("w", suffix=".po", encoding="utf-8",
                                     delete=False) as f:
        f.write(text)
        tmp = f.name
    try:
        po = polib.pofile(tmp)
        return {e.msgid: ("" if "fuzzy" in e.flags else e.msgstr)
                for e in po if not e.obsolete and e.msgid}
    finally:
        os.unlink(tmp)


def po_dateien(ref):
    aus = git("ls-tree", "-r", "--name-only", ref) or ""
    return {z for z in aus.split("\n") if z.endswith(".po")}


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--von", required=True, metavar="BRANCH",
                   help="Quellbranch, aus dem uebertragen wird (z. B. 3.15)")
    p.add_argument("--apply", action="store_true",
                   help="tatsaechlich schreiben (ohne dies nur ein Trockenlauf)")
    p.add_argument("--leere-ziele", action="store_true", dest="leere_ziele",
                   help="auch Eintraege fuellen, die im Ziel leer sind, "
                        "obwohl sich die Quelle nicht bewegt hat")
    p.add_argument("--nur", help="nur diese Pfadanfaenge, kommagetrennt")
    p.add_argument("--streng", action="store_true",
                   help="mit Rueckgabewert 1 enden, wenn Konflikte gefunden wurden "
                        "(fuer die CI; Luecken allein gelten nicht als Fehler)")
    p.add_argument("-o", "--ausgabe", metavar="DATEI",
                   help="Konfliktbericht als Markdown schreiben")
    args = p.parse_args()

    ziel = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if not ziel:
        sys.exit("Kein Git-Repository -- bitte im python-docs-de-Verzeichnis starten.")
    if ziel == args.von:
        sys.exit(f"Quelle und Ziel sind derselbe Branch ({ziel}).")
    # Nur geaenderte .po-Dateien sind gefaehrlich: sie wuerden mit den
    # uebertragenen Eintraegen vermischt. Unversionierte Dateien und
    # geaenderte Skripte stoeren nicht.
    schmutzig = [z[3:] for z in (git("status", "--porcelain", "--untracked-files=no")
                                 or "").splitlines() if z[3:].endswith(".po")]
    if schmutzig and args.apply:
        sys.exit(f"{len(schmutzig)} .po-Datei(en) sind ungespeichert geaendert, "
                 f"z. B. {schmutzig[0]}.\nBitte erst committen oder stashen.")

    vorfahr = (git("merge-base", ziel, args.von) or "").strip()
    if not vorfahr:
        sys.exit(f"Kein gemeinsamer Vorfahr von {ziel} und {args.von} gefunden.")

    dateien = sorted(po_dateien(ziel) & po_dateien(args.von))
    if args.nur:
        anfaenge = tuple(a.strip() for a in args.nur.split(",") if a.strip())
        dateien = [d for d in dateien if d.startswith(anfaenge)]

    print(f"{ziel} <- {args.von}   (Vorfahr {vorfahr[:8]}, {len(dateien)} Dateien)",
          file=sys.stderr)

    uebertragen, gefuellt, konflikte = [], [], []
    for nr, pfad in enumerate(dateien, 1):
        if nr % 100 == 0:
            print(f"  {nr}/{len(dateien)}", file=sys.stderr)
        t_quelle = git("show", f"{args.von}:{pfad}")
        if t_quelle is None or not os.path.isfile(pfad):
            continue
        quelle = karte(t_quelle)
        t_basis = git("show", f"{vorfahr}:{pfad}")
        basis = karte(t_basis) if t_basis else {}

        po = polib.pofile(pfad, wrapwidth=79)
        n = 0
        for e in po:
            if e.obsolete or not e.msgid or e.msgid not in quelle:
                continue
            hier = "" if "fuzzy" in e.flags else e.msgstr
            dort = quelle[e.msgid]
            if not dort or dort == hier:
                continue
            urzustand = basis.get(e.msgid, "")
            hier_neu, dort_neu = hier != urzustand, dort != urzustand
            if dort_neu and hier_neu:
                konflikte.append((pfad, e.msgid, hier, dort))
                continue
            if not dort_neu and not (args.leere_ziele and not hier):
                continue
            if args.apply:
                e.msgstr = dort
                if "fuzzy" in e.flags:
                    e.flags.remove("fuzzy")
            (gefuellt if not hier else uebertragen).append(pfad)
            n += 1
        if n and args.apply:
            if po.metadata_is_fuzzy:
                po.metadata_is_fuzzy = False
            po.save(pfad)

    kopf = "GESCHRIEBEN" if args.apply else "TROCKENLAUF"
    print(f"\n{kopf}: {len(uebertragen)} aktualisiert, {len(gefuellt)} Luecken gefuellt, "
          f"{len(konflikte)} Konflikte")
    if not args.apply:
        print("Mit --apply wird tatsaechlich geschrieben.")

    if konflikte:
        zeilen = [f"# Konflikte beim Abgleich {ziel} <- {args.von}\n",
                  "Beide Seiten haben sich seit dem gemeinsamen Vorfahren bewegt.",
                  "Diese Eintraege wurden NICHT angefasst.\n"]
        aktuell = None
        for pfad, mid, hier, dort in konflikte:
            if pfad != aktuell:
                zeilen.append(f"\n## {pfad}\n"); aktuell = pfad
            zeilen.append(f"- msgid: `{mid[:100]}`")
            zeilen.append(f"  - {ziel}: {hier[:160]}")
            zeilen.append(f"  - {args.von}: {dort[:160]}")
        bericht = "\n".join(zeilen)
        if args.ausgabe:
            open(args.ausgabe, "w", encoding="utf-8").write(bericht + "\n")
            print(f"Konfliktbericht geschrieben: {args.ausgabe}", file=sys.stderr)
        else:
            print("\n" + bericht[:3000])

    if args.apply:
        print("\nJetzt noch: powrap -m  und  ./pre_push_check.sh --quick")

    if args.streng and konflikte:
        sys.exit(1)


if __name__ == "__main__":
    main()
