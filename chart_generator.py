"""
Chart rendering for lesson diagrams (§6 extension, added 16 July 2026), scoped to
Physiology and Pharmacology per the request that prompted it. Bot-owned, matching the
rest of §6's discipline: Claude cannot generate images at all (the Messages API has no
image-output capability), so it never tries to — Claude picks a TYPE from a fixed enum
and a handful of structured parameters (curve shift direction, which conditions to
compare, a half-life value, etc.); this module renders the actual pixels
deterministically from hand-written, textbook-standard formulas. No LLM-authored
numbers or shapes ever reach the image.

Uses the non-interactive 'Agg' backend explicitly — Railway's container has no
display, and the default backend can fail to import cleanly in a headless environment.

Curve shapes below are illustrative teaching approximations (standard Hill-equation /
exponential-decay / saturating-curve models tuned to match well-known qualitative
textbook shapes), not a reproduction of any single published dataset — appropriate for
a revision aid, not a claim of research-grade numerical precision.
"""
from __future__ import annotations

import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

CHART_TYPES = {
    # Physiology
    "oxyhaemoglobin_dissociation_curve",
    "frank_starling_curve",
    "cerebral_autoregulation_curve",
    "compliance_curve",
    # Pharmacology
    "concentration_time_curve",
    "dose_response_curve",
    "context_sensitive_half_time",
}

_FIGSIZE = (7.2, 5.0)
_DPI = 150


def _new_fig():
    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    ax.grid(True, alpha=0.3)
    return fig, ax


def _finish(fig, ax, title: str, xlabel: str, ylabel: str, output_path: str, legend=True):
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    if legend and ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=9, loc="best")
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Physiology
# --------------------------------------------------------------------------- #

def _render_oxyhaemoglobin_dissociation_curve(params: dict, output_path: str):
    shift = str(params.get("shift", "none")).lower()
    p50_normal = 26.6
    shift_map = {"right": 35.0, "left": 20.0, "none": p50_normal}
    p50_shifted = float(params.get("p50", shift_map.get(shift, p50_normal)))
    hill_n = 2.7

    po2 = np.linspace(0, 100, 400)

    def so2(po2_arr, p50):
        return 100 * po2_arr ** hill_n / (p50 ** hill_n + po2_arr ** hill_n)

    fig, ax = _new_fig()
    ax.plot(po2, so2(po2, p50_normal), color="#1f77b4", linewidth=2.5, label="Normal (P50 ≈ 26.6 mmHg)")
    ax.axvline(p50_normal, color="#1f77b4", linestyle=":", alpha=0.5, linewidth=1)

    if shift in ("right", "left"):
        label = f"Shifted {shift} (P50 ≈ {p50_shifted:.0f} mmHg)"
        factors = params.get("shift_factors")
        if isinstance(factors, list) and factors:
            label += f"\n({', '.join(str(f) for f in factors[:4])})"
        ax.plot(po2, so2(po2, p50_shifted), color="#d62728", linewidth=2.5, linestyle="--", label=label)

    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    _finish(fig, ax, "Oxyhaemoglobin Dissociation Curve", "PO2 (mmHg)", "SO2 (%)", output_path)


def _render_frank_starling_curve(params: dict, output_path: str):
    curves = params.get("curves") or ["normal"]
    if not isinstance(curves, list):
        curves = [str(curves)]
    preload = np.linspace(0, 30, 300)

    presets = {
        "normal": (100, 8, "#1f77b4", "Normal"),
        "increased_contractility": (130, 5, "#2ca02c", "Increased contractility (e.g. inotrope)"),
        "hyperdynamic": (130, 5, "#2ca02c", "Increased contractility (e.g. inotrope)"),
        "heart_failure": (55, 14, "#d62728", "Heart failure"),
    }
    fig, ax = _new_fig()
    for key in curves:
        k = str(key).lower()
        svmax, half_k, color, label = presets.get(k, presets["normal"])
        sv = svmax * preload / (preload + half_k)
        ax.plot(preload, sv, color=color, linewidth=2.5, label=label)

    ax.set_xlim(0, 30)
    ax.set_ylim(0, 140)
    _finish(fig, ax, "Frank-Starling Curve", "Preload (LVEDP, mmHg)", "Stroke volume (mL)", output_path)


