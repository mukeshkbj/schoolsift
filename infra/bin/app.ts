import { App } from "aws-cdk-lib";
import { SchoolSiftStack } from "../lib/schoolsift-stack.ts";

const app = new App();

new SchoolSiftStack(app, "SchoolSiftStack", {
  env: { account: "000000000000", region: "us-east-1" },
});

app.synth();
