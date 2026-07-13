#!/usr/bin/env bash
# Build a self-contained .deb with a venv under /opt/ordine.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_ROOT}/dist"
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

rm -rf "${STAGING_DIR}"
mkdir -p "${DIST_DIR}"
rm -rf "${DIST_DIR:?}/"*
mkdir -p "${DIST_DIR}" "${STAGED_ROOT}" "${STAGING_DIR}/usr/bin"
mkdir -p "${STAGING_DIR}/usr/lib/systemd/user"

# --copies embeds a real python3 binary (no symlinks to the build machine). Deb size grows by one
# interpreter copy; avoids ELOOP if python3 were rewritten to a self-referential symlink.
python3 -m venv --copies "${STAGED_ROOT}"

echo "Installing ordine ${VERSION} into staging venv..."
"${STAGED_BIN}/pip" install --upgrade --no-compile pip
# Install from repo root (not a pre-built wheel) so paths with spaces stay robust.
"${STAGED_BIN}/pip" install --no-compile "${REPO_ROOT}"

# Editable/source provenance is not useful in the relocatable artifact and can embed REPO_ROOT.
while IFS= read -r -d '' direct_url; do
  record="${direct_url%/direct_url.json}/RECORD"
  rm -f "${direct_url}"
  if [[ -f "${record}" ]]; then
    grep -v 'direct_url\.json' "${record}" >"${record}.tmp"
    mv "${record}.tmp" "${record}"
  fi
done < <(find "${STAGED_ROOT}" -type f -name direct_url.json -print0)

if [[ ! -f "${STAGED_ORDINE}" ]]; then
  echo "missing venv entry point: ${VENV_ORDINE}" >&2
  exit 1
fi

# Rewrite entry-script shebangs to the installed venv python (leave python3/python as venv copies).
while IFS= read -r -d '' script; do
  if grep -q '^#!' <<<"$(head -n 1 "${script}")"; then
    sed -i "1s|^#!.*python3.*|#!${VENV_PYTHON}|" "${script}"
  fi
done < <(find "${STAGED_BIN}" -maxdepth 1 -type f -print0)

if [[ ! -f "${STAGED_PYTHON}" ]] || [[ -L "${STAGED_PYTHON}" ]]; then
  echo "staged ${VENV_PYTHON} must be a regular file (venv --copies)" >&2
  exit 1
fi
"${STAGED_PYTHON}" --version
ordine_shebang="$(head -n 1 "${STAGED_ORDINE}")"
if [[ "${ordine_shebang}" != "#!${VENV_PYTHON}" ]]; then
  echo "unexpected ordine shebang: ${ordine_shebang} (want #!${VENV_PYTHON})" >&2
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

if home_hits="$(grep -RIl --binary-files=without-match '/home/' "${STAGING_DIR}")"; then
  echo "build-machine paths found in deb staging:" >&2
  printf '%s\n' "${home_hits}" >&2
  exit 1
fi
echo "Assertion passed: no /home/ build-machine paths in staging"

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build the .deb (gem install fpm)" >&2
  exit 1
fi

DEB_OUT="${DIST_DIR}/ordine_${VERSION}_amd64.deb"
fpm -s dir -t deb -n ordine -v "${VERSION}" -p "${DEB_OUT}" \
  -C "${STAGING_DIR}" \
  --url "https://github.com/Antikatoptis-Pareidolia/ordine" \
  --maintainer "Constantin Vlad" \
  --depends "python3 (>= 3.11)" \
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