def _render_cerebral_autoregulation_curve(params: dict, output_path: str):
    lower = float(params.get("lower_limit_mmhg", 60))
    upper = float(params.get("upper_limit_mmhg", 150))
    show_band = bool(params.get("show_controversy_band", True))
    plateau_cbf = 50.0

    map_pressure = np.linspace(20, 200, 500)

    def cbf(map_arr):
        # Smooth plateau with tanh transitions at each limit, rather than sharp corners.
        lower_edge = plateau_cbf * (0.15 + 0.85 / (1 + np.exp(-(map_arr - lower) / 5)))
        upper_edge = plateau_cbf + (map_arr - upper).clip(min=0) * 0.9
        blended = np.where(map_arr < upper, lower_edge, upper_edge)
        return blended

    fig, ax = _new_fig()
    ax.plot(map_pressure, cbf(map_pressure), color="#1f77b4", linewidth=2.5, label="Cerebral blood flow")

    if show_band:
        ax.axvspan(lower - 10, lower + 10, color="#d62728", alpha=0.15,
                   label=f"Debated lower-limit range (~{lower - 10:.0f}-{lower + 10:.0f} mmHg)")
    ax.axvline(lower, color="#555555", linestyle=":", linewidth=1)
    ax.axvline(upper, color="#555555", linestyle=":", linewidth=1)

    ax.set_xlim(20, 200)
    ax.set_ylim(0, 100)
    _finish(fig, ax, "Cerebral Autoregulation", "Mean arterial pressure (mmHg)",
            "Cerebral blood flow (mL/100g/min)", output_path)


def _render_compliance_curve(params: dict, output_path: str):
    curves = params.get("curves") or ["lung", "chest_wall", "total_respiratory_system"]
    if not isinstance(curves, list):
        curves = [str(curves)]
    volume = np.linspace(0, 100, 300)  # % of TLC

    fig, ax = _new_fig()

    if "lung" in curves:
        # Sigmoid: flattens near RV and near TLC.
        p_lung = 30 * (1 / (1 + np.exp(-(volume - 50) / 12))) - 5
        ax.plot(p_lung, volume, color="#1f77b4", linewidth=2.5, label="Lung")

    if "chest_wall" in curves:
        # Roughly linear-decreasing-compliance shape, crossing ~0 near FRC (~40% TLC).
        p_cw = 0.45 * (volume - 40)
        ax.plot(p_cw, volume, color="#2ca02c", linewidth=2.5, label="Chest wall")

    if "total_respiratory_system" in curves or "total" in curves:
        p_lung = 30 * (1 / (1 + np.exp(-(volume - 50) / 12))) - 5
        p_cw = 0.45 * (volume - 40)
        ax.plot(p_lung + p_cw, volume, color="#d62728", linewidth=2.5, label="Total respiratory system")

    ax.axhline(40, color="#888888", linestyle=":", linewidth=1)
    ax.text(ax.get_xlim()[1] if ax.get_xlim()[1] else 20, 41, " FRC", fontsize=8, color="#888888")

    ax.set_ylim(0, 100)
    _finish(fig, ax, "Compliance Curves", "Pressure (cmH2O)", "Lung volume (% TLC)", output_path)


# --------------------------------------------------------------------------- #
# Pharmacology
# --------------------------------------------------------------------------- #

def _render_concentration_time_curve(params: dict, output_path: str):
    route = str(params.get("route", "iv_bolus")).lower()
    compartments = int(params.get("compartments", 1) or 1)
    half_life = float(params.get("half_life_min", 60))
    ke = math.log(2) / max(half_life, 0.1)
    t = np.linspace(0, max(half_life * 5, 30), 400)

    fig, ax = _new_fig()

    if route == "oral":
        ka = ke * 6  # illustrative: absorption faster than elimination
        c = (ka / (ka - ke)) * (np.exp(-ke * t) - np.exp(-ka * t))
        c = 100 * c / c.max()
        ax.plot(t, c, color="#1f77b4", linewidth=2.5, label="Oral (absorption + elimination)")
    elif route == "iv_infusion":
        # Rise to steady state during infusion, then decline once stopped (at t_stop).
        t_stop = t.max() * 0.5
        c = np.where(t <= t_stop, 100 * (1 - np.exp(-ke * t)),
                    100 * (1 - np.exp(-ke * t_stop)) * np.exp(-ke * (t - t_stop)))
        ax.plot(t, c, color="#1f77b4", linewidth=2.5, label="IV infusion (stopped at dashed line)")
        ax.axvline(t_stop, color="#888888", linestyle=":", linewidth=1)
    else:
        if compartments == 2:
            beta = ke
            alpha = beta * 5
            a_frac, b_frac = 0.75, 0.25
            c = 100 * (a_frac * np.exp(-alpha * t) + b_frac * np.exp(-beta * t))
            ax.plot(t, c, color="#1f77b4", linewidth=2.5, label="IV bolus (two-compartment)")
        else:
            c = 100 * np.exp(-ke * t)
            ax.plot(t, c, color="#1f77b4", linewidth=2.5, label="IV bolus (one-compartment)")

    ax.axvline(half_life, color="#d62728", linestyle="--", linewidth=1.2,
              label=f"Terminal half-life ≈ {half_life:.0f} min")
    ax.set_xlim(0, t.max())
    ax.set_ylim(0, 105)
    _finish(fig, ax, "Plasma Concentration vs Time", "Time (min)", "Plasma concentration (% of peak)", output_path)


