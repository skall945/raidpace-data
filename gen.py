# -*- coding: utf-8 -*-
"""Genera logdata.json per RaidPace.

Due modalita' per zona:
- LIGHT: mappa di CHI ha un log pubblico (fightRankings, ~15 query/boss).
  Voce = 1 (solo indicatore).
- FULL (env FULL="46 50"): in piu', per ogni gilda con log, PULL ALLA PRIMA KILL
  + URL del log della prima kill, per TUTTI i boss della zona in UNA passata
  (i report della gilda vengono letti una volta sola per l'intero raid).
  Voce = [pull, url]. INCREMENTALE: le gilde gia' calcolate non si rifanno
  (il first-kill non cambia mai), quindi le notti successive costano poco.

Autonomo. Credenziali WCL da env WCL_CLIENT_ID / WCL_CLIENT_SECRET.
Uso:  python gen.py 46 44 ...   (ID zona WCL; default env ZONES)
Env:  DIFFS="5" | FULL="46 50" | WORKERS=8 | OUT=logdata.json
"""
import sys
import os
import json
import time
import base64
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
GQL_URL = "https://www.warcraftlogs.com/api/v2/client"
DIFFS = [int(x) for x in os.environ.get("DIFFS", "5").split()]
ZONES = [int(x) for x in os.environ.get("ZONES", "31 33 35 38 42 44 46 50 53 54 57").split()]
FULL = set(int(x) for x in os.environ.get("FULL", "46 50").split() if x.strip())
WORKERS = int(os.environ.get("WORKERS", "8"))
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "0"))   # 0 = illimitato
# Tetto di first-kill calcolati per UNA zona in UN run: chiudendo la fetta prima
# del budget, la zona viene marcata "fatta di recente" e saltata per SKIP_HOURS,
# cosi' il run PROSEGUE a ritroso sulle zone piu' vecchie (tier attuale -> passati)
# invece di restare bloccato all'infinito sul tier in progress (che non finisce mai).
PER_ZONE_CAP = int(os.environ.get("PER_ZONE_CAP", "150"))
_START = time.time()

FR_QUERY = ("query($e:Int!,$d:Int!,$p:Int!){worldData{encounter(id:$e){"
            "fightRankings(difficulty:$d,page:$p)}}}")
ENC_QUERY = "query($z:Int!){worldData{zone(id:$z){name encounters{id name}}}}"
# limit 100 (4x meno richieste) con difficolta' come ARGOMENTO di fights: filtra
# server-side e resta sotto il tetto di complessita' (50000).
REP_ALL_QUERY = ("query($g:Int!,$z:Int!,$p:Int!,$d:Int!){reportData{reports("
                 "guildID:$g,zoneID:$z,page:$p,limit:100){data{code startTime "
                 "fights(killType:Encounters,difficulty:$d){id encounterID kill startTime}}"
                 "has_more_pages}}}")
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


_BUDGET_OUT = False   # budget orario WCL esaurito -> il run esce pulito (niente grind)


def gql(query, variables, tries=5):
    global _BUDGET_OUT
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL_URL, data=body, headers={
        "Authorization": "Bearer " + _TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode()).get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code in (502, 503) and tries > 1:
            time.sleep(3.0 * (6 - tries))
            return gql(query, variables, tries - 1)
        if e.code == 429 and tries > 1:
            time.sleep(20.0)
            return gql(query, variables, tries - 1)
        if e.code == 429:
            # 429 anche dopo i retry = budget orario finito: segnalo lo stop.
            # Meglio uscire e committare, che sprecare l'ora restante sui 429;
            # le corse notturne (ogni 2h) ripartono col budget fresco.
            _BUDGET_OUT = True
        raise


def zone_encounters(zid):
    z = (gql(ENC_QUERY, {"z": zid}).get("worldData") or {}).get("zone") or {}
    return z.get("name") or str(zid), (z.get("encounters") or [])


