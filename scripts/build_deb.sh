#!/usr/bin/env bash
# Build a locked .deb with an isolated venv under /opt/ordine.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEB_DIST_DIR="${REPO_ROOT}/deb-dist"
STAGING_DIR="${REPO_ROOT}/build/deb-staging"
VERSION="$(cd "${REPO_ROOT}" && uv run python -c "import ordine; print(ordine.__version__)")"
VENV_ROOT="/opt/ordine"
VENV_BIN="${VENV_ROOT}/bin"
VENV_PYTHON="${VENV_BIN}/python3"
VENV_ORDINE="${VENV_BIN}/ordine"
STAGED_ROOT="${STAGING_DIR}${VENV_ROOT}"
STAGED_BIN="${STAGING_DIR}${VENV_BIN}"
STAGED_PYTHON="${STAGING_DIR}${VENV_PYTHON}"
STAGED_ORDINE="${STAGING_DIR}${VENV_ORDINE}"
WHEEL="${REPO_ROOT}/dist/ordine-${VERSION}-py3-none-any.whl"
LOCKED_REQUIREMENTS="${STAGING_DIR}/requirements.locked.txt"
PYTHON_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_NEXT_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor + 1}")')"

if [[ ! -f "${WHEEL}" ]]; then
  echo "missing pre-built wheel: ${WHEEL}; run 'uv build' first" >&2
  exit 1
fi

rm -rf "${STAGING_DIR}"
mkdir -p "${DEB_DIST_DIR}"
rm -rf "${DEB_DIST_DIR:?}/"*
mkdir -p "${DEB_DIST_DIR}" "${STAGED_ROOT}" "${STAGING_DIR}/usr/bin"
mkdir -p "${STAGING_DIR}/usr/lib/systemd/user"

# Runtime and compiled-wheel paths are Python-minor-specific. Use the target's system interpreter
# and declare the matching minor range below instead of copying a build-host interpreter.
python3 -m venv --symlinks "${STAGED_ROOT}"

echo "Installing ordine ${VERSION} into staging venv..."
# Install the exact dependency versions tested under uv.lock, then the already verified wheel.
(cd "${REPO_ROOT}" && uv export --quiet --locked --no-dev --no-emit-project \
  --format requirements-txt --output-file "${LOCKED_REQUIREMENTS}")
"${STAGED_BIN}/pip" install --no-compile --require-hashes -r "${LOCKED_REQUIREMENTS}"
"${STAGED_BIN}/pip" install --no-compile --no-deps "${WHEEL}"

# Editable/source provenance is not useful in the relocatable artifact and can embed REPO_ROOT.
while IFS= read -r -d '' direct_url; do
  record="${direct_url%/direct_url.json}/RECORD"
  rm -f "${direct_url}"
  if [[ -f "${record}" ]]; then
    grep -v 'direct_url\.json' "${record}" >"${record}.tmp"
    mv "${record}.tmp" "${record}"
  fi
done < <(find "${STAGED_ROOT}" -type f -name direct_url.json -print0)
rm -f "${STAGED_BIN}/activate" "${STAGED_BIN}/activate.csh" \
  "${STAGED_BIN}/activate.fish" "${STAGED_BIN}/Activate.ps1"
sed -i '/^command = /d' "${STAGED_ROOT}/pyvenv.cfg"

if [[ ! -f "${STAGED_ORDINE}" ]]; then
  echo "missing venv entry point: ${VENV_ORDINE}" >&2
  exit 1
fi

# Rewrite entry-script shebangs to the installed venv python (leave python3/python as venv copies).
while IFS= read -r -d '' script; do
  if ! grep -Iq . "${script}"; then
    continue
  fi
  first_line="$(head -n 1 "${script}")"
  if [[ "${first_line}" == "#!/bin/sh" ]] && sed -n '2p' "${script}" | grep -q "'''exec'"; then
    sed -i "2s|\"[^\"]*python3\"|\"${VENV_PYTHON}\"|" "${script}"
  elif [[ "${first_line}" == '#!'*python3* ]]; then
    sed -i "1s|^#!.*python3.*|#!${VENV_PYTHON}|" "${script}"
  fi
