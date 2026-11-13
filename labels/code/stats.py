"""Agreement statistics, all computed here: no number is typed by hand."""


def confusion(a, b):
    """Binary confusion counts for paired label lists a (reference) and b."""
    assert len(a) == len(b)
    c = dict(a1_b1=0, a1_b0=0, a0_b1=0, a0_b0=0)
    for x, y in zip(a, b):
        c[f'a{int(x)}_b{int(y)}'] += 1
    return c


def cohen_kappa(a, b):
    """Cohen's kappa for two binary raters."""
    n = len(a)
    c = confusion(a, b)
    po = (c['a1_b1'] + c['a0_b0']) / n
    pa, pb = sum(map(int, a)) / n, sum(map(int, b)) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def agreement(a, b):
    """Counts, raw agreement, kappa, and precision/recall of b against a."""
    c = confusion(a, b)
    n = len(a)
    tp, fn, fp = c['a1_b1'], c['a1_b0'], c['a0_b1']
    return dict(n=n, a_positive=tp + fn, b_positive=tp + fp, confusion=c,
                agreement=(tp + c['a0_b0']) / n, cohen_kappa=cohen_kappa(a, b),
                b_precision_vs_a=tp / (tp + fp) if tp + fp else None,
                b_recall_vs_a=tp / (tp + fn) if tp + fn else None)


def rate(k, n):
    return dict(k=k, n=n, pct=100 * k / n if n else None)
