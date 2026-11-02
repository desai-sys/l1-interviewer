"""AWS infrastructure for the L1 Interviewer.

CloudFront (HTTPS, needed for microphone access)
  -> Application Load Balancer (only accepts requests carrying CloudFront's secret header)
    -> ECS Fargate service (FastAPI app + static frontend)
         -> S3 (resumes, audio, transcripts, reports)   -> DynamoDB (session state)
         -> Amazon Transcribe (speech-to-text)          -> Amazon Nova via Bedrock
"""
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from image_build import CloudImageBuild

APP_DIR = str(Path(__file__).resolve().parents[1] / "app")


class L1InterviewerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = lambda key, default="": self.node.try_get_context(key) or default  # noqa: E731

        provider = ctx("llmProvider", "nova")

        # ---- Storage --------------------------------------------------------
        bucket = s3.Bucket(
            self, "Data",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,  # interview recordings are kept if the stack is deleted
        )
        table = dynamodb.Table(
            self, "Sessions",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Shared secret: CloudFront sends it as a header, the app rejects requests without it.
        origin_secret = secretsmanager.Secret(
            self, "OriginVerify",
            generate_secret_string=secretsmanager.SecretStringGenerator(exclude_punctuation=True, password_length=40),
        )

        # ---- Network (public subnets only - no NAT gateway cost) -------------
        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC)],
        )
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.ENABLED)

        environment = {
            "BUCKET_NAME": bucket.bucket_name,
            "TABLE_NAME": table.table_name,
            "LLM_PROVIDER": provider,
            "NUM_QUESTIONS": ctx("numQuestions", "10"),
        }
        if ctx("llmModel"):
            environment["LLM_MODEL"] = ctx("llmModel")
        if ctx("webResearch"):
            environment["WEB_RESEARCH"] = ctx("webResearch")
        if ctx("webResearchModel"):
            environment["WEB_RESEARCH_MODEL"] = ctx("webResearchModel")
        if ctx("accessCode"):
            environment["ACCESS_CODE"] = ctx("accessCode")
        if provider == "aws":
            environment["ANTHROPIC_AWS_WORKSPACE_ID"] = ctx("awsWorkspaceId")

        secrets = {"ORIGIN_VERIFY_SECRET": ecs.Secret.from_secrets_manager(origin_secret)}
        if provider == "anthropic":
            # Create beforehand:  aws secretsmanager create-secret --name l1-interviewer/anthropic-api-key --secret-string sk-ant-...
            api_key = secretsmanager.Secret.from_secret_name_v2(self, "AnthropicKey", "l1-interviewer/anthropic-api-key")
            secrets["ANTHROPIC_API_KEY"] = ecs.Secret.from_secrets_manager(api_key)

        # Container image is built in AWS CodeBuild - no Docker needed on the deploying machine.
        image = CloudImageBuild(self, "Image", source_dir=APP_DIR)

        service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "Service",
            cluster=cluster,
            cpu=1024,
            memory_limit_mib=2048,
            desired_count=1,
            assign_public_ip=True,
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            public_load_balancer=True,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_ecr_repository(image.repository, tag=image.tag),
                container_port=8000,
                environment=environment,
                secrets=secrets,
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="app", log_retention=logs.RetentionDays.ONE_MONTH),
            ),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            # Long-running background work lives in the task; give it time to finish on deploys.
            min_healthy_percent=100,
            max_healthy_percent=200,
        )
        service.service.node.add_dependency(image.resource)  # wait for the image build to finish
        service.target_group.configure_health_check(path="/api/health", healthy_http_codes="200")
        service.target_group.set_attribute("deregistration_delay.timeout_seconds", "30")
        service.load_balancer.set_attribute("idle_timeout.timeout_seconds", "120")

        # ---- Permissions -----------------------------------------------------
        role = service.task_definition.task_role
        bucket.grant_read_write(role)
        table.grant_read_write_data(role)
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"],
            resources=["*"],
        ))
        if provider == "nova":
            # Converse calls need InvokeModel on the inference profile and the foundation models it routes to.
            role.add_to_principal_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                ],
            ))
            # Amazon Nova Web Grounding (built-in web search used to find real interview questions).
            role.add_to_principal_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeTool"],
                resources=[f"arn:aws:bedrock::{self.account}:system-tool/amazon.nova_grounding"],
            ))
        if provider == "bedrock":
            role.add_to_principal_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock-mantle:*"],
                resources=["*"],
            ))
        if provider == "aws":
            role.add_to_principal_policy(iam.PolicyStatement(actions=["aws-external-anthropic:*"], resources=["*"]))

        # ---- CloudFront (HTTPS) ----------------------------------------------
        distribution = cloudfront.Distribution(
            self, "Cdn",
            comment="L1 Interviewer",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.LoadBalancerV2Origin(
                    service.load_balancer,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    custom_headers={"X-Origin-Verify": origin_secret.secret_value.unsafe_unwrap()},
                    read_timeout=Duration.seconds(60),
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_ALL,
        )

        # Presigned audio URLs are fetched by the browser straight from S3.
        bucket.add_cors_rule(
            allowed_methods=[s3.HttpMethods.GET],
            allowed_origins=[f"https://{distribution.distribution_domain_name}"],
            allowed_headers=["*"],
        )

        CfnOutput(self, "AppUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "DataBucket", value=bucket.bucket_name)
        CfnOutput(self, "SessionsTable", value=table.table_name)
