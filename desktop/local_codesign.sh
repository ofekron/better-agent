#!/bin/bash

set -euo pipefail

IDENTITY_NAME="${BETTER_AGENT_LOCAL_CODESIGN_IDENTITY:-Better Agent Local Development}"
IDENTIFIER="com.betteragent.app"
LOGIN_KEYCHAIN="$(security default-keychain -d user | tr -d '"[:space:]')"

identity_hash() {
  security find-identity -v -p codesigning "$LOGIN_KEYCHAIN" \
    | awk -v name="$IDENTITY_NAME" 'index($0, "\"" name "\"") { print $2; exit }'
}

ensure_identity() {
  local identity
  identity="$(identity_hash)"
  if [ -n "$identity" ]; then
    printf '%s\n' "$identity"
    return 0
  fi

  local temp_dir password
  temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/ba-codesign.XXXXXX")"
  chmod 700 "$temp_dir"
  trap 'rm -rf "$temp_dir"' EXIT
  if ! security find-certificate -c "$IDENTITY_NAME" -p "$LOGIN_KEYCHAIN" \
    > "$temp_dir/cert.pem" 2>/dev/null; then
    password="$(openssl rand -hex 32)"
    openssl req -new -newkey rsa:3072 -x509 -sha256 -days 3650 -nodes \
      -subj "/CN=$IDENTITY_NAME/O=Better Agent Local Development" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,digitalSignature,keyCertSign" \
      -addext "extendedKeyUsage=codeSigning" \
      -keyout "$temp_dir/key.pem" -out "$temp_dir/cert.pem" >/dev/null 2>&1
    openssl pkcs12 -export -legacy \
      -inkey "$temp_dir/key.pem" -in "$temp_dir/cert.pem" \
      -name "$IDENTITY_NAME" -passout "pass:$password" \
      -out "$temp_dir/identity.p12"
    security import "$temp_dir/identity.p12" -k "$LOGIN_KEYCHAIN" \
      -P "$password" -x -T /usr/bin/codesign >/dev/null
  fi

  echo "macOS will ask once to trust the local Better Agent signing identity."
  security add-trusted-cert -r trustRoot -p codeSign \
    -k "$LOGIN_KEYCHAIN" "$temp_dir/cert.pem"

  identity="$(identity_hash)"
  if [ -z "$identity" ]; then
    echo "Better Agent local code-signing identity is unavailable." >&2
    return 1
  fi
  rm -rf "$temp_dir"
  trap - EXIT
  printf '%s\n' "$identity"
}

sign_target() {
  local target="$1"
  local identity
  identity="$(ensure_identity | tail -n 1)"
  codesign --force --deep --timestamp=none --identifier "$IDENTIFIER" \
    --sign "$identity" "$target"
  verify_target "$target"
}

verify_target() {
  local target="$1"
  local details
  codesign --verify --deep --strict "$target"
  details="$(codesign --display --verbose=4 "$target" 2>&1)"
  grep -Fq "Identifier=$IDENTIFIER" <<<"$details"
  grep -Fq "Authority=$IDENTITY_NAME" <<<"$details"
}

case "${1:-}" in
  ensure)
    ensure_identity
    ;;
  sign)
    [ "$#" -eq 2 ] || { echo "usage: $0 sign TARGET" >&2; exit 2; }
    sign_target "$2"
    ;;
  verify)
    [ "$#" -eq 2 ] || { echo "usage: $0 verify TARGET" >&2; exit 2; }
    verify_target "$2"
    ;;
  *)
    echo "usage: $0 {ensure|sign TARGET|verify TARGET}" >&2
    exit 2
    ;;
esac
