import os


# Tests must never open or migrate the operator's configured production database.
os.environ["GOLD_AGENT_DB"] = ":memory:"
