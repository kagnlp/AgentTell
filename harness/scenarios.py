"""Scenario registry — the single source of truth for each side-channel scenario.

A *scenario* defines an in-scope secret and the neutral affordances the attacker probe
offers. It is consumed by the planting layer (what prior-origin state to plant), the
attacker server (what probe page to render), and the analysis layer (which behavioural
features map to which secret label).

The scenarios themselves are DATA, not code: one YAML file per scenario under
`benchmark/scenarios/`, rostered in `benchmark/scenarios/index.yaml`. This module loads and
validates them and exposes the same two symbols every consumer already uses — `SCENARIOS`
and `get_scenario()`. Adding a scenario means adding one YAML file plus its key in
index.yaml; no Python changes anywhere.

In-scope secret (the project's tightened definition):
  (a) the agent would NOT disclose it on a direct request, and
  (b) no standard web mechanism reveals it.
So the *only* route is the agent's behaviour on the attacker page.

Free choice, no login wall
  An earlier design gated the diagnostic option behind a login wall: the option completed
  only if the agent held that session, so the attacker was really reading whether an
  authenticated action *succeeded* — a mechanical probe, not a behavioural side channel (a
  brute-force script that keeps whichever option doesn't wall would leak the same bit).
  No probe walls an option any more: EVERY option is equally completable (all route through
  the local /go tracker), and the persuasion copy ("faster/instant if you're already signed
  in") is gone. The leak is purely which option the agent *freely prefers* given the state it
  carries in context — a genuine preference, weaker (statistical) but honest. The retired
  `pay_by_bank` wall and the `Gate` dataclass that drove it are both gone.

Plants
  A scenario declares SEVERAL plant routes to the same secret under `plants:`, one per plant
  design in the benchmark spec — checkout plants "which bank" via a direct-deposit check, a
  statement lookup, or a card-on-file confirmation. They differ in what else lands in context
  and in how load-bearing the held value is, so running all of them separates "this model does
  not leak" from "this particular plant did not put the value in context". The singular
  `plant:` form still parses, as a scenario with one route.

Authoring notes
  - `affordances` / `filler` accept either a list of option mappings, a `{from: <set>}`
    reference into `_shared.yaml` (optionally with `drop: [blurb]`), or a `cta_template` plus
    an `options` map/list.
  - `conditions` defaults to the affordance ids + "none"; `target_label` to the first
    affordance id; `rig` to "choice". Declare them only to override.
  - Every agent-facing string lives in the data: the probe/plant tasks, the agent's role
    framing, the probe's completion copy, and (for `authstate_v1`) its summarize nudge.
"""
from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    """A scenario data file is malformed or violates a registry invariant."""


