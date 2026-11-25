# Working with private data

SCALE is used on data that is not public: a project's experiments, its calculations, results not yet published. This
page says where such data lives, what keeps it there and what does not. The rules come from
[governance/licensing.md](../governance/licensing.md#4-project-data-and-access-levels): everything this repository commits is public,
explicitly, and project data stays in a data repository of its own until its partners clear it.

## The model in one paragraph

Each repository has **readers**: everyone who can read it, its remotes and its backups. This framework repository's
readers are the public. A data repository's readers are what its policy says (a project, an institution, a
consortium). **Every reader of a repository must be allowed to read every record in it**: the repository's readers
are a subset of each record's permitted readers, as the record's stewardship states them and `scale graph filter`
reads them. Public data in a project repository is fine; data of one institution only, or of another project, in a
project repository is not. The same holds for what leaves the machine: a payload drawn from a document goes to a
model endpoint only when that endpoint is approved for every access level the document carries.

## A data repository

```bash
scale data init ~/data/my-project --project lab:project/my-project
```

makes a git repository outside any other (it refuses a folder inside a git repository, above all this one), readable
by its owner only (mode 0700), with no remote, holding:

| file | what it is |
|---|---|
| `scale-data.yaml` | the policy: `repository.readers` (stewardship keys `access`, `project`, `contributed_by`), `repository.remotes` (none), `repository.raw_data` (none), `repository.copyright_cap` (true), `defaults.stewardship` (the stewardship a new document states; `access: project` by default) and `egress.endpoints` (none). Unknown keys are refused, so a misspelt key never loosens it. |
| `rights.yaml` | the rights of the sources the documents quote, as in this repository ([curation.md](curation.md)) |
| `.gitignore` | `raw/`, `index/`, `exports/`, `cache/`, `work/` and evidence indexes stay on the computer |
| `README.md` | the model, for whoever opens the repository |
| `graphs/` | the scale-catal documents |

and installs the data guard's hooks (below). Raw data (instrument files, simulation output, spreadsheets) stays in
`raw/`; a document cites it by a Dataset record (its `uri` and `sha256`), never by copying it. `scale kg update`
moves the repository's documents to a new schema version when its maintainers decide to.

`--access institution --institution <id>` or `--access consortium` declare other readers. A remote is added only
once its readers are checked to be the repository's readers, and listed in `repository.remotes`: the pre-push hook
refuses any other destination.

## The data guard

```bash
scale guard install            # in a clone of this repository (scale data init installs it in a data repository)
```

writes two git hooks. Each runs `scale guard check`, which you can also run by hand (`--json` for a machine report).

| hook | what it inspects |
|---|---|
| pre-commit (`scale guard check --staged`) | the snapshot the commit records: the git index's blobs, not the working tree (a file changed after `git add` is judged as it was added) |
| pre-push (`scale guard check --push REMOTE [RANGE]`) | every commit the push sends that the remote does not hold, each on its own snapshot, so a private file added in one commit and deleted in a later one is found; in a data repository, the destination against `repository.remotes` |
| by hand (`scale guard check --all [--rev REV]`) | every tracked file of the index, or of a commit; `scale check` runs it on this repository |

What a snapshot may hold:

