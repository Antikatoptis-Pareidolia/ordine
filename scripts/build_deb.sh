#!/usr/bin/env bash
# Build a self-contained .deb with a venv under /opt/conveyor.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_ROOT}/dist"
STAGING_DIR="${REPO_ROOT}/build/deb-staging"
VERSION="$(cd "${REPO_ROOT}" && uv run python -c "import conveyor; print(conveyor.__version__)")"

rm -rf "${STAGING_DIR}"
mkdir -p "${DIST_DIR}" "${STAGING_DIR}/opt/conveyor" "${STAGING_DIR}/usr/bin"
mkdir -p "${STAGING_DIR}/usr/lib/systemd/user"

python3 -m venv "${STAGING_DIR}/opt/conveyor"

echo "Installing conveyor ${VERSION} into staging venv..."
"${STAGING_DIR}/opt/conveyor/bin/pip" install --upgrade pip
"${STAGING_DIR}/opt/conveyor/bin/pip" install "${REPO_ROOT}"

# Rewrite shebangs in /opt/conveyor/bin to the installed venv python.
VENV_PYTHON="/opt/conveyor/bin/python3"
while IFS= read -r -d '' script; do
  if head -n 1 "${script}" | grep -q '^#!'; then
  sed -i "1s|^#!.*python3.*|#!${VENV_PYTHON}|" "${script}"
  fi
done < <(find "${STAGING_DIR}/opt/conveyor/bin" -maxdepth 1 -type f -print0)

ln -sf "${VENV_PYTHON}" "${STAGING_DIR}/opt/conveyor/bin/python3"
ln -sf "../opt/conveyor/bin/conveyor" "${STAGING_DIR}/usr/bin/conveyor"
cp "${REPO_ROOT}/packaging/conveyor.service" "${STAGING_DIR}/usr/lib/systemd/user/conveyor.service"

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

echo "Built ${DEB_OUT}"
