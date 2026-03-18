from __future__ import annotations


def net_expectancy_bps(*, hit_rate: float, avg_win_bps: float, avg_loss_bps: float, costs_bps: float) -> float:
    return float((hit_rate * avg_win_bps) - ((1.0 - hit_rate) * abs(avg_loss_bps)) - costs_bps)
