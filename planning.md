# Provenance Guard — Planning & Spec

## 1. Architecture Narrative

A creator on a writing platform submits a piece of text for attribution analysis.
Here is the full path that text takes, naming every component it touches:

1. **`POST /submit`** receives a JSON body `{ "text": ..., "creator_id": ... }`.
   The request first passes through the **rate limiter** (Flask-Limiter), which checks the caller's IP/identity against the configured limits. If the caller is over the
   limit, the request is rejected with `429` before any detection work happens (detection
   costs an LLM call, so we gate it early).

2. The text is handed to the **detection pipeline**, which runs **two independent signals**:
   - **Signal 1 — LLM classifier (Groq `llama-3.3-70b-versatile`):** sends the text to the
     model with a structured prompt asking "how likely is this AI-generated?" and parses a
     JSON response into `llm_score` (0–1 = P(AI)) plus a short rationale. This captures
     *semantic / holistic* properties of does the text *read* like AI?
   - **Signal 2 — Stylometric heuristics (pure Python):** measures *structural* statistics
     of the text (sentence-length variance, vocabulary diversity, punctuation density) and
     maps them to `stylo_score` (0–1 = P(AI)). This captures *measurable form*, independent
     of meaning.

3. Both signal scores flow into the **confidence scorer**, which combines them into a single
   `ai_probability` (weighted blend) and a separate `confidence` value that reflects how
   *certain* we are of the verdict which is lowered when the two signals disagree. The scorer
   applies **asymmetric thresholds** (a high bar to call something "AI") to protect against
   the worst error: falsely accusing a human.

4. The scorer's output goes to the **label generator**, which maps `ai_probability` +
   `confidence` to one of **three transparency-label variants** (high-confidence AI,
   high-confidence human, or uncertain).

5. Everything is written as one structured row to the **audit log** (SQLite): `content_id`,
   `creator_id`, timestamp, attribution, confidence, both individual signal scores, and
   status (`classified`).

6. `POST /submit` returns `{ content_id, attribution, confidence, ai_probability,
   signals: {llm_score, stylo_score}, label }`. The **`content_id`** is the handle the
   creator uses later to appeal.

**Appeal flow:** A creator who disputes a verdict calls **`POST /appeal`** with their
`content_id` and `creator_reasoning`. The system looks up the original audit row, sets its
**status to `under_review`**, writes a new audit entry that stores the appeal reasoning
alongside the original decision, and returns a confirmation. No automated re-classification and the row simply enters a human reviewer's queue (visible via `GET /log`).

---

## 2. Detection Signals

### Signal 1 — LLM classifier (Groq `llama-3.3-70b-versatile`)
- **What it measures:** Holistic "does this read like AI?" judgment — tone, hedging, predictability of phrasing, the generic-but-fluent quality of LLM prose.
- **Why it differs human↔AI:** LLMs trained on human text still produce recognizable patterns even hedging ("It is important to note…"), balanced both-sides framing, low risk-taking, smooth transitions. A model is good at recognizing its own output's "voice."
- **Output:** JSON → `{ "ai_score": 0.0–1.0, "rationale": "<one sentence>" }`. Called at `temperature=0` for determinism; if the call fails or returns junk, the signal degrades to `0.5` (maximally uncertain) and we lower overall confidence.
- **Blind spot:** Fooled by lightly edited AI text and by non-native English speakers / very formal human writers, whose careful prose can *read* AI-ish. Also non-deterministic in principle and dependent on an external API. It can be confidently wrong.

### Signal 2 — Stylometric heuristics
Three measurable metrics, each normalized to a 0–1 "AI-likeness" contribution, then averaged:
- **Sentence-length burstiness (variance):** humans vary sentence length a lot (short punchy + long winding) but AI tends toward uniform medium-length sentences. *Low* variance → AI-like.
- **Type-token ratio (vocabulary diversity):** ratio of unique words to total words. AI prose is often lexically "smooth" and repeats connective vocabulary humans (especially casual) show messier diversity. We map both extremes carefully (very low TTR on a long text → AI-like).
- **Punctuation & casing irregularity:** humans use lowercase starts, "...", "?!", em-dashes, ALL CAPS for emphasis; AI is grammatically tidy. *High* tidiness → AI-like.
- **Output:** `stylo_score` (0–1 = P(AI)) plus the raw sub-metrics for transparency/logging.
- **Why it differs human↔AI:** these are *form* properties independent of meaning. You can measure them without understanding the text. AI text is statistically *more uniform*.
- **Blind spot:** Short texts (< ~40 words) are statistically unstable — variance and TTR are noisy, so the signal is unreliable. Formal/academic human writing (uniform, tidy) scores AI-like, casual or intentionally messy AI output scores human-like. Poetry and lists break the sentence-based assumptions entirely.

