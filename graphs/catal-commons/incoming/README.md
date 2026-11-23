# Contributions waiting to be folded in

A graph curated in the [graph explorer](https://trace-ai-org.github.io/scale/) is contributed here, with every quote
of a source in copyright withheld. Its *Contribute* button opens the pull request for you, the graph attached, under
your GitHub login (your credit on the leaderboard), once you connect GitHub with a token that has the `public_repo`
scope. By hand instead: download the graph from the explorer, drop it on GitHub's upload page for this folder, then
**Propose changes** and **Create pull request**.

The commons intake (`.github/workflows/commons-intake.yml`, `service/scaleweb/intake.py`) reads the file as data,
folds it into catal-commons with `scale graph contribute` (the source taken as in copyright: no quote kept), opens the
fold pull request crediting you, and closes yours. A person reviews and merges the fold pull request. Nothing stays in
this folder.
