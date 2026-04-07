#!/bin/sh
# Install bambu-bridge — standalone Bambu Lab printer bridge
set -e

REPO="estampo/bambox"
INSTALL_DIR="${BAMBOX_INSTALL_DIR:-$HOME/.local/bin}"

# Detect platform
OS=$(uname -s)
ARCH=$(uname -m)

case "$OS" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="macos" ;;
  *)      echo "Unsupported OS: $OS"; exit 1 ;;
esac

case "$ARCH" in
  x86_64|amd64) ARCH_TAG="x86_64" ;;
  arm64|aarch64) ARCH_TAG="arm64" ;;
  *)             echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

BINARY="bambu-bridge-${PLATFORM}-${ARCH_TAG}"

# Get latest release tag
TAG=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" | grep '"tag_name"' | head -1 | cut -d'"' -f4)
if [ -z "$TAG" ]; then
  echo "Error: could not determine latest release"
  exit 1
fi

URL="https://github.com/${REPO}/releases/download/${TAG}/${BINARY}"

echo "Downloading bambu-bridge ${TAG} for ${PLATFORM}/${ARCH_TAG}..."
mkdir -p "$INSTALL_DIR"
curl -fsSL -o "${INSTALL_DIR}/bambu-bridge" "$URL"
chmod +x "${INSTALL_DIR}/bambu-bridge"

echo ""
echo "Installed bambu-bridge to ${INSTALL_DIR}/bambu-bridge"
echo ""
if ! echo "$PATH" | tr ':' '\n' | grep -q "^${INSTALL_DIR}$"; then
  echo "Add ${INSTALL_DIR} to your PATH:"
  echo "  export PATH=\"${INSTALL_DIR}:\$PATH\""
  echo ""
fi
echo "Run 'bambu-bridge --help' to get started."
echo "The Bambu networking library will be downloaded automatically on first use."
