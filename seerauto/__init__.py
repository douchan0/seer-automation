"""seerauto - pure-vision automation core for the XIN计划 (SeerGame) client.

Design constraints (hard, see README/CLI ``doctor``):

* no LLM / agent SDK, no network at runtime;
* no dependency on a running game to import or to run the replay pipeline;
* every expensive stage is timed through :mod:`seerauto.perf`.

The public surface is intentionally small.  Import submodules directly
(``from seerauto import vision``) - importing the package itself pulls in
nothing heavy, so a broken ``cv2`` install never hides the ``doctor`` command.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
