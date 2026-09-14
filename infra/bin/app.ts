import { App } from "aws-cdk-lib";
import { SchoolSiftStack } from "../lib/schoolsift-stack.ts";

const app = new App();

const csvContext = (key: string): string[] | undefined => {
  const value: unknown = app.node.tryGetContext(key);
  if (typeof value !== "string" || value.length === 0) return undefined;
  return value
    .split(",")
    .map((v) => v.trim())
    .filter((v) => v.length > 0);
};

const stringContext = (key: string): string | undefined => {
  const value: unknown = app.node.tryGetContext(key);
  return typeof value === "string" && value.length > 0 ? value : undefined;
};

new SchoolSiftStack(app, "SchoolSiftStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT ?? "000000000000",
    region: process.env.CDK_DEFAULT_REGION ?? "us-east-1",
  },
  cognitoDomainPrefix: stringContext("cognitoDomainPrefix"),
  callbackUrls: csvContext("callbackUrls"),
  logoutUrls: csvContext("logoutUrls"),
  includeAgentRuntime:
    app.node.tryGetContext("includeAgentRuntime") !== "false",
  bedrockModelId: stringContext("bedrockModelId"),
});

app.synth();
