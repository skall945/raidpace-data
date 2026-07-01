# raidpace-data

Dati **pubblici** per [RaidPace]: mappa dei log pubblici su Warcraft Logs per boss.
`logdata.json` è rigenerato ogni notte da una GitHub Action (`gen.py`), che
interroga WCL **una sola volta** — così l'app non deve interrogare WCL per ogni
utente (il token è condiviso). L'app scarica questo file con una GET.

Formato: `{ "generatedAt": <epoch>, "encounters": { "<encId>:<diff>": { "<nome|server|regione>": "<url log>" } } }`
