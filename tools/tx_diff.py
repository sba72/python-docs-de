#!/usr/bin/env python3
"""Vergleicht Transifex mit dem lokalen Repo -- Eintrag fuer Eintrag.

Sucht zuerst die Ressourcen, in denen in den letzten Tagen jemand etwas
geaendert hat, und stellt dann fuer jede davon den Text aus Transifex dem
lokalen msgstr gegenueber. Ausgegeben werden nur die Abweichungen.

Der Token kommt aus TX_TOKEN oder aus ~/.transifexrc.

Aufruf (im Repo, im Branch, dessen Projekt gemeint ist):
    python tools/tx_diff.py --tage 7 -o bericht.md
    python tools/tx_diff.py --benutzer JystBreisgau --tage 14 -o jyst.md
    python tools/tx_diff.py --ressource reference--datamodel -o datamodel.md
    python tools/tx_diff.py --tage 7 --voll -o bericht.md   # ungekuerzte Texte

Es wird nur gelesen; weder Transifex noch die lokalen Dateien werden
veraendert.
"""

import argparse
import configparser
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://rest.api.transifex.com"

# Wie oft ein voruebergehender Serverfehler wiederholt wird.
VERSUCHE = 4
KUERZUNG = 160


def ssl_kontext() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


KONTEXT = ssl_kontext()


def token() -> str:
    t = os.environ.get("TX_TOKEN")
    if t:
        return t.strip()
    rc = Path.home() / ".transifexrc"
    if rc.is_file():
        cfg = configparser.ConfigParser()
        cfg.read(rc)
        for abschnitt in cfg.sections():
            if cfg[abschnitt].get("token"):
                return cfg[abschnitt]["token"].strip()
    sys.exit("Kein API-Token gefunden. Setze TX_TOKEN oder pflege ~/.transifexrc.")


def git(*args: str) -> str:
    e = subprocess.run(["git", *args], capture_output=True, text=True)
    return e.stdout if e.returncode == 0 else ""


def repo_wurzel() -> Path:
    aus = git("rev-parse", "--show-toplevel").strip()
    if not aus:
        sys.exit("Kein Git-Repository -- bitte im python-docs-de-Verzeichnis starten.")
    return Path(aus)


def ressourcen(wurzel: Path) -> tuple:
    pfad = wurzel / ".tx" / "config"
    if not pfad.is_file():
        sys.exit(f"{pfad} nicht gefunden.")
    cfg = configparser.ConfigParser()
    cfg.read(pfad, encoding="utf-8")
    karte, org, projekt = {}, None, None
    for name in cfg.sections():
        m = re.match(r"o:([^:]+):p:([^:]+):r:(.+)", name)
        if not m:
            continue
        org, projekt, slug = m.groups()
        datei = (cfg[name].get("trans.de") or cfg[name].get("file_filter", ""))
        karte[slug] = datei.replace("<lang>", "de").strip()
    if not karte:
        sys.exit("In .tx/config wurden keine Ressourcen gefunden.")
    return karte, org, projekt


def hole(url: str, tok: str) -> dict:
    """Eine Seite abrufen -- mit Wiederholung bei voruebergehenden Fehlern.

    Transifex liefert sporadisch ein 502. Frueher wurde die Ressource dann
    stillschweigend uebersprungen und fehlte im Bericht.
    """
    for versuch in range(1, VERSUCHE + 1):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.api+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=90, context=KONTEXT) as antwort:
                return json.load(antwort)
        except urllib.error.HTTPError as e:
            if (e.code >= 500 or e.code == 429) and versuch < VERSUCHE:
                warte = 2 ** versuch
                print(f"  HTTP {e.code}, neuer Versuch in {warte}s "
                      f"({versuch}/{VERSUCHE - 1})", file=sys.stderr)
                time.sleep(warte)
                continue
            rumpf = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {e.code}: {rumpf}") from None
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
                sys.exit('Wurzelzertifikate fehlen -- "Install Certificates.command" '
                         "ausfuehren oder certifi installieren.")
            if versuch < VERSUCHE:
                warte = 2 ** versuch
                print(f"  Netzwerkfehler, neuer Versuch in {warte}s", file=sys.stderr)
                time.sleep(warte)
                continue
            raise RuntimeError(f"Netzwerkfehler bei {url}: {e.reason}") from None
    raise RuntimeError(f"Nach {VERSUCHE} Versuchen aufgegeben: {url}")