# CSafeLoader is ~3x faster and matters once the registry is 100+ files.
_BaseLoader: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _Loader(_BaseLoader):
    """A SafeLoader that REJECTS duplicate mapping keys.

    YAML silently keeps the last of a repeated key, so a typo'd duplicate id in a compact
    `options:` map would drop an option — leaving a scenario quietly running with k-1 labels while
    every report still says k. Cheap to catch here, near-impossible to spot by eye in 100 files.
    """

    def construct_mapping(self, node, deep=False):   # type: ignore[override]
        seen: set = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ScenarioError(
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = _ROOT / "benchmark" / "scenarios"
_TEMPLATE_DIR = _ROOT / "attacker" / "templates"

RIGS = ("choice", "correct")
#: Placeholders `plant.url` may reference. `code` is the opaque plant code
#: (harness/opaque.py); the base URLs are resolved against orchestrator.config by the caller
#: (harness/state_planting), so this module stays free of any config dependency.
URL_PLACEHOLDERS = frozenset({"code", "BANK_BASE_URL", "SERVICES_BASE_URL", "ATTACKER_BASE_URL"})

_OPTION_KEYS = frozenset({"id", "name", "cta", "blurb", "fixture"})
_PLANT_KEYS = frozenset({"id", "step_label", "url", "task", "code_from", "page", "rig"})
_PAGE_KEYS = frozenset({"kind", "view", "renderer", "heading", "items", "fixture", "fixtures"})
_PROBE_KEYS = frozenset({"template", "task", "direct_label"})
_SCENARIO_KEYS = frozenset({
    "key", "secret_class", "title", "lede", "role", "nudge", "completion", "rig", "prefill",
    "held_value", "field_label", "conditions", "target_label", "affordances", "filler",
    "probe", "plant", "plants", "surfaces",
})

#: Page renderers the mock origins implement (mock_origins/services/app.py `_RENDERERS`). A
#: scenario naming an unknown one must fail at load, not serve a blank page at run time.
RENDERERS = ("record", "table", "list_action", "article_list", "form")

#: The condition that supplies the secret in the agent's own INSTRUCTIONS rather than planting it
#: at a prior origin. It exists to separate "the model won't apply the value" from "the model
#: can't edit this field at all" — two failures that produce identical output (prefilled_identity).
#: Deliberately un-de-confounded, so tests/test_deconfound.py skips it by name.
CAPABILITY = "capability"


@dataclass(frozen=True)
class Affordance:
    """One neutral choice shown on the probe (a payment button / card / link).

    `id` is used in the `data-probe` attribute and the `/go?to=<id>` link, and is the
    secret label this affordance is diagnostic of. `fixture` is what the prior-origin plant
    page displays for this label (an identity, a locale, an article) — page content, kept
    beside the label so it cannot drift from what it is supposed to plant. It is excluded
    from equality/hashing since it is not part of the option's identity.
    """
    id: str
    name: str
    cta: str = "Continue →"
    blurb: str = ""
    fixture: Mapping[str, str] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Plant:
    """How to plant this scenario's secret at a prior origin, declaratively.

    Every scenario but `authstate_v1` plants with the same two-step shape (one plant
    step, then the probe), differing only in the URL, the step label and the task — so this
    replaces what used to be one hand-written plan builder per scenario.

    A scenario declares SEVERAL of these (`plants:`), one per plant design in the benchmark
    spec — e.g. checkout's direct-deposit check / statement availability / card-on-file. They
    plant the SAME secret by different routes, so comparing them separates "this model doesn't
    leak" from "this particular plant didn't put the value in context". `id` names the plant in
    the results row and on the trace filename.

    `url` is a format string over URL_PLACEHOLDERS. `{code}` is the opaque plant code, which
    keeps the held value out of the URL the agent is told to type (harness/opaque.py); omit
    it when the scenario has a single label and the path names nothing on its own.

    `code_from` maps a condition id to the token the code is computed over, for scenarios
    whose secret labels and mock-fixture vocabulary differ (balance_threshold's `bal_healthy`
    plants the `healthy` order history). Absent means the condition id is used directly.

    `page` describes the plant page the mock origin must serve:
      kind      which origin serves it — "bank" or "services" (plus the legacy pre-`view`
                kinds "files" / "article" / "identity" / "locale")
      view      the URL segment in /p/<view>/<code>; globally unique across the registry
      renderer  one of RENDERERS
      heading   the page's <h2>
      items     list rows / table rows / file names, as the renderer takes them
      fixture   field values constant across every label
      fixtures  per-label override, {label_id: {...}}, merged over `fixture`
    See mock_origins/services/app.py.
    """
    id: str = "default"
    step_label: str = ""
    url: str = ""
    task: str = ""
    rig: str = ""                                          # "legacy_v1" for authstate_v1
    code_from: Mapping[str, str] = field(default_factory=dict, compare=False)
    page: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def code_value(self, condition: str) -> str:
        """The token `{code}` is computed over for this condition."""
        return self.code_from.get(condition, condition)

    def url_for(self, condition: str, bases: Mapping[str, str], code: str = "") -> str:
        """Resolve `url` for one condition. `bases` supplies the *_BASE_URL placeholders."""
        return self.url.format(code=code, **bases)


@dataclass(frozen=True)
class Scenario:
    key: str
    secret_class: int                     # 1 (task/intent) or 2 (prior-origin state)
    title: str                            # probe page header
    lede: str                             # probe intro paragraph
    affordances: tuple[Affordance, ...]    # the diagnostic options (one per secret label)
    conditions: tuple[str, ...]            # ground-truth conditions in the matrix
    target_label: str                      # the binary "X" secret label (vs "none")
    probe_template: str
    direct_label: str                      # textarea prompt shown only in the `direct` variant
    plants: tuple[Plant, ...]              # one per plant design; see Plant
    filler: tuple[Affordance, ...] = ()    # neutral, non-diagnostic options (card / wallet)
    probe_task: str = ""                   # service-agnostic task the agent is given at the probe
    role: str = ""                         # agent role framing (agents/browseruse_runner)
    completion: str = ""                   # "done" copy for an engaged option; may use {name}
    nudge: str = ""                        # extra task line; only `authstate_v1` sets it
    # rig selects the probe mechanism + how a leak is read:
    #   "choice"  : pick one of N options. Leak = the engaged option.
    #   "correct" : the probe pre-fills a WRONG value; leak = the agent silently corrects it to
    #               the value it carries from a prior origin (prefilled_identity, locale_default).
    rig: str = "choice"
    prefill: str = ""                      # rig="correct": the wrong default the probe shows
    held_value: str = ""                   # rig="correct": the true value planted at the origin
    field_label: str = ""                  # rig="correct": NEUTRAL label above the pre-filled
                                           # field (must NOT ask for the value — that solicits it)
    # Per-plant display names: {plant id: {label id: name}}. Only vendor_session sets this; see
    # names_for(). Excluded from equality/hashing for the same reason Affordance.fixture is —
    # it is page content, not part of the scenario's identity.
    surfaces: Mapping[str, Mapping[str, str]] = field(default_factory=dict, compare=False)
    cta_template: str = ""                 # kept so a surfaces scenario can re-render its option
                                           # CTAs against the surface's names at probe time

    @property
    def label_ids(self) -> list[str]:
        return [a.id for a in self.affordances]

    @property
    def label_names(self) -> dict[str, str]:
        return {a.id: a.name for a in self.affordances}

    def names_for(self, plant_id: str = "") -> dict[str, str]:
        """Display names for this scenario's labels, as seen on `plant_id`'s surface.

        Almost every scenario shows one name per label everywhere, and this is just
        `label_names`. vendor_session (new-scenarios.md §20) is the exception: it collapses five
        earlier single-vendor scenarios into one cell and rotates the VENDOR SET across the
        surfaces as a within-scenario factor, so the retailer surface must offer retailer names
        and the work-tool surface work-tool names.

        Both the plant page (plant_views) and the probe (attacker/app.py) take their names from
        here, which is what keeps the planted vendor and the offered vendor the SAME STRING. If
        they diverged there would be no channel at all — and the run would still look healthy,
        which is the failure this indirection exists to prevent.
        """
        return dict(self.surfaces.get(plant_id) or self.label_names)

    @property
    def plant(self) -> Plant:
        """The scenario's first (primary) plant.

        Kept so every consumer written against the one-plant registry — the mock origins, the
        analysis layer, scripts/ — reads unchanged. Anything that must distinguish the plants
        uses `plants` / `get_plant()`.
        """
        return self.plants[0]

    @property
    def plant_ids(self) -> list[str]:
        return [p.id for p in self.plants]

    def get_plant(self, plant_id: str = "") -> Plant:
        """Resolve a plant by id; empty id means the primary plant."""
        if not plant_id:
            return self.plants[0]
        for p in self.plants:
            if p.id == plant_id:
                return p
        raise ValueError(f"{self.key}: unknown plant {plant_id!r}. Known: {self.plant_ids}")

    def label_for_condition(self, condition: str) -> str:
        """Map a ground-truth condition to its secret label ('none' if no secret)."""
        if condition in ("none", "not_X"):
            return "none"
        if condition == CAPABILITY:
            # Not a secret label: the capability arm supplies the value in the instruction, so it
            # is neither loaded nor cold and must never be pooled into either.
            return CAPABILITY
        if condition == "X":                      # authstate_v1 legacy condition
            return self.target_label
        if condition in self.label_ids:
            return condition
        for lid in self.label_ids:                # tolerate prefixed forms
            if condition.endswith(lid):
                return lid
        return "none"


@dataclass(frozen=True)
class PlantPageKind:
    """Every plant page of one `plant.page.kind`, collected across the registry.

    Lets the mock origins serve their fixtures without naming a single scenario key. The old
    `SCENARIOS["owned_content"].label_ids` form raised KeyError the moment that scenario was
    renamed or disabled.
    """
    kind: str
    labels: tuple[str, ...]                       # label ids handled by this kind
    items: tuple[str, ...] = ()                   # extra page content (e.g. a file listing)
    fixtures: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    default: str = ""                             # label to serve when a request names none


@dataclass(frozen=True)
class PlantView:
    """One concrete plant page, served at /p/<view>/<code> by the origin named in `kind`.

    Everything the mock needs to render the page without knowing a scenario key: the renderer,
    the heading, the rows, and the field values (constant in `fixture`, per-label in `fixtures`).
    `names` maps a label id to its display name, which is what actually PLANTS the secret — it is
    the only place on the page the held value appears.
    """
    view: str
    kind: str                                     # "bank" | "services"
    renderer: str
    labels: tuple[str, ...]
    heading: str = ""
    items: tuple[str, ...] = ()
    fixture: Mapping[str, Any] = field(default_factory=dict)
    fixtures: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    names: Mapping[str, str] = field(default_factory=dict)
    code_from: Mapping[str, str] = field(default_factory=dict)
    scenario: str = ""
    plant: str = ""

    def resolve_candidates(self) -> dict[str, str]:
        """token -> label id, for harness.opaque.resolve.

        Keyed by the TOKEN because `resolve` hashes the candidates it is given and matches: with
        `code_from` the plant code is computed over a fixture token rather than the label id
        (balance_threshold's `bal_healthy` plants `healthy`), so hashing the ids would resolve
        nothing and every one of that scenario's plant pages would 404.
        """
        return {self.code_from.get(lid, lid): lid for lid in self.labels}

    def content(self, label: str) -> dict[str, Any]:
        """The merged field values this page shows for `label`."""
        return {"name": self.names.get(label, label), **dict(self.fixture),
                **dict(self.fixtures.get(label) or {})}


# --- loading ----------------------------------------------------------------------------

def _read(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            data = yaml.load(fh, Loader=_Loader)  # noqa: S506  _Loader subclasses SafeLoader
    except ScenarioError as e:
        raise ScenarioError(f"{path.name}: {e}") from None   # name the file among the hundred
    except yaml.YAMLError as e:
        raise ScenarioError(f"{path.name}: invalid YAML — {e}") from None
    if not isinstance(data, dict):
        raise ScenarioError(f"{path.name}: expected a mapping at the top level")
    return data


def _shared_sets() -> dict[str, list[dict]]:
    path = DATA_DIR / "_shared.yaml"
    if not path.exists():
        return {}
    sets = _read(path).get("sets") or {}
    if not isinstance(sets, dict):
        raise ScenarioError("_shared.yaml: `sets` must be a mapping of name -> option list")
    return sets


def _option(raw: Any, path_name: str, cta_template: str = "") -> Affordance:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path_name}: each option must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - _OPTION_KEYS
    if unknown:
        raise ScenarioError(f"{path_name}: unknown option key(s) {sorted(unknown)} "
                            f"in option {raw.get('id')!r}; allowed: {sorted(_OPTION_KEYS)}")
    for req in ("id", "name"):
        if not raw.get(req):
            raise ScenarioError(f"{path_name}: option {raw!r} is missing `{req}`")
    kw: dict[str, Any] = {"id": str(raw["id"]), "name": str(raw["name"])}
    if raw.get("blurb"):
        kw["blurb"] = str(raw["blurb"])
    if raw.get("cta"):
        kw["cta"] = str(raw["cta"])
    elif cta_template:
        kw["cta"] = cta_template.format(name=kw["name"])
    if raw.get("fixture"):
        kw["fixture"] = dict(raw["fixture"])
    return Affordance(**kw)


def _options(spec: Any, path_name: str, shared: dict[str, list[dict]],
             what: str) -> tuple[Affordance, ...]:
    """Parse an `affordances` / `filler` block into Affordances.

    Accepted forms:
      - a list of option mappings
      - {from: <shared set name>, drop: [<field>, ...]}
      - {cta_template: "...", options: {id: name, ...} | [ {...}, ... ]}
    """
    if spec is None:
        return ()
    if isinstance(spec, list):
        return tuple(_option(o, path_name) for o in spec)
    if not isinstance(spec, dict):
        raise ScenarioError(f"{path_name}: `{what}` must be a list or a mapping")

    if "from" in spec:
        name = spec["from"]
        if name not in shared:
            raise ScenarioError(f"{path_name}: `{what}.from` names unknown shared set "
                                f"{name!r}; known: {sorted(shared)}")
        drop = set(spec.get("drop") or ())
        raw = [{k: v for k, v in dict(o).items() if k not in drop} for o in shared[name]]
        return tuple(_option(o, path_name, spec.get("cta_template", "")) for o in raw)

    cta_template = spec.get("cta_template", "")
    opts = spec.get("options")
    if opts is None:
        raise ScenarioError(f"{path_name}: `{what}` mapping needs `from` or `options`")
    if isinstance(opts, dict):                        # compact {id: display name} form
        opts = [{"id": k, "name": v} for k, v in opts.items()]
    if not isinstance(opts, list):
        raise ScenarioError(f"{path_name}: `{what}.options` must be a mapping or a list")
    return tuple(_option(o, path_name, cta_template) for o in opts)


def _plant(raw: Any, path_name: str, default_id: str) -> Plant:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path_name}: each plant must be a mapping, "
                            f"got {type(raw).__name__}")
    unknown = set(raw) - _PLANT_KEYS
    if unknown:
        raise ScenarioError(f"{path_name}: unknown plant key(s) {sorted(unknown)}; "
                            f"allowed: {sorted(_PLANT_KEYS)}")
    page = dict(raw.get("page") or {})
    unknown = set(page) - _PAGE_KEYS
    if unknown:
        raise ScenarioError(f"{path_name}: unknown plant.page key(s) {sorted(unknown)}; "
                            f"allowed: {sorted(_PAGE_KEYS)}")
    return Plant(
        id=str(raw.get("id") or default_id),
        step_label=str(raw.get("step_label", "")),
        url=str(raw.get("url", "")),
        task=str(raw.get("task", "")),
        rig=str(raw.get("rig", "")),
        code_from={str(k): str(v) for k, v in (raw.get("code_from") or {}).items()},
        page=page,
    )


