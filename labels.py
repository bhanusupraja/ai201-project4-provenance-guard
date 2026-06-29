"""Transparency-label generation.

Maps an attribution + confidence to the reader-facing label text. Three variants,
written to be plain-language and honest about uncertainty. The AI variant is
deliberately hedged ("likely", "estimate", "can appeal") because a false positive
(accusing a human) is the worst error on a creative platform.
"""


def make_label(attribution, confidence):
    """Return the transparency label dict for a verdict.

    {"variant": ..., "headline": ..., "body": ..., "confidence_pct": int}
    """
    confidence_pct = round(confidence * 100)

    if attribution == "likely_ai":
        return {
            "variant": "high_confidence_ai",
            "headline": "Likely AI-generated",
            "body": (
                f"Our analysis suggests this text was probably produced with significant "
                f"AI assistance (confidence: {confidence_pct}%). This is an automated "
                f"estimate, not a certainty and the creator can appeal if this is wrong."
            ),
            "confidence_pct": confidence_pct,
        }

    if attribution == "likely_human":
        return {
            "variant": "high_confidence_human",
            "headline": "Likely human-written",
            "body": (
                f"Our analysis found no strong signs of AI generation in this text "
                f"(confidence: {confidence_pct}%). This is an automated estimate, not a "
                f"guarantee."
            ),
            "confidence_pct": confidence_pct,
        }

    # default / uncertain
    return {
        "variant": "uncertain",
        "headline": "Attribution uncertain",
        "body": (
            f"Our signals disagree or are inconclusive for this text, so we are not "
            f"assigning an attribution (confidence: {confidence_pct}%). When we can't "
            f"tell, we don't label and the benefit of the doubt goes to the creator."
        ),
        "confidence_pct": confidence_pct,
    }
