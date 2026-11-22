"""eSCL scanner simulator for Scanbox (mimics an HP ScanJet Pro N4000 snw1).

In-process use::

    from escl_sim import create_app, SimState
    state = SimState()                       # letter_de stack loaded
    app = create_app(state)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim")
"""

from .app import SimulatedConnectionDrop, SimulatedOffline, create_app
from .pages import PageSpec, QrSpec
from .presets import PRESETS
from .state import EsclError, Faults, Job, SimConfig, SimState

__all__ = [
    "PRESETS",
    "EsclError",
    "Faults",
    "Job",
    "PageSpec",
    "QrSpec",
    "SimConfig",
    "SimState",
    "SimulatedConnectionDrop",
    "SimulatedOffline",
    "create_app",
]
__version__ = "0.1.0"