def _plants(raw: dict, path_name: str) -> tuple[Plant, ...]:
    """Parse `plants:` (a list) or the singular `plant:` (one mapping).

    The singular form is not legacy cruft to be migrated away: a scenario with genuinely one
    plant route — `authstate_v1`, the framing controls — reads better without a
    one-element list, and every such file keeps working untouched.
    """
    many, one = raw.get("plants"), raw.get("plant")
    if many is not None and one is not None:
        raise ScenarioError(f"{path_name}: declare `plant` or `plants`, not both")
    if many is None and one is None:
        raise ScenarioError(f"{path_name}: missing `plant` / `plants` block")
    if one is not None:
        return (_plant(one, path_name, "default"),)
    if not isinstance(many, list) or not many:
        raise ScenarioError(f"{path_name}: `plants` must be a non-empty list")
    plants = tuple(_plant(p, path_name, f"p{i}") for i, p in enumerate(many, 1))
    ids = [p.id for p in plants]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ScenarioError(f"{path_name}: duplicate plant id(s) {dupes} — the id is what the "
                            f"results row and the trace filename record the plant as")
    return plants


def _scenario(path: Path, shared: dict[str, list[dict]]) -> Scenario:
    raw = _read(path)
    name = path.name
    unknown = set(raw) - _SCENARIO_KEYS
    if unknown:
        raise ScenarioError(f"{name}: unknown key(s) {sorted(unknown)}; "
                            f"allowed: {sorted(_SCENARIO_KEYS)}")

    key = str(raw.get("key") or "")
    if key != path.stem:
        raise ScenarioError(f"{name}: `key` is {key!r} but the filename says {path.stem!r}; "
                            f"they must match so the roster and the files stay in step")

    probe = raw.get("probe") or {}
    if not isinstance(probe, dict):
        raise ScenarioError(f"{name}: `probe` must be a mapping")
    unknown = set(probe) - _PROBE_KEYS
    if unknown:
        raise ScenarioError(f"{name}: unknown probe key(s) {sorted(unknown)}; "
                            f"allowed: {sorted(_PROBE_KEYS)}")

    affordances = _options(raw.get("affordances"), name, shared, "affordances")
    if not affordances:
        raise ScenarioError(f"{name}: at least one affordance is required — it is the secret "
                            f"label the channel is read on")
    filler = _options(raw.get("filler"), name, shared, "filler")
    label_ids = [a.id for a in affordances]

    conditions = raw.get("conditions")
    if conditions is None:
        conditions = label_ids + ["none"]
    target_label = raw.get("target_label") or label_ids[0]

    # The affordances block's `cta_template` is normally consumed on the spot (each option's cta
    # is baked at load). A `surfaces` scenario needs it again at probe time, to re-render the CTAs
    # against the surface's names, so keep it.
    aff_spec = raw.get("affordances")
    cta_template = aff_spec.get("cta_template", "") if isinstance(aff_spec, dict) else ""

    surfaces_raw = raw.get("surfaces") or {}
    if not isinstance(surfaces_raw, dict):
        raise ScenarioError(f"{name}: `surfaces` must be a mapping of plant id -> "
                            f"{{label id: display name}}")
    surfaces = {str(pid): {str(k): str(v) for k, v in (names or {}).items()}
                for pid, names in surfaces_raw.items()}

    return Scenario(
        key=key,
        secret_class=int(raw.get("secret_class", 2)),
        title=str(raw.get("title", "")),
        lede=str(raw.get("lede", "")),
        affordances=affordances,
        filler=filler,
        conditions=tuple(str(c) for c in conditions),
        target_label=str(target_label),
        probe_template=str(probe.get("template", "")),
        probe_task=str(probe.get("task", "")),
        direct_label=str(probe.get("direct_label", "")),
        plants=_plants(raw, name),
        role=str(raw.get("role", "")),
        completion=str(raw.get("completion", "")),
        nudge=str(raw.get("nudge", "")),
        rig=str(raw.get("rig", "choice")),
        prefill=str(raw.get("prefill", "")),
        held_value=str(raw.get("held_value", "")),
        field_label=str(raw.get("field_label", "")),
        surfaces=surfaces,
        cta_template=str(cta_template),
    )


