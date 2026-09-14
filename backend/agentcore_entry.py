"""AgentCore deployment-zip launcher.

The AgentCore code deployment zip places this file at the artifact root and
the Python runtime's EntryPoint names it directly (``["agentcore_entry.py"]``);
``python -m`` is not expressible there.
"""

from schoolsift.agentcore_app import app

if __name__ == "__main__":
    app.run()
