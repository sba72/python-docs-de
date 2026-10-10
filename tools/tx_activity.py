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
    python tools/tx_activity.py --probe          # erst die API-Form klaeren
    python tools/tx_activity.py --bestand        # was wuerde ein Force-Push loeschen?
    python tools/tx_activity.py                  # letzte 7 Tage
    python tools/tx_activity.py --tage 1
    python tools/tx_activity.py --ohne-upload    # eigene Pushes ausblenden
    python tools/tx_activity.py --csv aktivitaet.csv
    python tools/tx_activity.py --pause 1.0      # langsamer, wenn gedrosselt
    python tools/tx_activity.py --ab library--os # Lauf fortsetzen
    python tools/tx_activity.py --alle           # ohne Vorauswahl (langsam)

Vorauswahl: die Sammelstatistik resource_language_stats nennt in vier
Anfragen, welche Ressourcen ueberhaupt angefasst wurden. Nur die werden
danach einzeln abgefragt -- sonst waeren es 553 Anfragen.
    python tools/tx_activity.py --debug          # Rohdatensatz zum Pruefen

Transifex drosselt am Gateway. Bei 429/503 wird laenger gewartet und
danach abgebrochen statt die restlichen Ressourcen gegen dieselbe Wand
zu fahren; die Meldung nennt den Slug zum Fortsetzen.

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

# Mindestabstand zwischen zwei Anfragen in Sekunden (--pause).
# Ohne Pause antwortet das Gateway nach einigen hundert Anfragen mit 503.
TAKT = 0.35

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
    """Alle Seiten einer JSON:API-Sammlung durchlaufen."""
    url = API + pfad + ("?" + urllib.parse.urlencode(params) if params else "")
    while url:
        daten, kopf = hole(url, tok)
        grenze(kopf)
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


# Felder der Sammelstatistik, die menschliche Arbeit anzeigen.
# last_update bleibt bewusst aussen vor: das wandert auch bei
# Aenderungen am englischen Quelltext und wuerde das Netz unnoetig weiten.
STATFELDER = ("last_translation_update", "last_review_update",
              "last_proofread_update")


def slug_von(eintrag: dict) -> str:
    """Ressourcen-Slug aus einem Statistik-Eintrag.

    Die id hat die Form o:ORG:p:PROJ:r:SLUG:l:de; falls sich das aendert,
    dient die Beziehung zur Ressource als Rueckfallebene.
    """
    treffer = re.search(r":r:(.+?):l:", eintrag.get("id") or "")
    if treffer:
        return treffer.group(1)
    bez = ((eintrag.get("relationships") or {}).get("resource") or {}).get("data")
    if bez and bez.get("id"):
        return bez["id"].rsplit(":r:", 1)[-1]
    return ""


def frische_ressourcen(org: str, projekt: str, seit: str, tok: str) -> set:
    """Ressourcen, an deren Uebersetzung seit *seit* gearbeitet wurde.

    Die Sammelstatistik liefert 150 Ressourcen je Seite, also vier Anfragen
    fuer das ganze Projekt. Die Alternative ist eine Anfrage je Ressource --
    553 Stueck, und genau daran ist der Lauf in die Drosselung gelaufen.
    """
    frisch, gesehen = set(), 0
    params = {"filter[project]": f"o:{org}:p:{projekt}",
              "filter[language]": "l:de"}
    for e in seiten("/resource_language_stats", params, tok):
        gesehen += 1
        attr = e.get("attributes") or {}
        juengste = max((attr.get(f) or "") for f in STATFELDER)
        if juengste >= seit:
            slug = slug_von(e)
            if slug:
                frisch.add(slug)
    print(f"  Vorauswahl: {len(frisch)} von {gesehen} Ressourcen angefasst",
          file=sys.stderr)
    return frisch


# Ressource, die nachweislich Handarbeit enthaelt. Sie dient als Kontrolle:
# ein Herkunftsfilter, der hier nichts findet, ist kaputt -- und "nichts
# gefunden" ist genau die Antwort, die uns zum Loeschen verleiten wuerde.
KONTROLLE = "reference--datamodel"


def nur_editor(res_id: str, tok: str) -> int:
    """Anzahl der im Web-Editor eingegebenen Uebersetzungen einer Ressource."""
    return sum(1 for _ in seiten("/resource_translations", {
        "filter[resource]": res_id,
        "filter[language]": "l:de",
        "filter[origin]": "EDITOR",
    }, tok))


def slugs_aus_bericht(pfad: Path) -> list:
    """Ressourcen-Slugs aus einem --bestand-Bericht lesen."""
    text = pfad.read_text(encoding="utf-8")
    return [m.group(1) for m in
            re.finditer(r'^\| `[^`]+` \| (\S+) \|', text, re.M)]


