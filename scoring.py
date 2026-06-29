"""Confidence scoring for Provenance Guard (planning.md §3).

Combines the two signal scores into:
  - ai_probability : blended P(AI)  = 0.60*llm + 0.40*stylo
  - confidence     : how SURE we are of the verdict, drained by signal disagreement
And maps them, via ASYMMETRIC thresholds, to one of three attributions. The bar to
call something "AI" is deliberately higher than the bar to call it "human" because a
false positive (accusing a human) is the worst error on a creative platform.
"""

# Signal weights — LLM is the stronger single detector (planning.md §3).
LLM_WEIGHT = 0.60
STYLO_WEIGHT = 0.40

# Asymmetric thresholds (planning.md §3).
AI_PROB_MIN = 0.65      # need a high probability ...
AI_CONF_MIN = 0.55      # ... AND high confidence to say "AI"
HUMAN_PROB_MAX = 0.40   # easier to clear: lower probability ...
HUMAN_CONF_MIN = 0.50   # ... and lower confidence to say "human"


def score(llm_score, stylo_score, stylo_reliable=True):
    """Combine two signal scores into a verdict.

    Returns a dict: ai_probability, confidence, attribution.
    `stylo_reliable=False` (very short text) caps confidence so we don't over-trust
    a noisy structural signal.
    """
    ai_probability = LLM_WEIGHT * llm_score + STYLO_WEIGHT * stylo_score

    certainty = abs(ai_probability - 0.5) * 2.0           # 0 at the fence, 1 at extremes

    # Disagreement is about DIRECTION, not raw magnitude: two signals that both lean
    # "AI" (or both lean "human") corroborate each other even at different intensities.
    # We only fully penalize when the signals straddle 0.5 (point to opposite verdicts).
    raw_gap = abs(llm_score - stylo_score)
    same_side = (llm_score - 0.5) * (stylo_score - 0.5) >= 0
    disagreement = raw_gap * (0.4 if same_side else 1.0)   # mild penalty when corroborating
    confidence = certainty * (1.0 - 0.5 * disagreement)    # divergence drains confidence

    # Short / unreliable structural signal -> don't let confidence run high.
    if not stylo_reliable:
        confidence = min(confidence, 0.5)

    ai_probability = round(ai_probability, 4)
    confidence = round(max(0.0, min(1.0, confidence)), 4)

    if ai_probability >= AI_PROB_MIN and confidence >= AI_CONF_MIN:
        attribution = "likely_ai"
    elif ai_probability <= HUMAN_PROB_MAX and confidence >= HUMAN_CONF_MIN:
        attribution = "likely_human"
    else:
        attribution = "uncertain"

    return {
        "ai_probability": ai_probability,
        "confidence": confidence,
        "attribution": attribution,
        "disagreement": round(raw_gap, 4),
    }
