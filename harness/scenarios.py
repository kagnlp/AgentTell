"""Scenario registry — the single source of truth for each side-channel scenario."""
from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    """A scenario data file is malformed or violates a registry invariant."""


_BaseLoader: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _Loader(_BaseLoader):
    """A SafeLoader that REJECTS duplicate mapping keys."""

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

URL_PLACEHOLDERS = frozenset({"code", "BANK_BASE_URL", "SERVICES_BASE_URL"})

_OPTION_KEYS = frozenset({"id", "name", "cta", "blurb", "fixture"})
_PLANT_KEYS = frozenset({"id", "step_label", "url", "task", "page"})
_PAGE_KEYS = frozenset({"kind", "view", "renderer", "heading", "items", "fixture", "fixtures"})
_PROBE_KEYS = frozenset({"template", "task"})
_SCENARIO_KEYS = frozenset({
    "key", "secret_class", "title", "lede", "role", "completion", "general_completion",
    "affordances", "filler", "probe", "plants", "surfaces",
})

RENDERERS = ("record", "table", "list_action", "article_list", "form")


@dataclass(frozen=True)
class Affordance:
    """One neutral choice shown on the probe (a payment button / card / link)."""
    id: str
    name: str
    cta: str = "Continue →"
    blurb: str = ""
    fixture: Mapping[str, str] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Plant:
    """How to plant this scenario's secret at a prior origin, declaratively."""
    id: str = "default"
    step_label: str = ""
    url: str = ""
    task: str = ""
    page: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def url_for(self, bases: Mapping[str, str], code: str = "") -> str:
        """Resolve `url` for one condition's plant code."""
        return self.url.format(code=code, **bases)


@dataclass(frozen=True)
class Scenario:
    key: str
    secret_class: int
    title: str
    lede: str
    affordances: tuple[Affordance, ...]
    probe_template: str
    plants: tuple[Plant, ...]
    filler: tuple[Affordance, ...] = ()
    probe_task: str = ""
    role: str = ""
    completion: str = ""
    general_completion: str = ""
    surfaces: Mapping[str, Mapping[str, str]] = field(default_factory=dict, compare=False)
    cta_template: str = ""

    @property
    def label_ids(self) -> list[str]:
        return [a.id for a in self.affordances]

    @property
    def label_names(self) -> dict[str, str]:
        return {a.id: a.name for a in self.affordances}

    @property
    def conditions(self) -> tuple[str, ...]:
        """Every held value plus the cold condition 'none'."""
        return (*self.label_ids, "none")

    def names_for(self, plant_id: str = "") -> dict[str, str]:
        """Display names for this scenario's labels, as seen on `plant_id`'s surface."""
        return dict(self.surfaces.get(plant_id) or self.label_names)

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
        return condition if condition in self.label_ids else "none"


@dataclass(frozen=True)
class PlantView:
    """One concrete plant page, served at /p/<view>/<code> by the origin named in `kind`."""
    view: str
    kind: str
    renderer: str
    labels: tuple[str, ...]
    heading: str = ""
    items: tuple[str, ...] = ()
    fixture: Mapping[str, Any] = field(default_factory=dict)
    fixtures: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    names: Mapping[str, str] = field(default_factory=dict)
    scenario: str = ""
    plant: str = ""

    def content(self, label: str) -> dict[str, Any]:
        """The merged field values this page shows for `label`."""
        return {"name": self.names.get(label, label), **dict(self.fixture),
                **dict(self.fixtures.get(label) or {})}


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            data = yaml.load(fh, Loader=_Loader)  # noqa: S506  _Loader subclasses SafeLoader
    except ScenarioError as e:
        raise ScenarioError(f"{path.name}: {e}") from None
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
    """Parse an `affordances` / `filler` block into Affordances."""
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
        return tuple(_option(dict(o), path_name, spec.get("cta_template", ""))
                     for o in shared[name])

    cta_template = spec.get("cta_template", "")
    opts = spec.get("options")
    if opts is None:
        raise ScenarioError(f"{path_name}: `{what}` mapping needs `from` or `options`")
    if isinstance(opts, dict):
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
        page=page,
    )


