#!/usr/bin/env bash
#
# pre_push_check.sh -- Ein Skript, zwei Stufen:
#   Stufe 1 (immer, ca. 1 Minute): msgfmt-Syntaxcheck + sphinx-lint + eigener
#            Rollen-Konsistenz-Check (findet die meisten "echten" Bugs wie
#            falsche/fehlende/ueberzaehlige :ref:/:term:/:class:/... -Rollen,
#            OHNE dass ein kompletter Sphinx-Build noetig ist)
#   Stufe 2 (optional, ca. 10-20 Min. auf einem M1 Mac): echter Sphinx-Build
#            wie in test-build.yml, fuer die letzte Sicherheit vor dem Push
#
# Der zu pruefende Branch wird automatisch erkannt (aktuell ausgecheckter
# Branch), nicht abgefragt.
#
# Nutzung:
#   ./pre_push_check.sh                    		# fragt interaktiv beide Stufen ab
#   ./pre_push_check.sh --quick            		# nur Stufe 1, keine Nachfrage
#   ./pre_push_check.sh --full             		# Stufe 1 + 2, keine Nachfrage
#   ./pre_push_check.sh --full --version 3.15   # Stufe 2 mit expliziter Version

set -uo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
CPYTHON_ROOT="${CPYTHON_ROOT:-/Volumes/Dev_SSD/GitHub}"
CHECK_ROLES_PY="${CHECK_ROLES_PY:-$REPO_ROOT/check_roles.py}"
EXCLUDE_DIRS=("c-api" ".venv" ".git")

BRANCH="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null)"
if [[ -z "$BRANCH" ]]; then
    echo "Konnte den aktuellen Git-Branch nicht erkennen (bist du in einem Git-Repo?)."
    exit 1
fi
echo "Aktiver Branch: $BRANCH"

RUN_QUICK=true
RUN_FULL=false
VERSION="$BRANCH"