def herkunft_pruefen(org: str, projekt: str, slugs: list, tok: str,
                     ziel=None) -> int:
    """Pruefen, ob in *slugs* Handarbeit steckt, die wir verlieren wuerden.

    Erst wird der Filter an KONTROLLE belegt. Ohne diesen Nachweis ist ein
    leeres Ergebnis wertlos, weil ein defekter Filter dasselbe liefert.

    Rueckgabe: Anzahl der Ressourcen mit EDITOR-Strings.
    """
    print(f"Kontrolle: Herkunftsfilter an {KONTROLLE} belegen ...",
          file=sys.stderr)
    probe = nur_editor(f"o:{org}:p:{projekt}:r:{KONTROLLE}", tok)
    if probe == 0:
        sys.exit(
            f"Abbruch: der Filter findet in {KONTROLLE} keine EDITOR-Strings,\n"
            f"obwohl dort nachweislich Handarbeit liegt. Der Filter ist also\n"
            f"nicht verlaesslich -- ein leeres Ergebnis waere wertlos.")
    print(f"  {probe} EDITOR-Strings gefunden, der Filter arbeitet.\n",
          file=sys.stderr)

    treffer = []
    for nr, slug in enumerate(slugs, 1):
        if nr % 25 == 0:
            print(f"  {nr}/{len(slugs)}", file=sys.stderr)
        if slug == KONTROLLE:
            continue
        try:
            n = nur_editor(f"o:{org}:p:{projekt}:r:{slug}", tok)
        except Gedrosselt as fehler:
            print(f"\n!! Abbruch bei {slug}: {str(fehler).splitlines()[0]}",
                  file=sys.stderr)
            sys.exit(2)
        except RuntimeError as fehler:
            print(f"  !! {slug}: {str(fehler).splitlines()[0]}", file=sys.stderr)
            continue
        if n:
            treffer.append((n, slug))

    treffer.sort(reverse=True)
    aus = [f"# Handarbeit in {len(slugs)} geprueften Ressourcen\n",
           f"- Kontrolle {KONTROLLE}: {probe} EDITOR-Strings (Filter belegt)",
           f"- Ressourcen mit EDITOR-Strings: {len(treffer)}",
           f"- EDITOR-Strings insgesamt: {sum(t[0] for t in treffer)}\n"]
    if treffer:
        aus.append("| Ressource | EDITOR-Strings |")
        aus.append("|---|---:|")
        aus += [f"| {s} | {n} |" for n, s in treffer]
        aus.append("\nDiese Stellen vor einem Force-Push ansehen.")
    else:
        aus.append("Keine Handarbeit gefunden -- alles dort ist TM, MT oder "
                   "Hochladung. Ein Force-Push loescht keine menschliche Arbeit.")
    text = "\n".join(aus)
    if ziel:
        Path(ziel).write_text(text + "\n", encoding="utf-8")
        print(f"\nBericht geschrieben: {ziel}", file=sys.stderr)
    else:
        print("\n" + text)
    return len(treffer)


def po_uebersetzt(pfad: Path) -> tuple:
    """(uebersetzt, gesamt) einer lokalen PO-Datei, ohne Fremdbibliothek."""
    if not pfad.is_file():
        return (0, 0)
    txt = pfad.read_text(encoding="utf-8", errors="replace")
    gesamt = uebersetzt = 0
    for block in txt.split("\n\n"):
        if not block.strip() or block.lstrip().startswith("#~"):
            continue
        m = re.search(r'^msgid ((?:"[^"]*"\s*)+)', block, re.M)
        if not m or m.group(1).strip() == '""':
            continue
        gesamt += 1
        s = re.search(r'^msgstr ((?:"[^"]*"\s*)+)', block, re.M)
        if s and s.group(1).strip() != '""':
            uebersetzt += 1
    return (uebersetzt, gesamt)


