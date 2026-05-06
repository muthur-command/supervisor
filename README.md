# Muthur Command Supervisor

## Private-cloud style stack for home automation

Muthur Command Supervisor is a container-based system for managing the **Core** application
stack (Home Assistant–compatible) and related add-ons. The UI or automation layer
talks to Supervisor; Supervisor exposes an API for the installation (network, add-ons,
backups, OS integration, and more).

## Installation

Installation instructions: https://www.muthur-command.com/getting-started

## Development

For small changes and bugfixes you can follow the repository guidelines; for
significant changes, open an RFC first. Development instructions: [development][development].

## Release

Releases use three channels:

1. Pull requests merge to the default branch (`main` or `mc`, per fork policy).
2. A new build is pushed to the `dev` channel.
3. Releases are published.
4. A new build is pushed to the `beta` channel.
5. The [`stable.json`][stable] file is updated (Muthur Command OS `version` feed).
6. The build from `beta` is promoted to `stable`.

[development]: https://www.muthur-command.com/docs/supervisor/development
[stable]: https://github.com/muthur-command/version/blob/master/stable.json

## Breaking changes (recent rename train)

The following must move in lockstep with **cli**, **version**, **plugin-dns**, **docker**/mc_bd images, and optional **operating-system** releases:

- HTTP routes under **`/mc_bd/*`** and **`/muthurcommand/*`** for the application slot (legacy **`/homeassistant/*`** and **`/core/*`** are not part of this tree’s contract).
- Headers **`X-Mcio-Key`** and request context **`MCIO_FROM`** (no **`X-Hassio-Key`** / **`HASSIO_FROM`**).
- On-disk paths **`muthurcommand.json`**, data directory **`/data/muthurcommand`**.
- Environment **`MUTHURCOMMAND_REPOSITORY`**; **`version`** JSON keys **`muthurcommand`**, **`mcos`** / **`mcos_upgrade`**; add-on manifest keys **`mcio_api`**, **`mcio_role`**, **`muthurcommand_api`**, **`muthurcommand`** version pin.
- DNS search suffix **`local.mcio`**.

## Origin

- **Upstream:** [home-assistant/supervisor](https://github.com/home-assistant/supervisor) — Home Assistant Supervisor, from which this tree was ported.
- **In this repo:** **Muthur Command** keeps this fork for **Muthur Command OS**; behavior may diverge from upstream over time.
- **License:** Code inherited from upstream remains **Apache-2.0**; see **LICENSE** (retain upstream copyright / NOTICE where required).
