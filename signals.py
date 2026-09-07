import config


def sharp_side(market):
    """Returns (side_label, gap) for whichever side's handle% clears bets%
    by config.SHARP_GAP_THRESHOLD points, else None. This is the real
    bet/handle divergence — not a manual guess."""
    gap_a = market["handle_a"] - market["bets_a"]
    gap_b = market["handle_b"] - market["bets_b"]
    if gap_a >= config.SHARP_GAP_THRESHOLD and gap_a >= gap_b:
        return market["side_a"], gap_a
    if gap_b >= config.SHARP_GAP_THRESHOLD and gap_b > gap_a:
        return market["side_b"], gap_b
    return None