def fr_guilds(eid, diff):
    """{key -> guildID} delle gilde con log pubblico sul boss."""
    out = {}
    for page in range(1, 60):
        try:
            fr = ((gql(FR_QUERY, {"e": eid, "d": diff, "p": page})
                   .get("worldData") or {}).get("encounter") or {}).get("fightRankings") or {}
        except Exception as ex:
            sys.stderr.write(f"  fightRankings enc {eid} d{diff} p{page}: {ex}\n")
            break
        for r in (fr.get("rankings") or []):
            g = r.get("guild") or {}
            s = r.get("server") or {}
            nm, gid = g.get("name"), g.get("id")
            if nm and gid:
                out.setdefault(_lognorm(nm) + "|" + _lognorm(s.get("name")) + "|"
                               + (s.get("region") or "").lower(), gid)
        if not fr.get("hasMorePages"):
            break
    return out


def guild_firstkills(gid, zid):
    """Report della gilda per la zona: {(enc,diff) -> [pull, url, ts_prima_kill]}.
    ts = istante (ms) della prima kill, per capire lato app se i log coprono il
    PROGRESS (finestra dal 1o all'ultimo boss) o sono farm.
    Deduplica la stessa fight presente in piu' log (stesso boss, stesso istante):
    senza, una farm-clear loggata da 2 persone gonfierebbe pull e sembrerebbe
    progresso su piu' occasioni."""
    DEDUP_MS = 15_000
    KILL_DEDUP_MS = 12 * 3600 * 1000
    out, counters = {}, {}
    for diff in DIFFS:
        fights = []   # (ts, enc, kill, code, fid)
        for page in range(1, 13):   # limit 100 -> bastano meno pagine
            try:
                blk = ((gql(REP_ALL_QUERY, {"g": gid, "z": zid, "p": page, "d": diff})
                        .get("reportData") or {}).get("reports") or {})
            except Exception:
                break
            for r in (blk.get("data") or []):
                rs = r.get("startTime") or 0
                code = r.get("code")
                for f in (r.get("fights") or []):
                    fights.append((rs + (f.get("startTime") or 0),
                                   f.get("encounterID"), bool(f.get("kill")),
                                   code, f.get("id")))
            if not blk.get("has_more_pages"):
                break
        fights.sort(key=lambda x: x[0])
        seen = {}   # enc -> [(ts, kill), ...] gia' tenute (per la dedup)
        for ts, enc, kill, code, fid in fights:
            lst = seen.setdefault(enc, [])
            if any(abs(ts - x_ts) <= DEDUP_MS or
                   (kill and x_k and abs(ts - x_ts) <= KILL_DEDUP_MS)
                   for x_ts, x_k in lst):
                continue   # stessa fight di un altro log
            lst.append((ts, kill))
            k = (enc, diff)
            if k in out:
                continue
            counters[k] = counters.get(k, 0) + 1
            if kill:
                out[k] = [counters[k],
                          f"https://www.warcraftlogs.com/reports/{code}?fight={fid or 'last'}",
                          int(ts)]
    return out