def bestand(org: str, projekt: str, karte: dict, wurzel: Path, tok: str,
            ziel=None) -> int:
    """Vergleicht je Ressource den Transifex-Bestand mit dem Repo-Stand.

    Ein Force-Push schiebt unseren Stand ueber alles. Wo Transifex mehr
    uebersetzte Strings hat als wir, gehen genau diese verloren -- auch
    solche, die aelter sind als jedes Aktivitaetsfenster. Die Sammel-
    statistik beantwortet das fuer das ganze Projekt in vier Anfragen.

    Rueckgabe: Anzahl der Ressourcen, bei denen wir Inhalt verlieren.
    """
    params = {"filter[project]": f"o:{org}:p:{projekt}",
              "filter[language]": "l:de"}
    zeilen, fehlen = [], []
    tx_summe = repo_summe = 0

    for e in seiten("/resource_language_stats", params, tok):
        slug = slug_von(e)
        if not slug:
            continue
        attr = e.get("attributes") or {}
        tx_ue = attr.get("translated_strings") or 0
        tx_ges = attr.get("total_strings") or 0
        datei = karte.get(slug)
        if datei is None:
            fehlen.append((slug, tx_ue))
            continue
        repo_ue, repo_ges = po_uebersetzt(wurzel / datei)
        tx_summe += tx_ue
        repo_summe += repo_ue
        if tx_ue > repo_ue:
            zeilen.append((tx_ue - repo_ue, datei, slug, tx_ue, repo_ue, tx_ges))

    zeilen.sort(reverse=True)
    verlust = sum(z[0] for z in zeilen)

    aus = []
    aus.append(f"# Bestandsvergleich {org}:{projekt} gegen das Repo\n")
    aus.append(f"- Transifex uebersetzt (gesamt): {tx_summe}")
    aus.append(f"- Repo uebersetzt (gesamt):      {repo_summe}")
    aus.append(f"- Dateien mit Verlustgefahr:     {len(zeilen)}")
    aus.append(f"- Strings, die ein Force-Push loeschen wuerde: {verlust}\n")
    if zeilen:
        aus.append("| Datei | Ressource | TX | Repo | Verlust | TX gesamt |")
        aus.append("|---|---|---:|---:|---:|---:|")
        for d, datei, slug, tx_ue, repo_ue, tx_ges in zeilen:
            aus.append(f"| `{datei}` | {slug} | {tx_ue} | {repo_ue} | **{d}** | {tx_ges} |")
    else:
        aus.append("Keine Datei, bei der Transifex mehr hat als wir.")
    if fehlen:
        aus.append(f"\nNicht in .tx/config ({len(fehlen)}): "
                   + ", ".join(f"{s} ({n})" for s, n in sorted(fehlen)[:20]))

    text = "\n".join(aus)
    if ziel:
        Path(ziel).write_text(text + "\n", encoding="utf-8")
        print(f"Bericht geschrieben: {ziel}", file=sys.stderr)
    else:
        print(text)
    return len(zeilen)


def probe_anfrage(was: str, pfad: str, params: dict, tok: str) -> tuple:
    """Eine einzelne Seite holen und beschreiben, ohne zu blaettern."""
    url = API + pfad + ("?" + urllib.parse.urlencode(params) if params else "")
    print(f"\n{was}", file=sys.stderr)
    try:
        daten, kopf = hole(url, tok)
    except RuntimeError as fehler:
        print(f"  -> geht nicht: {str(fehler).splitlines()[0]}", file=sys.stderr)
        return None, {}
    menge = daten.get("data", [])
    weiter = bool((daten.get("links") or {}).get("next"))
    print(f"  -> {len(menge)} Eintraege auf Seite 1, weitere Seiten: "
          f"{'ja' if weiter else 'nein'}", file=sys.stderr)
    grenze(kopf)
    return daten, kopf


