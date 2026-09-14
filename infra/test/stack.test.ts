import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import {
  SchoolSiftStack,
  type SchoolSiftStackProps,
} from "../lib/schoolsift-stack.ts";

function template(props: SchoolSiftStackProps = {}): Template {
  const app = new App();
  const stack = new SchoolSiftStack(app, "TestStack", {
    env: { account: "000000000000", region: "us-east-1" },
    ...props,
  });
  return Template.fromStack(stack);
}

describe("SchoolSiftStack", () => {
  it("creates a customer-managed KMS key with rotation", () => {
    template().hasResourceProperties("AWS::KMS::Key", {
      EnableKeyRotation: true,
    });
  });

  it("creates a private versioned KMS-encrypted content bucket", () => {
    const t = template();
    t.hasResourceProperties("AWS::S3::Bucket", {
      VersioningConfiguration: { Status: "Enabled" },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: Match.objectLike({
        ServerSideEncryptionConfiguration: [
          Match.objectLike({
            ServerSideEncryptionByDefault: { SSEAlgorithm: "aws:kms" },
          }),
        ],
      }),
      LifecycleConfiguration: {
        Rules: [
          Match.objectLike({
            ExpirationInDays: 30,
            NoncurrentVersionExpiration: { NoncurrentDays: 30 },
            AbortIncompleteMultipartUpload: { DaysAfterInitiation: 30 },
          }),
        ],
      },
    });
    t.hasResource("AWS::S3::BucketPolicy", {
      Properties: Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Condition: Match.objectLike({
                Bool: { "aws:SecureTransport": "false" },
              }),
            }),
          ]),
        }),
      }),
    });
  });

  it("creates a DynamoDB table with GSI keys, PITR, TTL and CMK", () => {
    const t = template();
    t.hasResourceProperties("AWS::DynamoDB::Table", {
      BillingMode: "PAY_PER_REQUEST",
      KeySchema: [
        { AttributeName: "PK", KeyType: "HASH" },
        { AttributeName: "SK", KeyType: "RANGE" },
      ],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "GSI1",
          KeySchema: [
            { AttributeName: "GSI1PK", KeyType: "HASH" },
            { AttributeName: "GSI1SK", KeyType: "RANGE" },
          ],
        }),
        Match.objectLike({
          IndexName: "GSI2",
          KeySchema: [
            { AttributeName: "GSI2PK", KeyType: "HASH" },
            { AttributeName: "GSI2SK", KeyType: "RANGE" },
          ],
        }),
      ]),
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
      SSESpecification: { SSEEnabled: true },
      TimeToLiveSpecification: {
        AttributeName: "expires_at",
        Enabled: true,
      },
      DeletionProtectionEnabled: true,
    });
  });

  it("creates FIFO queues with KMS encryption and DLQ redrive", () => {
    const t = template();
    t.hasResourceProperties("AWS::SQS::Queue", {
      FifoQueue: true,
      KmsMasterKeyId: Match.anyValue(),
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 5 }),
      VisibilityTimeout: 120,
    });
    t.hasResourceProperties("AWS::SQS::Queue", {
      FifoQueue: true,
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 5 }),
      VisibilityTimeout: 300,
    });
    const queues = t.findResources("AWS::SQS::Queue", {
      Properties: Match.objectLike({ FifoQueue: true }),
    });
    expect(Object.keys(queues).length).toBe(4);
  });

  it("creates a Cognito pool with email sign-in and a PKCE client", () => {
    const t = template();
    t.hasResourceProperties("AWS::Cognito::UserPool", {
      UsernameAttributes: ["email"],
      DeletionProtection: "ACTIVE",
      MfaConfiguration: "OPTIONAL",
      Policies: Match.objectLike({
        PasswordPolicy: Match.objectLike({ MinimumLength: 12 }),
      }),
    });
    t.hasResourceProperties("AWS::Cognito::UserPoolClient", {
      GenerateSecret: false,
      AllowedOAuthFlows: ["code"],
      AllowedOAuthFlowsUserPoolClient: true,
      SupportedIdentityProviders: ["COGNITO"],
    });
    const client = t.findResources("AWS::Cognito::UserPoolClient");
    const props = Object.values(client)[0].Properties as Record<
      string,
      unknown
    >;
    expect(props["CallbackURLs"]).toBeDefined();
    expect(props["LogoutURLs"]).toBeDefined();
  });

  it("creates immutable scan-on-push ECR repositories", () => {
    const t = template();
    t.resourceCountIs("AWS::ECR::Repository", 2);
    t.hasResourceProperties("AWS::ECR::Repository", {
      ImageScanningConfiguration: { ScanOnPush: true },
      ImageTagMutability: "IMMUTABLE",
    });
  });

  it("creates 30-day log groups and queue alarms", () => {
    const t = template();
    t.hasResourceProperties("AWS::Logs::LogGroup", {
      RetentionInDays: 30,
    });
    t.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "ApproximateAgeOfOldestMessage",
      Threshold: 300,
    });
    const alarms = t.findResources("AWS::CloudWatch::Alarm", {
      Properties: Match.objectLike({
        MetricName: "ApproximateNumberOfMessagesVisible",
      }),
    });
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(2);
  });

  it("creates a disabled scheduler group and schedules", () => {
    const t = template();
    t.hasResourceProperties("AWS::Scheduler::ScheduleGroup", {
      Name: "schoolsift",
    });
    const schedules = t.findResources("AWS::Scheduler::Schedule");
    expect(Object.keys(schedules).length).toBe(2);
    for (const schedule of Object.values(schedules)) {
      expect(schedule.Properties["State"]).toBe("DISABLED");
    }
  });

  it("creates an AgentCore runtime with JWT auth and S3 artifact params", () => {
    const t = template();
    t.hasParameter("AgentCodeBucket", { Type: "String" });
    t.hasParameter("AgentCodePrefix", { Type: "String" });
    t.hasParameter("AgentCodeVersionId", { Type: "String" });
    t.hasParameter("GmailTopic", { Type: "String" });
    t.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      AgentRuntimeName: "schoolsift_processor",
      AgentRuntimeArtifact: {
        CodeConfiguration: {
          Runtime: "PYTHON_3_12",
          EntryPoint: ["python", "agentcore_entry.py"],
          Code: {
            S3: {
              Bucket: { Ref: "AgentCodeBucket" },
              Prefix: { Ref: "AgentCodePrefix" },
              VersionId: { Ref: "AgentCodeVersionId" },
            },
          },
        },
      },
      NetworkConfiguration: { NetworkMode: "PUBLIC" },
      AuthorizerConfiguration: {
        CustomJWTAuthorizer: Match.objectLike({
          AllowedAudience: Match.anyValue(),
          DiscoveryUrl: Match.anyValue(),
        }),
      },
    });
    const runtimes = t.findResources("AWS::BedrockAgentCore::Runtime");
    const authorizer = Object.values(runtimes)[0].Properties[
      "AuthorizerConfiguration"
    ] as Record<string, { AllowedClients?: unknown }>;
    expect(
      authorizer["CustomJWTAuthorizer"]["AllowedClients"],
    ).toBeUndefined();
  });

  it("omits the AgentCore runtime when includeAgentRuntime is false", () => {
    const t = template({ includeAgentRuntime: false });
    const runtimes = t.findResources("AWS::BedrockAgentCore::Runtime");
    expect(Object.keys(runtimes)).toHaveLength(0);
    const parameters = Object.keys(
      (JSON.parse(JSON.stringify(t.toJSON()))["Parameters"] ?? {}) as Record<
        string,
        unknown
      >,
    );
    expect(parameters).not.toContain("AgentCodeBucket");
    expect(parameters).not.toContain("AgentCodePrefix");
    expect(parameters).not.toContain("AgentCodeVersionId");
    expect(parameters).toContain("GmailTopic");
    t.resourceCountIs("AWS::KMS::Key", 1);
    expect(Object.keys(t.findResources("AWS::S3::Bucket")).length).toBe(1);
    expect(
      Object.keys(t.findResources("AWS::DynamoDB::Table")).length,
    ).toBe(1);
    expect(
      Object.keys(t.findResources("AWS::SQS::Queue")).length,
    ).toBe(4);
    expect(
      Object.keys(t.findResources("AWS::Cognito::UserPool")).length,
    ).toBe(1);
    const outputs = Object.keys(t.findOutputs("*"));
    expect(outputs).not.toContain("AgentRuntimeArn");
    expect(Object.keys(t.findResources("AWS::Logs::LogGroup")).length)
      .toBeGreaterThan(0);
    const roles = t.findResources("AWS::IAM::Role");
    expect(
      Object.keys(roles).some((id) => id.startsWith("AgentRuntimeRole")),
    ).toBe(true);
  });

  it("includes the AgentCore runtime by default", () => {
    const t = template();
    t.resourceCountIs("AWS::BedrockAgentCore::Runtime", 1);
    expect(Object.keys(t.findOutputs("*"))).toContain("AgentRuntimeArn");
  });

  it("gives the Gmail topic parameter an unset default", () => {
    const t = template();
    t.hasParameter("GmailTopic", {
      Type: "String",
      Default: "projects/unset/topics/unset",
    });
  });

  it("creates a Cognito hosted UI domain and outputs its origin", () => {
    const t = template();
    t.hasResourceProperties("AWS::Cognito::UserPoolDomain", {
      Domain: "schoolsift-dev",
    });
    const outputs = t.findOutputs("*");
    expect(outputs["CognitoHostedUiOrigin"]).toBeDefined();
  });

  it("keeps the AgentCore runtime role read-only", () => {
    const t = template();
    const roles = t.findResources("AWS::IAM::Role");
    const runtimeRoleId = Object.keys(roles).find((id) =>
      id.startsWith("AgentRuntimeRole")
    );
    expect(runtimeRoleId).toBeDefined();
    const policies = t.findResources("AWS::IAM::Policy");
    const runtimePolicies = Object.values(policies).filter((p) =>
      (p.Properties["Roles"] as Array<{ Ref: string }>).some(
        (r) => r.Ref === runtimeRoleId
      )
    );
    expect(runtimePolicies.length).toBeGreaterThan(0);

    const actions: string[] = [];
    for (const policy of runtimePolicies) {
      for (const statement of policy.Properties["PolicyDocument"][
        "Statement"
      ] as Array<{ Action: string | string[] }>) {
        const list = Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action];
        actions.push(...list);
      }
    }
    const forbidden = [
      /^secretsmanager:/,
      /^s3:(Put|Delete|Create|Restore|Replicate|Abort)/,
      /^dynamodb:(Put|Update|Delete|BatchWrite|TransactWrite|Create|PartiQLUpdate|PartiQLDelete)/,
      /^kms:(Encrypt|GenerateDataKey|ReEncrypt|CreateGrant)/,
    ];
    for (const action of actions) {
      for (const pattern of forbidden) {
        expect(action).not.toMatch(pattern);
      }
    }
    expect(actions).toContain("bedrock:InvokeModel");
    expect(actions).toContain("s3:GetObject");
    expect(actions).toContain("kms:Decrypt");
    expect(actions).toContain("dynamodb:GetItem");
  });

  it("has no wildcard IAM actions or bare wildcard resources", () => {
    const t = template();
    const policies = t.findResources("AWS::IAM::Policy");
    for (const policy of Object.values(policies)) {
      const statements =
        policy.Properties["PolicyDocument"]["Statement"] as Array<
          Record<string, unknown>
        >;
      for (const statement of statements) {
        const actions = Array.isArray(statement["Action"])
          ? statement["Action"]
          : [statement["Action"]];
        for (const action of actions) {
          expect(action).not.toBe("*");
        }
        const resources = Array.isArray(statement["Resource"])
          ? statement["Resource"]
          : [statement["Resource"]];
        for (const resource of resources) {
          expect(resource).not.toBe("*");
        }
      }
    }
  });

  it("emits required outputs", () => {
    const t = template();
    const outputs = Object.keys(
      JSON.parse(JSON.stringify(t.toJSON()))["Outputs"] ?? {}
    );
    for (const name of [
      "ContentBucketName",
      "StateTableName",
      "KmsKeyArn",
      "IngestionQueueUrl",
      "ExecutionQueueUrl",
      "UserPoolId",
      "UserPoolClientId",
      "ApiRepositoryUri",
      "WorkerRepositoryUri",
      "AgentRuntimeArn",
      "GmailTopicName",
    ]) {
      expect(outputs).toContain(name);
    }
  });
});
