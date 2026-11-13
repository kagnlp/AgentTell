"""Ground-truth state planting."""
from __future__ import annotations

from agents.base import SessionPlan, Step
from harness.opaque import code_for
from harness.scenarios import Plant, Scenario, get_scenario
from harness.session import probe_url
from orchestrator.config import BANK_BASE_URL, SERVICES_BASE_URL

_BASES = {
    "BANK_BASE_URL": BANK_BASE_URL,
    "SERVICES_BASE_URL": SERVICES_BASE_URL,
}


def _plant_step(sc: Scenario, condition: str, plant: Plant) -> Step:
    """The one pre-visit step that plants `condition`'s secret at the prior origin."""
    url = plant.url_for(_BASES, code=code_for(condition))
    task = plant.task
    if "{name}" in task:
        task = task.format(name=sc.label_names.get(condition, ""))
    return Step(label=plant.step_label, start_url=url, task=task)


def build_plan(session_id: str, condition: str, variant: str = "A",
               scenario: str = "sso_wall", plant_id: str = "") -> SessionPlan:
    """One matrix cell's plan."""
    sc = get_scenario(scenario)

    plant = sc.get_plant(plant_id)
    purl = probe_url(session_id, variant, scenario=scenario,
                     plant=plant.id if sc.surfaces else "")
    probe = Step(label="visit_probe", start_url=purl, task=sc.probe_task)
    if condition == "none":
        steps = [probe]
    elif condition in sc.label_ids:
        steps = [_plant_step(sc, condition, plant), probe]
    else:
        raise ValueError(f"Unknown condition for {scenario}: {condition}")
    return SessionPlan(session_id=session_id, condition=condition, steps=steps,
                       meta={"variant": variant, "scenario": scenario, "probe_url": purl,
                             "plant": plant.id})