def probe(org: str, projekt: str, karte: dict, seit: str, tok: str) -> None:
    """Die offenen Fragen zur API mit wenigen Anfragen klaeren.

    Der vollstaendige Lauf stellt 553 Anfragen und bricht nach 20 Minuten
    ab. Vorher muessen drei Dinge feststehen: wirkt der Datumsfilter
    ueberhaupt, wie viel davon ist unsere eigene Hochladung, und laesst
    sich die Vorauswahl billiger beantworten als Ressource fuer Ressource.
    """
    slug = sorted(karte)[0]
    res_id = f"o:{org}:p:{projekt}:r:{slug}"
    basis = {"filter[resource]": res_id, "filter[language]": "l:de"}

    print(f"Probelauf gegen {org}:{projekt}, Ressource {slug}, seit {seit}",
          file=sys.stderr)

    # 1. Wirkt der Datumsfilter, oder wird er stillschweigend ignoriert?
    mit, filt = None, None
    for name in DATUMSFILTER:
        mit, _ = probe_anfrage(f"1. Uebersetzungen MIT Datumsfilter ({name})",
                               "/resource_translations", {**basis, name: seit}, tok)
        if mit is not None:
            filt = name
            break
    if filt is None:
        print("\n  Kein Datumsfilter wird angenommen -- es muss clientseitig"
              "\n  gefiltert werden, und das heisst: ganzer Katalog.", file=sys.stderr)

    ohne, _ = probe_anfrage(
        "2. Dieselbe Ressource OHNE Datumsfilter (zum Vergleich)",
        "/resource_translations", basis, tok)

    if mit is not None and ohne is not None:
        a, b = len(mit.get("data", [])), len(ohne.get("data", []))
        weiter_mit = bool((mit.get("links") or {}).get("next"))
        weiter_ohne = bool((ohne.get("links") or {}).get("next"))
        if a == b and weiter_mit == weiter_ohne:
            print("\n  ACHTUNG: beide Abfragen liefern gleich viel. Der Filter"
                  "\n  wirkt vermutlich nicht -- dann laedt der Lauf den ganzen"
                  "\n  Katalog und das erklaert die Drosselung.", file=sys.stderr)
        else:
            print(f"\n  Der Filter wirkt: {a} statt {b} Eintraege auf Seite 1.",
                  file=sys.stderr)

    # 2. Woher kommen die Eintraege im Zeitraum? Eine eigene Hochladung
    #    stempelt jeden String neu und blaeht die Antwort auf.
    if mit and mit.get("data"):
        herkuenfte, juengste, aelteste = {}, "", "9"
        for e in mit["data"]:
            q = herkunft(e)
            herkuenfte[q] = herkuenfte.get(q, 0) + 1
            z = zeitpunkt(e)
            if z:
                juengste, aelteste = max(juengste, z), min(aelteste, z)
        print("\n3. Herkunft auf Seite 1: "
              + ", ".join(f"{k}={v}" for k, v in sorted(herkuenfte.items()))
              + f"\n   Zeitraum: {aelteste[:16]} bis {juengste[:16]}",
              file=sys.stderr)
        print("   Felder des ersten Datensatzes: "
              + ", ".join(sorted((mit["data"][0].get("attributes") or {}))),
              file=sys.stderr)

    # 3. Laesst sich serverseitig auf Handarbeit einschraenken? Das waere
    #    der groesste Hebel: eine eigene Hochladung stempelt jeden String
    #    neu, und die werfen wir bisher erst nach dem Laden weg.
    #    Ohne Datumsfilter, damit eine Vergleichsmenge da ist -- gegen eine
    #    leere Antwort laesst sich nicht pruefen, ob der Filter wirkt.
    nur_editor, _ = probe_anfrage(
        "4. Laesst sich nach Herkunft filtern (filter[origin]=EDITOR,"
        " ohne Datumsfilter)?", "/resource_translations",
        {**basis, "filter[origin]": "EDITOR"}, tok)
    if nur_editor is not None and ohne is not None:
        a, b = len(nur_editor.get("data", [])), len(ohne.get("data", []))
        if a == b:
            print("   -> gleich viele wie ungefiltert: der Filter wirkt nicht.",
                  file=sys.stderr)
        else:
            print(f"   -> wirkt: {a} statt {b}. Damit liesse sich die eigene"
                  "\n      Hochladung serverseitig ausblenden.", file=sys.stderr)

    # 4. Die Vorauswahl in wenigen Anfragen statt in 553.
    stats, _ = probe_anfrage(
        "5. Sammelstatistik je Ressource (resource_language_stats)",
        "/resource_language_stats",
        {"filter[project]": f"o:{org}:p:{projekt}", "filter[language]": "l:de"}, tok)
    if stats and stats.get("data"):
        attr = stats["data"][0].get("attributes") or {}
        print("   Felder: " + ", ".join(sorted(attr)), file=sys.stderr)
        zeitfelder = [k for k in attr if "last" in k or "update" in k]
        if zeitfelder:
            feld = zeitfelder[0]
            frisch = [e for e in stats["data"]
                      if ((e.get("attributes") or {}).get(feld) or "") >= seit]
            print(f"   Mit {feld} >= {seit[:10]}: {len(frisch)} von "
                  f"{len(stats['data'])} auf Seite 1", file=sys.stderr)

    print("\nProbelauf fertig -- es wurde nichts veraendert.", file=sys.stderr)


