# -*- coding: utf-8 -*-
"""Unisce (monotòno) il logdata GENERATO dentro logdata.json corrente.

Uso: python merge_ld.py <generato.json>   (logdata.json = base, viene riscritto)

Regola: per ogni chiave gilda si tiene il MEGLIO — una lista [pull,url] (first-kill)
batte l'indicatore 1; fra due liste vince quella generata (piu' recente). Cosi' la
copertura non puo' MAI diminuire, anche se i run si accavallano o partono da dati
piu' vecchi.
"""
import json
import sys
import time


def load(path):
    try:
        return json.load(open(path, encoding="utf-8")).get("encounters", {})
    except Exception as e:
        sys.stderr.write("merge: impossibile leggere %s (%s)\n" % (path, e))
        return {}


def main():
    gen = load(sys.argv[1]) if len(sys.argv) > 1 else {}
    cur = load("logdata.json")
    out = {}
    for ek in set(gen) | set(cur):
        m = dict(cur.get(ek, {}))
        for k, v in gen.get(ek, {}).items():
            if isinstance(v, list) or not isinstance(m.get(k), list):
                m[k] = v
        out[ek] = m
    json.dump({"generatedAt": int(time.time()), "encounters": out},
              open("logdata.json", "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    full = sum(1 for m in out.values() for v in m.values() if isinstance(v, list))
    sys.stderr.write("merge: %d encounter, %d first-kill\n" % (len(out), full))


if __name__ == "__main__":
    main()
