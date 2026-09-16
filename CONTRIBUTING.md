# Contributing

Thanks for your interest in `llm-relay`. This project is intentionally small
and dependency-free, and contributions are expected to keep it that way.

## Zero dependencies is a hard constraint

Do **not** introduce any pip dependency, including for tests. Use the Python
standard library only. There is no requirement file, no virtualenv step, and no
packaging step: everything runs from a plain Python 3 interpreter.

## Getting started

```sh
# 1. Generate a neutral local config (writes config.json)
python3 llm_relay.py --init

# 2. Put the provider keys you actually use into keys.env (chmod 600 keys.env)

# 3. Run the relay
python3 llm_relay.py
```

After editing the configuration, validate it:

```sh
python3 llm_relay.py --check
```

(`--check` validates your `config.json` and prints the candidate order it would use; add
`--config config.example.json` to validate the shipped example instead.)

## Testing requirements (merge gate)

- `python3 -m unittest test_relay` must pass in full, and the number of tests
  may only grow — never shrink. A change that fixes a bug should add a test
  that would have caught it.
- The suite is dependency-free and runs in CI.
- If you change the dashboard or its frontend, also run
  `dev/dashboard_smoke_test.py` on your machine. It needs a live service and a
  headless browser, so it is **not** part of CI. Note that it reports its
  result as `OK` / `FAILED` text, not through its exit code — read the output.

## Adding a provider

1. Add an entry to `providers[]` in `config.json` with:

   - `name`, `base_url`, `wire`
   - `rpm`, `max_concurrency`, `cooldown_backoff`
   - `models[]`, where each model may carry `caps`, `free`, and `chain`

2. Point `key_env` at the variable name that holds the key inside `keys.env`.
3. The field-by-field reference lives in `docs/providers.md`.
4. Routing and caller configuration (`routes` / `callers`) are described in the
   README.

## Commit conventions

- Keep commits small and self-contained, and include tests with the change they
  cover.
- **Never commit** `config.json`, `keys.env`, or `access-key.txt`. `.gitignore`
  blocks them, and CI adds a dedicated credential guard on top.
- Documentation and user-facing text are written in English. Code comments may
  be in Chinese, matching the existing style. Comments should explain *why*
  something is done, not restate what the code already says.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License (see `LICENSE`).
