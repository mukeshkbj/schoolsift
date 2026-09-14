"""AgentCore deployment-zip launcher.

The AgentCore code deployment zip places this file at the artifact root and
the runtime executes ``python agentcore_entry.py`` (the CloudFormation
EntryPoint property allows at most two argv elements, so ``python -m`` is not
expressible).
"""

from schoolsift.agentcore_app import app

if __name__ == "__main__":
    app.run()