def _render_dose_response_curve(params: dict, output_path: str):
    curves = params.get("curves") or ["full_agonist"]
    if not isinstance(curves, list):
        curves = [str(curves)]
    log_dose = np.linspace(-2, 2, 400)  # log10(dose), arbitrary units centred on EC50=1

    def hill(logd, ec50_log, emax, n=1.0):
        d = 10 ** logd
        ec50 = 10 ** ec50_log
        return emax * d ** n / (ec50 ** n + d ** n)

    presets = {
        "full_agonist": (0.0, 100, "#1f77b4", "Full agonist"),
        "partial_agonist": (0.0, 50, "#2ca02c", "Partial agonist"),
        "competitive_antagonist_shift": (0.7, 100, "#d62728", "Agonist + competitive antagonist"),
        "non_competitive_antagonist": (0.0, 55, "#9467bd", "Agonist + non-competitive antagonist"),
    }
    fig, ax = _new_fig()
    for key in curves:
        k = str(key).lower()
        ec50_log, emax, color, label = presets.get(k, presets["full_agonist"])
        ax.plot(log_dose, hill(log_dose, ec50_log, emax), color=color, linewidth=2.5, label=label)

    ax.set_xlim(-2, 2)
    ax.set_ylim(0, 105)
    _finish(fig, ax, "Dose-Response Curve", "log[dose]", "Effect (%)", output_path)


def _render_context_sensitive_half_time(params: dict, output_path: str):
    drugs = params.get("drugs") or ["propofol", "fentanyl", "remifentanil"]
    if not isinstance(drugs, list):
        drugs = [str(drugs)]
    hours = np.linspace(0, 8, 200)

    # (plateau_minutes, time_constant_hours, color) — illustrative saturating curves
    # tuned to match well-known qualitative teaching shapes, not literal published data.
    presets = {
        "remifentanil": (4, 3, "#2ca02c"),
        "sufentanil": (30, 3, "#17becf"),
        "propofol": (28, 2.5, "#1f77b4"),
        "alfentanil": (60, 2.5, "#9467bd"),
        "midazolam": (55, 2, "#8c564b"),
        "fentanyl": (260, 2, "#d62728"),
        "thiopentone": (310, 1.8, "#ff7f0e"),
    }
    fig, ax = _new_fig()
    for name in drugs:
        key = str(name).lower()
        plateau, tau, color = presets.get(key, presets["propofol"])
        csht = plateau * (1 - np.exp(-hours / tau))
        ax.plot(hours, csht, color=color, linewidth=2.5, label=key.capitalize())

    ax.set_xlim(0, 8)
    _finish(fig, ax, "Context-Sensitive Half-Time", "Infusion duration (hours)",
            "Context-sensitive half-time (min)", output_path)


_RENDERERS = {
    "oxyhaemoglobin_dissociation_curve": _render_oxyhaemoglobin_dissociation_curve,
    "frank_starling_curve": _render_frank_starling_curve,
    "cerebral_autoregulation_curve": _render_cerebral_autoregulation_curve,
    "compliance_curve": _render_compliance_curve,
    "concentration_time_curve": _render_concentration_time_curve,
    "dose_response_curve": _render_dose_response_curve,
    "context_sensitive_half_time": _render_context_sensitive_half_time,
}


def render_chart(chart_spec: dict, output_path: str) -> bool:
    """Render `chart_spec` (already validated to have a known `type`) to `output_path`
    as a PNG. Returns True on success. Never raises — a chart bug must never block
    lesson delivery; callers treat a False return as "no chart for this lesson"."""
    chart_type = str(chart_spec.get("type", ""))
    renderer = _RENDERERS.get(chart_type)
    if renderer is None:
        return False
    params = chart_spec.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    try:
        renderer(params, output_path)
        return True
    except Exception:  # noqa: BLE001 - a chart is a bonus, never worth failing the lesson over
        return False