def seiten(pfad: str, params: dict, tok: str):
    """Alle Seiten einer JSON:API-Sammlung durchlaufen."""
    url = API + pfad + ("?" + urllib.parse.urlencode(params) if params else "")
    while url:
        daten = hole(url, tok)
        for eintrag in daten.get("data", []):
            yield eintrag
        url = (daten.get("links") or {}).get("next")


# --- lokale PO-Datei --------------------------------------------------------

def po_lesen(pfad: Path) -> dict:
    """{(msgctxt, msgid): msgstr} der lokalen Datei."""
    if not pfad.is_file():
        return {}

    def join(block, key):
        out, on = [], False
        for l in block.split("\n"):
            if l.startswith(key + " "):
                on = True; out += re.findall(r'"(.*)"', l)
            elif l.startswith('"') and on:
                out += re.findall(r'"(.*)"', l)
            elif l.startswith("msg") or l.startswith("#"):
                on = False
        return "".join(out)

    def ent(s):
        try:
            return s.encode().decode("unicode_escape").encode("latin-1").decode("utf-8")
        except Exception:
            return s

    aus = {}
    for b in pfad.read_text(encoding="utf-8").split("\n\n"):
        if not b.strip() or b.lstrip().startswith("#~"):
            continue
        mid = join(b, "msgid")
        if not mid:
            continue
        aus[(ent(join(b, "msgctxt")), ent(mid))] = ent(join(b, "msgstr"))
    return aus


# --- Transifex --------------------------------------------------------------

def text_von(strings) -> str:
    if isinstance(strings, dict):
        return strings.get("other") or strings.get("one") or ""
    return strings or ""


def benutzer(eintrag: dict) -> str:
    for rolle in ("translator", "reviewer"):
        bez = ((eintrag.get("relationships") or {}).get(rolle) or {}).get("data")
        if bez and bez.get("id"):
            return bez["id"].split(":", 1)[-1]
    return ""


def zeit(eintrag: dict) -> str:
    return (eintrag.get("attributes") or {}).get("datetime_translated") or ""


def tx_eintraege(res_id: str, tok: str) -> dict:
    """{(context, msgid): (text, benutzer, zeit, herkunft)} aus Transifex."""
    aus = {}
    params = {"filter[resource]": res_id, "filter[language]": "l:de",
              "include": "resource_string"}
    for e, beigefuegt in seiten("/resource_translations", params, tok):
        bez = ((e.get("relationships") or {}).get("resource_string") or {}).get("data")
        quelle = beigefuegt.get(bez["id"]) if bez else None
        if not quelle:
            continue
        qa = quelle.get("attributes") or {}
        schluessel = (qa.get("context") or "", text_von(qa.get("strings")))
        aus[schluessel] = (
            text_von((e.get("attributes") or {}).get("strings")),
            benutzer(e), zeit(e)[:16].replace("T", " "),
            (e.get("attributes") or {}).get("origin") or "?",
        )
    return aus


def kuerzen(s: str, voll: bool) -> str:
    s = s.replace("\n", " ⏎ ").replace("|", "\\|").strip()
    if not voll and len(s) > KUERZUNG:
        s = s[:KUERZUNG] + " …"
    return s or "—"


