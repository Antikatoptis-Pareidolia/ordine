#!/usr/bin/env bash
# Build a self-contained .deb with a venv under /opt/conveyor.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_ROOT}/dist"
STAGING_DIR="${REPO_ROOT}/build/deb-staging"
VERSION="$(cd "${REPO_ROOT}" && uv run python -c "import conveyor; print(conveyor.__version__)")"
VENV_ROOT="/opt/conveyor"
VENV_BIN="${VENV_ROOT}/bin"
VENV_PYTHON="${VENV_BIN}/python3"
VENV_CONVEYOR="${VENV_BIN}/conveyor"

rm -rf "${STAGING_DIR}"
mkdir -p "${DIST_DIR}" "${STAGING_DIR}${VENV_ROOT}" "${STAGING_DIR}/usr/bin"
mkdir -p "${STAGING_DIR}/usr/lib/systemd/user"

python3 -m venv "${STAGING_DIR}${VENV_ROOT}"

echo "Installing conveyor ${VERSION} into staging venv..."
"${STAGING_DIR}${VENV_BIN}/pip" install --upgrade pip
# Install from repo root (not a pre-built wheel) so paths with spaces stay robust.
"${STAGING_DIR}${VENV_BIN}/pip" install "${REPO_ROOT}"

if [[ ! -f "${STAGING_DIR}${VENV_CONVEYOR}" ]]; then
  echo "missing venv entry point: ${VENV_CONVEYOR}" >&2
  exit 1
fi

# Rewrite shebangs in /opt/conveyor/bin to the installed venv python.
while IFS= read -r -d '' script; do
  if head -n 1 "${script}" | grep -q '^#!'; then
    sed -i "1s|^#!.*python3.*|#!${VENV_PYTHON}|" "${script}"
  fi
done < <(find "${STAGING_DIR}${VENV_BIN}" -maxdepth 1 -type f -print0)

ln -sf "${VENV_PYTHON}" "${STAGING_DIR}${VENV_BIN}/python3"
# Absolute target: ../opt/... from /usr/bin resolves to /usr/opt/... (wrong).
ln -sf "${VENV_CONVEYOR}" "${STAGING_DIR}/usr/bin/conveyor"
cp "${REPO_ROOT}/packaging/conveyor.service" "${STAGING_DIR}/usr/lib/systemd/user/conveyor.service"

if [[ "$(readlink "${STAGING_DIR}/usr/bin/conveyor")" != "${VENV_CONVEYOR}" ]]; then
  echo "usr/bin/conveyor symlink target unexpected" >&2
  exit 1
fi

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build the .deb (gem install fpm)" >&2
  exit 1
fi

DEB_OUT="${DIST_DIR}/conveyor_${VERSION}_amd64.deb"
fpm -s dir -t deb -n conveyor -v "${VERSION}" -p "${DEB_OUT}" \
  -C "${STAGING_DIR}" \
  --depends "python3 (>= 3.11)" \
  --deb-recommends imagemagick \
  --description "Local-first automation pipelines" \
  opt usr

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "dpkg-deb is required to verify package contents" >&2
  exit 1
fi

DEB_LIST="$(dpkg-deb -c "${DEB_OUT}")"
echo "${DEB_LIST}" | grep -q './usr/bin/conveyor'
echo "${DEB_LIST}" | grep -q './opt/conveyor/bin/conveyor'

echo "Built ${DEB_OUT}"