def main() -> None:
    global TAKT
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tage", type=int, default=7, help="Zeitraum rueckwaerts (Vorgabe 7)")
    p.add_argument("--projekt", help="Projekt-Slug, sonst aus .tx/config")
    p.add_argument("--organisation", default=None, help="sonst aus .tx/config")
    p.add_argument("--ohne-upload", action="store_true", dest="ohne_upload",
                   help="Herkunft UPLOAD und API ausblenden (eigene Pushes)")
    p.add_argument("--csv", metavar="DATEI", help="Ergebnis zusaetzlich als CSV")
    p.add_argument("--pause", type=float, default=TAKT, metavar="SEK",
                   help=f"Mindestabstand zwischen Anfragen (Vorgabe {TAKT})")
    p.add_argument("--ab", metavar="SLUG",
                   help="erst ab diesem Ressourcen-Slug beginnen (Fortsetzung)")
    p.add_argument("--alle", action="store_true",
                   help="ohne Vorauswahl jede Ressource einzeln abfragen (langsam)")
    p.add_argument("--probe", action="store_true",
                   help="nur wenige Anfragen stellen und die API-Form pruefen")
    p.add_argument("--bestand", action="store_true",
                   help="je Datei Transifex-Bestand gegen Repo-Stand stellen "
                        "(zeigt, was ein Force-Push loeschen wuerde)")
    p.add_argument("--herkunft", metavar="BERICHT",
                   help="Ressourcen aus einem --bestand-Bericht auf Handarbeit "
                        "(EDITOR) pruefen; belegt den Filter erst an "
                        + KONTROLLE)
    p.add_argument("-o", "--out", metavar="DATEI",
                   help="Bericht in eine Datei schreiben statt auf die Konsole")
    p.add_argument("--debug", action="store_true",
                   help="ersten Rohdatensatz ausgeben (Feldnamen pruefen)")
    args = p.parse_args()

    TAKT = max(0.0, args.pause)

    tok = token()
    wurzel = repo_wurzel()
    karte, org, projekt = ressourcen(wurzel)
    org = args.organisation or org
    projekt = args.projekt or projekt
    branch = git("branch", "--show-current").strip()

    seit = (datetime.now(timezone.utc) - timedelta(days=args.tage)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    if args.probe:
        probe(org, projekt, karte, seit, tok)
        return

    if args.herkunft:
        bericht = Path(args.herkunft)
        if not bericht.is_file():
            sys.exit(f"{bericht} nicht gefunden -- erst --bestand laufen lassen.")
        slugs = slugs_aus_bericht(bericht)
        if not slugs:
            sys.exit(f"In {bericht} stehen keine Ressourcen.")
        print(f"{len(slugs)} Ressourcen aus {bericht}, Takt {TAKT}s",
              file=sys.stderr)
        betroffen = herkunft_pruefen(org, projekt, slugs, tok, args.out)
        sys.exit(1 if betroffen else 0)

    if args.bestand:
        print(f"Bestandsvergleich {org}:{projekt} (Branch {branch}) ...",
              file=sys.stderr)
        betroffen = bestand(org, projekt, karte, wurzel, tok, args.out)
        # Rueckgabewert 1, damit ein Skript den Push anhalten kann
        sys.exit(1 if betroffen else 0)

    print(f"Projekt {org}:{projekt} (Branch {branch}), Sprache de, seit {seit}",
          file=sys.stderr)

    liste = sorted(karte)
    if not args.alle:
        frisch = frische_ressourcen(org, projekt, seit, tok)
        unbekannt = frisch - set(karte)
        if unbekannt:
            print(f"  Hinweis: {len(unbekannt)} Ressource(n) in Transifex stehen"
                  f" nicht in .tx/config, z. B. {sorted(unbekannt)[0]}",
                  file=sys.stderr)
        liste = [s for s in liste if s in frisch]
        if not liste:
            print(f"\nKeine Aenderungen in den letzten {args.tage} Tagen gefunden.")
            return

    if args.ab:
        if args.ab not in liste:
            sys.exit(f"Ressource {args.ab} ist nicht in der Auswahl.")
        liste = liste[liste.index(args.ab):]

    print(f"{len(liste)} Ressourcen werden einzeln geprueft, Takt {TAKT}s ...",
          file=sys.stderr)

    filtername = list(DATUMSFILTER)
    gemeldet = False
    zeilen = []

    for nr, slug in enumerate(liste, 1):
        if nr % 50 == 0:
            print(f"  {nr}/{len(liste)} ({slug})", file=sys.stderr)
        res_id = f"o:{org}:p:{projekt}:r:{slug}"
        try:
            eintraege, benutzt = uebersetzungen(res_id, seit, tok, filtername)
        except Gedrosselt as fehler:
            # Das Gateway drosselt uns. Weiterlaufen heisst 500-mal gegen
            # dieselbe Wand fahren und die Drosselung verlaengern.
            print(f"\n!! Abbruch bei {slug}: {str(fehler).splitlines()[0]}\n"
                  f"   Transifex drosselt die Anfragen. Bisheriges Ergebnis\n"
                  f"   bleibt erhalten. Spaeter fortsetzen mit:\n"
                  f"     --ab {slug} --pause {max(1.0, TAKT * 3):.1f}",
                  file=sys.stderr)
            break
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
