# -*- coding: utf-8 -*-
"""Genera logdata.json per RaidPace: mappa dei LOG PUBBLICI su Warcraft Logs per
ogni boss delle zone indicate. Autonomo (nessuna dipendenza dall'app privata).

Credenziali WCL da variabili d'ambiente WCL_CLIENT_ID / WCL_CLIENT_SECRET.
Uso:  python gen.py 46 [altre zone...]   (default: 46)
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
DIFFS = [5, 4]  # Mythic, Heroic

FR_QUERY = ("query($e:Int!,$d:Int!,$p:Int!){worldData{encounter(id:$e){"
            "fightRankings(difficulty:$d,page:$p)}}}")
ENC_QUERY = "query($z:Int!){worldData{zone(id:$z){name encounters{id name}}}}"


def _lognorm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isascii() and ch.isalnum())


def get_token():
    cid = os.environ.get("WCL_CLIENT_ID", "").strip()
    sec = os.environ.get("WCL_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        sys.exit("Manca WCL_CLIENT_ID / WCL_CLIENT_SECRET")
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    data = b"grant_type=client_credentials"
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def gql(token, query, variables, tries=4):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL_URL, data=body, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read().decode())
        return j.get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code in (429, 502, 503) and tries > 1:
            time.sleep(2.0 * (5 - tries))
            return gql(token, query, variables, tries - 1)
        raise


def zone_encounters(token, zid):
    z = (gql(token, ENC_QUERY, {"z": zid}).get("worldData") or {}).get("zone") or {}
    return z.get("name") or str(zid), (z.get("encounters") or [])


def log_map(token, eid, diff):
    logs = {}
    for page in range(1, 60):
        try:
            fr = ((gql(token, FR_QUERY, {"e": eid, "d": diff, "p": page})
                   .get("worldData") or {}).get("encounter") or {}).get("fightRankings") or {}
        except Exception as e:
            sys.stderr.write(f"  enc {eid} diff {diff} page {page}: {e}\n")
            break
        for r in (fr.get("rankings") or []):
            g = r.get("guild") or {}
            s = r.get("server") or {}
            rep = r.get("report") or {}
            nm, code = g.get("name"), rep.get("code")
            if not (nm and code):
                continue
            key = (_lognorm(nm) + "|" + _lognorm(s.get("name"))
                   + "|" + (s.get("region") or "").lower())
            logs.setdefault(key, "https://www.warcraftlogs.com/reports/"
                            f"{code}?fight={rep.get('fightID') or 'last'}")
        if not fr.get("hasMorePages"):
            break
    return logs


def main():
    zones = [int(a) for a in sys.argv[1:]] or [46]
    token = get_token()
    out = {"generatedAt": int(time.time()), "encounters": {}}
    for zid in zones:
        zname, encs = zone_encounters(token, zid)
        sys.stderr.write(f"zona {zid} '{zname}': {len(encs)} boss\n")
        for e in encs:
            eid = e.get("id")
            for diff in DIFFS:
                m = log_map(token, eid, diff)
                if m:
                    out["encounters"][f"{eid}:{diff}"] = m
                sys.stderr.write(f"  {e.get('name')} (enc {eid} diff {diff}): "
                                 f"{len(m)} log\n")
    with open("logdata.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    sys.stderr.write(f"scritto logdata.json ({len(out['encounters'])} enc)\n")


if __name__ == "__main__":
    main()