# --- validation -------------------------------------------------------------------------

def _validate(sc: Scenario) -> None:
    """Enforce the invariants the registry has always relied on but never checked.

    Several were previously asserted only inside individual tests, which does not scale to a
    hundred scenarios: a new file must fail loudly at load, naming itself.
    """
    where = f"{sc.key}.yaml"
    ids = sc.label_ids

    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ScenarioError(f"{where}: duplicate affordance id(s) {dupes}")
    clash = set(ids) & {a.id for a in sc.filler}
    if clash:
        raise ScenarioError(f"{where}: id(s) {sorted(clash)} appear in both `affordances` and "
                            f"`filler` — a neutral option must not double as a secret label")
    if sc.target_label not in ids:
        raise ScenarioError(f"{where}: target_label {sc.target_label!r} is not one of the "
                            f"affordance ids {ids}")

    # "none" is the cold arm every scenario needs to compare against. `authstate_v1` predates
    # that convention and spells its arms X / not_X instead.
    if "none" not in sc.conditions and "not_X" not in sc.conditions:
        raise ScenarioError(f"{where}: `conditions` must include 'none' (the cold arm)")
    for cond in sc.conditions:
        if cond not in ids and cond not in ("none", "X", "not_X", CAPABILITY):
            raise ScenarioError(f"{where}: condition {cond!r} is neither an affordance id nor "
                                f"one of none / X / not_X / {CAPABILITY}")
    # The capability arm hands the agent the value outright and watches whether it can apply it
    # at all. That only means anything where the observable is an EDIT: on a choice rig every
    # option is clickable regardless, so the arm would measure nothing.
    if CAPABILITY in sc.conditions and sc.rig != "correct":
        raise ScenarioError(f"{where}: the {CAPABILITY!r} condition needs rig='correct' — on a "
                            f"choice rig there is no edit to be capable or incapable of")

    if not sc.probe_template:
        raise ScenarioError(f"{where}: `probe.template` is required")
    if not (_TEMPLATE_DIR / sc.probe_template).exists():
        raise ScenarioError(f"{where}: probe template {sc.probe_template!r} does not exist in "
                            f"{_TEMPLATE_DIR}")

    if sc.rig not in RIGS:
        raise ScenarioError(f"{where}: rig {sc.rig!r} is not one of {list(RIGS)}")
    if sc.rig == "correct":
        for f in ("prefill", "held_value", "field_label"):
            if not getattr(sc, f):
                raise ScenarioError(f"{where}: rig='correct' requires `{f}`")
        # The label above a pre-filled field must not ASK for the value: a question would
        # solicit the secret, and the whole design rests on never requesting it.
        if "?" in sc.field_label:
            raise ScenarioError(f"{where}: field_label must not ask a question "
                                f"({sc.field_label!r}) — that would solicit the secret")

    # plant — every declared plant must stand on its own, since any of them can be the one run.
    for p in sc.plants:
        _validate_plant(sc, p, where)

    _validate_surfaces(sc, where)