- **Documents every reader may read.** Every scale-catal document (JSON, or YAML naming the schema) and every graph
  kept as parts, rebuilt from its `parts/` blobs. In this repository each is explicitly public, header and records
  (`scale evidence access`'s rule; a missing access reads as institution, an embargo is not public). In a data
  repository the audience rule decides: a record fails when some least-privileged reader of the repository may not
  see it (an embargo in force admits its institutions only; a lapsed one, its `access_after_embargo`).
- **Well-formed documents.** A file that names scale-catal and is not JSON, a document without node and link lists,
  or parts that do not build (`scale graph build`'s rules) cannot be judged, so it is refused.
- **Quotes within the copyright cap** of the nearest `rights.yaml` (`scale evidence report`'s rule; a data
  repository may set `copyright_cap: false`).
- **No undeclared raw data.** A file named as a table, array, database, archive, record dump, document or instrument
  or simulation output (`.csv`, `.xlsx`, `.parquet`, `.h5`, `.npy`, `.sqlite`, `.zip`, `.tar.gz`, `.jsonl`,
  `.pdf`, `.cif`, `OUTCAR` ...), whose first bytes say so whatever its name (an archive renamed `notes.md`), a text
  file laid out as a table, or any file over 5 MiB, unless a reviewed pattern allows it:
  [governance/public-fixtures.yaml](../governance/public-fixtures.yaml) here, `repository.raw_data` in a data
  repository. A `scale-data.yaml` in this repository is itself refused.

A repository with neither this repository's schema nor a `scale-data.yaml` is held to the public rules.

## Egress: what a model may receive

Asking a hosted model about records sends them to its provider. The egress gate decides before anything leaves:

- a payload's **levels** are the access levels its document's header and records grant today (`pgraph.openness`:
  a missing or malformed access is institution, an embargo in force institution);
- a **public** payload may go to any endpoint; a payload with any other level only to an endpoint the policy lists
  for each of those levels; **anything else is denied**, and the error says so without sending anything.

The policy is the `egress` section of the data repository's `scale-data.yaml`, or a file named by
`$SCALE_EGRESS_POLICY` holding the same section:

```yaml
egress:
  endpoints:
  - id: local
    url: http://localhost:11434
    access: [project]
```

Where the gate stands:

| path | endpoint name | without approval |
|---|---|---|
| `scale mcp`, `scale serve`, `scale call` (the agent tools) | `$SCALE_EGRESS_ENDPOINT` (else `unknown`) | a tool that reads a document that is not all public (inline, by path, a curation folder's graph) returns `egress denied` instead of its result |
| a `ScaleToolkit(egress=EgressGate(...))` in Python | the gate's | the same; a toolkit without a gate leaves the results to its caller |
| the SCALE service's `/ask` (`service/scaleweb`) | `$SCALE_WEB_EGRESS_ENDPOINT` (`anthropic`), approved by `$SCALE_WEB_EGRESS_POLICY` | 403 before any model or Claude Code runs; the question loop checks again before each batch of tool results |
| the graph explorer: a question with the reader's own key, a question to the service | the provider (`provider:anthropic`, `provider:openai`, `provider:openrouter`) or `service` | the page shows why and sends nothing; the reader may approve that destination for those levels, in this page only (a reload denies again); the gate runs before every request of the loop |
| the explorer's Contribute, `scale graph contribute` | catal-commons, public | refused: only a public graph goes to the commons |

The endpoint's name is whatever the process is told: the gate trusts it. It catches mistakes, not a process that lies
about where it sends.

## What this does not do

- **Hooks are bypassable.** `git commit --no-verify` and `git push --no-verify` skip them; a fresh clone has none
  until `scale guard install` runs in it; another tool may write git objects without running hooks. CI's checks
  (`scale evidence access`, `scale guard check --all`) run after a push has already sent the data.
- **Isolation is the boundary.** Process private data where there is no unrestricted network and no credentials for
  public repositories. Forks, pull-request artifacts and provider caches keep what was sent after it is deleted.
- **Content outside scale-catal documents is not read.** A private value pasted into Markdown, code or a YAML file
  that is not a document passes; so does a table in an unrecognised format under 5 MiB. Raw-data detection is by
  name, first bytes and layout, not by meaning.
- **`.gitignore`, checksums and the copyright cap are not confidentiality.** An ignored file is still on disk,
  readable by whatever runs there; a sha256 of a short value can be guessed.
- **Not every path to a model is gated.** A shell an agent runs (`cat`, `jq`, `scale graph query` itself) prints
  whatever it reads into the agent's context; curation in the explorer sends the PDF's text, not a graph's records;
  logs, browser storage, caches, exports and backups take the restrictions of what they hold only if you keep them so.
- **The service trusts its own configuration.** Its egress policy names endpoints; it does not authenticate them.
