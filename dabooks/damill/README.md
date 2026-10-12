# Damill — Papermill Run Area

`damill` is the local staging area for **papermill** executions launched from
the [`dabooks/`](..) Jupyter work directory. It is intentionally separate from
the source notebooks: use it for parameterized runs, multi-cell experiments,
exported artifacts, and other output that should not clutter the repository's
reviewable notebook set.

The name is a small convention, not a second notebook system:

```text
dabooks/
├── <source notebook>.ipynb   # authored, reviewable notebook
└── damill/
    ├── <run>.ipynb            # papermill's executed notebook
    └── <artifacts>/           # files emitted by cells
```

## Git and data policy

Everything in this directory is ignored by default except this README.
Papermill outputs can contain cell results, downloaded files, credentials,
tokens, hostnames, screenshots, and other target data, so inspect them before
sharing or copying them elsewhere.

- Do not put real engagement data in the repository. Keep it in this local
  run area only when the engagement rules permit it.
- Lab data is the deliberate exception to the repository's normal target-data
  policy, but it should still be kept out of commits unless it is useful as a
  small, sanitized fixture.
- The ignore rule is a safety net, not a sanitization step. Do not use
  `git add -f` on a run output without reviewing its contents.
- Keep authored notebooks in `dabooks/`; keep generated notebooks and large
  or sensitive outputs here.

## Running a notebook

Papermill preserves the input notebook and writes an executed copy to the
output path. From the repository root, a typical run is:

```bash
papermill \
  dabooks/chopshop.ipynb \
  dabooks/damill/chopshop_<label>.ipynb \
  -p target 192.0.2.10
```

Use `-f parameters.yaml` when a run has more than a few parameters. Prefer
explicit output names that identify the notebook and lab/engagement context;
avoid putting secrets in filenames.

Inside the Jupyter container, the same directory is available below the
notebook work root:

```bash
docker exec dockered-jupyter-1 \
  papermill \
  /home/jovyan/work/chopshop.ipynb \
  /home/jovyan/work/damill/chopshop_<label>.ipynb \
  -p target 192.0.2.10
```

The container's default kernel is
`daharness (framework tools)`. Framework tools exposed in notebook cells are
async wrappers, so call them with `await`; otherwise the cell only creates a
coroutine and the tool does not run. Plain Python kernels remain appropriate
for notebooks that do not use framework tools.

## Widgets and multi-cell runs

`ipywidgets` is available in the Jupyter image for interactive parameter
selection and progress display. For unattended or repeatable runs, make the
parameters explicit in the notebook or pass them with papermill instead of
depending on a widget's current browser state.

Keep setup, execution, and reporting in separate cells. A useful pattern is:

1. define or load parameters;
2. establish the approved scope and session state;
3. run the gated/tool cells;
4. normalize the results into a small report or artifact;
5. save only the generated output under `damill/`.

Papermill executes cells in order. If a run fails, the output notebook is
still useful for debugging: inspect the first failing cell and rerun from a
clean output path after correcting the source notebook or parameters.

## Housekeeping

- Use a new output filename for meaningful reruns; do not overwrite evidence
  you may need to compare.
- Delete stale local outputs when they are no longer needed.
- Clear outputs in source notebooks before committing them.
- Before sharing a run notebook, review outputs, widget state, embedded
  images, and metadata for sensitive data.

For the Jupyter container, mount and kernel details, see the parent
[`dabooks/README.md`](../README.md).