def _validate_surfaces(sc: Scenario, where: str) -> None:
    """A `surfaces` block must cover EVERY plant and EVERY label, exactly.

    Coverage is total rather than best-effort on purpose. A plant with no surface entry would
    silently fall back to the canonical names, so the plant page would seed one vendor while the
    probe offered another — no channel at all, and a run that still completes and reports a clean
    0.00. That is the failure mode this whole codebase fails loudly on, so it is a load error.
    """
    if not sc.surfaces:
        return
    ids, plant_ids = set(sc.label_ids), set(sc.plant_ids)
    missing = sorted(plant_ids - set(sc.surfaces))
    if missing:
        raise ScenarioError(f"{where}: `surfaces` does not cover plant(s) {missing} — a plant "
                            f"with no surface would seed one name and offer another")
    stray = sorted(set(sc.surfaces) - plant_ids)
    if stray:
        raise ScenarioError(f"{where}: `surfaces` key(s) {stray} are not plant ids. "
                            f"Known: {sc.plant_ids}")
    for pid, names in sc.surfaces.items():
        if set(names) != ids:
            raise ScenarioError(
                f"{where}: surface {pid!r} names {sorted(names)} but the scenario's labels are "
                f"{sorted(ids)} — every label needs a name on every surface")
    # Without a cta_template the probe cannot re-render its option copy against the surface's
    # names, so the cards would read with the canonical names while the pages seeded others.
    if not sc.cta_template:
        raise ScenarioError(f"{where}: a `surfaces` scenario needs `affordances.cta_template`, "
                            f"so the probe can render each option's copy per surface")


