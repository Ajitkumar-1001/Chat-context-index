/**
 * Minimal ULID generation (spec/storage-format.md ID formats: t_/m_/c_/n_<ULID>).
 * Mirrors _ids.py: Crockford base32 over a 48-bit millisecond timestamp + 80 bits of
 * randomness. No new dependency (ponytail: the ~20 lines are smaller than adding a package).
 */

import { randomBytes } from "node:crypto";

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

function encode(value: bigint, length: number): string {
  let chars = "";
  let v = value;
  for (let i = 0; i < length; i++) {
    const rem = v % 32n;
    v = v / 32n;
    chars = CROCKFORD[Number(rem)] + chars;
  }
  return chars;
}

export function newUlid(): string {
  const tsMs = BigInt(Date.now());
  const randomness = BigInt("0x" + randomBytes(10).toString("hex"));
  return encode(tsMs, 10) + encode(randomness, 16);
}

export function prefixedId(prefix: string): string {
  return `${prefix}_${newUlid()}`;
}
