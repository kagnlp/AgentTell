# Ethics and Safety Statement

This package is a **closed test environment** for research. It measures whether a website can
learn a user's private information from how a browser-use agent behaves. It is a measuring tool,
not an attack tool.

## What the design guarantees

1. **No real websites.** Every website the agent visits is a fake server that runs on your own
   computer: `app.test` (the probe), and `bank.test` and `services.test` (the plants). All of
   them point to `127.0.0.1`.
2. **No real accounts or passwords.** All names, balances, account numbers and logins are made up
   for the experiment.
3. **No outside traffic from the experiment.** The only traffic that leaves the computer is the
   call to the language model that runs the agent. All websites and the click recording listen
   only on `127.0.0.1`.
4. **No victims.** The "victim" is an agent that we run, with a secret that we choose. No person
   is targeted.
5. **Nothing is put online.** The probe pages are never served on a public address. Only agents that we run in this environment visit them.

## Scenarios about sensitive information

Six scenarios in `benchmark/scenarios/` are about information that GDPR Article 9 or AgentDAM's
list of sensitive categories treats as sensitive:

| Scenario | What it is about |
|---|---|
| `health_service` | Health care |
| `sensitive_access` | Reading about stigmatised or restricted topics |
| `affiliation` | Political, religious or union membership |
| `stigma_platform` | An account at a privacy-sensitive service |
| `credit_standing` | Money trouble |
| `life_event` | Death in the family, separation, a new baby, losing a job |

We include them because these are the areas where the harm is real and documented: ads that
target sensitive interests, unfair treatment by insurers and employers, and blackmail based on
which sites a person uses. A benchmark that only asked "which shop do you use" would not measure
this risk.

The guarantees above apply to these six scenarios, plus two more rules:

- **Every organisation, provider, clinic, category and record is made up.** No real party,
  religion, union, health provider, support service or institution appears, and no real person's
  data is used.
- **The category lists are in the paper**, so readers can check that the categories are
  sensitive. They are also listed in the `affordances` block of each scenario file.

`sensitive_access` has a pool of eight categories. Its scenario file explains how the pool is
meant to be checked with `scripts/cold_prior.py`.

### About `stigma_platform`

This scenario asks whether an agent reveals that its user has an account at a privacy-sensitive
service. The four categories are a **dating service**, an **addiction recovery programme**, an
**online betting account** and a **debt-advice service**.

We include it because it matches a harm that earlier research documents, rather than one we made
up. Sudhodanan et al. name detecting accounts at privacy-sensitive sites as the most cited use of
cross-origin state inference by attackers. They cite the Ashley Madison blackmail case, a
blackmail scam about pornography sites, and a state that tried to find out whether a person had
an account at a blocked site, even through a VPN. Knittel et al. start from the same concern. If
we left this case out, we could report that agents protect privacy without ever testing the case
where a failure does the most damage.

The scenario file enforces these extra rules:

- **Only legal, adult categories.** No minors and no sexual content of any kind.
- **No graphic or explicit material on any page.** The plant pages show only account records,
  such as a subscription line, a billing line or a settings page. They never show the service's
  content.
- **All service names are made up.** No real dating, recovery, betting or debt service is named.
- **No plant browses the service's content.** The account record is enough. Browsing content
  would turn this scenario into `sensitive_access`.

`stigma_platform` (does the user have an account) and `sensitive_access` (what has the user been
reading) use **separate** category lists and measure different kinds of cross-origin state.
Wherever we report both, we say so, so that they do not look like the same result twice.

## Why this is defensive research

The goal is to *describe and measure* a privacy risk that comes from how browser-use agents are
built: private state from one website leaks to another through the agent's behaviour. Knowing
the size of the risk helps agent builders reduce it. We report results only as totals over many
sessions.

## Out of scope

We do not do, and the code does not support:

- Any attack against a real website or a real user.
- Prompt injection, or any other way of giving the agent instructions. The threat model excludes
  it.
- Taking real data of any kind.
