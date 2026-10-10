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

# Mindestabstand zwischen zwei Anfragen in Sekunden (--pause).
# Ohne Pause antwortet das Gateway nach einigen hundert Anfragen mit 503.
TAKT = 0.35
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


class Gedrosselt(RuntimeError):
    """Das Gateway weist uns ab (429/503).

    Das ist keine Eigenschaft der einzelnen Ressource, sondern unseres
    Anfragetempos insgesamt -- die naechste Ressource trifft dieselbe Wand.
    Der Aufrufer soll deshalb aufhoeren, nicht weiterprobieren.
    """


_letzte_anfrage = 0.0


def takt_halten() -> None:
    """Mindestabstand zur vorherigen Anfrage einhalten."""
    global _letzte_anfrage
    rest = TAKT - (time.monotonic() - _letzte_anfrage)
    if rest > 0:
        time.sleep(rest)
    _letzte_anfrage = time.monotonic()


def wartezeit(kopf, standard: int) -> int:
    """'Retry-After' auswerten, falls der Server es mitschickt."""
    wert = (kopf.get("Retry-After") or "").strip()
    if wert.isdigit():
        return max(1, min(300, int(wert)))
    return standard


def hole(url: str, tok: str) -> tuple:
    """Eine Seite abrufen. Liefert (daten, kopfzeilen).

    502/504 sind kurze Aussetzer -- da genuegt eine knappe Pause.
    429/503 ist Drosselung: laenger warten, 'Retry-After' beachten, und
    wenn es anhaelt, Gedrosselt ausloesen statt die restlichen Ressourcen
    gegen die Wand zu fahren (das verschaerft die Drosselung nur).
    """
    for versuch in range(1, VERSUCHE + 1):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.api+json",
        })
        takt_halten()
        try:
            with urllib.request.urlopen(req, timeout=90, context=KONTEXT) as antwort:
                return json.load(antwort), dict(antwort.headers)
        except urllib.error.HTTPError as e:
            drosselung = e.code in (429, 503)
            if e.code < 500 and not drosselung:
                rumpf = e.read().decode("utf-8", "replace")[:400]
                raise RuntimeError(f"HTTP {e.code} bei {url}\n{rumpf}") from None
            if versuch == VERSUCHE:
                rumpf = e.read().decode("utf-8", "replace")[:200]
                art = Gedrosselt if drosselung else RuntimeError
                raise art(f"HTTP {e.code} bei {url}\n{rumpf}") from None
            warte = wartezeit(e.headers,
                              15 * 2 ** (versuch - 1) if drosselung else 2 ** versuch)
            print(f"  HTTP {e.code}, neuer Versuch in {warte}s "
                  f"({versuch}/{VERSUCHE - 1})", file=sys.stderr)
            time.sleep(warte)
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


_grenze_gemeldet = False


def grenze(kopf: dict) -> None:
    """Die Drosselungs-Kopfzeilen einmal ausgeben, falls der Server welche setzt.

    Damit muss das Anfragetempo nicht geraten werden.
    """
    global _grenze_gemeldet
    if _grenze_gemeldet:
        return
    treffer = {k: v for k, v in kopf.items()
               if "ratelimit" in k.lower().replace("-", "").replace("_", "")}
    if treffer:
        print("  Drosselung laut Server: "
              + ", ".join(f"{k}={v}" for k, v in sorted(treffer.items())),
              file=sys.stderr)
    _grenze_gemeldet = True


def seiten(pfad: str, params: dict, tok: str):
    """Alle Seiten durchlaufen und je Eintrag die beigefuegten Daten mitgeben.

    Transifex liefert die Quelltexte per "include=resource_string" nicht im
    Eintrag selbst, sondern gesammelt unter "included". Ohne diese Zuordnung
    waere nicht erkennbar, zu welchem msgid eine Uebersetzung gehoert.
    """
    url = API + pfad + ("?" + urllib.parse.urlencode(params) if params else "")
    while url:
        daten, kopf = hole(url, tok)
        grenze(kopf)
        beigefuegt = {e["id"]: e for e in daten.get("included", []) if e.get("id")}
        for eintrag in daten.get("data", []):
            yield eintrag, beigefuegt
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
    global TAKT
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tage", type=int, default=7, help="Zeitraum rueckwaerts (Vorgabe 7)")
    p.add_argument("--benutzer", help="nur Ressourcen, die dieser Benutzer geaendert hat")
    p.add_argument("--ressource", action="append",
                   help="bestimmte Ressource(n) statt Suche nach Aenderungen")
    p.add_argument("--voll", action="store_true", help="Texte nicht kuerzen")
    p.add_argument("-o", "--out", default="tx_diff.md", help="Zieldatei (Vorgabe tx_diff.md)")
    p.add_argument("--pause", type=float, default=TAKT, metavar="SEK",
                   help=f"Mindestabstand zwischen Anfragen (Vorgabe {TAKT})")
    args = p.parse_args()

    TAKT = max(0.0, args.pause)

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
            except Gedrosselt as fehler:
                print(f"\n!! Abbruch bei {slug}: {str(fehler).splitlines()[0]}\n"
                      f"   Transifex drosselt die Anfragen. Besser die Ressourcen\n"
                      f"   mit --ressource gezielt angeben oder --pause erhoehen.",
                      file=sys.stderr)
                break
            except RuntimeError as fehler:
                # Nicht stillschweigend ueberspringen -- eine fehlende
                # Ressource wuerde sonst unbemerkt aus dem Bericht fallen.
                print(f"  !! {slug}: {str(fehler).splitlines()[0]}", file=sys.stderr)
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