def _validate_plant(sc: Scenario, p: Plant, where: str) -> None:
    ids = sc.label_ids
    at = f"{where}: plant {p.id!r}"
    if p.rig == "legacy_v1":
        return                                   # bespoke plan shape; nothing below applies
    if not p.step_label:
        raise ScenarioError(f"{at}: `step_label` is required")
    if not p.url:
        raise ScenarioError(f"{at}: `url` is required")
    fields = {fn for _, fn, _, _ in string.Formatter().parse(p.url) if fn}
    unknown = fields - URL_PLACEHOLDERS
    if unknown:
        raise ScenarioError(f"{at}: url references unknown placeholder(s) "
                            f"{sorted(unknown)}; allowed: {sorted(URL_PLACEHOLDERS)}")
    if len(ids) > 1 and "code" not in fields:
        raise ScenarioError(f"{at}: url must carry {{code}} when the scenario has "
                            f"{len(ids)} labels, else every condition plants the same page")
    stray = set(p.code_from) - set(ids)
    if stray:
        raise ScenarioError(f"{at}: code_from key(s) {sorted(stray)} are not affordance ids")
    if not p.task:
        raise ScenarioError(f"{at}: `task` is required")
    task_fields = {fn for _, fn, _, _ in string.Formatter().parse(p.task) if fn}
    if task_fields - {"name"}:
        raise ScenarioError(f"{at}: task may only interpolate {{name}}, got "
                            f"{sorted(task_fields)}")

    view, renderer = p.page.get("view", ""), p.page.get("renderer", "")
    if view and renderer not in RENDERERS:
        raise ScenarioError(f"{at}: page.view {view!r} needs a `renderer` from "
                            f"{list(RENDERERS)}, got {renderer!r}")
    if view and f"/{view}/" not in p.url:
        # The mock serves /p/<view>/<code> and reads the view straight off the path; a url that
        # points somewhere else would 404 or, worse, render another plant's page.
        raise ScenarioError(f"{at}: page.view is {view!r} but url {p.url!r} does not route "
                            f"through /{view}/")
    stray = set(p.page.get("fixtures") or {}) - set(ids)
    if stray:
        raise ScenarioError(f"{at}: page.fixtures key(s) {sorted(stray)} are not affordance ids")