def main():
    global _TOKEN
    zones = [int(a) for a in sys.argv[1:]] or ZONES
    _TOKEN = get_token()
    try:
        gen_worlddata()
    except Exception as ex:
        sys.stderr.write("worlddata: %s\n" % ex)
    outfile = os.environ.get("OUT", "logdata.json")
    try:
        out = json.load(open(outfile, encoding="utf-8"))
        out.setdefault("encounters", {})
    except Exception:
        out = {"encounters": {}}

    def save():
        out["generatedAt"] = int(time.time())
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    def expired():
        # stop se tempo scaduto OPPURE budget WCL esaurito (niente grind sui 429)
        return _BUDGET_OUT or (MAX_MINUTES and (time.time() - _START) > MAX_MINUTES * 60)

    # zone gia' COMPLETE (first-kill calcolati per tutti): si saltano per ~20h,
    # poi si ricontrollano (per le kill nuove). Cosi' ogni run spende budget solo
    # dove manca. In ordine di priorita' (tier attuale prima -> vedi ZONES).
    done_map = out.setdefault("fullComplete", {})
    SKIP_SEC = int(os.environ.get("SKIP_HOURS", "20")) * 3600
    from concurrent.futures import as_completed

    for zid in zones:
        if expired():
            sys.stderr.write("stop (tempo/budget): esco pulito.\n")
            break
        if (time.time() - done_map.get(str(zid), 0)) < SKIP_SEC:
            sys.stderr.write(f"zona {zid}: gia' completa di recente, salto.\n")
            continue
        try:
            zname, encs = zone_encounters(zid)
        except Exception as ex:
            sys.stderr.write(f"zona {zid}: {ex}\n")
            continue
        if not encs:
            continue
        sys.stderr.write(f"zona {zid} '{zname}': {len(encs)} boss"
                         f"{' [FULL]' if zid in FULL else ''}\n")
        # --- LIGHT: chi ha log pubblici + raccolta guildID ---
        guild_gids, guild_bosses = {}, {}
        for e in encs:
            if expired():
                break
            eid = e.get("id")
            for diff in DIFFS:
                t = time.time()
                m = fr_guilds(eid, diff)
                ek = f"{eid}:{diff}"
                enc_map = out["encounters"].setdefault(ek, {})
                for key, gid in m.items():
                    if not isinstance(enc_map.get(key), list):
                        enc_map[key] = 1        # indicatore (non degradare i [pull,url])
                    guild_gids.setdefault(key, gid)
                    guild_bosses.setdefault(key, set()).add(ek)
                save()
                sys.stderr.write(f"  {e.get('name')} (enc {eid} d{diff}): "
                                 f"{len(m)} con log in {time.time()-t:.0f}s\n")
        if zid not in FULL or expired():
            continue
        # --- FULL: first-kill [pull,url] per le gilde di QUESTA zona non ancora fatte ---
        # una gilda e' "da fare" se le manca la prima kill di un boss OPPURE la voce
        # e' nel vecchio formato [pull,url] senza data (len<3): la ricalcoliamo per
        # aggiungere la data (serve al rilevamento "progress pubblico" lato app).
        def _needs(ek, key):
            v = out["encounters"].get(ek, {}).get(key)
            return (not isinstance(v, list)) or (len(v) < 3)
        todo = [(key, gid) for key, gid in guild_gids.items()
                if any(_needs(ek, key) for ek in guild_bosses.get(key, ()))]
        todo_full = len(todo)
        # fetta di questo run: piu' del tetto lo lasciamo ai run successivi, cosi'
        # il budget scorre anche sulle zone piu' vecchie (marcia a ritroso).
        todo = todo[:PER_ZONE_CAP]
        capped = todo_full > len(todo)
        sys.stderr.write(f"FULL zona {zid}: {todo_full} da calcolare"
                         f"{f' (fetta di {len(todo)})' if capped else ''}, "
                         f"{len(guild_gids)-todo_full} gia' fatte\n")
        if not todo:
            done_map[str(zid)] = int(time.time())   # zona completa
            save()
            continue

        def work(item):
            key, gid = item
            if expired():
                return item[0], {}
            try:
                fk = guild_firstkills(gid, zid)
            except Exception:
                fk = {}
            return key, fk

        t0, done = time.time(), 0
        ex = ThreadPoolExecutor(max_workers=WORKERS)
        futs = [ex.submit(work, it) for it in todo]
        interrupted = False
        try:
            for fut in as_completed(futs):
                key, fk = fut.result()
                for (enc, diff), val in fk.items():
                    ek = f"{enc}:{diff}"
                    if ek in out["encounters"] and key in out["encounters"][ek]:
                        out["encounters"][ek][key] = val
                done += 1
                if done % 25 == 0:
                    save()
                    rate = done / max(1, time.time() - t0)
                    sys.stderr.write(f"  FULL: {done}/{len(todo)} ({rate:.2f}/s, "
                                     f"eta {int((len(todo)-done)/max(rate,0.01)/60)}m)\n")
                if expired():
                    interrupted = True
                    sys.stderr.write("  stop: fermo la FULL, salvo.\n")
                    break
        finally:
            for f in futs:
                f.cancel()
            ex.shutdown(wait=True, cancel_futures=True)
        # fetta chiusa senza esaurire il budget -> marca "fatta di recente":
        # verra' saltata per SKIP_HOURS e il run prosegue sulle zone piu' vecchie.
        if not interrupted:
            done_map[str(zid)] = int(time.time())
        save()
        _tag = " [INTERROTTA dal budget]" if interrupted else (
            " [FETTA, zona non ancora completa]" if capped else " [COMPLETA]")
        sys.stderr.write(f"FULL zona {zid}: {done}/{len(todo)} "
                         f"in {int((time.time()-t0)/60)}m{_tag}\n")