args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
    case "${args[$i]}" in
        --quick) RUN_FULL=false ;;
        --full) RUN_FULL=true ;;
        --version)
            if [[ $((i+1)) -lt ${#args[@]} ]]; then
                VERSION="${args[$((i+1))]}"
            fi
            ;;
    esac
done

NONINTERACTIVE=false
if [[ " $* " == *" --quick "* || " $* " == *" --full "* ]]; then
    NONINTERACTIVE=true
fi

pass=0
fail=0

log_section() { echo; echo "=== $1 ==="; }
is_excluded() {
    local f="$1"
    for d in "${EXCLUDE_DIRS[@]}"; do
        [[ "$f" == *"/$d/"* || "$f" == "$d/"* ]] && return 0
    done
    return 1
}

if ! $NONINTERACTIVE; then
    echo
    echo "Stufe 1: Schnellcheck (msgfmt + sphinx-lint + Rollen-Konsistenz) -- ca. 1 Minute."
    read -r -p "Durchfuehren? [J/n]: " ans
    if [[ "$ans" == "n" || "$ans" == "N" ]]; then
        RUN_QUICK=false
    fi
fi

if $RUN_QUICK; then
    log_section "1a/3  msgfmt --check (Syntax)"
    msgfmt_errors=0
    while IFS= read -r -d '' po; do
        rel="${po#"$REPO_ROOT"/}"
        is_excluded "$rel" && continue
        if ! out=$(msgfmt --check --check-format -o /dev/null "$po" 2>&1); then
            echo "FEHLER: $rel"
            echo "$out" | sed 's/^/    /'
            msgfmt_errors=$((msgfmt_errors + 1))
        fi
    done < <(find "$REPO_ROOT" -name "*.po" -print0)
    if [[ $msgfmt_errors -eq 0 ]]; then
        echo "OK -- keine Syntaxfehler."
        pass=$((pass + 1))
    else
        echo "$msgfmt_errors Datei(en) mit Syntaxfehlern."
        fail=$((fail + 1))
    fi

    log_section "1b/3  sphinx-lint (Backtick-/Rollen-Syntax)"
    if ! command -v sphinx-lint &>/dev/null; then
        echo "sphinx-lint nicht gefunden -- installiere mit: pip install sphinx-lint"
        fail=$((fail + 1))
    else
        lint_targets=()
        while IFS= read -r -d '' po; do
            rel="${po#"$REPO_ROOT"/}"
            is_excluded "$rel" && continue
            lint_targets+=("$po")
        done < <(find "$REPO_ROOT" -name "*.po" -print0)
        lint_out=$(sphinx-lint "${lint_targets[@]}" 2>&1)
        if [[ -z "$lint_out" || "$lint_out" == "No problems found."* ]]; then
            echo "OK -- keine sphinx-lint-Warnungen."
            pass=$((pass + 1))
        else
            echo "$lint_out"
            fail=$((fail + 1))
        fi
    fi

    log_section "1c/3  Rollen-Konsistenz (eigener Check, kein Build noetig)"
    if [[ ! -f "$CHECK_ROLES_PY" ]]; then
        echo "check_roles.py nicht gefunden unter $CHECK_ROLES_PY -- Schritt uebersprungen."
    else
        if python3 "$CHECK_ROLES_PY" "$REPO_ROOT" --exclude "$(IFS=,; echo "${EXCLUDE_DIRS[*]}")"; then
            pass=$((pass + 1))
        else
            fail=$((fail + 1))
        fi
    fi
fi

if ! $NONINTERACTIVE; then
    echo
    echo "Stufe 2: Vollstaendiger Sphinx-Build (Version $VERSION, wie test-build.yml)"
    echo "         -- ca. 10-20 Minuten auf einem M1 Mac."
    read -r -p "Durchfuehren? [j/N]: " ans
    if [[ "$ans" == "j" || "$ans" == "J" ]]; then
        RUN_FULL=true
    fi
fi

if $RUN_FULL; then
    log_section "2/2  Voller Sphinx-Build (Version $VERSION)"

    CPYTHON_SRC="$CPYTHON_ROOT/cpython-${VERSION}-src"

    if [[ ! -d "$CPYTHON_SRC" ]]; then
        echo "Kein Checkout unter $CPYTHON_SRC gefunden -- klone frisch (--depth 1)..."
        git clone --branch "$VERSION" --single-branch --depth 1 \
            https://github.com/python/cpython.git "$CPYTHON_SRC" || exit 1
    else
        echo "Aktualisiere vorhandenen Checkout..."
        (cd "$CPYTHON_SRC" && git pull)
    fi

    if ! command -v uv &>/dev/null; then
        echo "'uv' nicht gefunden -- installiere mit: pip install uv"
        exit 1
    fi

    echo "Baue venv (make venv)..."
    (cd "$CPYTHON_SRC/Doc" && make venv) || { echo "make venv fehlgeschlagen."; exit 1; }

    echo "Installiere rsvg-convert, falls noetig (fuer LaTeX-Build)..."
    if ! command -v rsvg-convert &>/dev/null; then
        if command -v brew &>/dev/null; then
            brew install librsvg
        else
            echo "Hinweis: rsvg-convert fehlt und Homebrew wurde nicht gefunden."
        fi
    fi

    LOCALE_DIR="$CPYTHON_SRC/Doc/locales/de/LC_MESSAGES"
    echo "Verknuepfe aktuellen Uebersetzungsstand ($BRANCH) nach $LOCALE_DIR ..."
    rm -rf "$LOCALE_DIR"
    mkdir -p "$(dirname "$LOCALE_DIR")"
    cp -r "$REPO_ROOT/." "$LOCALE_DIR/"
    rm -rf "$LOCALE_DIR/.git" "$LOCALE_DIR/.venv"

    echo "Starte 'make html' mit -W --keep-going (wie CI) ..."
    BUILD_LOG=$(mktemp)
    (cd "$CPYTHON_SRC/Doc" && \
        make -e SPHINXOPTS="--color -D language='de' -D suppress_warnings=i18n.inconsistent_references -W --keep-going" html \
        > "$BUILD_LOG" 2>&1)
    build_status=$?

    echo
    echo "--- Warnungen/Fehler (ohne den bekannten Sphinx-Bug i18n.inconsistent_references) ---"
    relevant=$(grep -E "WARNING|ERROR" "$BUILD_LOG" | grep -v "i18n.inconsistent_references" || true)
    if [[ -z "$relevant" ]]; then
        echo "Keine weiteren Warnungen gefunden."
    else
        echo "$relevant"
    fi
    echo
    echo "Vollstaendiges Build-Log: $BUILD_LOG"

    if [[ $build_status -eq 0 && -z "$relevant" ]]; then
        echo "OK -- Build erfolgreich, keine (neuen) Warnungen."
        pass=$((pass + 1))
    else
        echo "Build meldete Warnungen/Fehler (Exit-Code $build_status)."
        fail=$((fail + 1))
    fi
fi

log_section "Zusammenfassung (Branch: $BRANCH)"
if [[ $pass -eq 0 && $fail -eq 0 ]]; then
    echo "Keine Pruefungen durchgefuehrt."
    exit 0
fi
echo "Bestanden: $pass   Fehlgeschlagen: $fail"
if [[ $fail -eq 0 ]]; then
    echo "Alles sauber -- bereit zum Push."
    exit 0
else
    echo "Bitte die oben aufgefuehrten Punkte beheben, bevor gepusht wird."
    exit 1
fi