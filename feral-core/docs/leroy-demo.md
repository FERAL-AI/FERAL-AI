# Theora glasses + FERAL brain: the demo

Every capability in the shot list below was executed on this machine on
2026-08-06 and the real output is pasted with it. Nothing here is
described from reading the source. The last section lists what does not
work, which is the more useful half of this document.

---

## 1. The thesis

Glasses without memory are a camera. An assistant without your senses is
a chatbot. The category only exists when you put them together: a sensor
that rides on your face all day, and a memory that lives on your own
machine and never leaves it.

That combination buys one thing nothing else can offer. Because the
memory is local, it can hold what you would never upload. Your resting
heart rate at 3am, who you were with, what you were doing when it spiked,
what you said you would do and did not. A cloud assistant cannot ask for
that data because you would not give it. A wearable cannot use it because
it only sees numbers, not the day those numbers happened in. FERAL sees
both, and it keeps them in one place that belongs to you.

The second thing that falls out of local is that continuous capture stops
being expensive. The embedder on this machine is free and offline, so
remembering everything costs nothing per write. That is what makes
always-on plausible rather than a billing accident.

---

## 2. Why these two, now

A fitness tracker answers "what is my heart rate". The interesting
question is "what was I doing the last time it was this high". The first
is a number. The second needs the sensor and the memory to be the same
system, and on this machine the two stores already line up well enough to
answer it (Shot 2b proves it with real data). Be precise on camera: that
answer comes from a query across the two stores, not from a shipped
feature that does the correlation for you. Nothing in the codebase joins
biometrics to episodes today.

Three properties make this real rather than a mock:

- **It remembers by meaning, not keywords.** You do not have to recall
  the words you used.
- **It forgets.** 3,666 of 12,296 memories on this machine have decayed
  below the recall threshold. A database that keeps everything at equal
  weight is not a memory, it is a log.
- **It is yours, provably.** The brain holds an Ed25519 keypair, and the
  memory is a file on your disk.

---

## 3. Story arc

Five movements. Each one should make the previous one feel small.

**I. The ordinary question** (0:00 to 0:30)
Open on the glasses being worn, nothing dramatic. Ask the brain a plain
question about your body. It answers from data the glasses fed it.
*Beat: quiet competence. This is a wearable. You have seen this before.*

**II. The question a wearable cannot answer** (0:30 to 1:30)
Now ask it something in the wrong words. Different spelling, different
verb, a half memory. It finds the moment anyway. Then show a keyword
search for the same thing returning nothing at all, on the same database.
*Beat: the first turn. It is not matching text, it is matching meaning.*

**III. Where the memory lives** (1:30 to 2:30)
Cut to the terminal. The memory is a file. The embedder runs offline with
no API key. Pull the network and it still answers.
*Beat: relief, then ownership. Nobody is renting you your own past.*

**IV. It forgets** (2:30 to 3:15)
Show the decay numbers. Most memories fade. The ones you return to stay
sharp. Show a memory with 42 accesses sitting high while thousands sank.
*Beat: the uncanny one. This behaves like memory, not storage.*

**V. The chain** (3:15 to 4:00)
Reveal the topology: glasses over Bluetooth to the phone, phone to the
brain on your desk. Show the cryptographic identity. End on the QR code
and the idea that this is one person's brain, on one person's machine.
*Beat: this is a system, and it is already running.*

---

## 4. Shot list

Run everything from `/Users/mahmoudomar/Desktop/thoera-mac/ASOS/feral-core`.

### Shot 1: the body question
**On screen:** glasses worn, then the trend output.
**Narration:** "The glasses have been reading my heart all day."

```bash
python3 -c "
from agents.baseline_engine import BaselineEngine
import json
e = BaselineEngine(db_path='/Users/mahmoudomar/.feral/baselines.db')
t = e.get_trend('hr', days=7)
print(json.dumps({k:v for k,v in t.items() if k!='daily'}, indent=2))
for d in t['daily']: print(d)
"
```

**Verified output:**
```
{'metric': 'hr', 'days': 7, 'sample_count': 283, 'sources': ['jw_health_glasses'],
 'min': 83.0, 'max': 119.0, 'avg': 107.33}
{'date': '2026-08-03', 'count': 217, 'min': 93.0, 'max': 119.0, 'avg': 109.6}
{'date': '2026-08-05', 'count': 17,  'min': 103.0, 'max': 110.0, 'avg': 105.94}
{'date': '2026-08-06', 'count': 49,  'min': 83.0,  'max': 117.0, 'avg': 97.78}
```

