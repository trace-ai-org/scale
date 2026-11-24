# Catal commons

SCALE's evolving catalysis graph. It starts empty (its steward and project only) and grows one source at a time:
each pull request adds `parts/sources/<slug>.json`, the source's entry in `rights.yaml` and, where it reaches
species, reactions or people the graph already holds, links to them in `parts/shared.json`. Agents will grow it
with spare compute; the skills and the workflow for that come later.

[`literature/`](literature/README.md) holds what catalysis brings to SCALE's general skills: the topic vocabulary
`index-sources` uses and the catalysis source index's tools (the databases themselves stay outside git).

## Adding a source

From the [graph explorer](https://trace-ai-org.github.io/scale/): curate the publication with your own model key,
then **Contribute** on its card. The page downloads the graph with every quote withheld and opens GitHub's upload page
for [`incoming/`](incoming/README.md); drop the file and propose it, and GitHub opens the pull request under your login,
your credit. The commons intake (`.github/workflows/commons-intake.yml`, [`service/scaleweb/intake.py`](../../service/scaleweb/intake.py))
reads it as data, checks that the source is not here already (on main or in an open contribution), and opens the bot's
fold pull request that adds it, crediting you; a person reviews and merges it. With the web service running
([`service/`](../../service/README.md)), the service opens the fold pull request itself. From the command line:

```bash
scale source start --doi DOI --out W        # then curate W/<slug>.graph.json and scale source check W
scale graph contribute W/<slug>.graph.json --to graphs/catal-commons            # already here? what it would add
scale graph contribute W/<slug>.graph.json --to graphs/catal-commons --write    # write the files, then a pull request
```

`scale graph contribute` refuses a source already here: the same DOI, identifier, text digest, or title and year.
It moves the ids into `scale:commons`, leaves out the one-source graph's steward and project, adds the source's
rights entry, caps its quotes under the limits here, and merges its evidence into the shared records it reaches. It
writes only when the commons builds and validates with the source. By hand, the steps are:

1. Curate the source with the `curate-corpus` skill into a scale-catal document (`ns` `scale:commons`: the source is
   `scale:commons/source/<slug>`, its records `scale:commons/<slug>/...`), reusing the ids of the shared records
   already here (`parts/shared.json`).
2. Record its rights in `rights.yaml` (class, licence, basis, text_chars). A source in copyright keeps no quoted
   text: `scale evidence cap <doc> --rights graphs/catal-commons/rights.yaml --output <doc>` withholds it.
3. Add it as parts: `scale graph split <doc> --out <scratch>`, then copy its `sources/<slug>.json` here and merge its
   new shared records into `parts/shared.json`.
4. `scale graph validate graphs/catal-commons` (0 errors) and `scale check`, then open the pull request.

Rules for every graph here: [../README.md](../README.md).
