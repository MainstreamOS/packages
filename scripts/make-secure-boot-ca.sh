#!/usr/bin/env bash
# Creates Mainstream's Secure Boot certificate authority and a signing certificate under it.
#   make-secure-boot-ca.sh <private-directory>
#
# The authority's certificate is what a firmware's signature database and a list of
# well-known keys carry. The signing certificate is what kernels and boot loaders are
# signed with, so it can be replaced without anyone enrolling anything again.
# RSA 2048 with SHA-256, because that is what every UEFI firmware verifies.
#
# Nothing is overwritten: the script stops if the directory already holds an authority.
# The private keys are written unencrypted and readable by their owner only. Where they
# live after that (an offline copy, a hardware token) is for a person to decide.
set -euo pipefail
umask 077

dir="${1:?usage: make-secure-boot-ca.sh <private-directory>}"
mkdir -p "$dir"
[[ ! -e "$dir/ca.key" && ! -e "$dir/signer.key" ]] || { echo "$dir already holds keys; nothing was changed." >&2; exit 1; }
year="$(date +%Y)"

conf="$(mktemp)"
trap 'rm -f "$conf"' EXIT
cat > "$conf" <<'CONFEND'
[ req ]
distinguished_name = dn
prompt = no
[ dn ]
O = Mainstream OS
CN = placeholder
[ ca_ext ]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[ signer_ext ]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = codeSigning
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
CONFEND

openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$dir/ca.key"
openssl req -new -x509 -sha256 -days 7300 -key "$dir/ca.key" -config "$conf" -extensions ca_ext \
    -subj "/O=Mainstream OS/CN=Mainstream OS Secure Boot CA" -out "$dir/ca.crt"
openssl x509 -in "$dir/ca.crt" -outform DER -out "$dir/ca.cer"

openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$dir/signer.key"
openssl req -new -sha256 -key "$dir/signer.key" -config "$conf" \
    -subj "/O=Mainstream OS/CN=Mainstream OS Secure Boot Signing $year" -out "$dir/signer.csr"
openssl x509 -req -sha256 -days 1825 -in "$dir/signer.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" \
    -CAcreateserial -extfile "$conf" -extensions signer_ext -out "$dir/signer.crt" 2>/dev/null
rm -f "$dir/signer.csr"

chmod 600 "$dir/ca.key" "$dir/signer.key"
chmod 644 "$dir/ca.crt" "$dir/ca.cer" "$dir/signer.crt"
openssl verify -CAfile "$dir/ca.crt" "$dir/signer.crt" >/dev/null
echo "Authority:  $(openssl x509 -in "$dir/ca.crt" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')"
echo "  SHA-256:  $(openssl x509 -in "$dir/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
echo "Signer:     $(openssl x509 -in "$dir/signer.crt" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')"
echo "Written to $dir (ca.key and signer.key are private)."