The source string `jw_health_glasses` is worth a beat on screen. That is
the glasses naming themselves in the record. 1,244 heart samples total.

---

### Shot 2: the money shot, recall by meaning
**On screen:** split screen, keyword search left, FERAL right.
**Narration:** "I never typed the word colour. I typed color, months ago,
about a robot. Watch what normal search does with it."

```bash
python3 - <<'PY'
import asyncio, sqlite3
from memory.store import MemoryStore
from memory.fts_query import fts5_match_query
db = sqlite3.connect("/Users/mahmoudomar/.feral/memory.db"); db.row_factory = sqlite3.Row
expr = fts5_match_query("colour")
rows = db.execute("SELECT e.summary FROM episodes_fts f JOIN episodes e ON f.rowid=e.rowid "
                  "WHERE episodes_fts MATCH ? ORDER BY rank LIMIT 3", (expr,)).fetchall()
print("KEYWORD SEARCH 'colour' ->", len(rows), "results")
async def main():
    s = MemoryStore()
    for h in await s.episode_search_hybrid("changing the light colour", limit=3):
        print(f"SEMANTIC [{h['relevance_score']:.3f}] {h['summary']!r}")
asyncio.run(main())
PY
```

**Verified output:**
```
KEYWORD SEARCH 'colour' -> 0 results
SEMANTIC [1.005] 'now change the lights color to red and then turn it off'
SEMANTIC [0.994] 'Set the CuteBot lights to green'
SEMANTIC [0.992] 'Set the CuteBot lights to green (r:0, g:255, b:0)'
```

Same database, same instant. Keyword finds nothing, meaning finds the
exact moment. This is the strongest 15 seconds available to us.

**Second take, same idea, different shape:**
```
Q: 'make the robot move'
   [1.002] 'make the robot explore the table'
   [0.973] 'Drive the CuteBot with left=50 and right=-50 to spin in place'
```
"Move" retrieves "explore" and "drive". No shared word.

**Timing note:** each query takes about 1 second (measured median 1004 to
1188 ms). Do not cut the shot tight, and do not promise instant.

---

### Shot 2b: the question only this system can answer
**On screen:** the correlation output, one line at a time.
**Narration:** "My heart hit 119. A tracker would stop there. This knows
what I was doing."

```bash
python3 - <<'PY'
import sqlite3, datetime
b = sqlite3.connect("/Users/mahmoudomar/.feral/baselines.db"); b.row_factory=sqlite3.Row
m = sqlite3.connect("/Users/mahmoudomar/.feral/memory.db"); m.row_factory=sqlite3.Row
for r in b.execute("SELECT ts, value FROM biometric_samples WHERE metric='hr' "
                   "ORDER BY value DESC LIMIT 2"):
    eps = m.execute("SELECT summary FROM episodes WHERE created_at BETWEEN ? AND ? LIMIT 3",
                    (r["ts"]-900, r["ts"]+900)).fetchall()
    when = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M")
    print(f"\nhr={r['value']:.0f} at {when}, what was happening:")
    for e in eps: print("   ", e["summary"][:64])
PY
```

**Verified output:**
```
hr=126 at 2026-06-28 23:52, what was happening:
    Screen: User is working in a macOS Terminal window, managing a s
    Screen: User is viewing a personal AI dashboard called FERAL in
    Screen: User is viewing a web-based dashboard application called

hr=119 at 2026-08-03 15:16, what was happening:
    Start CuteBot line-following now. Call cutebot__follow_line and
    Drive the CuteBot with left=50 and right=-50 to spin in place. T
    Spin the cutebot for a few seconds with red lights then halt
```

Highest heart rate of the record, at 11:52pm, working in a terminal. That
is the thesis in one frame, and it is real data.

**Honesty constraint:** this is a SQL join across the two stores that I
wrote for this demo. No shipped feature does this correlation. Film it as
a terminal shot demonstrating that the data supports it. Do not stage it
as asking the assistant a question, because the assistant cannot answer
it yet.

---

### Shot 3: it runs on your machine, free
**On screen:** terminal, then the memory file in Finder.
**Narration:** "No API key. No account. The model runs here."

