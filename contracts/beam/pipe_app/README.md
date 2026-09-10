# Beam bridge pipe app — one receiver key per deposit

This directory holds the **wallet-side app shader** Pgas.me uses to talk to the Beam bridge's pipe
contracts, patched so that every deposit can name a **different** receiver key on Ethereum. The
bridge contracts themselves — `EthPipe`/`EthERC20Pipe` on Ethereum and the pipe shader on Beam —
are untouched; the change lives entirely in the app that the wallet runs to *derive keys, list
incoming messages and sign the claim*.

## Why

The upstream app derives exactly one receiver public key per (wallet, pipe contract): the key is
`Env::DerivePk(cid)`, so every `sendFunds` the service ever asked a user to make carried the same
33 bytes, and every deposit was linkable to every other one by that field alone. The pipe contract
does not care which key a message names — its claim method authorises *whatever* key the relayer
stored with the message (`Env::AddSig(msg.m_UserPK)`) — so the wallet is free to derive many keys,
as long as it can sign for each of them.

## The change

A packed blob `KeyID{cid, index}` is the key identity for index ≥ 1; the blob for index 0 (or no
index) is the contract id alone, byte-for-byte the upstream derivation. The same blob is handed to
`SigRequest` when the claim is signed, which is how the wallet proves it owns the key the message
names. Concretely, in `shaders/pipe_app.cpp`:

| action | upstream | this build |
|---|---|---|
| `get_pk cid [index]` | `{"pk"}` from `DerivePk(cid)` | `{"pk","index"}`; index absent or 0 → the legacy key, otherwise `DerivePk(KeyID{cid,index})` |
| `receive cid msgId [index]` | signs with the cid blob | signs with the blob for `index`; **refuses before signing** (`receiver key mismatch`) when the derived key is not the one the message names |
| `view_incoming cid [startFrom] [maxIndex] [indexes]` | lists messages sent to the one key | matches each message against a bounded set: the legacy key plus keys `1..maxIndex` (≤ 256; default 64 when nothing is given) and/or an explicit `indexes` list; answers `index` next to `MsgId` (0 = legacy) |

`indexes` is a `;`-separated list (`indexes=3;7;12`): the wallet splits the whole `args` string on
`,` before the app sees it, so a comma can never be inside a value. The bounds are refused in words
(`maxIndex too large (max 256)`, `too many indexes (max 256)`, `indexes list too long`).

Everything else — `send`, `push_remote` (the relayer's own key, deliberately untouched), the
local/remote message readers, `msg_status`, `view_params` — is byte-for-byte upstream. The full
patched source is `pipe_app.cpp`; the authoritative change is `pipe_app.indexed.diff` against the
pinned upstream commit in `upstream.txt`.

## Build (reproducible)

```
./build.sh          # clones the pinned commit, applies the diff, compiles in docker, checks SHA256SUMS
```

The toolchain is a container built from `Dockerfile` (Debian bookworm's clang 14 + LLD 14), with
the flags Beam's own `cmake/AddShader.cmake` uses. The build is deterministic: two independent
builds produced the same file, and `build.sh` exits 0 only when the sha256 matches `SHA256SUMS`.
`upstream.txt` also records the hash of the *unpatched* source through the same toolchain, so the
patch's effect on the binary can be reproduced by anyone.

## Proofs

`proofs.py` runs against a live wallet-api and makes **read-only** calls only — every one is
`invoke_contract` with `create_tx: false`, which runs the app and returns what it *would* sign;
nothing is signed, sent or stored, and `process_invoke_data` is never called. `proofs.log` is the
run against the production wallet on 2026-09-10 (76 checks, all passed):

- **P1** for all three pipes (ETH, DAI, WBTC) the patched app's key with no index equals the
  currently deployed app's key and the key the service has configured — so every message already
  sent to us stays claimable and nothing changes for a deposit made before the switch.
- **P2** indexes 1–5 give five distinct 33-byte keys, stable across calls, each different from the
  legacy key; index 1 on one pipe differs from index 1 on another (the cid is in the blob). The
  currently deployed app answers the *legacy* key for `index=1` — that is the tell the service's
  key allocator refuses, so an unpatched wallet can never issue N quotes on one shared key.
- **P3** `view_incoming` lists the same messages with the same amounts as the deployed app under
  every window shape (default, explicit list, `maxIndex=0`, `maxIndex=256`, list + window), with
  `index: 0` on a legacy message; 257 keys are refused, 256 accepted.
- **P4** `receive` for an unclaimed legacy message produces invoke data **byte-equal** to the
  deployed app's (100 bytes); the same call with `index=1` is refused with `receiver key mismatch`
  and returns no invoke data; an already-claimed message is refused as before.
- **P5** the unchanged read actions answer identically. Two upstream actions (`view_params`,
  `msg_status`) predate the app currently deployed, which answers `invalid Action.` for them; the
  patched build answers them.

## Deploying it

The app runs inside the wallet, so switching is a file replacement: put `pipe_app.wasm` where the
service's `PGAS_BEAM_SHADER` points and restart the API. No chain transaction, no contract change,
and rolling back is restoring the previous file. The service side (index allocation, the key in
each quote's calldata, attribution by key, claiming with the row's index) is gated by
`PGAS_RECEIVER_KEY_PER_DEPOSIT` and is off until the wasm is in place — with the stock app deployed
the allocator refuses to issue keys rather than silently issuing one shared key under many indexes.

## What this does and does not buy

Each deposit can now name a fresh key, so the receiver field no longer ties one user's deposits to
another's. It is not a stealth address: the keys are ours, the bridge relayer still sees which
wallet claims what, and on a bridge with as little traffic as this one a never-repeated key is
itself a pattern. Amounts and timing remain the stronger link; this closes the one that was a
constant.
