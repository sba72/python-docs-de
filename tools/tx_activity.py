#!/usr/bin/env python3
"""Wer hat in den letzten Tagen welche Datei in Transifex bearbeitet?

Fragt die Transifex-API v3 ab und listet pro Datei auf, welcher Benutzer
dort Uebersetzungen geaendert hat, woher sie kamen und wann zuletzt.

Der Token kommt aus TX_TOKEN oder aus ~/.transifexrc -- er wird nirgends
ausgegeben.

Herkunft (Feld "origin" der API):
    EDITOR   im Web-Editor eingegeben
    TM       aus dem Translation Memory uebernommen
    MT       maschineller Vorschlag
    UPLOAD   per Datei-Upload oder tx push eingespielt
    API      ueber die API gesetzt

Aufruf (im Repo, im Branch, dessen Projekt gemeint ist):
    python tools/tx_activity.py                  # letzte 7 Tage
    python tools/tx_activity.py --tage 1
    python tools/tx_activity.py --ohne-upload    # eigene Pushes ausblenden
    python tools/tx_activity.py --csv aktivitaet.csv
    python tools/tx_activity.py --debug          # Rohdatensatz zum Pruefen

Es werden nur Lesezugriffe gemacht; nichts wird veraendert.
"""

import argparse
import configparser
import csv
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

# Am 2026-10-03 gegen python-newest geprueft: das Attribut heisst
# datetime_translated, der zugehoerige Filter date_translated.
DATUMSFILTER = [
    "filter[date_translated][gt]",
    "filter[datetime_translated][gt]",
]

# Herkuenfte, die keine Handarbeit im Editor sind.
MASCHINELL = {"UPLOAD", "API"}


def ssl_kontext() -> ssl.SSLContext:
    """Wurzelzertifikate: certifi, falls vorhanden, sonst die des Systems."""
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
            wert = cfg[abschnitt].get("token")
            if wert:
                return wert.strip()
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
    """({resource-slug: po-pfad}, organisation, projekt) aus .tx/config."""
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
            rumpf = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {e.code} bei {url}\n{rumpf}") from None
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
                sys.exit(
                    "Wurzelzertifikate fehlen. Einmalig ausfuehren:\n"
                    '  open "/Applications/Python 3.14/Install Certificates.command"\n'
                    "oder im aktiven venv:  pip install certifi"
                )
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


def benutzer(eintrag: dict) -> str:
    """Name des Uebersetzers, z. B. 'u:JystBreisgau' -> 'JystBreisgau'."""
    for rolle in ("translator", "reviewer", "proofreader"):
        bez = ((eintrag.get("relationships") or {}).get(rolle) or {}).get("data")
        if bez and bez.get("id"):
            return bez["id"].split(":", 1)[-1]
    return "(unbekannt)"


def zeitpunkt(eintrag: dict) -> str:
    attr = eintrag.get("attributes") or {}
    for schluessel in ("datetime_translated", "datetime_reviewed", "datetime_created"):
        if attr.get(schluessel):
            return attr[schluessel]
    return ""


def herkunft(eintrag: dict) -> str:
    return (eintrag.get("attributes") or {}).get("origin") or "?"