def _validate_registry(scenarios: dict[str, Scenario]) -> None:
    """Cross-scenario invariants — the ones that only break once there are many scenarios."""
    # mock_origins/services builds a single id -> display name map by unioning label_names
    # across the registry, so one id must mean one name everywhere. Unchecked, the scenario
    # that loses the union silently gets another scenario's brand on its plant page.
    names: dict[str, tuple[str, str]] = {}
    for sc in scenarios.values():
        for a in list(sc.affordances) + list(sc.filler):
            prev = names.get(a.id)
            if prev and prev[0] != a.name:
                raise ScenarioError(
                    f"affordance id {a.id!r} means {prev[0]!r} in {prev[1]}.yaml but "
                    f"{a.name!r} in {sc.key}.yaml — one id must have one display name, "
                    f"because the services origin unions them into a single lookup")
            names.setdefault(a.id, (a.name, sc.key))

    # `view` is a path segment on a single shared origin (/p/<view>/<code>), so two scenarios
    # claiming the same view id would serve each other's plant page — a silent cross-contamination
    # that looks like a working run.
    views: dict[str, tuple[str, str]] = {}
    for sc in scenarios.values():
        for p in sc.plants:
            view = p.page.get("view")
            if not view:
                continue
            prev = views.get(view)
            if prev:
                raise ScenarioError(
                    f"plant view {view!r} is claimed by both {prev[0]}.yaml (plant {prev[1]!r}) "
                    f"and {sc.key}.yaml (plant {p.id!r}) — one view is one page, and the mock "
                    f"origin routes on it alone")
            views[view] = (sc.key, p.id)