done < <(find "${STAGED_BIN}" -maxdepth 1 -type f -print0)

if [[ ! -L "${STAGED_PYTHON}" ]] || [[ "$(readlink "${STAGED_PYTHON}")" != "/usr/bin/python3" ]]; then
  echo "staged ${VENV_PYTHON} must link to /usr/bin/python3" >&2
  exit 1
fi
"${STAGED_PYTHON}" --version
ordine_shebang="$(head -n 1 "${STAGED_ORDINE}")"
if [[ "${ordine_shebang}" == "#!/bin/sh" ]]; then
  ordine_exec="$(sed -n '2p' "${STAGED_ORDINE}")"
  ordine_launcher_ok=false
  [[ "${ordine_exec}" == *"\"${VENV_PYTHON}\""* ]] && ordine_launcher_ok=true
else
  ordine_launcher_ok=false
  [[ "${ordine_shebang}" == "#!${VENV_PYTHON}" ]] && ordine_launcher_ok=true
fi
if [[ "${ordine_launcher_ok}" != true ]]; then
  echo "unexpected ordine launcher: ${ordine_shebang}" >&2
  exit 1
fi

metadata_name="$("${STAGED_PYTHON}" -c 'from importlib.metadata import metadata; print(metadata("ordine")["Name"])')"
if [[ "${metadata_name}" != "ordine" ]]; then
  echo "unexpected Python artifact metadata Name: ${metadata_name}" >&2
  exit 1
fi
echo "Assertion passed: Python artifact metadata Name=ordine"

# Absolute target: ../opt/... from /usr/bin resolves to /usr/opt/... (wrong).
ln -sf "${VENV_ORDINE}" "${STAGING_DIR}/usr/bin/ordine"
cp "${REPO_ROOT}/packaging/ordine.service" "${STAGING_DIR}/usr/lib/systemd/user/ordine.service"

if [[ "$(readlink "${STAGING_DIR}/usr/bin/ordine")" != "${VENV_ORDINE}" ]]; then
  echo "usr/bin/ordine symlink target unexpected" >&2
  exit 1
fi

# Normalize package permissions after installers have populated the tree.
find "${STAGING_DIR}" -type d -exec chmod 0755 {} +
find "${STAGING_DIR}" -type f -exec chmod 0644 {} +
find "${STAGED_BIN}" -maxdepth 1 -type f -exec chmod 0755 {} +

provenance_hits="$(find "${STAGING_DIR}/opt" "${STAGING_DIR}/usr" -type f \
  -exec grep -Il --binary-files=without-match -F "${REPO_ROOT}" {} + || true)"
if [[ -n "${provenance_hits}" ]]; then
  echo "repository path found in deb staging:" >&2
  printf '%s\n' "${provenance_hits}" >&2
  exit 1
fi
echo "Assertion passed: no repository path in packaged files"

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build the .deb (gem install fpm)" >&2
  exit 1
fi

DEB_OUT="${DEB_DIST_DIR}/ordine_${VERSION}_amd64.deb"
fpm -s dir -t deb -n ordine -v "${VERSION}" -p "${DEB_OUT}" \
  -C "${STAGING_DIR}" \
  --url "https://github.com/Antikatoptis-Pareidolia/ordine" \
  --maintainer "Constantin Vlad" \
  --depends "python3 (>= ${PYTHON_MINOR})" \
  --depends "python3 (<< ${PYTHON_NEXT_MINOR})" \
  --deb-recommends imagemagick \
  --description "Ordine — self-healing task pipelines for your desktop." \
  opt usr

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "dpkg-deb is required to verify package contents" >&2
  exit 1
fi

listing="$(dpkg-deb -c "${DEB_OUT}")"
grep -q './usr/bin/ordine' <<<"${listing}"
grep -q './opt/ordine/bin/ordine' <<<"${listing}"
package_name="$(dpkg-deb -f "${DEB_OUT}" Package)"
if [[ "${package_name}" != "ordine" ]]; then
  echo "unexpected deb package name: ${package_name}" >&2
  exit 1
fi
echo "Assertion passed: deb Package=ordine"

echo "Built ${DEB_OUT}"