**Why the pairing works:** Signal 1 is semantic and can be fooled by editing; Signal 2 is structural and can be fooled by genre. They fail in *different* directions, so when they *agree* we're more confident, and when they *disagree* we honestly report uncertainty.

---

## 3. Uncertainty Representation (Confidence Scoring)

**Design decision first, math second** (per the hint). I decided what the numbers should *mean* to a user, then built the formula:

- `ai_probability` ∈ [0,1] — our blended estimate of P(text is AI). This drives the verdict.
- `confidence` ∈ [0,1] — how *sure* we are of that verdict. **This is what gates the label.**
  - `confidence ≈ 0.5` is the honest "we genuinely don't know" zone → **Uncertain** label.
  - `confidence ≥ ~0.7` means strong agreement + a clear-cut score → a high-confidence label.

**Combining the signals:**
```
ai_probability = 0.60 * llm_score + 0.40 * stylo_score
```
The LLM gets the larger weight because it reasons over meaning and is empirically the stronger single detector; stylometry is a cheaper, noisier structural check. (Weights are documented and easy to tune.)

**Confidence from certainty + agreement:**
```
certainty     = abs(ai_probability - 0.5) * 2          # 0 at the fence, 1 at extremes
disagreement  = abs(llm_score - stylo_score)           # 0..1, how far the signals diverge
confidence    = certainty * (1 - 0.5 * disagreement)   # divergence drains confidence
```
So a text can score `ai_probability = 0.8` but, if the two signals sharply disagree, end up with only moderate confidence and pushing it toward the **Uncertain** label rather than a confident accusation. Confidence honestly *cannot* be high when our two lenses contradict.

