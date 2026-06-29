"""Provenance Guard — Flask backend.

Milestone 3: POST /submit (signal 1 only), structured audit log, GET /log.
Confidence scoring (M4), the second signal (M4), transparency labels, appeals,
and rate limiting (M5) are layered on in later milestones.
"""

import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import audit
import scoring
from labels import make_label
from signals import llm_signal, stylometry_signal

load_dotenv()  # load GROQ_API_KEY from .env

app = Flask(__name__)
audit.init_db()

# --- Rate limiting ---
# Per-IP limits sized for a real creative platform: a writer submits their own work a
# handful of times an hour, never hundreds. Tight per-minute burst cap blocks a script
# flooding the (LLM-backed, paid) endpoint; daily cap bounds sustained abuse.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;60 per hour;200 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = (data.get("creator_id") or "").strip()

    if not text:
        return jsonify({"error": "Field 'text' is required and must be non-empty."}), 400
    if not creator_id:
        return jsonify({"error": "Field 'creator_id' is required."}), 400

    content_id = str(uuid.uuid4())

    # --- Detection pipeline: two independent signals ---
    sig1 = llm_signal(text)               # Signal 1: semantic (Groq)
    sig2 = stylometry_signal(text)        # Signal 2: structural (pure Python)
    llm_score = sig1["ai_score"]
    stylo_score = sig2["ai_score"]

    # --- Confidence scoring: combine both signals (planning.md §3) ---
    result = scoring.score(llm_score, stylo_score, stylo_reliable=sig2["reliable"])
    attribution = result["attribution"]
    confidence = result["confidence"]
    ai_probability = result["ai_probability"]

    # --- Transparency label ---
    label = make_label(attribution, confidence)

    signals = {"llm_score": llm_score, "stylo_score": stylo_score}

    audit.log_classification(
        content_id=content_id,
        creator_id=creator_id,
        attribution=attribution,
        confidence=confidence,
        ai_probability=ai_probability,
        signals=signals,
        status="classified",
    )

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": confidence,
        "ai_probability": ai_probability,
        "signals": {
            "llm_score": llm_score,
            "llm_rationale": sig1["rationale"],
            "stylo_score": stylo_score,
            "stylo_metrics": sig2["metrics"],
        },
        "label": label,
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = (data.get("content_id") or "").strip()
    creator_reasoning = (data.get("creator_reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "Field 'content_id' is required."}), 400
    if not creator_reasoning:
        return jsonify({"error": "Field 'creator_reasoning' is required."}), 400

    original = audit.get_classification(content_id)
    if original is None:
        return jsonify({"error": f"No classification found for content_id '{content_id}'."}), 404

    # Status -> under_review, and log the appeal next to the original decision.
    audit.set_status(content_id, "under_review")
    audit.log_appeal(original, creator_reasoning)

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": (
            "Appeal received. This content is now under review by a human moderator. "
            "Its original classification is unchanged pending that review."
        ),
        "original_decision": {
            "attribution": original.get("attribution"),
            "confidence": original.get("confidence"),
            "ai_probability": original.get("ai_probability"),
        },
    })


@app.route("/log", methods=["GET"])
def log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": audit.get_log(limit=limit)})


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "rate_limit_exceeded",
        "message": f"Too many requests — {e.description}. Please slow down and try again later.",
    }), 429


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
