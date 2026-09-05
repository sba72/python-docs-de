# Übersetzungsregeln python-docs-de 

## Sprache
- Konsequent "Du"-Form
- msgid (Englisch) NIE verändern, nur msgstr bearbeiten

## Sphinx/reST-Syntax
- `:ref:`Anzeigetext <ziel>`` → nur Anzeigetext übersetzen, Ziel bleibt Englisch
- `:class:`/:func:`/:meth:`/:attr:`/:mod:`/:exc:`/:const:`/:keyword:`/:program:`/:pep:`/:kbd:`X`` ohne <...> → X bleibt unverändert
- `~`-Präfix (z.B. `:meth:`~object.__lt__``) → kompletter Ausdruck inkl. ~ unverändert
- `!`-Präfix (z.B. `:mod:`!string``) → nur Verlinkung unterdrückt, Bezeichner unverändert
- Backticks sauber schließen, kein Leerzeichen davor
- `::` am Satzende (leitet Codeblock ein) bleibt stehen
- Zitierte Originaltitel aus externen Standards (z.B. Unicode Standard "Default Case Folding") bleiben unübersetzt in Anführungszeichen
- Platzhalter/Makros (%s, {0}, <TRANSLATION_REPO_>) unverändert

## Code-/REPL-Beispiele
- Reiner Code, Ausgaben, Tracebacks: unverändert
- NUR `#`-Kommentare darin übersetzen
- msgstr immer identisch zu msgid befüllen, NIE leer lassen (auch bei reinem Code)

## Fuzzy-Flags
- Nach Prüfung/Übersetzung entfernen
- Datei-Header-fuzzy (über leerem msgid "") ist reine Altlast, ohne Prüfung löschbar

## Terminologie (unübersetzt lassen)
Dictionary, Tuple, List Comprehension, Sentinel, Lazy Import, Property,
Slice, Type Hints, f-string/f-String, t-string/T-String, Whitespace (Singular),
Subclassing, API, Repository, Wheel

## Terminologie (feste Übersetzung)
- frozen... → unveränderlich (NICHT "eingefroren")
- picklable → picklebar
- I/O (NICHT E/A)
- locale → Ländereinstellung
- presentation type → Darstellungstyp
- String (als Datentyp) → Zeichenkette
- Debug/Conversion/Format specifier → Debug-/Konvertierungs-/Formatbezeichner
- rich comparisons → erweiterte Vergleichsoperationen
- generic over → "Typparameter" bei mehreren festen Parametern (z.B. dict: zwei),
  "hinsichtlich des Typs" bei genau einem Parameter (list/set/frozenset/memoryview),
  Plural "Typen" bei variabler Anzahl (tuple)

## PO-Datei-Struktur
- Genau ein leerer msgid ""-Header mit charset=UTF-8 pro Datei
- Zeilenlänge ~80 Zeichen (powrap via CI erledigt das automatisch bei Push)
- Vor Push immer lokal prüfen: find . -name "*.po" -not -path "./c-api/*" -exec msgfmt --check {} -o /dev/null \;

## Core-Dateien (Pflicht für Sprachschalter-Aufnahme)
Nur: bugs.po, library/functions.po, tutorial/*.po