```bash
python3 - <<'PY'
import asyncio, os, time
from memory.embeddings import EmbeddingProvider
for k in list(os.environ):
    if "API_KEY" in k or "OPENAI" in k or "ANTHROPIC" in k: os.environ.pop(k)
e = EmbeddingProvider()
print("provider:", e.active_provider, "| dims:", e.dimension, "| degraded:", e.degraded)
t=time.perf_counter(); v=asyncio.run(e.embed("the coffee machine needs descaling"))
print(f"embedded {len(v)} dims in {(time.perf_counter()-t)*1000:.0f} ms with zero API keys")
PY
ls -lh ~/.feral/memory.db
```

**Verified output:**
```
provider: fastembed | dims: 384 | degraded: False
embedded 384 dims in 563 ms with zero API keys
-rw-r--r--  110M  /Users/mahmoudomar/.feral/memory.db
```

Every API key was deleted from the environment before that ran, and it
still embedded. That is the proof, not a claim.

---

### Shot 4: it forgets
**On screen:** the decay numbers, large type.
**Narration:** "Most of what happened to me is already fading. The things
I keep returning to are the things that stayed."

```bash
sqlite3 ~/.feral/memory.db "
SELECT 'forgotten', count(*) FROM episodes WHERE forgotten_at IS NOT NULL
UNION ALL SELECT 'active', count(*) FROM episodes WHERE forgotten_at IS NULL;"

sqlite3 ~/.feral/memory.db "
SELECT access_count, round(decay_factor,3), substr(summary,1,50)
FROM episodes WHERE forgotten_at IS NULL ORDER BY access_count DESC LIMIT 3;"
```

**Verified output:**
```
forgotten|3666
active|8630
42|0.069|show me a python fizzbuzz with syntax highlighting and exp
39|0.072|can you build a pac-man game that is functional on my desk
38|0.072|hey
```

Decay is live: cadence 3600s, rate 0.001, forget threshold 0.05,
retention 365 days. Forgotten episodes sit between 0.0402 and 0.0501,
right under the line. This is not a metaphor we drew, it is the running
configuration.

---

### Shot 5: the chain, glasses to phone to brain
**On screen:** the topology table, then the physical objects in order.
**Narration:** "The glasses talk Bluetooth to the phone. The phone talks
to the brain. The brain is on my desk."

```bash
sqlite3 -header -column ~/.feral/memory.db "
SELECT node_id, capability, status, attrs, provenance,
       datetime(last_seen,'unixepoch','localtime') last_seen
FROM node_subdevices ORDER BY last_seen DESC LIMIT 4;"
```

**Verified output:**
```
feral-iphone-6053b3cdc4ed  jw_health_glasses  disconnected  {"device_name": "theora-glasses"}  ble  2026-08-06 15:52:06
feral-iphone-2a210fa1      jw_health_glasses  ready         {"device_name": "theora-glasses"}  ble  2026-07-07 14:25:02
feral-iphone-79447a4cd1ed  jw_health_glasses  ready         {"battery_pct": 86}      ble  2026-07-05 20:41:19
feral-iphone-c29f7fd3      jw_health_glasses  audio_ready   {"headset_paired":"true"} ble 2026-06-30 20:13:16
```

**SETUP REQUIRED:** the glasses read `disconnected` as of 15:52 today.
Reconnect and confirm status is `ready` before filming this shot.

---

### Shot 6: it is provably yours
**On screen:** the key, the signature, the rejection.
**Narration:** "This brain has an identity nobody else can forge."

```bash
python3 - <<'PY'
import security.brain_identity as bi
from cryptography.hazmat.primitives.asymmetric import ed25519
print("relay_id:", bi.relay_id())
sig = bi.private_key().sign(b"theora-demo")
pub = ed25519.Ed25519PublicKey.from_public_bytes(bi.public_key_bytes())
pub.verify(sig, b"theora-demo"); print("signature verifies")
try: pub.verify(sig, b"theora-demoX"); print("BAD")
except Exception as e: print("tampered message rejected:", type(e).__name__)
PY
```

**Verified output:**
```
relay_id: gljurzt6zhhydsvpas7y5eaiax36pk6q
signature verifies
tampered message rejected: InvalidSignature
```

---

### Shot 7: the health check as a closing card
**On screen:** `feral doctor`, scrolling.
**Narration:** "All of it, on one machine."

```bash
python3 -m cli.main doctor 2>/dev/null | grep -E \
  "Embedding provider|Memory database|LLM endpoint|Access mode|Phone pairing|cost.embedding|cost.chat"
```

