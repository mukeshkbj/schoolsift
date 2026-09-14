import {
  CfnOutput,
  CfnParameter,
  Duration,
  Fn,
  RemovalPolicy,
  Stack,
  type StackProps,
} from "aws-cdk-lib";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as sqs from "aws-cdk-lib/aws-sqs";
import type { Construct } from "constructs";

export interface SchoolSiftStackProps extends StackProps {
  readonly deletionProtection?: boolean;
  readonly callbackUrls?: string[];
  readonly logoutUrls?: string[];
  readonly cognitoDomainPrefix?: string;
}

export class SchoolSiftStack extends Stack {
  constructor(scope: Construct, id: string, props: SchoolSiftStackProps = {}) {
    super(scope, id, props);

    const deletionProtection = props.deletionProtection ?? true;
    const callbackUrls = props.callbackUrls ?? [
      "http://localhost:3210/auth/callback",
    ];
    const logoutUrls = props.logoutUrls ?? ["http://localhost:3210/"];
    const cognitoDomainPrefix =
      props.cognitoDomainPrefix ?? "schoolsift-dev";

    const codeBucketParam = new CfnParameter(this, "AgentCodeBucket", {
      type: "String",
      description: "S3 bucket holding the AgentCore runtime code package",
    });
    const codePrefixParam = new CfnParameter(this, "AgentCodePrefix", {
      type: "String",
      description: "S3 key prefix of the AgentCore runtime code package",
    });
    const codeVersionParam = new CfnParameter(this, "AgentCodeVersionId", {
      type: "String",
      description: "S3 object version of the AgentCore runtime code package",
    });
    const gmailTopicParam = new CfnParameter(this, "GmailTopic", {
      type: "String",
      description:
        "Fully-qualified Google Pub/Sub topic (projects/.../topics/...) " +
        "that delivers Gmail watch notifications; Pub/Sub itself is " +
        "external and not provisioned here",
    });

    const key = new kms.Key(this, "SchoolSiftKey", {
      enableKeyRotation: true,
      description: "SchoolSift content, state, and queue encryption",
    });

    const contentBucket = new s3.Bucket(this, "RawContentBucket", {
      versioned: true,
      encryption: s3.BucketEncryption.KMS,
      bucketKeyEnabled: true,
      encryptionKey: key,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          id: "expire-raw-content",
          enabled: true,
          expiration: Duration.days(30),
          noncurrentVersionExpiration: Duration.days(30),
          abortIncompleteMultipartUploadAfter: Duration.days(30),
        },
      ],
    });

    const stateTable = new dynamodb.Table(this, "StateTable", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: key,
      timeToLiveAttribute: "expires_at",
      deletionProtection,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    stateTable.addGlobalSecondaryIndex({
      indexName: "GSI1",
      partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
    });
    stateTable.addGlobalSecondaryIndex({
      indexName: "GSI2",
      partitionKey: { name: "GSI2PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI2SK", type: dynamodb.AttributeType.STRING },
    });

    const ingestionDlq = new sqs.Queue(this, "IngestionDlq", {
      fifo: true,
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: key,
      retentionPeriod: Duration.days(14),
    });
    const ingestionQueue = new sqs.Queue(this, "IngestionQueue", {
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: key,
      visibilityTimeout: Duration.seconds(120),
      retentionPeriod: Duration.days(4),
      deadLetterQueue: { queue: ingestionDlq, maxReceiveCount: 5 },
    });

    const executionDlq = new sqs.Queue(this, "ExecutionDlq", {
      fifo: true,
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: key,
      retentionPeriod: Duration.days(14),
    });
    const executionQueue = new sqs.Queue(this, "ExecutionQueue", {
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: key,
      visibilityTimeout: Duration.seconds(300),
      retentionPeriod: Duration.days(4),
      deadLetterQueue: { queue: executionDlq, maxReceiveCount: 5 },
    });

    const userPool = new cognito.UserPool(this, "CaregiverPool", {
      signInAliases: { email: true },
      selfSignUpEnabled: false,
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { sms: false, otp: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const appClient = userPool.addClient("WebClient", {
      generateSecret: false,
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls,
        logoutUrls,
      },
      preventUserExistenceErrors: true,
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.COGNITO,
      ],
    });
    const userPoolDomain = userPool.addDomain("HostedDomain", {
      cognitoDomain: { domainPrefix: cognitoDomainPrefix },
    });

    const apiRepo = new ecr.Repository(this, "ApiImageRepo", {
      repositoryName: "schoolsift-api",
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const workerRepo = new ecr.Repository(this, "WorkerImageRepo", {
      repositoryName: "schoolsift-worker",
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const apiLogs = new logs.LogGroup(this, "ApiLogGroup", {
      logGroupName: "/schoolsift/api",
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const runtimeLogs = new logs.LogGroup(this, "RuntimeLogGroup", {
      logGroupName: "/schoolsift/agent-runtime",
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    ingestionQueue
      .metricApproximateAgeOfOldestMessage({ period: Duration.minutes(5) })
      .createAlarm(this, "IngestionAgeAlarm", {
        threshold: 300,
        evaluationPeriods: 2,
        alarmDescription: "Ingestion queue messages are not being processed",
      });
    for (const [id, dlq] of [
      ["IngestionDlqAlarm", ingestionDlq],
      ["ExecutionDlqAlarm", executionDlq],
    ] as const) {
      dlq
        .metricApproximateNumberOfMessagesVisible({
          period: Duration.minutes(5),
        })
        .createAlarm(this, id, {
          threshold: 1,
          evaluationPeriods: 1,
          alarmDescription: "Messages landed in the dead-letter queue",
        });
    }

    const scheduleGroup = new scheduler.CfnScheduleGroup(this, "ScheduleGroup", {
      name: "schoolsift",
    });
    const schedulerRole = new iam.Role(this, "SchedulerRole", {
      assumedBy: new iam.ServicePrincipal("scheduler.amazonaws.com"),
    });
    executionQueue.grantSendMessages(schedulerRole);
    for (const [id, name, expression] of [
      ["SubscriptionRenewalSchedule", "subscription-renewal", "rate(1 hour)"],
      ["RetentionSchedule", "data-retention", "rate(1 day)"],
    ] as const) {
      new scheduler.CfnSchedule(this, id, {
        name,
        groupName: scheduleGroup.ref,
        scheduleExpression: expression,
        state: "DISABLED",
        flexibleTimeWindow: { mode: "OFF" },
        target: {
          arn: executionQueue.queueArn,
          roleArn: schedulerRole.roleArn,
          sqsParameters: { messageGroupId: name },
        },
      });
    }

    const runtimeRole = new iam.Role(this, "AgentRuntimeRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    });
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          Fn.sub(
            "arn:aws:bedrock:${AWS::Region}::foundation-model/*"
          ),
          Fn.sub(
            "arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:inference-profile/*"
          ),
        ],
      })
    );
    contentBucket.grantRead(runtimeRole);
    stateTable.grantReadData(runtimeRole);
    key.grantDecrypt(runtimeRole);
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
        ],
        resources: [runtimeLogs.logGroupArn, `${runtimeLogs.logGroupArn}:*`],
      })
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:GetObjectVersion"],
        resources: [
          Fn.sub(
            "arn:aws:s3:::${Bucket}/${Prefix}*",
            {
              Bucket: codeBucketParam.valueAsString,
              Prefix: codePrefixParam.valueAsString,
            }
          ),
        ],
      })
    );

    const discoveryUrl = Fn.join("", [
      "https://cognito-idp.",
      this.region,
      ".amazonaws.com/",
      userPool.userPoolId,
      "/.well-known/openid-configuration",
    ]);

    const runtime = new bedrockagentcore.CfnRuntime(this, "AgentRuntime", {
      agentRuntimeName: "schoolsift_processor",
      description: "SchoolSift message analysis entrypoint (no persistence)",
      roleArn: runtimeRole.roleArn,
      agentRuntimeArtifact: {
        codeConfiguration: {
          runtime: "PYTHON_3_12",
          entryPoint: ["python", "agentcore_entry.py"],
          code: {
            s3: {
              bucket: codeBucketParam.valueAsString,
              prefix: codePrefixParam.valueAsString,
              versionId: codeVersionParam.valueAsString,
            },
          },
        },
      },
      networkConfiguration: { networkMode: "PUBLIC" },
      protocolConfiguration: "HTTP",
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl,
          allowedAudience: [appClient.userPoolClientId],
        },
      },
      environmentVariables: {
        SCHOOLSIFT_AWS_REGION: this.region,
        SCHOOLSIFT_CONTENT_BUCKET: contentBucket.bucketName,
        SCHOOLSIFT_STATE_TABLE: stateTable.tableName,
        SCHOOLSIFT_KMS_KEY_ARN: key.keyArn,
      },
    });
    runtime.node.addDependency(runtimeLogs);

    new CfnOutput(this, "ContentBucketName", {
      value: contentBucket.bucketName,
    });
    new CfnOutput(this, "StateTableName", { value: stateTable.tableName });
    new CfnOutput(this, "KmsKeyArn", { value: key.keyArn });
    new CfnOutput(this, "IngestionQueueUrl", {
      value: ingestionQueue.queueUrl,
    });
    new CfnOutput(this, "ExecutionQueueUrl", {
      value: executionQueue.queueUrl,
    });
    new CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new CfnOutput(this, "UserPoolClientId", {
      value: appClient.userPoolClientId,
    });
    new CfnOutput(this, "CognitoHostedUiOrigin", {
      value: Fn.join("", [
        "https://",
        userPoolDomain.domainName,
        ".auth.",
        this.region,
        ".amazoncognito.com",
      ]),
    });
    new CfnOutput(this, "ApiRepositoryUri", {
      value: apiRepo.repositoryUri,
    });
    new CfnOutput(this, "WorkerRepositoryUri", {
      value: workerRepo.repositoryUri,
    });
    new CfnOutput(this, "AgentRuntimeArn", {
      value: runtime.attrAgentRuntimeArn,
    });
    new CfnOutput(this, "ScheduleGroupName", {
      value: scheduleGroup.ref,
    });
    new CfnOutput(this, "GmailTopicName", {
      value: gmailTopicParam.valueAsString,
      description: "SCHOOLSIFT_GMAIL_TOPIC value for future workers",
    });
  }
}
