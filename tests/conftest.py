import sys
from pathlib import Path

# Make `netpulse` importable regardless of where pytest is invoked from,
# without requiring an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
