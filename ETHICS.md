# Ethics

This package is a closed measurement environment, not an attack tool.

- Every website the agent visits is a local mock on `127.0.0.1` (`app.test`, `bank.test`,
  `services.test`). Probe pages are never served publicly.
- All names, accounts, balances, organisations, providers and records are invented. No real
  person or service is involved.
- The only outbound traffic is the call to the language model that runs the agent.
- The "victim" is an agent we run, holding a secret we choose. Results are reported only as
  aggregates over many sessions.
- No prompt injection or other instruction of the agent by the probe page.

Six scenarios concern sensitive categories (GDPR Art. 9 / AgentDAM): `health_service`,
`sensitive_access`, `affiliation`, `stigma_platform`, `credit_standing`, `life_event`. They are
included because these are where the documented harms lie. Their categories are legal and adult,
pages show only account-style records with no explicit content, and every organisation is
fictional.
