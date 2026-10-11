"""Degrade-patch for JupyterLab's PyPI extension manager against PyPI's XML-RPC removal.

PyPI removed the deprecated XML-RPC ``browse`` method after it was abused and
triggered outages (https://status.python.org/incidents/y3pt84s54b50). JupyterLab
4.6.x's PyPI extension manager lists prebuilt extensions exclusively through
that method, so the whole Extensions panel 500s with a masked error:

    UnboundLocalError: cannot access local variable 'data' where it is not
    associated with a value   (jupyterlab/extensions/pypi.py __throttleRequest)

Upstream tracks the real rewrite in jupyterlab/jupyterlab#19932; until a
release ships, this script patches the *installed* package in the image:

1. ``__throttleRequest``: re-raise non-throttling XMLRPC faults (same
   semantics as jupyterlab main) instead of falling through to an unbound
   ``data`` local.
2. ``__get_all_extensions``: catch the browse fault, log a clear warning, and
   serve an empty listing. The panel loads, installed-extension management and
   install-by-exact-name still work; Discover search returns only language
   packs until upstream picks a replacement data source (BigQuery or a
   moderated listing).
3. ``list_packages``: report ``None`` (no pagination) for an empty result, so
   the UI treats a degraded search exactly like the readonly manager does.

Every replacement is an exact-text match. If the installed jupyterlab already
contains this patch, or upstream shipped changed code, the affected hunk is
logged and skipped instead of failing the build.
"""

from __future__ import annotations

import sys
from importlib.metadata import distribution
from pathlib import Path

PATCHES: list[tuple[str, str, str]] = [
    (
        "throttle-honest-raise",
        """                else:
                    data = await current_loop.run_in_executor(None, fn, *args)

        return data""",
        """                else:
                    data = await current_loop.run_in_executor(None, fn, *args)
            else:
                raise

        return data""",
    ),
    (
        "browse-graceful-degrade",
        """            self.__all_packages_cache = await self.__throttleRequest(
                True,
                self._rpc_client.browse,
                ["Framework :: Jupyter :: JupyterLab :: Extensions :: Prebuilt"],
            )""",
        """            try:
                self.__all_packages_cache = await self.__throttleRequest(
                    True,
                    self._rpc_client.browse,
                    ["Framework :: Jupyter :: JupyterLab :: Extensions :: Prebuilt"],
                )
            except xmlrpc.client.Fault as err:
                # PyPI removed the deprecated XML-RPC browse method; the
                # upstream rewrite is tracked at jupyterlab/jupyterlab#19932.
                # Degrade instead of 500-ing the whole Extensions panel.
                self.log.warning(
                    "PyPI XML-RPC browse unavailable (%s). Extension search is "
                    "disabled; install extensions by exact package name from "
                    "the Install tab or with pip.",
                    err,
                )
                self.__all_packages_cache = []""",
    ),
    (
        "empty-search-no-pagination",
        """        total_pages = math.ceil(len(sorted_matches) / per_page)

        return extensions, total_pages""",
        """        total_pages = math.ceil(len(sorted_matches) / per_page)

        return extensions, total_pages or None""",
    ),
]


def locate_pypi_py() -> Path | None:
    """Return the installed jupyterlab/extensions/pypi.py path, if any."""
    try:
        dist = distribution("jupyterlab")
    except Exception:
        return None
    for file in dist.files or []:
        if file and str(file).endswith("extensions/pypi.py"):
            path = Path(dist.locate_file(file))
            if path.is_file():
                return path
    return None


def main() -> int:
    pypi_py = locate_pypi_py()
    if pypi_py is None:
        print("patch_jupyterlab_pypi: jupyterlab/extensions/pypi.py not found; nothing to do")
        return 0

    text = pypi_py.read_text(encoding="utf-8")
    changed = False
    for name, old, new in PATCHES:
        if new in text:
            print(f"patch_jupyterlab_pypi: {name}: already applied")
        elif old in text:
            text = text.replace(old, new, 1)
            changed = True
            print(f"patch_jupyterlab_pypi: {name}: patched")
        else:
            print(
                f"patch_jupyterlab_pypi: {name}: pattern not found; skipping "
                "(installed jupyterlab differs from 4.6.4)"
            )

    if changed:
        pypi_py.write_text(text, encoding="utf-8")
        print(f"patch_jupyterlab_pypi: wrote {pypi_py}")
    return 0


if __name__ == "__main__":
    sys.exit(main())