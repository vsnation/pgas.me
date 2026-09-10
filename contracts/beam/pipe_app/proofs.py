#!/usr/bin/env python3
"""K1 proofs — READ-ONLY. Every wallet call is invoke_contract with create_tx:false, which runs the
app shader and returns what it WOULD sign; nothing is signed, nothing is sent, nothing is stored.
process_invoke_data is never called. Prints PASS/FAIL per proof; exit 1 if any proof fails."""
import json, os, sys, httpx

def env(path):
    d = {}
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k] = v.strip().strip('"').strip("'")
    return d

# Where things are: the service's env file (never printed), or explicit overrides.
ENV_FILE = os.environ.get("PGAS_ENV_FILE", "/etc/pgasme.env")
E = env(ENV_FILE) if os.path.exists(ENV_FILE) else {}
URL = os.environ.get("WALLET_API") or E.get("PGAS_BEAM_WALLET_API", "http://127.0.0.1:10001/api/wallet")
DEPLOYED = os.environ.get("SHADER_DEPLOYED") or E.get("PGAS_BEAM_SHADER", "/opt/pgasme/beam/pipe_app.wasm")
INDEXED = os.environ.get("SHADER_INDEXED") or os.path.join(os.path.dirname(DEPLOYED), "pipe_app.indexed.wasm")
PIPES = {
    "ETH": ("8872509d36a8e2aa7a60839a1828c372af47c0a5309f3f6186379cddec847369", E.get("PGAS_BEAM_PIPE_PUBKEY_ETH", "")),
    "DAI": ("02fb908e55a59ab5acc5bf6f1707a8dcdb70a944d6f2a7bff3c7af18c8e278da", E.get("PGAS_BEAM_PIPE_PUBKEY_DAI", "")),
    "WBTC": ("7c66181ba4625202aae6e46afe89acbf1f839523344b0b371fc7988ac2e8c056", E.get("PGAS_BEAM_PIPE_PUBKEY_WBTC", "")),
}
MSG = int(os.environ.get("PROOF_MSG_ID", "138"))  # an unclaimed ETH-pipe message delivered to the legacy key
fails = []

def invoke(shader, args):
    body = {"jsonrpc": "2.0", "id": 1, "method": "invoke_contract",
            "params": {"contract_file": shader, "args": args, "create_tx": False}}
    assert body["params"]["create_tx"] is False
    r = httpx.post(URL, json=body, timeout=120)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        return {"rpc_error": j["error"], "output": {}, "raw": None}
    res = j.get("result") or {}
    out = res.get("output")
    try:
        out = json.loads(out) if isinstance(out, str) else (out or {})
    except ValueError:
        out = {"_unparsed": out}
    raw = res.get("raw_data")
    if isinstance(raw, list):
        raw = bytes(int(b) & 0xFF for b in raw)
    elif isinstance(raw, str):
        try: raw = bytes.fromhex(raw)
        except ValueError: raw = raw.encode()
    return {"output": out, "raw": raw}

def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (("  — " + detail) if detail else ""))
    if not ok:
        fails.append(name)

print("wallet-api:", URL.replace("127.0.0.1", "localhost"))
# ── P1: legacy get_pk equality on every pipe ─────────────────────────────────────────────
for sym, (cid, cfg) in PIPES.items():
    a = invoke(DEPLOYED, f"role=user,action=get_pk,cid={cid}")["output"]
    b = invoke(INDEXED, f"role=user,action=get_pk,cid={cid}")["output"]
    pa, pb = a.get("pk"), b.get("pk")
    check(f"P1 {sym}: indexed app legacy pk == deployed app pk", bool(pa) and pa == pb, f"deployed={pa} indexed={pb} index={b.get('index')}")
    if cfg:
        check(f"P1 {sym}: == configured pubkey", pa == cfg, "config matches" if pa == cfg else f"config differs ({cfg[:12]}…)")
    b0 = invoke(INDEXED, f"role=user,action=get_pk,cid={cid},index=0")["output"]
    check(f"P1 {sym}: index=0 == legacy", b0.get("pk") == pa)

