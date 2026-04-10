#!/bin/sh
# Install bambox-bridge — standalone Bambu Lab printer bridge
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/estampo/bambox/main/install.sh | sh
#
# Environment variables:
#   BAMBOX_INSTALL_DIR  Override install location (default: ~/.local/bin)
#   BAMBOX_VERSION      Install a specific version (default: latest)

main() {
    set -e

    REPO="estampo/bambox"
    BIN_NAME="bambox-bridge"
    INSTALL_DIR="${BAMBOX_INSTALL_DIR:-$HOME/.local/bin}"
    VERSION="${BAMBOX_VERSION:-}"

    # -- Dependency check ------------------------------------------------------

    need_cmd curl
    need_cmd chmod
    need_cmd uname
    need_cmd mktemp

    # -- Platform detection ----------------------------------------------------

    OS=$(uname -s)
    ARCH=$(uname -m)

    case "$OS" in
        Linux)  PLATFORM="linux" ;;
        Darwin) PLATFORM="macos" ;;
        *)      err "Unsupported OS: $OS (only Linux and macOS are supported)" ;;
    esac

    case "$ARCH" in
        x86_64|amd64)   ARCH_TAG="x86_64" ;;
        arm64|aarch64)  ARCH_TAG="arm64" ;;
        *)              err "Unsupported architecture: $ARCH" ;;
    esac

    # Linux arm64 is not currently built
    if [ "$PLATFORM" = "linux" ] && [ "$ARCH_TAG" = "arm64" ]; then
        err "Linux arm64 binaries are not yet available. See https://github.com/${REPO}/issues for updates."
    fi

    ARTIFACT="${BIN_NAME}-${PLATFORM}-${ARCH_TAG}"

    # -- Resolve version -------------------------------------------------------

    if [ -z "$VERSION" ]; then
        info "Fetching latest release..."
        VERSION=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
            | grep '"tag_name"' | head -1 | cut -d'"' -f4) || true
        if [ -z "$VERSION" ]; then
            err "Could not determine latest release. Check https://github.com/${REPO}/releases"
        fi
    fi

    URL="https://github.com/${REPO}/releases/download/${VERSION}/${ARTIFACT}"

    # -- Download to temp file -------------------------------------------------

    TMPDIR=$(mktemp -d 2>/dev/null || mktemp -d -t bambox_install)
    # shellcheck disable=SC2064
    trap "rm -rf '$TMPDIR'" EXIT INT TERM

    TMPFILE="${TMPDIR}/${BIN_NAME}"

    info "Downloading ${BIN_NAME} ${VERSION} for ${PLATFORM}/${ARCH_TAG}..."
    if ! curl -fSL --progress-bar -o "$TMPFILE" "$URL"; then
        err "Download failed. Check that ${VERSION} has a release asset for ${PLATFORM}/${ARCH_TAG}."
    fi

    chmod +x "$TMPFILE"

    # -- Install ---------------------------------------------------------------

    mkdir -p "$INSTALL_DIR" 2>/dev/null || true

    if is_writable "$INSTALL_DIR"; then
        mv "$TMPFILE" "${INSTALL_DIR}/${BIN_NAME}"
    elif has_cmd sudo; then
        info "Elevated permissions required to install to ${INSTALL_DIR}"
        sudo mkdir -p "$INSTALL_DIR"
        sudo mv "$TMPFILE" "${INSTALL_DIR}/${BIN_NAME}"
        sudo chmod +x "${INSTALL_DIR}/${BIN_NAME}"
    else
        err "${INSTALL_DIR} is not writable and sudo is not available.\nSet BAMBOX_INSTALL_DIR to a writable directory:\n  BAMBOX_INSTALL_DIR=~/bin sh -c '\$(curl -fsSL ...)'"
    fi

    # macOS: remove quarantine attribute to avoid Gatekeeper prompts
    if [ "$OS" = "Darwin" ]; then
        xattr -d com.apple.quarantine "${INSTALL_DIR}/${BIN_NAME}" 2>/dev/null || true
    fi

    # -- Success ---------------------------------------------------------------

    echo ""
    success "Installed ${BIN_NAME} ${VERSION} to ${INSTALL_DIR}/${BIN_NAME}"
    echo ""
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$INSTALL_DIR"; then
        warn "Add ${INSTALL_DIR} to your PATH:"
        echo "  export PATH=\"${INSTALL_DIR}:\$PATH\""
        echo ""
    fi
    echo "Run '${BIN_NAME} --help' to get started."
    echo "The Bambu networking library will be downloaded automatically on first use."
}

# -- Helpers -------------------------------------------------------------------

info() {
    printf '\033[1;34m==>\033[0m %s\n' "$1"
}

success() {
    printf '\033[1;32m==>\033[0m %s\n' "$1"
}

warn() {
    printf '\033[1;33m==>\033[0m %s\n' "$1"
}

err() {
    printf '\033[1;31merror:\033[0m %s\n' "$1" >&2
    exit 1
}

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "Required command '$1' not found. Please install it and try again."
    fi
}

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

is_writable() {
    # Check if directory exists and is writable, or if parent is writable
    if [ -d "$1" ]; then
        [ -w "$1" ]
    else
        # Directory doesn't exist yet — check if we can create it
        _parent=$(dirname "$1")
        [ -d "$_parent" ] && [ -w "$_parent" ]
    fi
}

main "$@"