**Thresholds (asymmetric — false positives are the worst error):**
A false positive (calling a *human's* work AI) is more damaging on a creative platform than a false negative, so the bar to say "AI" is deliberately **higher** than the bar to say "human":

| Condition | Attribution | Label variant |
|---|---|---|
| `ai_probability ≥ 0.65` **and** `confidence ≥ 0.55` | `likely_ai` | High-confidence AI |
| `ai_probability ≤ 0.40` **and** `confidence ≥ 0.50` | `likely_human` | High-confidence human |
| everything else | `uncertain` | Uncertain |

Note the AI gate needs *both* a high probability **and** high confidence; the human gate is
easier to clear (lower probability bar, lower confidence bar). When in doubt, the system
defaults to **Uncertain**, never to an AI accusation.

**What 0.6 confidence means:** "We lean toward a verdict but a reasonable reviewer could
disagree." It lands in the Uncertain band on purpose and it is *not* enough to label someone.

**How I'll test it's meaningful** (Milestone 4): run the 4 provided calibration inputs
(clear-AI, clear-human, formal-human borderline, edited-AI borderline) plus my own, print
`llm_score`, `stylo_score`, `ai_probability`, `confidence`, and verify (a) clear cases land in
the right bands with high confidence, (b) borderline cases land in Uncertain or near it with
lower confidence, (c) the scores *spread out* rather than clustering at one value.

---

## 4. Transparency Label Design

### Variant A — High-confidence AI  (`attribution == likely_ai`)
> **Likely AI-generated**
> Our analysis suggests this text was probably produced with significant AI assistance
> (confidence: {confidence_pct}%). This is an automated estimate, not a certainty —the creator can appeal if this is wrong.

### Variant B — High-confidence human  (`attribution == likely_human`)
> **Likely human-written**
> Our analysis found no strong signs of AI generation in this text
> (confidence: {confidence_pct}%). This is an automated estimate, not a guarantee.

### Variant C — Uncertain  (`attribution == uncertain`)
> **Attribution uncertain**
> Our signals disagree or are inconclusive for this text, so we are not assigning an
> attribution (confidence: {confidence_pct}%). When we can't tell, we don't label — the
> benefit of the doubt goes to the creator.

`{confidence_pct}` = `round(confidence * 100)`. The AI variant is intentionally hedged
("likely", "estimate", "can appeal") to avoid the harm of a confident false accusation.

---

## 5. Appeals Workflow

- **Who can appeal:** any creator who has a `content_id` from a prior `/submit` response.
  (In a real system this would be auth-gated to the content's owner; here `content_id` is the
  handle.)
- **What they provide:** `content_id` (which submission) + `creator_reasoning` (free-text
  explanation, e.g. "I'm a non-native speaker; my formal style reads as AI").
- **What the system does on receipt:**
  1. Look up the original audit row by `content_id`. If not found → `404`.
  2. Update that content's **status → `under_review`**.
  3. Write a **new audit entry** of type `appeal` that stores the `creator_reasoning`
     *alongside* a snapshot of the original decision (attribution, confidence, signals).
  4. Return `{ content_id, status: "under_review", message: "Appeal received..." }`.
  - **No automated re-classification** — the verdict is unchanged; the item just enters review.
- **What a human reviewer sees** (via `GET /log`): the original classification entry and the
  appeal entry for that `content_id`, including the creator's reasoning and all signal scores,
  so they have full context to make a manual call.

---

## 6. Anticipated Edge Cases

1. **The repetitive / simple-vocabulary poem.** A poem built on refrains and plain words has
   *low* sentence-length variance and *low* type-token ratio exactly the structural markers
   our stylometric signal reads as "AI." The LLM signal may pull the other way, so this should
   land in **Uncertain** (good) rather than a false AI accusation but it's a known weak spot.
2. **The non-native English speaker's formal essay.** Careful, grammatically tidy, evenly-paced
   prose from a human ESL writer trips *both* signals toward AI: stylometry sees uniformity, the
   LLM sees "generic fluent." This is our highest false-positive risk and the exact scenario the
   appeal flow exists to catch. The asymmetric threshold + hedged label are the mitigation.
3. **Very short text (< ~40 words).** Stylometric variance/TTR are statistically meaningless on
   a sentence or two, so Signal 2 is noise. We lower confidence for short inputs and lean on
   Uncertain.
4. **Lightly-edited AI output.** A human paragraph-editing AI text removes the LLM's easiest
   tells while keeping AI structure; signals may split → Uncertain by design.

---

## Architecture

```
                          ┌─────────────────────────────────────────────────────────┐
│ PROVENANCE GUARD (Flask)                                │
                          └─────────────────────────────────────────────────────────┘

SUBMISSION FLOW
===============

  Creator
    │  POST /submit  { text, creator_id }
    ▼
┌──────────────┐   over limit? → 429
│ Rate Limiter │───────────────────────────────►  (reject before any LLM cost)
│(Flask-Limiter│
│ 10/min,      │  raw text
│ 60/hr,200/day│──────────────┐
└──────────────┘              ▼
                      ┌─────────────────────────────────────────┐
                      │           DETECTION PIPELINE             │
                      │                                          │
        raw text ────►│  Signal 1: Groq LLM   ──► llm_score 0-1  │
        raw text ────►│  Signal 2: Stylometry ──► stylo_score 0-1│
                      └───────────────┬──────────────────────────┘
                                      │ llm_score, stylo_score
                                      ▼
                      ┌─────────────────────────────────────────┐
                      │           CONFIDENCE SCORER              │
                      │ ai_probability = .60*llm + .40*stylo     │
                      │ confidence = certainty*(1-.5*disagree)   │
                      │ asymmetric thresholds (high bar for AI)  │
                      └───────────────┬──────────────────────────┘
                                      │ attribution, ai_probability, confidence
                                      ▼
                      ┌─────────────────────────────────────────┐
                      │           LABEL GENERATOR                │
                      │ → High-conf AI / High-conf human /       │
                      │   Uncertain  (+ confidence %)            │
                      └───────────────┬──────────────────────────┘
                                      │ full record
                       ┌──────────────┴───────────────┐
                       ▼                               ▼
              ┌─────────────────┐            ┌──────────────────────┐
              │  AUDIT LOG       │            │  HTTP RESPONSE (JSON) │
              │  (SQLite)        │            │  content_id,          │
              │ content_id,      │            │  attribution,         │
              │ creator_id, ts,  │            │  confidence,          │
              │ attribution,     │            │  ai_probability,      │
              │ confidence,      │            │  signals{llm,stylo},  │
              │ llm,stylo,       │            │  label                │
              │ status=classified│            └──────────────────────┘
              └─────────────────┘

APPEAL FLOW
===========

  Creator
    │  POST /appeal  { content_id, creator_reasoning }
    ▼
┌────────────────────┐  lookup by content_id (404 if missing)
│  Audit Log (SQLite)│◄──────────────────────────────────────┐
└─────────┬──────────┘                                        │
          │ original decision found                           │
          ▼                                                    │
┌────────────────────┐   status → "under_review"              │
│  STATUS UPDATE      │   + new audit entry (type=appeal,      │
│                     │     creator_reasoning + orig decision) ┘
└─────────┬──────────┘
          │
          ▼
   HTTP RESPONSE { content_id, status:"under_review", message }

  GET /log  ──►  most recent N audit entries as JSON (reviewer queue + grading evidence)
```

**Narrative:** In the *submission flow*, text enters `POST /submit`, clears the rate limiter,
runs through both detection signals in the pipeline, gets blended into an `ai_probability` and
a disagreement-aware `confidence`, is turned into one of three reader-facing labels, written to
the SQLite audit log, and returned as JSON keyed by a `content_id`. In the *appeal flow*, the
creator sends that `content_id` plus their reasoning to `POST /appeal`; the system flips the
original record's status to `under_review`, logs the appeal next to the original decision, and
returns a confirmation all visible to a human reviewer through `GET /log`.

### API Surface

| Endpoint | Method | Accepts | Returns |
|---|---|---|---|
| `/submit` | POST | `{text, creator_id}` | `{content_id, attribution, confidence, ai_probability, signals{llm_score,stylo_score}, label}` |
| `/appeal` | POST | `{content_id, creator_reasoning}` | `{content_id, status:"under_review", message}` |
| `/log` | GET | `?limit=N` (optional) | `{entries:[...]}` most recent audit rows |
| `/health` | GET | — | `{status:"ok"}` (sanity check) |

---

## AI Tool Plan

How I'll use AI tools (Claude / Groq-assisted) across the three build milestones, and how I
verify each output instead of pasting blindly.

### M3 — submission endpoint + first signal
- **Spec I provide:** Detection Signals (Signal 1) + the Architecture diagram + §PI Surface.
- **Ask it to generate:** Flask app skeleton with the `POST /submit` route stub returning a
  hardcoded response, then the Groq LLM signal function returning `{ai_score, rationale}`, then
  the SQLite audit-log helpers + `GET /log`.
- **How I verify:** call the LLM signal function *directly* on 3 inputs and inspect the JSON
  before wiring it into the route; confirm the route shape matches §API Surface; `curl /submit`
  and confirm a `content_id` + a structured audit row are produced.

### M4 — second signal + confidence scoring
- **Spec I provide:** Signal 2 + 3 Uncertainty Representation + the diagram.
- **Ask it to generate:** the stylometric signal function (3 metrics → `stylo_score`) and the
  `score()` function combining both signals per the 3 formula and thresholds.
- **How I verify:** confirm the generated scoring uses *my exact weights (0.60/0.40), my
  confidence formula, and my asymmetric thresholds* — AI often invents plausible-but-different
  numbers. Run the 4 calibration inputs and check scores spread and land in the right bands.

### M5 — production layer
- **Spec I provide:** 4 Label variants + 5 Appeals workflow + the diagram.
- **Ask it to generate:** the label-generation function mapping score→variant text, and the
  `POST /appeal` endpoint; plus Flask-Limiter wiring.
- **How I verify:** ask it to print all 3 label variants and diff against §4 verbatim text;
  submit inputs that hit each band to confirm all three are reachable; appeal a real
  `content_id` and confirm status flips to `under_review` in `GET /log`; fire 12 rapid requests
  and confirm `429`s after the 10th.