def uebersetzungen(res_id: str, seit: str, tok: str, filtername: list) -> tuple:
    """Seit *seit* geaenderte Uebersetzungen einer Ressource."""
    basis = {"filter[resource]": res_id, "filter[language]": "l:de"}

    for name in list(filtername):
        try:
            return list(seiten("/resource_translations",
                               {**basis, name: seit}, tok)), name
        except RuntimeError as fehler:
            if "HTTP 400" in str(fehler) or "HTTP 409" in str(fehler):
                filtername.remove(name)
                continue
            raise

    alle = list(seiten("/resource_translations", basis, tok))
    return [e for e in alle if zeitpunkt(e) >= seit], None


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tage", type=int, default=7, help="Zeitraum rueckwaerts (Vorgabe 7)")
    p.add_argument("--projekt", help="Projekt-Slug, sonst aus .tx/config")
    p.add_argument("--organisation", default=None, help="sonst aus .tx/config")
    p.add_argument("--ohne-upload", action="store_true", dest="ohne_upload",
                   help="Herkunft UPLOAD und API ausblenden (eigene Pushes)")
    p.add_argument("--csv", metavar="DATEI", help="Ergebnis zusaetzlich als CSV")
    p.add_argument("--debug", action="store_true",
                   help="ersten Rohdatensatz ausgeben (Feldnamen pruefen)")
    args = p.parse_args()

    tok = token()
    wurzel = repo_wurzel()
    karte, org, projekt = ressourcen(wurzel)
    org = args.organisation or org
    projekt = args.projekt or projekt
    branch = git("branch", "--show-current").strip()

    seit = (datetime.now(timezone.utc) - timedelta(days=args.tage)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    print(f"Projekt {org}:{projekt} (Branch {branch}), Sprache de, seit {seit}\n"
          f"{len(karte)} Ressourcen werden geprueft ...", file=sys.stderr)

    filtername = list(DATUMSFILTER)
    gemeldet = False
    zeilen = []

    for nr, slug in enumerate(sorted(karte), 1):
        if nr % 50 == 0:
            print(f"  {nr}/{len(karte)}", file=sys.stderr)
        res_id = f"o:{org}:p:{projekt}:r:{slug}"
        try:
            eintraege, benutzt = uebersetzungen(res_id, seit, tok, filtername)
        except RuntimeError as fehler:
            print(f"  !! {slug}: {fehler}", file=sys.stderr)
            continue

        if not gemeldet and eintraege:
            print(f"  Datumsfilter: {benutzt or 'clientseitig (langsam)'}",
                  file=sys.stderr)
            gemeldet = True

        if args.debug and eintraege:
            print(json.dumps(eintraege[0], indent=2, ensure_ascii=False))
            return

        gruppen = {}
        for e in eintraege:
            quelle = herkunft(e)
            if args.ohne_upload and quelle in MASCHINELL:
                continue
            schluessel = (benutzer(e), quelle)
            anzahl, letzte = gruppen.get(schluessel, (0, ""))
            gruppen[schluessel] = (anzahl + 1, max(letzte, zeitpunkt(e)))

        for (wer, quelle), (anzahl, letzte) in sorted(gruppen.items()):
            zeilen.append((karte[slug] or slug, slug, wer, quelle, anzahl,
                           letzte[:16].replace("T", " ")))

    if not zeilen:
        print(f"\nKeine Aenderungen in den letzten {args.tage} Tagen gefunden.")
        return

    zeilen.sort(key=lambda z: (z[5], z[0]), reverse=True)

    print("\n| Datei | Ressource | Benutzer | Herkunft | Strings | zuletzt (UTC) |")
    print("|---|---|---|---|---:|---|")
    for datei, slug, wer, quelle, anzahl, wann in zeilen:
        print(f"| `{datei}` | {slug} | {wer} | {quelle} | {anzahl} | {wann} |")

    print("\n### Nach Benutzer\n")
    print("| Benutzer | Dateien | Strings |")
    print("|---|---:|---:|")
    proWer = {}
    for datei, _slug, wer, _quelle, anzahl, _wann in zeilen:
        dateien, strings = proWer.get(wer, (set(), 0))
        dateien.add(datei)
        proWer[wer] = (dateien, strings + anzahl)
    for wer, (dateien, strings) in sorted(proWer.items(), key=lambda x: -x[1][1]):
        print(f"| {wer} | {len(dateien)} | {strings} |")

    print("\n### Nach Herkunft\n")
    print("| Herkunft | Strings |")
    print("|---|---:|")
    proQuelle = {}
    for _datei, _slug, _wer, quelle, anzahl, _wann in zeilen:
        proQuelle[quelle] = proQuelle.get(quelle, 0) + anzahl
    for quelle, anzahl in sorted(proQuelle.items(), key=lambda x: -x[1]):
        print(f"| {quelle} | {anzahl} |")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["datei", "ressource", "benutzer", "herkunft", "strings",
                        "zuletzt_utc"])
            w.writerows(zeilen)
        print(f"\nCSV geschrieben: {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
