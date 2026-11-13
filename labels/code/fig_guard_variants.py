"""Findings 6 and 7 figure: leakage under the four privacy instructions, grouped by backbone.

Data: the run folders results/paper18-scoped-<model> (main instruction) and
results/rebuttal18-<plain|strict|action>-<model>. Main is restricted to tasks a-c so it matches the
plain, strict and action runs (3 tasks x 1 rep). Claude is not in the three variants, so it is left
out. The observable O follows Appendix B: the first option whose completion request reached /go;
if there is none, the option the agent clicked; otherwise none.

Metric (--metric):
  rate (default)  % of loaded sessions that selected the option matching the secret (Finding 1 style)
  ls              Leakage Score, mean over the 18 scenarios (pooled per-candidate p_load - p_cold;
                  vendor_session uses its per-task cold pool)

rate is drawn at 0.60 of the 5.5 in text width -> <out>/fig_guard_variants.{pdf,png,json};
ls at 0.50 -> <out>/fig_guard_variants_ls.{pdf,png,json}.
Run from the repository root: uv run python labels/code/fig_guard_variants.py [--metric rate|ls]
"""
import argparse, collections, glob, json, os, sqlite3, sys
from statistics import mean
import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.dirname(HERE)
REPO = os.path.dirname(LABELS)
sys.path.insert(0, REPO)
from harness.scenarios import SCENARIOS  # noqa: E402

DATA = os.environ.get("AGENTTELL_DATA", os.path.join(REPO, "results"))
OUT = os.environ.get("AGENTTELL_OUT", os.path.join(LABELS, "instruction_variants"))
# metric -> (output name, fraction of the 5.5 in text width)
LAYOUT = {"rate": ("fig_guard_variants", 0.60), "ls": ("fig_guard_variants_ls", 0.50)}

ARMS = [("main", "paper18-scoped", "Main"), ("plain", "rebuttal18-plain", "Plain"),
        ("strict", "rebuttal18-strict", "Strict"), ("action", "rebuttal18-action", "Defense")]
MODELS = [("gemini-3.7-flash", "Gemini"), ("gpt-5.6-luna", "GPT-5.6"), ("qwen3-vl-235b", "Qwen"),
          ("glm-4.6v", "GLM"), ("kimi-k2.6", "Kimi")]
TASKS = {"a", "b", "c"}

# ---- type sizes (pt) ------------------------------------------------------------
FS_AXIS = 5.5; FS_LABEL = 6.0; FS_LEGEND = 6.0; FS_GROUP = 6.0; FS_DIRECT = 5.0

# ---- geometry (inches) ----------------------------------------------------------
H = 1.30
LEFT_IN = 0.35        # room for the y label and ticks
BAR = 0.19            # bar width, in group units (group pitch = 1)

# ---- colours --------------------------------------------------------------------
# Rust is the heatmap's 0.72 stop and the paper's leakage colour; blue is Finding 5's.
# The four pass the dataviz palette validator (all pairs: CVD dE 9.4 protan, normal-vision dE 20.0).
COLORS = {"main": "#9A4526", "plain": "#D9922E", "strict": "#2FA37F", "action": "#2B6FB8"}
INK = "#2B2B2B"; MUTED = "#8A8A8A"; GRID = "#ECE8E3"


def task_letter(sc, plant):
    return "abcde"[SCENARIOS[sc].plant_ids.index(plant)]


def run_dir(prefix, model):
    return glob.glob(f"{DATA}/{prefix}-{model}")[0]


def observed(d):
    """session id -> O: first /go navigation, else the last option clicked, else absent."""
    ro = sqlite3.connect("file:" + d + "/events.db?mode=ro", uri=True)
    nav, click = {}, {}
    for sid, kind, t in ro.execute("select session_id, event_type, target_id from events "
                                   "where event_type in ('nav', 'click') order by ts, id"):
        if kind == "nav":
            nav.setdefault(sid, t)
        elif t and t.startswith("opt_"):
            click[sid] = t[4:]
    return {**click, **nav}


def load(d):
    ro = lambda p: sqlite3.connect("file:" + p + "?mode=ro", uri=True)
    obs = observed(d)
    rows = []
    for sid, sc, cond, plant, err in ro(d + "/results.db").execute(
            "select session_id, scenario, condition, plant, error from sessions"):
        if err:
            continue
        if cond != "none" and task_letter(sc, plant) not in TASKS:
            continue
        rows.append(dict(sc=sc, cond=cond, plant=plant, O=obs.get(sid)))
    return rows