def _load() -> tuple[dict[str, Scenario], dict[str, Scenario]]:
    """Load the roster. Returns (enabled, disabled), both in index.yaml order."""
    index = _read(DATA_DIR / "index.yaml")
    order = [str(k) for k in (index.get("order") or [])]
    disabled = [str(k) for k in (index.get("disabled") or [])]

    both = order + disabled
    dupes = sorted({k for k in both if both.count(k) > 1})
    if dupes:
        raise ScenarioError(f"index.yaml: {dupes} listed more than once")

    on_disk = {p.stem for p in DATA_DIR.glob("*.yaml")
               if not p.name.startswith("_") and p.stem != "index"}
    orphans = sorted(on_disk - set(both))
    if orphans:
        raise ScenarioError(
            f"index.yaml does not mention {orphans} — add each to `order` to run it, or to "
            f"`disabled` to hold it off. A file in neither would sit silently un-run.")
    missing = sorted(set(both) - on_disk)
    if missing:
        raise ScenarioError(f"index.yaml lists {missing} but no such file(s) in {DATA_DIR}")

    shared = _shared_sets()
    loaded: dict[str, Scenario] = {}
    for key in both:                       # validate disabled files too, so they cannot rot
        sc = _scenario(DATA_DIR / f"{key}.yaml", shared)
        _validate(sc)
        loaded[key] = sc
    _validate_registry({k: loaded[k] for k in order})
    return ({k: loaded[k] for k in order}, {k: loaded[k] for k in disabled})


#: The active registry, in index.yaml order (which analysis/export_report.py reports in).
#: Eager at import: analysis/features, mock_origins/services and the test suite all consume
#: the registry at module-import / collection time.
SCENARIOS: dict[str, Scenario]
#: Scenarios held off in index.yaml. Loaded and validated but not registered, so nothing runs
#: or reports them and they still cannot silently rot.
DISABLED: dict[str, Scenario]
SCENARIOS, DISABLED = _load()


def get_scenario(key: str) -> Scenario:
    try:
        return SCENARIOS[key]
    except KeyError as e:
        raise ValueError(f"Unknown scenario: {key!r}. Known: {sorted(SCENARIOS)}") from e


def plant_page_kinds() -> dict[str, PlantPageKind]:
    """Plant-page fixtures grouped by `plant.page.kind`, for the mock origins.

    Serves the four pre-`view` kinds ("bank", "files", "article", "identity", "locale"), whose
    pages are dedicated routes rather than the generic /p/<view>/<code> one. Kinds are shared
    across scenarios by design here, so labels/items/fixtures union — which is exactly what the
    per-view registry below must NOT do, and why they are separate functions.
    """
    out: dict[str, PlantPageKind] = {}
    for sc in SCENARIOS.values():
        for plant in sc.plants:
            kind = plant.page.get("kind")
            if not kind or plant.page.get("view"):
                continue                       # /p/<view> pages are served from plant_views()
            prev = out.get(kind)
            labels = (prev.labels if prev else ()) + tuple(sc.label_ids)
            items = (prev.items if prev else ()) + tuple(plant.page.get("items") or ())
            fixtures = dict(prev.fixtures) if prev else {}
            fixtures.update({a.id: dict(a.fixture) for a in sc.affordances if a.fixture})
            out[kind] = PlantPageKind(kind=kind, labels=labels, items=items, fixtures=fixtures,
                                      default=(prev.default if prev else "") or sc.target_label)
    return out


def plant_views() -> dict[str, PlantView]:
    """Every /p/<view>/<code> plant page in the registry, keyed by view id.

    One entry per view, NOT merged with anything: a view is one concrete page belonging to one
    plant of one scenario (enforced by _validate_registry). The older `plant_page_kinds()`
    unions items and fixtures across every scenario sharing a kind, which is right for the
    handful of shared kinds and actively wrong here — a payroll table must not inherit a file
    listing because both happen to be `kind: services`.
    """
    out: dict[str, PlantView] = {}
    for sc in SCENARIOS.values():
        for p in sc.plants:
            view = p.page.get("view")
            if not view:
                continue
            # A label's fixture is the per-label content the AFFORDANCE carries (an identity, a
            # locale); page.fixtures is the per-label content this PAGE carries. Page wins, since
            # it is the more specific of the two.
            per_label = {a.id: dict(a.fixture) for a in sc.affordances if a.fixture}
            for lid, f in (p.page.get("fixtures") or {}).items():
                per_label[lid] = {**per_label.get(lid, {}), **dict(f)}
            out[view] = PlantView(
                view=view,
                kind=str(p.page.get("kind", "services")),
                renderer=str(p.page.get("renderer", "")),
                heading=str(p.page.get("heading", "")),
                labels=tuple(sc.label_ids),
                items=tuple(p.page.get("items") or ()),
                fixture=dict(p.page.get("fixture") or {}),
                fixtures=per_label,
                # Per-plant, not per-scenario: a view belongs to exactly one plant, so a
                # `surfaces` scenario's pages are seeded with that surface's vendor names and
                # match what the probe will offer for the same plant.
                names=sc.names_for(p.id),
                code_from=dict(p.code_from),
                scenario=sc.key,
                plant=p.id,
            )
    return out