if __name__ == "__main__":
    main()


# ---- worlddata.json: espansioni+raid per i menu dell'app (statico, zero WCL a runtime)
WD_QUERY = ("query{worldData{expansions{id name zones{id name "
            "difficulties{id name} encounters{id name}}}}}")
RAID_DIFF_NAMES = {"lfr", "looking for raid", "normal", "heroic", "mythic",
                   "10 player", "25 player", "10 player (heroic)",
                   "25 player (heroic)", "40 player"}
NON_RAID_KEYWORDS = ("mythic+", "dungeon", "delve", "arena", "torghast",
                     "horrific vision", "island expedition", "scenario",
                     "proving grounds", "brawl")
BLOCKED_NAME_TOKENS = {"beta", "ptr", "alpha"}
BLOCKED_NAME_PHRASES = ("complete raid", "test realm", "tournament realm")


def _name_blocked(name):
    low = (name or "").lower()
    if any(p in low for p in BLOCKED_NAME_PHRASES):
        return True
    tokens = "".join(c if c.isalnum() else " " for c in low).split()
    return any(t in BLOCKED_NAME_TOKENS for t in tokens)


def _is_raid_zone(z):
    name = (z.get("name") or "").lower()
    if _name_blocked(name) or any(k in name for k in NON_RAID_KEYWORDS):
        return False
    dn = {(d.get("name") or "").lower() for d in (z.get("difficulties") or [])}
    return len(dn & RAID_DIFF_NAMES) >= 2


def gen_worlddata():
    """Scrive worlddata.json: stessa struttura di fetch_worlddata dell'app."""
    data = gql(WD_QUERY, {})
    out = []
    for exp in ((data.get("worldData") or {}).get("expansions") or []):
        if _name_blocked(exp.get("name")):
            continue
        zones = [{"id": z.get("id"), "name": z.get("name"),
                  "difficulties": [{"id": d.get("id"), "name": d.get("name")}
                                   for d in (z.get("difficulties") or [])
                                   if "lfr" not in (d.get("name") or "").lower()
                                   and "raid finder" not in (d.get("name") or "").lower()],
                  "encounters": [{"id": e.get("id"), "name": e.get("name")}
                                 for e in (z.get("encounters") or [])]}
                 for z in (exp.get("zones") or []) if _is_raid_zone(z)]
        if zones:
            out.append({"id": exp.get("id"), "name": exp.get("name"), "zones": zones})
    out.sort(key=lambda e: -(e.get("id") or 0))
    with open("worlddata.json", "w", encoding="utf-8") as f:
        json.dump({"generatedAt": int(time.time()), "expansions": out}, f,
                  ensure_ascii=False, separators=(",", ":"))
    sys.stderr.write("worlddata.json: %d espansioni\n" % len(out))