# ── P2: indexed keys distinct, 33 bytes, stable; deployed app ignores index (the trap) ────
cid_eth, legacy = PIPES["ETH"][0], None
legacy = invoke(INDEXED, f"role=user,action=get_pk,cid={cid_eth}")["output"]["pk"]
seen = {}
for i in range(1, 6):
    x = invoke(INDEXED, f"role=user,action=get_pk,cid={cid_eth},index={i}")["output"]
    y = invoke(INDEXED, f"role=user,action=get_pk,cid={cid_eth},index={i}")["output"]
    pk = x.get("pk")
    check(f"P2 index={i}: 33 bytes", isinstance(pk, str) and len(pk) == 66, pk)
    check(f"P2 index={i}: stable across two calls", pk == y.get("pk"))
    check(f"P2 index={i}: != legacy", pk != legacy)
    check(f"P2 index={i}: != every other index", pk not in seen.values())
    check(f"P2 index={i}: echoes index", x.get("index") == i, f"index={x.get('index')}")
    seen[i] = pk
big = invoke(INDEXED, f"role=user,action=get_pk,cid={cid_eth},index=18446744073709551615")["output"]
check("P2 index=2^64-1: derives a distinct key", big.get("pk") and big["pk"] not in seen.values() and big["pk"] != legacy)
d1 = invoke(DEPLOYED, f"role=user,action=get_pk,cid={cid_eth},index=1")["output"]
check("P2 trap: DEPLOYED app answers the legacy key for index=1 (documented, expected)", d1.get("pk") == legacy)
# same index on another pipe → different key (the cid is in the blob)
dai1 = invoke(INDEXED, f"role=user,action=get_pk,cid={PIPES['DAI'][0]},index=1")["output"]
check("P2 index=1 on DAI pipe != index=1 on ETH pipe", dai1.get("pk") != seen[1])

# ── P3: view_incoming lists msg 138 with index 0, under every window shape ───────────────
def incoming(shader, extra=""):
    r = invoke(shader, f"role=manager,action=view_incoming,cid={cid_eth},startFrom=0{extra}")
    return r["output"], r["output"].get("incoming")
dep_out, dep_rows = incoming(DEPLOYED)
print("   deployed view_incoming:", json.dumps(dep_rows)[:300])
for label, extra in (("default window", ""), ("indexes=1;2;3", ",indexes=1;2;3"), ("maxIndex=0", ",maxIndex=0"), ("maxIndex=256", ",maxIndex=256"), ("indexes=5;9 + maxIndex=2", ",indexes=5;9,maxIndex=2")):
    out, rows = incoming(INDEXED, extra)
    print(f"   indexed view_incoming [{label}]:", json.dumps(rows)[:300], ("error=" + out["error"]) if "error" in out else "")
    check(f"P3 [{label}]: returns a list", isinstance(rows, list))
    if isinstance(rows, list):
        mine = [m for m in rows if m.get("MsgId") == MSG]
        check(f"P3 [{label}]: msg {MSG} listed", bool(mine))
        check(f"P3 [{label}]: msg {MSG} index == 0 (legacy)", bool(mine) and mine[0].get("index") == 0)
        check(f"P3 [{label}]: same MsgId set as the deployed app", isinstance(dep_rows, list) and sorted(m["MsgId"] for m in rows) == sorted(m["MsgId"] for m in dep_rows))
        check(f"P3 [{label}]: same amounts as the deployed app", isinstance(dep_rows, list) and {m["MsgId"]: m["amount"] for m in rows} == {m["MsgId"]: m["amount"] for m in dep_rows})
