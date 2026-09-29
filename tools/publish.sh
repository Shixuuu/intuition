#!/usr/bin/env bash
# Publish the two artifacts this repo produces.
#
#   tools/publish.sh --check            preflights only, nothing is uploaded
#   tools/publish.sh --npm              publish the Pi extension to npm
#   tools/publish.sh --pypi             publish the Python package to PyPI
#
# Both uploads are irreversible: a published npm version can only be replaced
# within 72 hours, and PyPI never accepts the same version twice. The check mode
# is what you run before either.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PY:-$ROOT/.venv/bin/python}"
NPM_NAME="$(python3 -c "import json;print(json.load(open('package.json'))['name'])")"
DIST_NAME="$(python3 -c "
import tomllib
print(tomllib.load(open('pyproject.toml','rb'))['project']['name'])
")"

mode="${1:---check}"

say() { printf '%s\n' "$*"; }
fail() { printf '✘ %s\n' "$*" >&2; exit 1; }

preflight() {
  say "== preflight =="
  git diff --quiet && git diff --cached --quiet \
    || fail "working tree is dirty; publish the commit you tested"
  "$PY" -c "
import json, tomllib, pathlib
root = json.loads(pathlib.Path('package.json').read_text())
nested = json.loads(pathlib.Path('src/intuition/adapters/pi_package/package.json').read_text())
project = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']
assert root['version'] == nested['version'] == project['version'], (
    root['version'], nested['version'], project['version'])
print(f'   versions agree at {root[\"version\"]}')
"
  "$PY" -c "
import json, pathlib, sys
sys.path.insert(0, 'src')
from intuition.tools import host_manifest
shipped = json.loads(pathlib.Path('src/intuition/adapters/pi_package/extensions/intuition/tools.json').read_text())
assert shipped == host_manifest(), 'regenerate the shipped tools.json'
print('   shipped tool manifest matches the schema')
"
  "$PY" -m pytest tests -q | tail -1
  "$PY" -m ruff check src tests
  say "   npm name $NPM_NAME: $(registry_state "https://registry.npmjs.org/$NPM_NAME")"
  say "   PyPI name $DIST_NAME: $(registry_state "https://pypi.org/pypi/$DIST_NAME/json")"
  if [ "$(registry_state "https://pypi.org/pypi/$DIST_NAME/json")" = "taken" ]; then
    say "   PyPI needs a free name; change [project] name in pyproject.toml."
    for candidate in intuition-memory intuition-agent-memory; do
      say "     candidate $candidate: $(registry_state "https://pypi.org/pypi/$candidate/json")"
    done
  fi
}

registry_state() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$1" || echo 000)"
  case "$code" in
    404) echo available ;;
    200) echo taken ;;
    000) echo unknown ;;
    *)   echo "http $code" ;;
  esac
}

case "$mode" in
  --check)
    preflight
    say
    say "== artifacts =="
    npm pack --dry-run 2>&1 | grep -E "Tarball Contents|npm notice [0-9]" | sed 's/npm notice/  /' || true
    rm -rf dist && "$PY" -m build >/dev/null
    ls -1 dist
    say
    say "Nothing was uploaded. To publish:"
    say "  npm login && tools/publish.sh --npm"
    say "  tools/publish.sh --pypi        # needs ~/.pypirc or TWINE_PASSWORD"
    ;;
  --npm)
    command -v npm >/dev/null || fail "npm is not installed"
    npm whoami >/dev/null 2>&1 || fail "npm has no credentials here; run 'npm login' first"
    preflight
    say "== publishing $NPM_NAME to npm =="
    npm publish
    say "verify with: pi install npm:$NPM_NAME"
    ;;
  --pypi)
    "$PY" -m twine --version >/dev/null 2>&1 \
      || fail "install the uploader first: $PY -m pip install twine"
    [ -f "$HOME/.pypirc" ] || [ -n "${TWINE_PASSWORD:-}" ] \
      || fail "no PyPI credentials; write ~/.pypirc or export TWINE_PASSWORD"
    preflight
    say "== publishing $DIST_NAME to PyPI =="
    rm -rf dist && "$PY" -m build
    "$PY" -m twine upload dist/*
    say "verify with: pipx install $DIST_NAME"
    ;;
  *)
    fail "usage: tools/publish.sh [--check|--npm|--pypi]"
    ;;
esac
