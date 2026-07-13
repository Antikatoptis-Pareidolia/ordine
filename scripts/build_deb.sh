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
STAGED_ROOT="${STAGING_DIR}${VENV_ROOT}"
STAGED_BIN="${STAGING_DIR}${VENV_BIN}"
STAGED_PYTHON="${STAGING_DIR}${VENV_PYTHON}"
STAGED_CONVEYOR="${STAGING_DIR}${VENV_CONVEYOR}"

rm -rf "${STAGING_DIR}"
mkdir -p "${DIST_DIR}" "${STAGED_ROOT}" "${STAGING_DIR}/usr/bin"
mkdir -p "${STAGING_DIR}/usr/lib/systemd/user"

# --copies embeds a real python3 binary (no symlinks to the build machine). Deb size grows by one
# interpreter copy; avoids ELOOP if python3 were rewritten to a self-referential symlink.
python3 -m venv --copies "${STAGED_ROOT}"

echo "Installing conveyor ${VERSION} into staging venv..."
"${STAGED_BIN}/pip" install --upgrade pip
# Install from repo root (not a pre-built wheel) so paths with spaces stay robust.
"${STAGED_BIN}/pip" install "${REPO_ROOT}"

if [[ ! -f "${STAGED_CONVEYOR}" ]]; then
  echo "missing venv entry point: ${VENV_CONVEYOR}" >&2
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
conveyor_shebang="$(head -n 1 "${STAGED_CONVEYOR}")"
if [[ "${conveyor_shebang}" != "#!${VENV_PYTHON}" ]]; then
  echo "unexpected conveyor shebang: ${conveyor_shebang} (want #!${VENV_PYTHON})" >&2
  exit 1
fi

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

listing="$(dpkg-deb -c "${DEB_OUT}")"
grep -q './usr/bin/conveyor' <<<"${listing}"
grep -q './opt/conveyor/bin/conveyor' <<<"${listing}"

echo "Built ${DEB_OUT}"
