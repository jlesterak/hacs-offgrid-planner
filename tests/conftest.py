import sys
from pathlib import Path

# Import the core as a top-level package so tests don't need Home Assistant installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "offgrid_planner"))
