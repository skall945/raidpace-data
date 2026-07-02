# -*- coding: utf-8 -*-
"""Genera logdata.json per RaidPace: mappa LEGGERA di CHI ha un log pubblico su
Warcraft Logs, per ogni boss delle zone indicate (fightRankings, ~15 query/boss).

L'app usa questa mappa come indicatore 'ha log' + filtro; i pull/log ESATTI della
prima kill li prende al clic (dal vivo). Cosi' copre TUTTI i raid senza il costo
enorme del pre-calcolo first-kill.

Autonomo. Credenziali WCL da env WCL_CLIENT_ID / WCL_CLIENT_SECRET.
Uso:  python gen.py 46 44 42 ...   (ID zona WCL). Default: le zone in ZONES.
Diff: env DIFFS (default "5 4" = Mythic+Heroic).

Formato: {"generatedAt":epoch,"encounters":{"<enc>:<diff>":{"<nome|server|reg>":1}}}
"""
import sys
import os
import json
import time
import base64
import urllib.request
import urllib.error

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
GQL_URL = "https://www.warcraftlogs.com/api/v2/client"
DIFFS = [int(x) for x in os.environ.get("DIFFS", "5 4").split()]
# zone di default: dai raid recenti (Dragonflight in poi). Sovrascrivibili da argv.
ZONES = [int(x) for x in os.environ.get("ZONES", "31 33 35 38 42 44 46 50 53 54 57").split()]

FR_QUERY = ("query($e:Int!,$d:Int!,$p:Int!){worldData{encounter(id:$e){"
            "fightRankings(difficulty:$d,page:$p)}}}")
ENC_QUERY = "query($z:Int!){worldData{zone(id:$z){name encounters{id name}}}}"
_TOKEN = None


def _lognorm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isascii() and ch.isalnum())


def get_token():
    cid = os.environ.get("WCL_CLIENT_ID", "").strip()
    sec = os.environ.get("WCL_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        sys.exit("Manca WCL_CLIENT_ID / WCL_CLIENT_SECRET")
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=b"grant_type=client_credentials",
                                 headers={"Authorization": "Basic " + basic,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def gql(query, variables, tries=6):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL_URL, data=body, headers={
        "Authorization": "Bearer " + _TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode()).get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code in (429, 502, 503) and tries > 1:
            time.sleep(3.0 * (7 - tries))
            return gql(query, variables, tries - 1)
        raise


def zone_encounters(zid):
    z = (gql(ENC_QUERY, {"z": zid}).get("worldData") or {}).get("zone") or {}
    return z.get("name") or str(zid), (z.get("encounters") or [])


def haslog_map(eid, diff):
    out = {}
    for page in range(1, 60):
        try:
            fr = ((gql(FR_QUERY, {"e": eid, "d": diff, "p": page})
                   .get("worldData") or {}).get("encounter") or {}).get("fightRankings") or {}
        except Exception as ex:
            sys.stderr.write(f"  enc {eid} d{diff} p{page}: {ex}\n")
            break
        for r in (fr.get("rankings") or []):
            g = r.get("guild") or {}
            s = r.get("server") or {}
            nm = g.get("name")
            if not nm:
                continue
            out[_lognorm(nm) + "|" + _lognorm(s.get("name")) + "|"
                + (s.get("region") or "").lower()] = 1
        if not fr.get("hasMorePages"):
            break
    return out


def main():
    global _TOKEN
    zones = [int(a) for a in sys.argv[1:]] or ZONES
    _TOKEN = get_token()
    outfile = os.environ.get("OUT", "logdata.json")
    # riparti dal file esistente: aggiornamenti incrementali per zona
    try:
        out = json.load(open(outfile, encoding="utf-8"))
        out.setdefault("encounters", {})
    except Exception:
        out = {"generatedAt": int(time.time()), "encounters": {}}
    for zid in zones:
        try:
            zname, encs = zone_encounters(zid)
        except Exception as ex:
            sys.stderr.write(f"zona {zid}: {ex}\n")
            continue
        if not encs:
            continue
        sys.stderr.write(f"zona {zid} '{zname}': {len(encs)} boss\n")
        for e in encs:
            eid = e.get("id")
            for diff in DIFFS:
                t = time.time()
                m = haslog_map(eid, diff)
                if m:
                    out["encounters"][f"{eid}:{diff}"] = m
                out["generatedAt"] = int(time.time())
                with open(outfile, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
                sys.stderr.write(f"  {e.get('name')} (enc {eid} d{diff}): "
                                 f"{len(m)} con log in {time.time()-t:.0f}s\n")


if __name__ == "__main__":
    main()