**Verified output (this exact command):**
```
✔  Memory database  /Users/mahmoudomar/.feral/memory.db  (112472 KB)
✔  Embedding provider  fastembed (384d, local and free)
✔  LLM endpoint  openrouter -> provider default
✔  Access mode coherence  local (binds 0.0.0.0)
✔  Phone pairing  a QR would point at http://192.168.68.112:9090
✔  cost.chat  $0.0347 / $0.00 per hour (resets in 45m)
✔  cost.embedding  $0.0000 / $0.00 per hour (resets in 45m)
```

**Freeze on the last two lines.** That is the whole economic argument in
one frame, out of the brain's own cost ledger: talking to a cloud model
costs money, and remembering costs exactly zero. That is why always-on
capture is plausible here and nowhere else.

**Warning:** the raw uncropped output interleaves noise ("Dropping
bedrock from the LLM failover chain", a multi-line sqlite-vec warning).
Use the crop above. A bare `grep '✔'` is not enough, it tails into the
cost table.

---

### Shot 8 (optional closer): pair a phone on camera
The QR endpoint serves a real PNG, verified:
```bash
curl -s http://127.0.0.1:9090/api/devices/pair/qr --output /tmp/pair.png && file /tmp/pair.png
```
59 devices have paired successfully, most recently today at 16:08.

---

## 5. What we are NOT showing, and why

This list exists so nothing breaks on stage.

**Sleep and HRV.** There is no HRV metric and no sleep metric on this
machine, at all. The briefing's `sleep` field returns `null` and will
keep returning `null`. Do not write narration around sleep quality or
recovery. The glasses record `hr`, `spo2` and `steps`, nothing else.

**Live SpO2.** 149 samples exist but the most recent is 2026-07-07, a
month old. Showing it live implies a freshness we do not have.

**Step counts.** The `steps_daily` baseline mean is 19.6 while the
underlying samples average 951. Those two numbers disagree and I did not
resolve why. Keep steps off camera until that is understood.

**The morning briefing as a whole.** It is fixed and no longer crashes
(`degraded: []`), but on real data it returns an empty shell: sleep null,
weather null, agenda empty, goals empty. It only populates if a goal
exists first, because `intent_plans` is empty. Verified: with one plan
inserted, agenda and goals both populate correctly. If you want this
shot, create a real goal first and accept that sleep and weather stay
null.

**Weather.** No `OPENWEATHER_API_KEY` is set, so the field is always
null.

**VIP email.** Not implemented anywhere in the codebase. The field
reports `vip_emails:not_implemented` by design now, rather than pretending
the inbox was quiet.

**Asking about something the brain does not know.** This is the real
stage risk, and the one most likely to embarrass us. The similarity
cutoff is 0.25, and on all four queries I measured, 11,606 of the 11,610
episode chunks cleared it. The cutoff is doing nothing, so the brain
returns its three closest memories no matter how far away they are. There
is no "I do not have anything about that" state.

Verified failures, both from real queries:

| Asked | Returned |
|---|---|
| "the pairing bug on the phone" | "Hi" (0.762) |
| "how does the relay work" | "Just control it and move it" (0.636) |
| "my oxygen levels" | heart rate episodes, no spo2 |
| "how many steps did I take" | heart rate, then an unrelated HTML task |

Note why the first one fails: the embedded text includes the raw JSON
metadata blob, so the word "phone" matched `"source": "phone_surface"` in
the detail field rather than anything about phones. Content and metadata
are being embedded together.

**Only ask the questions verified in Shot 2.** Do not let anyone ask a
freestyle question on camera, and do not hand the demo to Leroy to drive
unscripted.

**Voice.** Voice session records exist in the database
(`voice-feral-iphone-...`), so it has run, but I did not verify the live
voice path in this pass. Do not film it without testing it first.

**Search speed.** About one second per query, because sqlite-vec cannot
load on this Python build and the vector scan falls back to numpy brute
force over 11,610 vectors. Correct, but linear. Note that `feral doctor`
currently reports the backend as `sqlite_vec` while the runtime is
actually using the numpy fallback, so do not quote the doctor line on
vector backend.

**Anything through the running brain on port 9090.** It is serving older
code until it is restarted. Every in-process result above was produced by
calling the real classes and route functions directly. Restart the brain
before filming anything that goes over HTTP.