# --- Hauptprogramm ----------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tage", type=int, default=7, help="Zeitraum rueckwaerts (Vorgabe 7)")
    p.add_argument("--benutzer", help="nur Ressourcen, die dieser Benutzer geaendert hat")
    p.add_argument("--ressource", action="append",
                   help="bestimmte Ressource(n) statt Suche nach Aenderungen")
    p.add_argument("--voll", action="store_true", help="Texte nicht kuerzen")
    p.add_argument("-o", "--out", default="tx_diff.md", help="Zieldatei (Vorgabe tx_diff.md)")
    args = p.parse_args()

    tok = token()
    wurzel = repo_wurzel()
    karte, org, projekt = ressourcen(wurzel)
    branch = git("branch", "--show-current").strip()
    seit = (datetime.now(timezone.utc) - timedelta(days=args.tage)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    # 1. Welche Ressourcen sind betroffen?
    if args.ressource:
        betroffen = {r: None for r in args.ressource if r in karte}
        fehlend = [r for r in args.ressource if r not in karte]
        for r in fehlend:
            print(f"!! unbekannte Ressource: {r}", file=sys.stderr)
    else:
        print(f"Suche Aenderungen seit {seit} in {len(karte)} Ressourcen ...",
              file=sys.stderr)
        betroffen = {}
        for nr, slug in enumerate(sorted(karte), 1):
            if nr % 100 == 0:
                print(f"  {nr}/{len(karte)}", file=sys.stderr)
            try:
                treffer = list(seiten("/resource_translations", {
                    "filter[resource]": f"o:{org}:p:{projekt}:r:{slug}",
                    "filter[language]": "l:de",
                    "filter[date_translated][gt]": seit,
                }, tok))
            except RuntimeError:
                continue
            leute = {benutzer(e) for e, _ in treffer}
            if not treffer:
                continue
            if args.benutzer and args.benutzer not in leute:
                continue
            betroffen[slug] = leute

    if not betroffen:
        print("Keine betroffenen Ressourcen gefunden.")
        return

    # 2. Je Ressource vergleichen
    zeilen = [f"# Abgleich Transifex ↔ lokales Repo",
              "",
              f"Projekt **{org}:{projekt}**, Branch **{branch}**, Sprache de  ",
              f"Erstellt: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
              (f"Zeitraum: letzte {args.tage} Tage"
               + (f", Benutzer **{args.benutzer}**" if args.benutzer else "")
               if not args.ressource else "Ausgewaehlte Ressourcen"),
              ""]
    gesamt_nur_tx = gesamt_abw = gesamt_nur_lokal = 0

    for slug in sorted(betroffen):
        datei = karte[slug]
        print(f"vergleiche {slug} ...", file=sys.stderr)
        try:
            tx = tx_eintraege(f"o:{org}:p:{projekt}:r:{slug}", tok)
        except RuntimeError as fehler:
            print(f"!! {slug}: {fehler}", file=sys.stderr)
            continue
        lokal = po_lesen(wurzel / datei)

        nur_tx, abweichend, nur_lokal = [], [], []
        for schluessel, (txt, wer, wann, quelle) in tx.items():
            lok = lokal.get(schluessel)
            if lok is None:
                continue                      # String kennt die lokale Datei nicht
            if txt and not lok:
                nur_tx.append((schluessel[1], txt, wer, wann, quelle))
            elif txt and lok and txt != lok:
                abweichend.append((schluessel[1], lok, txt, wer, wann, quelle))
            elif lok and not txt:
                nur_lokal.append((schluessel[1], lok))

        if not (nur_tx or abweichend or nur_lokal):
            continue

        gesamt_nur_tx += len(nur_tx)
        gesamt_abw += len(abweichend)
        gesamt_nur_lokal += len(nur_lokal)

        zeilen += [f"## `{datei}`", "",
                   f"Ressource `{slug}` — nur in Transifex: **{len(nur_tx)}**, "
                   f"abweichend: **{len(abweichend)}**, nur lokal: **{len(nur_lokal)}**",
                   ""]

        if nur_tx:
            zeilen += ["### Nur in Transifex übersetzt (lokal leer)", "",
                       "| msgid | Transifex | wer | wann | Herkunft |",
                       "|---|---|---|---|---|"]
            for mid, txt, wer, wann, quelle in nur_tx:
                zeilen.append(f"| {kuerzen(mid, args.voll)} | {kuerzen(txt, args.voll)} "
                              f"| {wer} | {wann} | {quelle} |")
            zeilen.append("")

        if abweichend:
            zeilen += ["### Unterschiedlich übersetzt", "",
                       "| msgid | lokal | Transifex | wer | wann | Herkunft |",
                       "|---|---|---|---|---|---|"]
            for mid, lok, txt, wer, wann, quelle in abweichend:
                zeilen.append(f"| {kuerzen(mid, args.voll)} | {kuerzen(lok, args.voll)} "
                              f"| {kuerzen(txt, args.voll)} | {wer} | {wann} | {quelle} |")
            zeilen.append("")

        if nur_lokal:
            zeilen += ["### Nur lokal übersetzt (in Transifex leer)", "",
                       "| msgid | lokal |", "|---|---|"]
            for mid, lok in nur_lokal:
                zeilen.append(f"| {kuerzen(mid, args.voll)} | {kuerzen(lok, args.voll)} |")
            zeilen.append("")

    zeilen[5:5] = [
        "",
        "| | |",
        "|---|---:|",
        f"| nur in Transifex übersetzt | {gesamt_nur_tx} |",
        f"| unterschiedlich übersetzt | {gesamt_abw} |",
        f"| nur lokal übersetzt | {gesamt_nur_lokal} |",
    ]

    Path(args.out).write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    print(f"\nGeschrieben: {args.out}", file=sys.stderr)
    print(f"nur Transifex: {gesamt_nur_tx}, abweichend: {gesamt_abw}, "
          f"nur lokal: {gesamt_nur_lokal}", file=sys.stderr)


if __name__ == "__main__":
    main()