def leakage_score(rows):
    vals = []
    for sc in sorted({r["sc"] for r in rows}):
        R = [r for r in rows if r["sc"] == sc]
        L = [r for r in R if r["cond"] != "none"]; C = [r for r in R if r["cond"] == "none"]
        cands = sorted({r["cond"] for r in L})
        rate = lambda xs, s: mean(r["O"] == s for r in xs)
        if sc == "vendor_session":
            per = []
            for j in sorted({r["plant"] for r in L}):
                Cj = [r for r in C if r["plant"] == j]
                per.append(mean(rate([r for r in L if r["plant"] == j and r["cond"] == s], s) - rate(Cj, s)
                                for s in cands if any(r["plant"] == j and r["cond"] == s for r in L)))
            vals.append(mean(per))
        else:
            vals.append(mean(rate([r for r in L if r["cond"] == s], s) - rate(C, s) for s in cands))
    return 100 * mean(vals)


def compute(metric):
    res = {}
    for key, prefix, _ in ARMS:
        res[key] = {}; pooled = [0, 0]
        for mk, mn in MODELS:
            rows = load(run_dir(prefix, mk))
            L = [r for r in rows if r["cond"] != "none"]
            hit = sum(r["O"] == r["cond"] for r in L)
            pooled[0] += hit; pooled[1] += len(L)
            res[key][mn] = 100 * hit / len(L) if metric == "rate" else leakage_score(rows)
        res[key]["Mean"] = (100 * pooled[0] / pooled[1] if metric == "rate"
                            else mean(res[key][mn] for _, mn in MODELS))
    return res


def build(metric):
    NAME, frac = LAYOUT[metric]
    W = frac * 5.5
    res = compute(metric)
    groups = [mn for _, mn in MODELS]
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{NAME}.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(metric=("% of loaded sessions selecting the matching option" if metric == "rate"
                               else "Leakage Score, mean over 18 scenarios"),
                       tasks="a-c of each scenario; main restricted to the same tasks",
                       mean=("pooled over the five backbones' loaded sessions" if metric == "rate"
                             else "mean of the five backbones"),
                       values=res), fh, indent=2)

    fig = plt.figure(figsize=(W, H))
    ax = fig.add_axes([LEFT_IN / W, 0.115, 1 - LEFT_IN / W - 0.01, 0.70])
    xs = list(range(len(groups)))
    offs = [(-1.5 + k) * BAR for k in range(len(ARMS))]
    for y in (0, 25, 50, 75, 100):
        ax.axhline(y, color=GRID, linewidth=0.5, zorder=0)
    for k, (key, _, _) in enumerate(ARMS):
        for x, g in zip(xs, groups):
            v = res[key][g]
            ax.bar(x + offs[k], v, width=BAR, color=COLORS[key], edgecolor="white",
                   linewidth=0.4, zorder=2)
    ax.set_xticks(xs)
    ax.set_xticklabels(groups, fontsize=FS_GROUP, color=INK)
    ax.set_xlim(xs[0] - 0.5, xs[-1] + 0.5)
    ax.set_ylim(0, 100)
    ax.set_yticks((0, 25, 50, 75, 100))
    ax.tick_params(axis="y", length=0, labelsize=FS_AXIS, colors=MUTED, pad=1.5)
    ax.tick_params(axis="x", length=0, pad=2)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(MUTED); ax.spines["bottom"].set_linewidth(0.4)
    ax.set_ylabel("Leakage (%)" if metric == "rate" else "Leakage Score",
                  fontsize=FS_LABEL, color=INK, labelpad=2)
    handles = [Patch(facecolor=COLORS[k], edgecolor="none", label=lbl) for k, _, lbl in ARMS]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.55, 1.0), ncol=4,
               frameon=False, fontsize=FS_LEGEND, handlelength=0.9, handleheight=0.7,
               handletextpad=0.3, columnspacing=1.0, borderaxespad=0.1, labelcolor=INK)
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(OUT, f"{NAME}.{ext}"), dpi=600, facecolor="white")
    plt.close(fig)
    print(f"wrote {OUT}/{NAME}.pdf, .png, .json  ({W:.2f} x {H:.2f} in, metric={metric})")
    for g in groups:
        print(f"  {g:8s} " + "  ".join(f"{lbl} {res[k][g]:5.1f}" for k, _, lbl in ARMS))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric", choices=("rate", "ls"), default="rate")
    build(ap.parse_args().metric)