o, rows = incoming(INDEXED, ",maxIndex=257")
check("P3 maxIndex=257 refused", o.get("error", "").startswith("maxIndex too large"), o.get("error"))
o, rows = incoming(INDEXED, ",indexes=" + ";".join(str(i) for i in range(1, 258)))
check("P3 257 explicit indexes refused", o.get("error", "").startswith("too many indexes"), o.get("error"))
o, rows = incoming(INDEXED, ",indexes=" + ";".join(str(i) for i in range(1, 257)))
check("P3 256 explicit indexes accepted", isinstance(rows, list))
for sym in ("DAI", "WBTC"):
    r = invoke(INDEXED, f"role=manager,action=view_incoming,cid={PIPES[sym][0]},startFrom=0")["output"]
    print(f"   indexed view_incoming {sym}:", json.dumps(r.get('incoming'))[:200])

# ── P4: receive for msg 138 — identical invoke data; wrong index refused before signing ──
ra = invoke(DEPLOYED, f"role=user,action=receive,cid={cid_eth},msgId={MSG}")
rb = invoke(INDEXED, f"role=user,action=receive,cid={cid_eth},msgId={MSG}")
print("   deployed receive output:", json.dumps(ra["output"])[:200], "raw_len=", None if ra["raw"] is None else len(ra["raw"]))
print("   indexed  receive output:", json.dumps(rb["output"])[:200], "raw_len=", None if rb["raw"] is None else len(rb["raw"]))
check("P4 deployed app produced invoke data", bool(ra["raw"]))
check("P4 indexed app (no index) produced invoke data", bool(rb["raw"]))
check("P4 raw invoke data BYTE-EQUAL deployed vs indexed", bool(ra["raw"]) and ra["raw"] == rb["raw"], f"{len(ra['raw'] or b'')} bytes")
check("P4 invoke data contains the cid", bool(rb["raw"]) and bytes.fromhex(cid_eth) in rb["raw"])
check("P4 indexed receive output index == 0", rb["output"].get("index") == 0)
rc = invoke(INDEXED, f"role=user,action=receive,cid={cid_eth},msgId={MSG},index=1")
print("   indexed receive index=1:", json.dumps(rc["output"])[:200], "raw_len=", None if rc["raw"] is None else len(rc["raw"]))
check("P4 wrong index REFUSED with 'receiver key mismatch'", "receiver key mismatch" in rc["output"].get("error", ""))
check("P4 wrong index: NO invoke data", not rc["raw"])
rd = invoke(INDEXED, f"role=user,action=receive,cid={cid_eth},msgId=140")  # already claimed
check("P4 claimed msg 140 refused as processed/absent", rd["output"].get("error") in ("msg is processed", "msg with current id is absent"), rd["output"].get("error"))

# ── P5: the untouched actions answer the same ───────────────────────────────────────────
import hashlib
for act in (f"role=user,action=local_msg_count,cid={cid_eth}", f"role=manager,action=view_params,cid={cid_eth}", f"role=user,action=msg_status,cid={cid_eth},msgId={MSG}", f"role=user,action=remote_msg,cid={cid_eth},msgId={MSG}", f"role=user,action=local_msg,cid={cid_eth},msgId=1"):
    a = invoke(DEPLOYED, act)["output"]; b = invoke(INDEXED, act)["output"]
    name = act.split(",")[1]
    if a.get("error") == "invalid Action." and "error" not in b:
        print(f"INFO {name}: the deployed app predates this upstream action (answers 'invalid Action.'); the indexed build answers {json.dumps(b)[:160]}")
        continue
    check("P5 same output: " + name, a == b, f"deployed={json.dumps(a)[:120]} indexed={json.dumps(b)[:120]}" if a != b else json.dumps(b)[:120])
# ── P6: the two files ───────────────────────────────────────────────────────────────────
for label, path in (("deployed", DEPLOYED), ("indexed", INDEXED)):
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    print(f"   {label}: {os.path.getsize(path)} bytes sha256 {h}")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)
