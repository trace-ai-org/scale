# Contributions waiting to be folded in

A graph curated in the [graph explorer](https://trace-ai-org.github.io/scale/) is contributed here: its *Contribute*
button downloads the graph, with every quote of a source in copyright withheld, and opens GitHub's upload page for
this folder. Drop the file there, then **Propose changes** and **Create pull request**: GitHub opens the pull request
under your login, which is your credit on the leaderboard.

The commons intake (`.github/workflows/commons-intake.yml`, `service/scaleweb/intake.py`) reads the file as data,
folds it into catal-commons with `scale graph contribute` (the source taken as in copyright: no quote kept), opens the
fold pull request crediting you, and closes yours. A person reviews and merges the fold pull request. Nothing stays in
this folder.