def _plants(raw: dict, path_name: str) -> tuple[Plant, ...]:
    """Parse the `plants:` list."""
    many = raw.get("plants")
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
        probe_template=str(probe.get("template", "")),
        probe_task=str(probe.get("task", "")),
        plants=_plants(raw, name),
        role=str(raw.get("role", "")),
        completion=str(raw.get("completion", "")),
        general_completion=str(raw.get("general_completion", "")),
        surfaces=surfaces,
        cta_template=str(cta_template),
    )


def _validate(sc: Scenario) -> None:
    """Enforce the invariants the registry has always relied on but never checked."""
    where = f"{sc.key}.yaml"
    ids = sc.label_ids

    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ScenarioError(f"{where}: duplicate affordance id(s) {dupes}")
    clash = set(ids) & {a.id for a in sc.filler}
    if clash:
        raise ScenarioError(f"{where}: id(s) {sorted(clash)} appear in both `affordances` and "
                            f"`filler` — a neutral option must not double as a secret label")
    if "none" in ids:
        raise ScenarioError(f"{where}: 'none' is reserved for the cold condition")

    if not sc.probe_template:
        raise ScenarioError(f"{where}: `probe.template` is required")
    if not (_TEMPLATE_DIR / sc.probe_template).exists():
        raise ScenarioError(f"{where}: probe template {sc.probe_template!r} does not exist in "
                            f"{_TEMPLATE_DIR}")

    for p in sc.plants:
        _validate_plant(sc, p, where)

    _validate_surfaces(sc, where)


def _validate_surfaces(sc: Scenario, where: str) -> None:
    """A `surfaces` block must cover EVERY plant and EVERY label, exactly."""
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
    if not sc.cta_template:
        raise ScenarioError(f"{where}: a `surfaces` scenario needs `affordances.cta_template`, "
                            f"so the probe can render each option's copy per surface")


def _validate_plant(sc: Scenario, p: Plant, where: str) -> None:
    ids = sc.label_ids
    at = f"{where}: plant {p.id!r}"
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
        raise ScenarioError(f"{at}: page.view is {view!r} but url {p.url!r} does not route "
                            f"through /{view}/")
    stray = set(p.page.get("fixtures") or {}) - set(ids)
    if stray:
        raise ScenarioError(f"{at}: page.fixtures key(s) {sorted(stray)} are not affordance ids")


def _validate_registry(scenarios: dict[str, Scenario]) -> None:
    """Cross-scenario invariants — the ones that only break once there are many scenarios."""
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


def _load() -> dict[str, Scenario]:
    """Load the roster."""
    index = _read(DATA_DIR / "index.yaml")
    order = [str(k) for k in (index.get("order") or [])]

    dupes = sorted({k for k in order if order.count(k) > 1})
    if dupes:
        raise ScenarioError(f"index.yaml: {dupes} listed more than once")

    on_disk = {p.stem for p in DATA_DIR.glob("*.yaml")
               if not p.name.startswith("_") and p.stem != "index"}
    orphans = sorted(on_disk - set(order))
    if orphans:
        raise ScenarioError(f"index.yaml does not list {orphans}")
    missing = sorted(set(order) - on_disk)
    if missing:
        raise ScenarioError(f"index.yaml lists {missing} but no such file(s) in {DATA_DIR}")

    shared = _shared_sets()
    loaded: dict[str, Scenario] = {}
    for key in order:
        sc = _scenario(DATA_DIR / f"{key}.yaml", shared)
        _validate(sc)
        loaded[key] = sc
    _validate_registry(loaded)
    return loaded


SCENARIOS: dict[str, Scenario] = _load()


def get_scenario(key: str) -> Scenario:
    try:
        return SCENARIOS[key]
    except KeyError as e:
        raise ValueError(f"Unknown scenario: {key!r}. Known: {sorted(SCENARIOS)}") from e


def plant_views() -> dict[str, PlantView]:
    """Every /p/<view>/<code> plant page in the registry, keyed by view id."""
    out: dict[str, PlantView] = {}
    for sc in SCENARIOS.values():
        for p in sc.plants:
            view = p.page.get("view")
            if not view:
                continue
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
                names=sc.names_for(p.id),
                scenario=sc.key,
                plant=p.id,
            )
    return out
